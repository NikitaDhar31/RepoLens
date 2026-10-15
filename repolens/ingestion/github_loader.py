"""GitHub repository import.

Access model: the URL only identifies a repository. Whether we can read it is
decided entirely by GitHub, using the caller's token (or anonymous access for
public repositories). We never try to bypass a 404/403.

The repository is fetched as a zipball for a pinned commit SHA and handed to
the same :func:`load_zip` pipeline used for uploads.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse

import requests
from github import Auth, Github, GithubException

from repolens.config import DEFAULT_LIMITS, IngestLimits
from repolens.ingestion.models import IngestionError, RepoOrigin, RepoSnapshot
from repolens.ingestion.zip_loader import load_zip

log = logging.getLogger(__name__)

_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")

# Hosts we are willing to download archive bytes from (the API redirects to codeload).
ALLOWED_DOWNLOAD_HOSTS = {"api.github.com", "codeload.github.com"}

# Classic-token scopes that grant far more than reading code.
BROAD_SCOPES = {
    "admin:org", "admin:repo_hook", "admin:org_hook", "admin:public_key",
    "admin:gpg_key", "delete_repo", "write:packages", "delete:packages",
    "workflow", "user", "admin:enterprise", "write:org", "site_admin",
}


class SecretToken:
    """Wraps a token so it cannot leak through repr(), logging or st.write()."""

    __slots__ = ("_value",)

    def __init__(self, value: str | None):
        self._value = (value or "").strip() or None

    def reveal(self) -> str | None:
        return self._value

    def __bool__(self) -> bool:
        return self._value is not None

    def __repr__(self) -> str:
        return "SecretToken(****)" if self._value else "SecretToken(None)"

    __str__ = __repr__


@dataclass(frozen=True)
class GitHubRef:
    owner: str
    repo: str
    ref: str | None = None


@dataclass
class GitHubImportResult:
    snapshot: RepoSnapshot
    warnings: list[str] = field(default_factory=list)


def parse_github_url(url: str) -> GitHubRef:
    """Parse ``https://github.com/owner/repo[/tree/ref]`` or ``owner/repo``."""
    raw = (url or "").strip()
    if not raw:
        raise IngestionError("Enter a GitHub repository URL.")

    if "://" not in raw and not raw.startswith("github.com"):
        path = raw
    else:
        if raw.startswith("github.com"):
            raw = "https://" + raw
        parsed = urlparse(raw)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {
            "github.com",
            "www.github.com",
        }:
            raise IngestionError("Only https://github.com repository URLs are supported.")
        if parsed.username or parsed.password or parsed.port:
            raise IngestionError("Repository URLs must not contain credentials or ports.")
        path = parsed.path

    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) < 2:
        raise IngestionError("The URL must point at a repository, e.g. github.com/owner/repo.")
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not _OWNER_RE.match(owner) or not _REPO_RE.match(repo) or repo in {".", ".."}:
        raise IngestionError("That does not look like a valid GitHub owner/repository name.")

    ref = None
    if len(parts) >= 4 and parts[2] == "tree":
        ref = "/".join(parts[3:])
        if not _REF_RE.match(ref) or ".." in ref:
            raise IngestionError("The branch or tag in the URL is not valid.")
    return GitHubRef(owner=owner, repo=repo, ref=ref)


def _friendly_github_error(exc: GithubException, has_token: bool) -> IngestionError:
    status = exc.status
    if status == 401:
        return IngestionError("GitHub rejected the token. Check that it is valid and not expired.")
    if status == 404:
        hint = (
            "Check the URL, and that your token has read access to this repository."
            if has_token
            else "If it is private, provide a GitHub token with read access to it."
        )
        return IngestionError(f"Repository not found or not accessible. {hint}")
    if status == 403:
        message = str(getattr(exc, "data", "") or "")
        if "rate limit" in message.lower():
            return IngestionError("GitHub API rate limit reached. Try again later or use a token.")
        return IngestionError("GitHub denied access to this repository with the provided credentials.")
    return IngestionError(f"GitHub returned an error (HTTP {status}).")


def token_scope_warnings(scopes: list[str] | None) -> list[str]:
    """Least-privilege advice for classic tokens. Fine-grained tokens report no scopes."""
    if not scopes:
        return []
    broad = sorted(set(scopes) & BROAD_SCOPES)
    if broad:
        return [
            "Your token has broader permissions than needed ("
            + ", ".join(broad)
            + "). A fine-grained token with read-only Contents and Metadata access is enough."
        ]
    return []


def _download_zipball(
    url: str,
    token: SecretToken,
    limits: IngestLimits,
    http_get: Callable[..., requests.Response],
) -> bytes:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token.reveal()}"
    try:
        # requests drops the Authorization header when GitHub redirects to codeload;
        # the redirect URL carries its own short-lived access token.
        resp = http_get(url, headers=headers, stream=True, timeout=limits.download_timeout_s)
    except requests.RequestException as exc:
        raise IngestionError("Could not download the repository archive from GitHub.") from exc

    with resp:
        final_host = (urlparse(resp.url).hostname or "").lower()
        if final_host not in ALLOWED_DOWNLOAD_HOSTS:
            raise IngestionError("GitHub redirected the download to an unexpected host.")
        if resp.status_code != 200:
            raise IngestionError(f"GitHub archive download failed (HTTP {resp.status_code}).")
        declared = resp.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > limits.max_archive_bytes:
            raise IngestionError("Repository archive is larger than the allowed size.")
        chunks: list[bytes] = []
        received = 0
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            received += len(chunk)
            if received > limits.max_archive_bytes:
                raise IngestionError("Repository archive is larger than the allowed size.")
            chunks.append(chunk)
    return b"".join(chunks)


def import_github_repo(
    url: str,
    token: SecretToken | None = None,
    limits: IngestLimits = DEFAULT_LIMITS,
    github_factory: Callable[[SecretToken], Github] | None = None,
    http_get: Callable[..., requests.Response] = requests.get,
) -> GitHubImportResult:
    """Fetch repository metadata and contents through the GitHub API."""
    ref = parse_github_url(url)
    token = token or SecretToken(None)

    def default_factory(tok: SecretToken) -> Github:
        auth = Auth.Token(tok.reveal()) if tok else None
        return Github(auth=auth, timeout=int(limits.download_timeout_s), retry=None)

    gh = (github_factory or default_factory)(token)
    warnings: list[str] = []
    try:
        repo = gh.get_repo(f"{ref.owner}/{ref.repo}")
        branch_name = ref.ref or repo.default_branch
        sha = repo.get_commit(branch_name).sha
        warnings.extend(token_scope_warnings(getattr(gh, "oauth_scopes", None)))
        private = bool(repo.private)
        description = repo.description
    except GithubException as exc:
        raise _friendly_github_error(exc, bool(token)) from exc
    except requests.RequestException as exc:
        raise IngestionError("Could not reach GitHub. Check your network connection.") from exc
    finally:
        try:
            gh.close()
        except Exception:
            pass

    archive_url = f"https://api.github.com/repos/{ref.owner}/{ref.repo}/zipball/{sha}"
    data = _download_zipball(archive_url, token, limits, http_get)

    origin = RepoOrigin(
        kind="github",
        display_name=f"{ref.owner}/{ref.repo}",
        owner=ref.owner,
        repo=ref.repo,
        ref=sha,
        private=private,
        description=description,
    )
    log.info("Imported %s at %s (%d bytes)", origin.display_name, sha[:7], len(data))
    return GitHubImportResult(snapshot=load_zip(data, origin, limits), warnings=warnings)
