"""Shared result types for static analysis.

Every statement the analyzer makes carries ``evidence`` pointing at real lines,
and a ``verified`` flag: True means it was read directly from the source
(e.g. a dependency listed in package.json); False means it is a heuristic
inference (e.g. "the ``api/`` directory probably holds HTTP handlers").
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Evidence:
    path: str
    start_line: int | None = None
    end_line: int | None = None

    def label(self) -> str:
        if self.start_line is None:
            return self.path
        if self.end_line and self.end_line != self.start_line:
            return f"{self.path}:{self.start_line}-{self.end_line}"
        return f"{self.path}:{self.start_line}"


@dataclass
class Finding:
    statement: str
    evidence: list[Evidence] = field(default_factory=list)
    verified: bool = True
    category: str = "general"
    subject: str | None = None  # the file the finding is about, if any
    weight: float = 0.0  # entry points: how strongly this marks a real starting point (0-100)


@dataclass(frozen=True)
class ImportEdge:
    source: str
    target: str  # a repo path for internal edges, a package name for external ones
    line: int
    internal: bool


@dataclass
class DirectoryInfo:
    path: str
    file_count: int
    line_count: int
    languages: dict[str, int]
    role_guess: str | None = None  # heuristic, always unverified


@dataclass
class RankedFile:
    path: str
    score: float
    reasons: list[str]
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class LearningStep:
    title: str
    path: str
    reason: str
    evidence: list[Evidence] = field(default_factory=list)
    focus: str | None = None  # what to look for while reading
    verified: bool = True


@dataclass
class Snippet:
    path: str
    start_line: int
    end_line: int
    score: float
    matched_terms: list[str] = field(default_factory=list)


@dataclass
class RepoAnalysis:
    name: str
    languages: dict[str, int]  # language -> line count
    file_roles: dict[str, str]  # path -> source|test|docs|config|build|data|other
    stack: list[Finding]
    entrypoints: list[Finding]
    directories: list[DirectoryInfo]
    edges: list[ImportEdge]
    module_edges: dict[tuple[str, str], int]  # (module, module) -> import count
    module_of: dict[str, str]  # path -> module name
    ranked_files: list[RankedFile]
    learning_path: list[LearningStep]
    tree_text: str
    readme_path: str | None = None
    guide_path: str | None = None  # CLAUDE.md / AGENTS.md, used as the intro when there is no README
    design_doc: str | None = None
    name_collisions: list[tuple[str, list[str]]] = field(default_factory=list)

    @property
    def intro_path(self) -> str | None:
        return self.readme_path or self.guide_path

    @property
    def internal_edges(self) -> list[ImportEdge]:
        return [e for e in self.edges if e.internal]

    def importers_of(self, path: str) -> list[ImportEdge]:
        return [e for e in self.edges if e.internal and e.target == path]

    def imports_of(self, path: str) -> list[ImportEdge]:
        return [e for e in self.edges if e.source == path]
