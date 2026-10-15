"""Streamlit smoke tests: the UI renders every tab without errors and without AI."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from conftest import SAMPLE_TS_REPO, make_snapshot
from repolens.analysis import analyze

APP = str(Path(__file__).resolve().parents[1] / "app.py")


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("repolens.config.get_secret", lambda name: None)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setenv("REPOLENS_CACHE_DIR", str(tmp_path / "store"))
    return tmp_path / "store"


def test_landing_page_renders():
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert any("Understand a repository" in h.value for h in at.header)


def test_loaded_repository_renders_all_tabs_without_ai():
    at = AppTest.from_file(APP, default_timeout=30)
    snap = make_snapshot(SAMPLE_TS_REPO, kind="github")
    at.session_state["snapshot"] = snap
    at.session_state["analysis"] = analyze(snap)
    at.run()
    assert not at.exception
    assert [t.label for t in at.tabs] == [
        "Overview", "Architecture", "Learning path", "Ask about a feature", "File explorer"]
    # AI is off: the consent checkbox is disabled and no API call is possible.
    consent = at.sidebar.checkbox[0]
    assert consent.disabled and consent.value is False
    assert any("AI explanation is off" in i.value for i in at.info)

    at.text_area(key="feature_question").input("How is a webhook translated?").run()
    assert not at.exception
    assert any("src/translate.ts" in e.label or "src/webhook/handler.ts" in e.label for e in at.expander)


def test_graph_helpers_fall_back_to_files_for_single_module_repos():
    from repolens.ui.render import file_graph_dot, module_graph_nodes

    snap = make_snapshot({"pkg/a.py": "from pkg import b\n", "pkg/b.py": "X = 1\n"})
    ana = analyze(snap)
    assert module_graph_nodes(ana) == 0
    assert '"pkg/a.py" -> "pkg/b.py"' in file_graph_dot(ana)


def test_ai_result_shows_usage_cost_and_session_total():
    at = AppTest.from_file(APP, default_timeout=30)
    snap = make_snapshot(SAMPLE_TS_REPO, kind="github")
    at.session_state["snapshot"] = snap
    at.session_state["analysis"] = analyze(snap)
    at.session_state["ai"] = {"overview@claude-opus-5-5": {
        "data": {"summary": {"text": "A demo.", "kind": "assumption", "citations": []}},
        "grounding": {"grounded": 0, "unseen": 0, "bad_lines": 0, "missing_file": 0, "downgraded": 0},
        "usage": {"input_tokens": 118, "cache_write_tokens": 60_765, "cache_read_tokens": 0,
                  "output_tokens": 11_176},
        "model": "claude-opus-5-5",
    }}
    at.session_state["spend"] = {"requests": 1, "cost": 0.5278, "unpriced": 0}
    at.run()
    assert not at.exception
    captions = [c.value for c in at.caption]
    assert any("estimated cost ≈ $0.53" in c for c in captions)
    assert any("AI usage this session: 1 request(s)" in c and "$0.53" in c for c in captions)


def _loaded_app(**state):
    at = AppTest.from_file(APP, default_timeout=30)
    snap = make_snapshot(SAMPLE_TS_REPO, kind="github")
    at.session_state["snapshot"] = snap
    at.session_state["analysis"] = analyze(snap)
    from repolens.storage import snapshot_fingerprint
    at.session_state["fingerprint"] = snapshot_fingerprint(snap)
    for k, v in state.items():
        at.session_state[k] = v
    return at


SAVED = {"overview@claude-opus-5-5": {
    "data": {"summary": {"text": "A demo.", "kind": "assumption", "citations": []}},
    "grounding": {"grounded": 0, "unseen": 0, "bad_lines": 0, "missing_file": 0, "downgraded": 0},
    "usage": {"input_tokens": 10, "cache_write_tokens": 0, "cache_read_tokens": 0, "output_tokens": 10},
    "model": "claude-opus-5-5",
}}


def test_default_is_browser_only_with_a_clear_warning(isolated_store):
    at = _loaded_app(ai=dict(SAVED)).run()
    assert not at.exception
    assert at.toggle(key="save_to_disk").value is False
    assert any("Refreshing loses them" in c.value for c in at.sidebar.caption)
    assert not isolated_store.exists()  # nothing written unless the user opts in


def test_opting_in_saves_existing_results_to_disk(isolated_store):
    at = _loaded_app(ai=dict(SAVED)).run()
    at.toggle(key="save_to_disk").set_value(True).run()
    assert not at.exception
    files = list(isolated_store.glob("*.json"))
    assert len(files) == 1 and "A demo." in files[0].read_text(encoding="utf-8")
    assert any("Saved 1 AI result(s)" in m.value for m in at.success)


def test_saved_results_are_offered_and_load_without_charge(isolated_store):
    from repolens.storage import ResultStore, snapshot_fingerprint

    snap = make_snapshot(SAMPLE_TS_REPO, kind="github")
    store = ResultStore(isolated_store)
    store.save(snapshot_fingerprint(snap), "demo", SAVED)
    at = _loaded_app(saved_offer=store.load(snapshot_fingerprint(snap))).run()
    assert any("Saved AI results found" in m.value for m in at.markdown)
    next(b for b in at.button if "Load 1 saved result" in b.label).click().run()
    assert not at.exception
    assert at.session_state["ai"]["overview@claude-opus-5-5"]["from_disk"] is True
    assert at.session_state["spend"]["requests"] == 0
    assert any("loaded from saved results, no charge" in c.value for c in at.caption)


def test_delete_saved_results(isolated_store):
    from repolens.storage import ResultStore, snapshot_fingerprint

    snap = make_snapshot(SAMPLE_TS_REPO, kind="github")
    store = ResultStore(isolated_store)
    fp = snapshot_fingerprint(snap)
    store.save(fp, "demo", SAVED)
    at = _loaded_app(saved_offer=store.load(fp)).run()
    next(b for b in at.button if b.label == "Delete saved results").click().run()
    assert not at.exception
    assert store.load(fp) is None


def test_explain_this_file_opens_the_file_and_switches_tab():
    snap = make_snapshot(SAMPLE_TS_REPO)
    ana = analyze(snap)
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["snapshot"] = snap
    at.session_state["analysis"] = ana

    def file_picker():
        return next(s for s in at.selectbox if s.label.startswith("File"))

    at.run()
    # The user first picks a file by hand; the button must still win afterwards.
    file_picker().set_value("src/types/event.ts").run()
    step = next(i for i, s in enumerate(ana.learning_path) if s.path != "src/types/event.ts")
    [b for b in at.button if b.label == "Explain this file"][step].click().run()
    assert not at.exception
    assert file_picker().value == ana.learning_path[step].path
    assert at.session_state["active_tab"] == "File explorer"


def test_ask_this_fills_question_and_switches_tab():
    snap = make_snapshot(SAMPLE_TS_REPO)
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["snapshot"] = snap
    at.session_state["analysis"] = analyze(snap)
    at.session_state["ai"] = {"learning_path@claude-opus-5-5": {
        "data": {"goal": "g", "steps": [], "explore_next": [
            {"title": "Webhooks", "question": "How is a webhook translated?", "why": "core flow"}]},
        "grounding": {"grounded": 0, "unseen": 0, "bad_lines": 0, "missing_file": 0, "downgraded": 0},
        "usage": {}, "model": "claude-opus-5-5",
    }}
    at.run()
    next(b for b in at.button if b.label == "Ask this").click().run()
    assert not at.exception
    assert at.text_area(key="feature_question").value == "How is a webhook translated?"
    assert at.session_state["active_tab"] == "Ask about a feature"
