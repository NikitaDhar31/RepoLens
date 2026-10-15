"""Data model produced by ingestion and consumed by every later stage."""

from __future__ import annotations

from dataclasses import dataclass, field


class IngestionError(Exception):
    """A user-facing import failure. The message is safe to display."""


@dataclass(frozen=True)
class SourceFile:
    path: str  # POSIX-style path relative to the repository root
    text: str
    size: int

    @property
    def lines(self) -> list[str]:
        return self.text.splitlines()

    @property
    def line_count(self) -> int:
        return len(self.lines)

    def excerpt(self, start: int, end: int) -> str:
        """Lines start..end (1-indexed, inclusive)."""
        lines = self.lines
        start = max(1, start)
        end = min(len(lines), end)
        return "\n".join(lines[start - 1 : end])


@dataclass
class SkippedSummary:
    binary: int = 0
    too_large: int = 0
    ignored_dir: int = 0
    unsafe_path: int = 0
    symlink: int = 0
    over_file_limit: int = 0

    @property
    def total(self) -> int:
        return (
            self.binary
            + self.too_large
            + self.ignored_dir
            + self.unsafe_path
            + self.symlink
            + self.over_file_limit
        )


@dataclass
class RepoOrigin:
    """Where a snapshot came from; used to build source links."""

    kind: str  # "github" or "zip"
    display_name: str
    owner: str | None = None
    repo: str | None = None
    ref: str | None = None  # commit SHA when known, else branch
    private: bool | None = None
    description: str | None = None

    def link(self, path: str, start: int | None = None, end: int | None = None) -> str | None:
        if self.kind != "github" or not (self.owner and self.repo and self.ref):
            return None
        url = f"https://github.com/{self.owner}/{self.repo}/blob/{self.ref}/{path}"
        if start:
            url += f"#L{start}" + (f"-L{end}" if end and end != start else "")
        return url


@dataclass
class RepoSnapshot:
    """An in-memory, read-only view of a repository's text files."""

    origin: RepoOrigin
    files: dict[str, SourceFile] = field(default_factory=dict)
    skipped: SkippedSummary = field(default_factory=SkippedSummary)
    all_paths: list[str] = field(default_factory=list)  # includes binaries etc.

    def get(self, path: str) -> SourceFile | None:
        return self.files.get(path)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files.values())
