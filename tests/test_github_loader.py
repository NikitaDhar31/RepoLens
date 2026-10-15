from types import SimpleNamespace

import pytest
import requests
from github import GithubException

from conftest import make_zip
from repolens.config import IngestLimits
from repolens.ingestion import IngestionError, SecretToken, import_github_repo, parse_github_url
from repolens.ingestion.github_loader import token_scope_warnings


# ---------------------------------------------------------------- URL parsing
@pytest.mark.parametrize("url,expected", [
    ("https://github.com/psf/requests", ("psf", "requests", None)),
    ("https://github.com/psf/requests.git", ("psf", "requests", None)),
    ("https://www.github.com/psf/requests/", ("psf", "requests", None)),
    ("github.com/psf/requests", ("psf", "requests", None)),
    ("psf/requests", ("psf", "requests", None)),
    ("https://github.com/psf/requests/tree/main", ("psf", "requests", "main")),
    ("https://github.com/o/r/tree/feature/x-1", ("o", "r", "feature/x-1")),
    ("https://github.com/o/r/blob/main/README.md", ("o", "r", None)),
])
def test_parse_valid_urls(url, expected):
    ref = parse_github_url(url)
    assert (ref.owner, ref.repo, ref.ref) == expected


@pytest.mark.parametrize("url", [
    "", "https://gitlab.com/o/r", "http://github.com/o/r", "https://github.com.evil.com/o/r",
    "https://user:pass@github.com/o/r", "https://github.com:8443/o/r", "https://github.com/onlyowner",
    "https://github.com/-bad/repo", "https://github.com/o/r/tree/../../etc", "file:///etc/passwd",
    "https://github.com/o/..",
])
def test_parse_rejects_invalid_urls(url):
    with pytest.raises(IngestionError):
        parse_github_url(url)


# ---------------------------------------------------------------- tokens
def test_secret_token_never_reveals_itself_in_repr():
    tok = SecretToken("ghp_supersecretvalue1234567890")
    assert "supersecret" not in repr(tok)
    assert "supersecret" not in str(tok)
    assert "supersecret" not in f"{tok}"
    assert tok.reveal() == "ghp_supersecretvalue1234567890"
    assert not SecretToken("   ")


def test_scope_warnings_flag_broad_classic_tokens():
    assert token_scope_warnings(None) == []
    assert token_scope_warnings(["repo"]) == []
    warning = token_scope_warnings(["repo", "delete_repo", "admin:org"])
    assert warning and "admin:org" in warning[0] and "delete_repo" in warning[0]


# ---------------------------------------------------------------- import with fakes
class FakeRepo:
    default_branch = "main"
    private = True
    description = "A private repo"

    def get_commit(self, ref):
        assert ref == "main"
        return SimpleNamespace(sha="0123456789abcdef")


class FakeGithub:
    def __init__(self, error=None, scopes=None):
        self.error = error
        self.oauth_scopes = scopes
        self.closed = False

    def get_repo(self, name):
        if self.error:
            raise self.error
        assert name == "acme/secret-app"
        return FakeRepo()

    def close(self):
        self.closed = True


