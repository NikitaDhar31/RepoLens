import json
from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import pytest

from conftest import make_snapshot
from repolens.ai import AIError, AnthropicLLM, LLMResponse, prepare, run_experience
from repolens.ai.context import ContextBuilder, ContextManifest, build_base_context, redact_secrets
from repolens.ai.experiences import SYSTEM_PROMPT
from repolens.ai.grounding import BAD_LINES, GROUNDED, MISSING, UNSEEN, check_citation, ground
from repolens.analysis import analyze


# ---------------------------------------------------------------- context building
def test_context_is_line_numbered_and_recorded(ts_snapshot):
    b = ContextBuilder(ts_snapshot)
    assert b.add_file("src/translate.ts", 2, 4)
    text = b.render()
    assert '<file path="src/translate.ts" lines="2-4" total_lines="6">' in text
    assert "3 | // Translate a raw webhook payload" in text
    assert b.manifest.covers("src/translate.ts", 4, 10)
    assert not b.manifest.covers("src/translate.ts", 5, 6)


def test_sensitive_files_are_never_sent():
    snap = make_snapshot({".env": "API_KEY=abc", ".env.example": "API_KEY=", "id_rsa": "x", "a.py": "x"})
    b = ContextBuilder(snap)
    assert not b.add_file(".env")
    assert not b.add_file("id_rsa")
    assert b.add_file(".env.example")
    assert ".env" in b.manifest.excluded_sensitive and "API_KEY=abc" not in b.render()


def test_secret_like_strings_are_redacted():
    text = (
        "key = 'sk-ant-api03-abcdefghijklmnop'\ngh = 'ghp_" + "a" * 36 + "'\n"
        "aws = 'AKIAABCDEFGHIJKLMNOP'\n-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----"
    )
    out, n = redact_secrets(text)
    assert n == 4
    assert "sk-ant" not in out and "ghp_" not in out and "AKIA" not in out and "MIIE" not in out


def test_budget_truncates_instead_of_overflowing():
    snap = make_snapshot({"big.py": "\n".join(f"line_{i} = {i}" for i in range(5000))})
    b = ContextBuilder(snap, budget_chars=5_000)
    assert b.add_file("big.py")
    assert b.manifest.chars <= 5_000
    assert b.manifest.truncated
    (start, end), = b.manifest.ranges["big.py"]
    assert start == 1 and end < 5000


def test_base_context_contains_facts_tree_and_readme(ts_snapshot):
    text, manifest = build_base_context(ts_snapshot, analyze(ts_snapshot))
    assert "## Verified dependency facts" in text
    assert "express" in text
    assert "## Directory tree" in text
    assert "README.md" in manifest.ranges


# ---------------------------------------------------------------- grounding
def manifest_for(path, start, end):
    m = ContextManifest()
    m.add(path, start, end)
    return m


def test_check_citation_statuses(ts_snapshot):
    m = manifest_for("src/translate.ts", 1, 6)
    assert check_citation(ts_snapshot, m, {"path": "src/translate.ts", "start_line": 4, "end_line": 5}) == GROUNDED
    assert check_citation(ts_snapshot, m, {"path": "./src/translate.ts", "start_line": 5, "end_line": 4}) == GROUNDED
    assert check_citation(ts_snapshot, m, {"path": "src/server.ts", "start_line": 1, "end_line": 2}) == UNSEEN
    assert check_citation(ts_snapshot, m, {"path": "src/translate.ts", "start_line": 1, "end_line": 99}) == BAD_LINES
    assert check_citation(ts_snapshot, m, {"path": "src/nope.ts", "start_line": 1, "end_line": 1}) == MISSING


def test_ground_downgrades_unsupported_verified_claims(ts_snapshot):
    m = manifest_for("src/translate.ts", 1, 6)
    data = {
        "summary": {"text": "ok", "kind": "verified",
                    "citations": [{"path": "src/translate.ts", "start_line": 4, "end_line": 5}]},
        "components": [
            {"name": "fake", "kind": "verified",
             "citations": [{"path": "src/invented.ts", "start_line": 1, "end_line": 3}]},
            {"name": "unseen", "kind": "verified",
             "citations": [{"path": "src/server.ts", "start_line": 1, "end_line": 2}]},
            {"name": "guess", "kind": "assumption", "citations": []},
        ],
    }
    stats = ground(data, ts_snapshot, m)
    assert data["summary"]["kind"] == "verified"
    assert data["summary"]["citations"][0]["status"] == GROUNDED
    assert data["components"][0]["kind"] == "assumption" and data["components"][0]["downgraded"]
    assert data["components"][1]["kind"] == "assumption"
    assert "downgraded" not in data["components"][2]
    assert stats == {GROUNDED: 1, UNSEEN: 1, BAD_LINES: 0, MISSING: 1, "downgraded": 2}


