"""Build the repository context sent to the model.

The context is line-numbered so the model can cite exact ranges, and every
range included is recorded in a :class:`ContextManifest`. The manifest is shown
to the user before sending (explicit disclosure) and used afterwards to check
that citations point at code the model actually saw.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

from repolens.analysis.languages import file_role
from repolens.analysis.models import RepoAnalysis, Snippet
from repolens.ingestion.models import RepoSnapshot

DEFAULT_BUDGET_CHARS = 320_000  # roughly 80k tokens of repository text

_OUTLINE = re.compile(
    r"^(?:\s{0,4}(?:async\s+)?def\s|\s{0,4}class\s|@\w+(?:\.\w+)*\.(?:route|get|post|put|patch|delete)\("
    r"|export\s|(?:async\s+)?function\s|interface\s|type\s+\w+\s*=)"
)

# Files that commonly hold secrets are never sent, even if committed.
SENSITIVE_NAME_RE = re.compile(
    r"(^\.env($|\.(?!example$|sample$|template$))|\.pem$|\.key$|\.p12$|\.pfx$|^id_(rsa|dsa|ecdsa|ed25519)$"
    r"|^credentials(\.json)?$|^secrets?\.(json|ya?ml|toml)$|\.keystore$|^\.npmrc$|^\.pypirc$|^\.netrc$)",
    re.I,
)
SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]


def is_sensitive_path(path: str) -> bool:
    return bool(SENSITIVE_NAME_RE.search(posixpath.basename(path)))


def redact_secrets(text: str) -> tuple[str, int]:
    count = 0
    for pat in SECRET_PATTERNS:
        text, n = pat.subn("[REDACTED-SECRET]", text)
        count += n
    return text, count


@dataclass
class ContextManifest:
    ranges: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    chars: int = 0
    redactions: int = 0
    excluded_sensitive: list[str] = field(default_factory=list)
    truncated: bool = False

    def add(self, path: str, start: int, end: int) -> None:
        self.ranges.setdefault(path, []).append((start, end))

    def covers(self, path: str, start: int, end: int) -> bool:
        """True if any part of start..end was included in the context."""
        return any(s <= end and start <= e for s, e in self.ranges.get(path, []))

    def merge(self, other: "ContextManifest") -> "ContextManifest":
        out = ContextManifest(
            {k: list(v) for k, v in self.ranges.items()},
            self.chars + other.chars,
            self.redactions + other.redactions,
            sorted(set(self.excluded_sensitive) | set(other.excluded_sensitive)),
            self.truncated or other.truncated,
        )
        for path, ranges in other.ranges.items():
            out.ranges.setdefault(path, []).extend(ranges)
        return out


class ContextBuilder:
    def __init__(self, snapshot: RepoSnapshot, budget_chars: int = DEFAULT_BUDGET_CHARS):
        self.snapshot = snapshot
        self.budget = budget_chars
        self.parts: list[str] = []
        self.manifest = ContextManifest()
        self._included: set[tuple[str, int, int]] = set()

    @property
    def remaining(self) -> int:
        return self.budget - self.manifest.chars

    def add_text(self, text: str) -> None:
        self.parts.append(text)
        self.manifest.chars += len(text)

    def add_file(self, path: str, start: int = 1, end: int | None = None, note: str = "") -> bool:
        f = self.snapshot.get(path)
        if f is None:
            return False
        if is_sensitive_path(path):
            if path not in self.manifest.excluded_sensitive:
                self.manifest.excluded_sensitive.append(path)
            return False
        lines = f.lines
        total = len(lines)
        start = max(1, start)
        end = min(total, end or total)
        if end < start or (path, start, end) in self._included:
            return False
        width = len(str(end))
        body_lines = [f"{i:>{width}} | {lines[i - 1]}" for i in range(start, end + 1)]

        def build(last: int, note: str) -> tuple[str, int]:
            body, n = redact_secrets("\n".join(body_lines[: last - start + 1]))
            note_attr = f" note={note!r}" if note else ""
            header = f'<file path="{path}" lines="{start}-{last}" total_lines="{total}"{note_attr}>'
            return f"{header}\n{body}\n</file>\n", n

        block, n_redacted = build(end, note)
        if len(block) > self.remaining:
            # Shrink to fit rather than skipping entirely, if a useful amount remains.
            # Measure line by line; redaction only ever shortens the text.
            self.manifest.truncated = True
            overhead = len(build(start, "truncated to fit budget")[0]) - len(body_lines[0])
            allowed = self.remaining - overhead - 16  # the final "start-end" label may be longer
            fit = 0
            for line in body_lines:
                allowed -= len(line) + 1
                if allowed < 0:
                    break
                fit += 1
            if fit < 20:
                return False
            end = start + fit - 1
            block, n_redacted = build(end, "truncated to fit budget")
            if len(block) > self.remaining:
                return False
        self.parts.append(block)
        self.manifest.chars += len(block)
        self.manifest.redactions += n_redacted
        self.manifest.add(path, start, end)
        self._included.add((path, start, end))
        return True

    def add_outline(self, path: str, after_line: int = 0, max_lines: int = 220) -> bool:
        """Definitions only (def/class/route decorators/exports), each with its real line number.

        Used for files too large to send whole. Every outline line is recorded in the
        manifest, so a citation of exactly that line counts as seen by the model.
        """
        f = self.snapshot.get(path)
        if f is None or is_sensitive_path(path):
            return False
        picked = [(i, line) for i, line in enumerate(f.lines, start=1)
                  if i > after_line and _OUTLINE.match(line)][:max_lines]
        if not picked:
            return False
        width = len(str(picked[-1][0]))
        body, n = redact_secrets("\n".join(f"{i:>{width}} | {line.rstrip()}" for i, line in picked))
        block = (f'<outline path="{path}" total_lines="{f.line_count}" '
                 f'note="definitions only; bodies omitted">\n{body}\n</outline>\n')
        if len(block) > self.remaining:
            self.manifest.truncated = True
            return False
        self.parts.append(block)
        self.manifest.chars += len(block)
        self.manifest.redactions += n
        for i, _ in picked:
            self.manifest.add(path, i, i)
        return True

    def add_file_or_outline(self, path: str, max_lines: int, head_lines: int = 120) -> bool:
        """Whole file when small enough; otherwise its head plus an outline of the rest."""
        f = self.snapshot.get(path)
        if f is None:
            return False
        if f.line_count <= max_lines:
            return self.add_file(path, 1, max_lines)
        added = self.add_file(path, 1, head_lines, note="head of a large file; outline follows")
        return self.add_outline(path, after_line=head_lines) or added

    def render(self) -> str:
        return "".join(self.parts)


def facts_block(analysis: RepoAnalysis) -> str:
    """Static-analysis facts, so the model builds on verified ground truth."""
    lines = [f"# Repository: {analysis.name}", "", "## Languages (lines of text)"]
    lines += [f"- {k}: {v}" for k, v in list(analysis.languages.items())[:10]]
    lines += ["", "## Verified dependency facts (from manifests and import statements; path:line)"]
    lines += [f"- {f.statement} [{f.evidence[0].label()}]" for f in analysis.stack[:60] if f.evidence]
    lines += [f"- NOTE: {f.statement}" for f in analysis.stack if f.category == "warning" and not f.evidence]
    lines += ["", "## Detected entry points"]
    for e in analysis.entrypoints[:10]:
        tag = "verified" if e.verified else "heuristic"
        lines.append(f"- {e.subject}: {e.statement} ({tag}) [{e.evidence[0].label()}]")
    if not analysis.entrypoints:
        lines.append("- none detected")
    if analysis.name_collisions:
        lines += ["", "## Similarly named modules (different code; do not confuse them)"]
        for name, places in analysis.name_collisions:
            lines.append(f"- `{name}`: " + ", ".join(f"`{p}`" for p in places))
    docs = [("project introduction (README)", analysis.readme_path),
            ("project guide for AI assistants (no README)", analysis.guide_path),
            ("likely system design document (chosen by file name)", analysis.design_doc)]
    if any(p for _, p in docs):
        lines += ["", "## Key documents"]
        lines += [f"- {label}: {p}" for label, p in docs if p]
    lines += ["", "## Module dependency counts (static import analysis)"]
    for (a, b), n in sorted(analysis.module_edges.items(), key=lambda kv: -kv[1])[:40]:
        lines.append(f"- {a} -> {b}: {n} import(s)")
    lines += ["", "## Most-depended-on files"]
    for r in analysis.ranked_files[:20]:
        lines.append(f"- {r.path}: {'; '.join(r.reasons)}")
    lines += ["", "## Directory tree (text files analysed)", "```", analysis.tree_text, "```", ""]
    return "\n".join(lines)


def select_context_files(analysis: RepoAnalysis, max_files: int = 18) -> list[str]:
    """Choose which files represent the repository, in priority order.

    1. strong entry points (web apps, scheduled or launched scripts), at most 4;
    2. the best-ranked file of every module, so no layer is left out;
    3. the remaining ranked files.
    Tests, prompt definitions and docs are excluded here (docs are added separately).
    """
    eligible = {p for p, r in analysis.file_roles.items() if r in ("source", "config")}
    chosen: list[str] = []

    def take(path: str | None) -> None:
        if path and path in eligible and path not in chosen and len(chosen) < max_files:
            chosen.append(path)

    for e in analysis.entrypoints:
        if e.weight >= 70 and len(chosen) < 4:
            take(e.subject)
    best_per_module: dict[str, str] = {}
    for r in analysis.ranked_files:
        if r.path in eligible:
            best_per_module.setdefault(analysis.module_of.get(r.path, r.path), r.path)
    for path in best_per_module.values():  # ranked order is preserved
        take(path)
    for r in analysis.ranked_files:
        take(r.path)
    return chosen


def build_base_context(
    snapshot: RepoSnapshot,
    analysis: RepoAnalysis,
    budget_chars: int = DEFAULT_BUDGET_CHARS,
    max_files: int = 18,
    max_lines_per_file: int = 400,
) -> tuple[str, ContextManifest]:
    """Stable, repository-wide context shared by every learning experience."""
    b = ContextBuilder(snapshot, budget_chars)
    b.add_text(facts_block(analysis))
    b.add_text("## Selected documents and source files (line-numbered)\n")
    if analysis.intro_path:
        b.add_file(analysis.intro_path, 1, 400)
    if analysis.design_doc:
        b.add_file(analysis.design_doc, 1, 250)
    for path in select_context_files(analysis, max_files):
        if b.remaining < 2_000:
            break
        b.add_file_or_outline(path, max_lines_per_file)
    return b.render(), b.manifest


def build_snippet_context(
    snapshot: RepoSnapshot,
    snippets: list[Snippet],
    budget_chars: int = 120_000,
    extra_files: list[str] | None = None,
    full_file_hits: int = 3,
    max_full_lines: int = 600,
) -> tuple[str, ContextManifest]:
    """Question-specific context.

    The top ``full_file_hits`` source files are sent whole (up to ``max_full_lines``):
    a matching window often holds only the imports and types, while the function
    bodies that actually implement the behaviour sit further down. Remaining hits
    are sent as padded windows.
    """
    b = ContextBuilder(snapshot, budget_chars)
    b.add_text("## Code retrieved for this question (line-numbered)\n")
    for path in extra_files or []:
        b.add_file(path, 1, max_full_lines)
    full = [s.path for s in snippets if file_role(s.path) == "source"][:full_file_hits]
    for path in full:
        b.add_file(path, 1, max_full_lines)
    for s in snippets:
        if s.path in full or s.path in (extra_files or []):
            continue  # already sent whole
        b.add_file(s.path, max(1, s.start_line - 10), s.end_line + 10)
    return b.render(), b.manifest
