"""Optional, opt-in local storage of AI results.

By default nothing is written to disk: AI results live only in the browser
session. When the user chooses to save, this module stores the AI results
(explanations, citations as file paths and line numbers, token usage), never
the repository's source files, in one JSON file per repository version.

A repository version is identified by a fingerprint of its file contents, so
saved results are only ever offered for exactly the code they were generated
from, and their line citations stay valid.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from repolens.ingestion.models import RepoSnapshot

log = logging.getLogger(__name__)

DEFAULT_DIR = Path(__file__).resolve().parents[1] / ".repolens_cache"
_FILE_RE = re.compile(r"^[0-9a-f]{32}\.json$")


def snapshot_fingerprint(snapshot: RepoSnapshot) -> str:
    """Stable hash of the analysed files' paths and contents (32 hex chars).

    Identical code gives the same fingerprint whether it came from GitHub or a ZIP,
    and any change to a file gives a new one.
    """
    h = hashlib.sha256()
    for path in sorted(snapshot.files):
        h.update(path.encode("utf-8") + b"\0")
        h.update(hashlib.sha256(snapshot.files[path].text.encode("utf-8")).digest())
    return h.hexdigest()[:32]


@dataclass
class SavedResults:
    repo: str
    saved_at: str
    results: dict[str, dict[str, Any]]


class ResultStore:
    def __init__(self, directory: Path | str | None = None):
        self.directory = Path(directory or os.environ.get("REPOLENS_CACHE_DIR") or DEFAULT_DIR)

    def _path(self, fingerprint: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", fingerprint):
            raise ValueError("invalid fingerprint")
        return self.directory / f"{fingerprint}.json"

    def load(self, fingerprint: str) -> SavedResults | None:
        path = self._path(fingerprint)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            results = raw["results"]
            if not isinstance(results, dict):
                raise ValueError("results is not a mapping")
            return SavedResults(str(raw.get("repo", "")), str(raw.get("saved_at", "")), results)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("Ignoring unreadable saved results file %s: %s", path.name, exc)
            return None

    def save(self, fingerprint: str, repo: str, results: dict[str, dict[str, Any]]) -> Path:
        """Write all results for one repository version, replacing the previous file atomically."""
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "repo": repo,
            "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "note": "RepoLens AI results only. No source files are stored here.",
            "results": results,
        }
        target = self._path(fingerprint)
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return target

    def delete(self, fingerprint: str) -> bool:
        path = self._path(fingerprint)
        if path.is_file():
            path.unlink()
            return True
        return False

    def saved_count(self) -> int:
        if not self.directory.is_dir():
            return 0
        return sum(1 for p in self.directory.iterdir() if _FILE_RE.match(p.name))
