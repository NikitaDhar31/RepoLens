"""Check model citations against the real repository.

A claim stays "verified" only if at least one of its citations points at a
file that exists, at lines that exist, which were included in the context the
model saw. Otherwise it is downgraded to "assumption" and flagged, so the UI
never presents an unsupported statement as fact.
"""

from __future__ import annotations

from typing import Any

from repolens.ai.context import ContextManifest
from repolens.ingestion.models import RepoSnapshot

GROUNDED = "grounded"  # exists and was shown to the model
UNSEEN = "unseen"  # exists, but those lines were not in the context
BAD_LINES = "bad_lines"  # file exists, line range does not
MISSING = "missing_file"


def _norm(path: object) -> str:
    p = str(path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def check_citation(snapshot: RepoSnapshot, manifest: ContextManifest, cit: dict[str, Any]) -> str:
    path = _norm(cit.get("path", ""))
    f = snapshot.get(path)
    if f is None:
        return MISSING
    try:
        start, end = int(cit.get("start_line", 0)), int(cit.get("end_line", 0))
    except (TypeError, ValueError):
        return BAD_LINES
    if end < start:
        start, end = end, start
    if start < 1 or end > max(f.line_count, 1):
        return BAD_LINES
    return GROUNDED if manifest.covers(path, start, end) else UNSEEN


def ground(data: Any, snapshot: RepoSnapshot, manifest: ContextManifest) -> dict[str, int]:
    """Annotate ``data`` in place; return counts of citation statuses and downgrades."""
    stats = {GROUNDED: 0, UNSEEN: 0, BAD_LINES: 0, MISSING: 0, "downgraded": 0}

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        if isinstance(node.get("citations"), list):
            statuses = []
            for cit in node["citations"]:
                if isinstance(cit, dict):
                    status = check_citation(snapshot, manifest, cit)
                    cit["status"] = status
                    cit["path"] = _norm(cit.get("path", ""))
                    stats[status] += 1
                    statuses.append(status)
            if node.get("kind") == "verified" and GROUNDED not in statuses:
                node["kind"] = "assumption"
                node["downgraded"] = True
                stats["downgraded"] += 1
        for key, value in node.items():
            if key != "citations":
                visit(value)

    visit(data)
    return stats