# ---------------------------------------------------------------- experiences with a fake LLM
class FakeLLM:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def complete_json(self, system, shared_context, task_context, task, schema):
        self.calls.append(dict(system=system, shared=shared_context, task_context=task_context,
                               task=task, schema=schema))
        return LLMResponse(data=json.loads(json.dumps(self.data)), model="fake-model", input_tokens=10)


def test_feature_experience_sends_retrieved_code_and_grounds_answer(ts_snapshot):
    analysis = analyze(ts_snapshot)
    answer = {
        "short_answer": {"text": "translate() maps it", "kind": "verified",
                         "citations": [{"path": "src/translate.ts", "start_line": 4, "end_line": 6}]},
        "flow": [{"step": "made up", "explanation": "x", "kind": "verified",
                  "citations": [{"path": "src/ghost.ts", "start_line": 1, "end_line": 2}]}],
        "concepts": [], "caveats": [], "follow_up_questions": [],
    }
    llm = FakeLLM(answer)
    result = run_experience("feature", ts_snapshot, analysis, llm,
                            question="How is a webhook translated into a canonical event?")
    call = llm.calls[0]
    assert call["system"] == SYSTEM_PROMPT
    assert "How is a webhook translated" in call["task"]
    assert 'path="src/translate.ts"' in call["task_context"]
    assert call["schema"]["required"] == ["short_answer", "flow", "concepts", "caveats", "follow_up_questions"]
    assert result.data["short_answer"]["kind"] == "verified"
    assert result.data["flow"][0]["kind"] == "assumption"
    assert result.grounding["missing_file"] == 1


def test_prepare_discloses_without_calling_model(ts_snapshot):
    _, req, manifest = prepare("file", ts_snapshot, analyze(ts_snapshot), path="src/translate.ts")
    assert "src/translate.ts" in manifest.ranges
    assert "src/types/event.ts" not in req.task  # importers listed, not imports
    assert "Files that import it: src/webhook/handler.ts" in req.task


def test_all_registered_experiences_build(ts_snapshot):
    analysis = analyze(ts_snapshot)
    for key, params in [("overview", {}), ("learning_path", {}),
                        ("feature", {"question": "webhook"}), ("file", {"path": "src/server.ts"})]:
        _, req, manifest = prepare(key, ts_snapshot, analysis, **params)
        assert req.schema["additionalProperties"] is False
        assert manifest.chars > 0


def test_schemas_satisfy_structured_output_rules():
    from repolens.ai import schemas

    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                check(v)
        elif isinstance(node, list):
            for v in node:
                check(v)

    for s in (schemas.OVERVIEW_SCHEMA, schemas.LEARNING_PATH_SCHEMA, schemas.FEATURE_SCHEMA, schemas.FILE_SCHEMA):
        check(s)


# ---------------------------------------------------------------- Anthropic wrapper (mocked SDK)
def fake_message(text='{"a": 1}', stop_reason="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        model="claude-opus-5-5",
        usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=80),
    )


class FakeStream:
    def __init__(self, result):
        self.result = result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeMessages:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self.results.pop(0))


def fake_client(beta_results, plain_results=()):
    return SimpleNamespace(
        beta=SimpleNamespace(messages=FakeMessages(beta_results)),
        messages=FakeMessages(plain_results),
    )


def api_error(cls, status, message):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(status, request=req, json={"error": {"message": message}})
    return cls(message, response=resp, body={"error": {"message": message}})


