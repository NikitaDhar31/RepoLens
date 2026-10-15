"""Streamlit rendering helpers. Presentation only: no analysis or API calls here."""

from __future__ import annotations

from typing import Any

import streamlit as st

from repolens.ai.context import ContextManifest
from repolens.ai.grounding import BAD_LINES, GROUNDED, MISSING, UNSEEN
from repolens.analysis.languages import language_of
from repolens.analysis.models import Evidence, RepoAnalysis
from repolens.ingestion.models import RepoSnapshot

HIGHLIGHT = {
    "Python": "python", "TypeScript": "typescript", "JavaScript": "javascript", "Go": "go",
    "Rust": "rust", "Java": "java", "Kotlin": "kotlin", "Ruby": "ruby", "PHP": "php",
    "C#": "csharp", "C": "c", "C++": "cpp", "Shell": "bash", "SQL": "sql", "JSON": "json",
    "YAML": "yaml", "TOML": "toml", "Markdown": "markdown", "HTML": "html", "CSS": "css",
}

STATUS_LABEL = {
    GROUNDED: "✅ checked",
    UNSEEN: "👁️ exists, not shown to the AI",
    BAD_LINES: "❌ line range does not exist",
    MISSING: "❌ file not found",
}


def kind_badge(kind: str | None, downgraded: bool = False) -> str:
    if kind == "verified":
        return ":green-badge[Verified in source]"
    if downgraded:
        return ":orange-badge[Assumption — citation could not be verified]"
    return ":orange-badge[Assumption]"


def numbered_excerpt(snapshot: RepoSnapshot, path: str, start: int, end: int, pad: int = 0) -> str:
    f = snapshot.get(path)
    if f is None:
        return ""
    lines = f.lines
    s = max(1, start - pad)
    e = min(len(lines), end + pad)
    width = len(str(e))
    return "\n".join(f"{i:>{width}}  {lines[i - 1]}" for i in range(s, e + 1))


def show_code(snapshot: RepoSnapshot, path: str, start: int = 1, end: int | None = None,
              max_lines: int = 120) -> None:
    f = snapshot.get(path)
    if f is None:
        st.caption(f"`{path}` is not available in this snapshot.")
        return
    end = min(end or f.line_count, f.line_count, start + max_lines - 1)
    lang = HIGHLIGHT.get(language_of(path) or "", None)
    st.code(numbered_excerpt(snapshot, path, start, end), language=lang)
    if end < f.line_count:
        st.caption(f"Showing lines {start}-{end} of {f.line_count}.")


def source_label(snapshot: RepoSnapshot, ev: Evidence) -> str:
    """Markdown label for evidence, linked to GitHub at the pinned commit when possible."""
    label = f"`{ev.label()}`"
    url = snapshot.origin.link(ev.path, ev.start_line, ev.end_line)
    return f"[{label}]({url})" if url else label


def render_evidence(snapshot: RepoSnapshot, evidence: list[Evidence], key: str) -> None:
    if not evidence:
        return
    st.markdown("Source: " + ", ".join(source_label(snapshot, e) for e in evidence[:6]))


def render_citations(snapshot: RepoSnapshot, citations: list[dict[str, Any]], key: str) -> None:
    for i, cit in enumerate(citations or []):
        path = cit.get("path", "")
        start, end = int(cit.get("start_line") or 1), int(cit.get("end_line") or 1)
        status = cit.get("status", "")
        ev = Evidence(path, start, end)
        header = f"`{ev.label()}` · {STATUS_LABEL.get(status, status)}"
        with st.expander(header, expanded=False):
            url = snapshot.origin.link(path, start, end)
            if url:
                st.markdown(f"[Open on GitHub at this commit]({url})")
            if status in (GROUNDED, UNSEEN):
                show_code(snapshot, path, start, end, max_lines=80)
            else:
                st.caption("This citation does not match the repository, so treat the claim as unverified.")


