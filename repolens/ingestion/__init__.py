"""Repository ingestion: GitHub and ZIP sources feed one in-memory snapshot."""

from repolens.ingestion.github_loader import (
    GitHubImportResult,
    SecretToken,
    import_github_repo,
    parse_github_url,
)
from repolens.ingestion.models import (
    IngestionError,
    RepoOrigin,
    RepoSnapshot,
    SourceFile,
)
from repolens.ingestion.zip_loader import load_zip


def import_zip_upload(data: bytes, filename: str) -> RepoSnapshot:
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if name.lower().endswith(".zip"):
        name = name[:-4]
    return load_zip(data, RepoOrigin(kind="zip", display_name=name or "uploaded-repo"))


__all__ = [
    "GitHubImportResult",
    "IngestionError",
    "RepoOrigin",
    "RepoSnapshot",
    "SecretToken",
    "SourceFile",
    "import_github_repo",
    "import_zip_upload",
    "load_zip",
    "parse_github_url",
]