class FakeResponse:
    def __init__(self, body: bytes, status=200, url="https://codeload.github.com/acme/x", headers=None):
        self.body, self.status_code, self.url = body, status, url
        self.headers = headers or {}

    def iter_content(self, chunk_size):
        for i in range(0, len(self.body), chunk_size):
            yield self.body[i : i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def recording_get(response):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return response

    get.calls = calls
    return get


ZIPBALL = make_zip({"acme-secret-app-0123456/README.md": "# Secret\n", "acme-secret-app-0123456/main.py": "x=1\n"})


def test_import_private_repo_with_token_pins_commit_and_sends_auth_header():
    fake_gh = FakeGithub()
    get = recording_get(FakeResponse(ZIPBALL))
    result = import_github_repo(
        "https://github.com/acme/secret-app", SecretToken("tok123"),
        github_factory=lambda t: fake_gh, http_get=get,
    )
    snap = result.snapshot
    assert sorted(snap.files) == ["README.md", "main.py"]
    assert snap.origin.ref == "0123456789abcdef"
    assert snap.origin.private is True
    assert snap.origin.link("main.py", 1) == "https://github.com/acme/secret-app/blob/0123456789abcdef/main.py#L1"
    url, kwargs = get.calls[0]
    assert url.endswith("/repos/acme/secret-app/zipball/0123456789abcdef")
    assert kwargs["headers"]["Authorization"] == "Bearer tok123"
    assert kwargs["timeout"] > 0 and kwargs["stream"] is True
    assert fake_gh.closed


def test_anonymous_import_sends_no_auth_header():
    get = recording_get(FakeResponse(ZIPBALL))
    import_github_repo("acme/secret-app", None, github_factory=lambda t: FakeGithub(), http_get=get)
    assert "Authorization" not in get.calls[0][1]["headers"]


def test_url_alone_does_not_grant_access_404_is_reported_safely():
    err = GithubException(404, {"message": "Not Found"}, None)
    with pytest.raises(IngestionError, match="not found or not accessible.*provide a GitHub token"):
        import_github_repo("acme/secret-app", None, github_factory=lambda t: FakeGithub(err))


def test_404_with_token_hints_at_token_access():
    err = GithubException(404, {"message": "Not Found"}, None)
    with pytest.raises(IngestionError, match="token has read access"):
        import_github_repo("acme/secret-app", SecretToken("t"), github_factory=lambda t: FakeGithub(err))


def test_bad_credentials_message_does_not_contain_token():
    err = GithubException(401, {"message": "Bad credentials"}, None)
    with pytest.raises(IngestionError) as exc_info:
        import_github_repo("acme/secret-app", SecretToken("tok-should-not-leak"),
                           github_factory=lambda t: FakeGithub(err))
    assert "tok-should-not-leak" not in str(exc_info.value)
    assert "rejected the token" in str(exc_info.value)


def test_rate_limit_is_explained():
    err = GithubException(403, {"message": "API rate limit exceeded"}, None)
    with pytest.raises(IngestionError, match="rate limit"):
        import_github_repo("acme/secret-app", None, github_factory=lambda t: FakeGithub(err))


def test_broad_token_scopes_produce_warning():
    result = import_github_repo(
        "acme/secret-app", SecretToken("t"),
        github_factory=lambda t: FakeGithub(scopes=["repo", "delete_repo"]),
        http_get=recording_get(FakeResponse(ZIPBALL)),
    )
    assert any("delete_repo" in w for w in result.warnings)


def test_redirect_to_unexpected_host_is_rejected():
    get = recording_get(FakeResponse(ZIPBALL, url="https://evil.example.com/x.zip"))
    with pytest.raises(IngestionError, match="unexpected host"):
        import_github_repo("acme/secret-app", None, github_factory=lambda t: FakeGithub(), http_get=get)


def test_download_size_limit_is_enforced_while_streaming():
    limits = IngestLimits(max_archive_bytes=100)
    get = recording_get(FakeResponse(b"x" * 1000))
    with pytest.raises(IngestionError, match="larger than"):
        import_github_repo("acme/secret-app", None, limits=limits,
                           github_factory=lambda t: FakeGithub(), http_get=get)


def test_declared_content_length_over_limit_is_rejected_early():
    limits = IngestLimits(max_archive_bytes=100)
    get = recording_get(FakeResponse(b"", headers={"Content-Length": "999999"}))
    with pytest.raises(IngestionError, match="larger than"):
        import_github_repo("acme/secret-app", None, limits=limits,
                           github_factory=lambda t: FakeGithub(), http_get=get)


def test_download_http_error_and_network_error():
    with pytest.raises(IngestionError, match="HTTP 502"):
        import_github_repo("acme/secret-app", None, github_factory=lambda t: FakeGithub(),
                           http_get=recording_get(FakeResponse(b"", status=502)))

    def boom(*a, **k):
        raise requests.ConnectionError("down")

    with pytest.raises(IngestionError, match="Could not download"):
        import_github_repo("acme/secret-app", None, github_factory=lambda t: FakeGithub(), http_get=boom)
