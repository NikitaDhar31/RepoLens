"""Rank files by how much they matter for understanding the repository.

Scores are built only from observable facts (who imports a file, whether a
manifest declares it, its role), and each contributing fact is recorded as a
human-readable reason.
"""

from __future__ import annotations

import posixpath
import re
from collections import Counter

from repolens.analysis.languages import is_lockfile
from repolens.analysis.models import Evidence, Finding, ImportEdge, RankedFile
from repolens.ingestion.models import RepoSnapshot

README_NAMES = ("readme.md", "readme.rst", "readme.txt", "readme")


GUIDE_NAMES = ("claude.md", "agents.md", "gemini.md", ".github/copilot-instructions.md")


def find_readme(snapshot: RepoSnapshot) -> str | None:
    root = [p for p in snapshot.files if "/" not in p and p.lower() in README_NAMES]
    return sorted(root)[0] if root else None


def find_guide(snapshot: RepoSnapshot) -> str | None:
    """A project guide written for AI coding assistants (CLAUDE.md, AGENTS.md, ...).

    These are often the most complete description of a repository that has no README.
    The largest one wins, since projects that keep several usually mirror one into another.
    """
    found = [p for p in snapshot.files if p.lower() in GUIDE_NAMES]
    return max(found, key=lambda p: (snapshot.files[p].size, p)) if found else None


def doc_mentions(snapshot: RepoSnapshot, doc_paths: list[str]) -> dict[str, list[Evidence]]:
    """Code files that the given documents name explicitly, with the line that names them.

    A full path (``agents/orchestrator.py``) always counts; a bare file name
    (``committee_agent.py``) counts only when it is unique in the repository.
    """
    code = [p for p in snapshot.files if p.endswith((".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs"))]
    basenames = Counter(p.rsplit("/", 1)[-1] for p in code)
    patterns = []
    for p in code:
        base = p.rsplit("/", 1)[-1]
        names = [p] + ([base] if basenames[base] == 1 and len(base) >= 8 and base != "__init__.py" else [])
        patterns.append((p, re.compile(r"(?<![\w/.-])(?:" + "|".join(re.escape(n) for n in names) + r")(?![\w-])")))
    found: dict[str, list[Evidence]] = {}
    for doc in doc_paths:
        f = snapshot.get(doc)
        if f is None:
            continue
        for lineno, line in enumerate(f.lines, start=1):
            if ".py" not in line and ".ts" not in line and ".js" not in line and ".go" not in line and ".rs" not in line:
                continue
            for path, pat in patterns:
                if pat.search(line) and not any(e.path == doc for e in found.get(path, [])):
                    found.setdefault(path, []).append(Evidence(doc, lineno, lineno))
    return found


def rank_files(
    snapshot: RepoSnapshot,
    roles: dict[str, str],
    edges: list[ImportEdge],
    entrypoints: list[Finding],
    doc_paths: list[str] | None = None,
) -> list[RankedFile]:
    in_deg: Counter[str] = Counter()
    out_deg: Counter[str] = Counter()
    first_importer: dict[tuple[str, str], ImportEdge] = {}
    for e in edges:
        if e.internal:
            if (e.source, e.target) in first_importer:
                continue
            first_importer[(e.source, e.target)] = e
            in_deg[e.target] += 1
            out_deg[e.source] += 1

    entry_paths = {f.subject: f for f in entrypoints if f.subject}
    mentioned = doc_mentions(snapshot, doc_paths or [])

    readme = find_readme(snapshot)
    guide = None if readme else find_guide(snapshot)
    ranked: list[RankedFile] = []
    for path, f in snapshot.files.items():
        if is_lockfile(path):
            continue
        role = roles.get(path, "other")
        score = 0.0
        reasons: list[str] = []
        evidence: list[Evidence] = []

        if path == readme:
            score += 30
            reasons.append("Project README — the author's own explanation")
        if path == guide:
            score += 25
            reasons.append("Project guide for AI assistants; the main overview, as there is no README")
        if path in entry_paths:
            # Scale by how clearly this is a real starting point (web app > demo __main__).
            score += 25 * entry_paths[path].weight / 100
            reasons.append(f"Entry point: {entry_paths[path].statement}")
            evidence.extend(entry_paths[path].evidence)
        if path in mentioned:
            docs = mentioned[path]
            score += 8 * min(len(docs), 2)
            reasons.append("Named in " + ", ".join(f"`{e.path}` (line {e.start_line})" for e in docs))
            evidence.extend(docs)
        if in_deg[path]:
            # Test helpers are imported by many tests but teach little about the product.
            weight = 1.0 if role == "test" else 2.0
            score += min(in_deg[path], 50) * weight
            reasons.append(f"Imported by {in_deg[path]} other file(s)")
            importers = [e for (s, t), e in first_importer.items() if t == path][:3]
            evidence.extend(Evidence(e.source, e.line, e.line) for e in importers)
        if out_deg[path] >= 5:
            score += 4
            reasons.append(f"Wires together {out_deg[path]} internal modules")
        name = posixpath.basename(path).lower()
        if role == "config" and path.count("/") == 0 and name in {
            "package.json", "pyproject.toml", "go.mod", "cargo.toml", "requirements.txt"
        }:
            score += 15
            reasons.append("Project manifest: dependencies, scripts and metadata")
        if role == "source":
            score += 2
        elif role == "test":
            score -= 2
        elif role in ("docs", "prompt") and path not in (readme, guide):
            score += 1 if path.count("/") <= 1 else 0
        depth_penalty = 0.3 * path.count("/")
        score -= depth_penalty
        if f.line_count < 3:
            score -= 3
        if not reasons:
            continue
        ranked.append(RankedFile(path, round(score, 2), reasons, evidence))
    ranked.sort(key=lambda r: (-r.score, r.path))
    return ranked