def test_anthropic_llm_builds_cached_structured_request():
    client = fake_client([fake_message()])
    llm = AnthropicLLM(client=client, model="claude-opus-5-5")
    resp = llm.complete_json("sys", "REPO", "SNIPPETS", "TASK", {"type": "object"})
    assert resp.data == {"a": 1} and resp.cache_read_tokens == 80
    call = client.beta.messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": {"type": "object"}}
    content = call["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "REPO", "cache_control": {"type": "ephemeral"}}
    assert [c["text"] for c in content[1:]] == ["SNIPPETS", "TASK"]
    assert "thinking" not in call and "temperature" not in call


def test_anthropic_llm_falls_back_to_plain_request_if_fallbacks_unsupported():
    err = api_error(anthropic.BadRequestError, 400, "fallbacks: not supported for this model")
    client = fake_client([err], [fake_message()])
    llm = AnthropicLLM(client=client)
    assert llm.complete_json("s", "c", "", "t", {}).data == {"a": 1}
    assert "fallbacks" not in client.messages.calls[0]


@pytest.mark.parametrize("message,match", [
    (fake_message(stop_reason="refusal"), "declined"),
    (fake_message(stop_reason="max_tokens"), "cut off"),
    (fake_message(text="not json"), "not valid JSON"),
    (fake_message(text=""), "empty"),
])
def test_anthropic_llm_response_problems(message, match):
    with pytest.raises(AIError, match=match):
        AnthropicLLM(client=fake_client([message])).complete_json("s", "c", "", "t", {})


def test_anthropic_llm_maps_errors_without_leaking_key():
    err = api_error(anthropic.AuthenticationError, 401, "invalid x-api-key")
    with pytest.raises(AIError, match="rejected") as exc_info:
        AnthropicLLM(api_key="sk-ant-secret", client=fake_client([err])).complete_json("s", "c", "", "t", {})
    assert "sk-ant-secret" not in str(exc_info.value)


def test_missing_api_key_raises_clear_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("repolens.ai.client.get_secret", lambda name: None)
    with pytest.raises(AIError, match="No Anthropic API key"):
        AnthropicLLM()


def test_overview_schema_stays_small_enough_for_the_api():
    """The API rejected an earlier overview schema with 'compiled grammar is too large'.

    That version had 7 citation-bearing fields; the accepted one has 5. Adding more
    should be re-checked against the live API before raising this limit.
    """
    from repolens.ai import schemas

    def count(node):
        if isinstance(node, dict):
            return (1 if node is schemas.CITATIONS else 0) + sum(count(v) for v in node.values())
        if isinstance(node, list):
            return sum(count(v) for v in node)
        return 0

    assert count(schemas.OVERVIEW_SCHEMA) <= 5


def test_snippet_context_sends_top_source_files_whole():
    from repolens.analysis.models import Snippet
    from repolens.ai.context import build_snippet_context

    body = "\n".join(["import x"] * 5 + [f"line {i}" for i in range(200)] + ["function core() { translate() }"])
    snap = make_snapshot({
        "src/translate.ts": body,
        "tests/translate.test.ts": "\n".join(f"t {i}" for i in range(300)),
        "docs/notes.md": "\n".join(f"n {i}" for i in range(300)),
    })
    hits = [Snippet("src/translate.ts", 1, 60, 9.0), Snippet("tests/translate.test.ts", 100, 160, 8.0),
            Snippet("docs/notes.md", 200, 260, 7.0)]
    text, manifest = build_snippet_context(snap, hits)
    # The source hit is sent in full, so code far below the matching window is visible.
    assert manifest.ranges["src/translate.ts"] == [(1, 206)]
    assert "function core()" in text
    # Tests and docs stay as padded windows, and nothing is sent twice.
    assert manifest.ranges["tests/translate.test.ts"] == [(90, 170)]
    assert manifest.ranges["docs/notes.md"] == [(190, 270)]


def test_snippet_context_caps_full_files_and_respects_budget():
    from repolens.analysis.models import Snippet
    from repolens.ai.context import build_snippet_context

    snap = make_snapshot({f"src/f{i}.py": "\n".join(f"x{i}_{n} = {n}" for n in range(1000)) for i in range(5)})
    hits = [Snippet(f"src/f{i}.py", 1, 60, 10 - i) for i in range(5)]
    _, manifest = build_snippet_context(snap, hits, budget_chars=20_000)
    assert manifest.ranges["src/f0.py"] == [(1, 600)]  # whole file capped at max_full_lines
    assert manifest.chars <= 20_000
    assert manifest.truncated


def test_haiku_is_called_without_server_side_fallbacks():
    client = fake_client([], [fake_message()])
    AnthropicLLM(client=client, model="claude-haiku-5-5").complete_json("s", "c", "", "t", {})
    assert client.beta.messages.calls == []
    call = client.messages.calls[0]
    assert call["model"] == "claude-haiku-5-5" and "fallbacks" not in call
