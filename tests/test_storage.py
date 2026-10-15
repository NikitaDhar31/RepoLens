import json

import pytest

from conftest import SAMPLE_TS_REPO, make_snapshot
from repolens.storage import ResultStore, snapshot_fingerprint

RESULT = {"data": {"summary": {"text": "explained"}}, "grounding": {}, "usage": {}, "model": "m"}


def test_fingerprint_depends_only_on_file_contents():
    a = make_snapshot(SAMPLE_TS_REPO)
    b = make_snapshot(SAMPLE_TS_REPO, kind="github")  # same code, different source
    assert snapshot_fingerprint(a) == snapshot_fingerprint(b)
    changed = make_snapshot({**SAMPLE_TS_REPO, "src/translate.ts": "// edited\n"})
    assert snapshot_fingerprint(changed) != snapshot_fingerprint(a)
    assert len(snapshot_fingerprint(a)) == 32


def test_save_load_delete_roundtrip(tmp_path):
    store = ResultStore(tmp_path)
    fp = snapshot_fingerprint(make_snapshot(SAMPLE_TS_REPO))
    assert store.load(fp) is None
    path = store.save(fp, "demo", {"overview@m": RESULT})
    assert path.parent == tmp_path and store.saved_count() == 1
    loaded = store.load(fp)
    assert loaded.repo == "demo" and loaded.results == {"overview@m": RESULT} and loaded.saved_at
    assert store.delete(fp) and store.load(fp) is None and not store.delete(fp)


def test_source_files_are_never_written(tmp_path):
    snap = make_snapshot(SAMPLE_TS_REPO)
    store = ResultStore(tmp_path)
    path = store.save(snapshot_fingerprint(snap), "demo", {"overview@m": RESULT})
    text = path.read_text(encoding="utf-8")
    assert "Translate a raw webhook payload" not in text  # a line from src/translate.ts
    assert "No source files are stored here" in json.loads(text)["note"]


def test_corrupt_file_is_ignored(tmp_path):
    store = ResultStore(tmp_path)
    fp = "0" * 32
    (tmp_path / f"{fp}.json").write_text("{not json", encoding="utf-8")
    assert store.load(fp) is None


def test_fingerprint_cannot_escape_the_directory(tmp_path):
    store = ResultStore(tmp_path)
    for bad in ("../../etc/passwd", "ABC", "0" * 31):
        with pytest.raises(ValueError):
            store.load(bad)


def test_no_temp_files_left_after_save(tmp_path):
    store = ResultStore(tmp_path)
    store.save("a" * 32, "demo", {"k": RESULT})
    assert [p.name for p in tmp_path.iterdir()] == ["a" * 32 + ".json"]