def render_claim(snapshot: RepoSnapshot, claim: dict[str, Any], key: str, title: str | None = None) -> None:
    if not claim:
        return
    if title:
        st.markdown(f"**{title}** {kind_badge(claim.get('kind'), claim.get('downgraded', False))}")
    else:
        st.markdown(kind_badge(claim.get("kind"), claim.get("downgraded", False)))
    st.markdown(claim.get("text", ""))
    render_citations(snapshot, claim.get("citations", []), key)


def render_manifest(manifest: ContextManifest, snapshot: RepoSnapshot) -> None:
    """Explicit disclosure of exactly what leaves the machine."""
    files = sorted(manifest.ranges)
    st.markdown(
        f"**{len(files)} file(s)**, about **{manifest.chars:,} characters** "
        f"(~{manifest.chars // 4:,} tokens) of repository content, plus static-analysis facts and "
        f"the directory tree, would be sent to the Anthropic API."
    )
    if manifest.redactions:
        st.caption(f"{manifest.redactions} secret-looking string(s) will be replaced with [REDACTED-SECRET].")
    if manifest.excluded_sensitive:
        st.caption("Never sent (sensitive file names): " + ", ".join(manifest.excluded_sensitive))
    if manifest.truncated:
        st.caption("Some files were truncated to stay within the context budget.")
    rows = [
        {"file": p, "lines": ", ".join(f"{s}-{e}" for s, e in manifest.ranges[p])}
        for p in files
    ]
    st.dataframe(rows, hide_index=True, width="stretch")


def module_graph_dot(analysis: RepoAnalysis, include_tests: bool = False, max_edges: int = 60) -> str:
    edges = sorted(analysis.module_edges.items(), key=lambda kv: -kv[1])
    if not include_tests:
        edges = [((a, b), n) for (a, b), n in edges if not _is_test_module(a) and not _is_test_module(b)]
    edges = edges[:max_edges]
    nodes = sorted({m for (a, b), _ in edges for m in (a, b)})
    max_n = max((n for _, n in edges), default=1)
    lines = [
        "digraph G {",
        '  rankdir=LR; bgcolor="transparent";',
        '  node [shape=box, style="rounded,filled", fillcolor="#eef2ff", color="#6366f1", fontname="Helvetica", fontsize=11];',
        '  edge [color="#94a3b8", fontname="Helvetica", fontsize=9];',
    ]
    for n in nodes:
        lines.append(f'  "{n}";')
    for (a, b), n in edges:
        width = 1 + 3 * n / max_n
        lines.append(f'  "{a}" -> "{b}" [label="{n}", penwidth={width:.1f}];')
    lines.append("}")
    return "\n".join(lines)


def module_graph_nodes(analysis: RepoAnalysis, include_tests: bool = False) -> int:
    pairs = [p for p in analysis.module_edges
             if include_tests or not (_is_test_module(p[0]) or _is_test_module(p[1]))]
    return len({m for pair in pairs for m in pair})


def file_graph_dot(analysis: RepoAnalysis, include_tests: bool = False, max_files: int = 30) -> str:
    """File-level view of the most-connected files, for repos that are a single module."""
    edges = {(e.source, e.target) for e in analysis.internal_edges
             if include_tests or (analysis.file_roles.get(e.source) != "test"
                                  and analysis.file_roles.get(e.target) != "test")}
    degree: dict[str, int] = {}
    for a, b in edges:
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1
    keep = set(sorted(degree, key=lambda p: -degree[p])[:max_files])
    lines = [
        "digraph G {",
        '  rankdir=LR; bgcolor="transparent";',
        '  node [shape=box, style="rounded,filled", fillcolor="#ecfeff", color="#0891b2", fontname="Helvetica", fontsize=10];',
        '  edge [color="#94a3b8"];',
    ]
    for a, b in sorted(edges):
        if a in keep and b in keep:
            lines.append(f'  "{a}" -> "{b}";')
    lines.append("}")
    return "\n".join(lines)


def _is_test_module(name: str) -> bool:
    return name.split("/")[0] in {"test", "tests", "__tests__", "spec"}
