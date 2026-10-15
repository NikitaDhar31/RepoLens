import io
import zipfile

import pytest

from conftest import make_zip
from repolens.config import IngestLimits
from repolens.ingestion import IngestionError, import_zip_upload, load_zip
from repolens.ingestion.models import RepoOrigin
from repolens.ingestion.zip_loader import safe_member_path

ORIGIN = RepoOrigin(kind="zip", display_name="t")


def test_loads_text_files_and_strips_github_root_folder():
    data = make_zip({
        "owner-repo-abc123/README.md": "# Hi\n",
        "owner-repo-abc123/src/app.py": "print('x')\n",
    })
    snap = load_zip(data, ORIGIN)
    assert sorted(snap.files) == ["README.md", "src/app.py"]
    assert snap.files["src/app.py"].line_count == 1


def test_keeps_root_when_files_live_at_top_level():
    snap = load_zip(make_zip({"README.md": "a", "src/x.py": "b"}), ORIGIN)
    assert "README.md" in snap.files and "src/x.py" in snap.files


def test_import_zip_upload_uses_file_name():
    snap = import_zip_upload(make_zip({"a.py": "x=1"}), "C:\\Users\\me\\my-project.zip")
    assert snap.origin.display_name == "my-project"
    assert snap.origin.kind == "zip"


@pytest.mark.parametrize("name", [
    "../evil.py", "a/../../evil.py", "/etc/passwd", "C:/Windows/evil.py",
    "C:evil.py", "..\\..\\evil.py", "a\\..\\..\\evil.py", "bad\x00name.py", "",
])
def test_safe_member_path_rejects_unsafe_names(name):
    assert safe_member_path(name) is None


def test_safe_member_path_normalises_harmless_names():
    assert safe_member_path("./src//a.py") == "src/a.py"
    assert safe_member_path("src\\pkg\\a.py") == "src/pkg/a.py"


def test_path_traversal_members_are_skipped_not_loaded():
    data = make_zip({"ok.py": "x = 1\n", "../../evil.py": "boom", "/abs.py": "boom"})
    snap = load_zip(data, ORIGIN)
    assert list(snap.files) == ["ok.py"]
    assert snap.skipped.unsafe_path == 2
    assert all(".." not in p and not p.startswith("/") for p in snap.all_paths)


def test_symlinks_are_skipped():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("real.py", "x = 1\n")
        link = zipfile.ZipInfo("link.py")
        link.external_attr = (0o120777 << 16)
        zf.writestr(link, "/etc/passwd")
    snap = load_zip(buf.getvalue(), ORIGIN)
    assert "link.py" not in snap.files
    assert snap.skipped.symlink == 1


def test_vendored_and_binary_files_are_skipped():
    data = make_zip({
        "src/a.py": "x = 1\n",
        "node_modules/lib/index.js": "module.exports = 1",
        ".git/config": "[core]",
        "logo.png": b"\x89PNG\r\n",
        "data.bin2": b"abc\x00def",
    })
    snap = load_zip(data, ORIGIN)
    assert list(snap.files) == ["src/a.py"]
    assert snap.skipped.ignored_dir == 2
    assert snap.skipped.binary == 2


def test_large_files_are_skipped():
    limits = IngestLimits(max_file_bytes=100)
    snap = load_zip(make_zip({"small.py": "x", "big.py": "x" * 500}), ORIGIN, limits)
    assert list(snap.files) == ["small.py"]
    assert snap.skipped.too_large == 1


def test_text_file_count_limit():
    limits = IngestLimits(max_text_files=2)
    snap = load_zip(make_zip({f"f{i}.py": "x" for i in range(5)}), ORIGIN, limits)
    assert len(snap.files) == 2
    assert snap.skipped.over_file_limit == 3


def test_rejects_too_many_members():
    limits = IngestLimits(max_members=3)
    with pytest.raises(IngestionError, match="entries"):
        load_zip(make_zip({f"f{i}.py": "x" for i in range(5)}), ORIGIN, limits)


def test_rejects_oversized_archive():
    limits = IngestLimits(max_archive_bytes=50)
    with pytest.raises(IngestionError, match="larger than"):
        load_zip(make_zip({"a.py": "x" * 1000}, compression=zipfile.ZIP_STORED), ORIGIN, limits)


def test_rejects_archive_exceeding_total_uncompressed_size():
    limits = IngestLimits(max_total_uncompressed_bytes=1000)
    with pytest.raises(IngestionError, match="uncompressed"):
        load_zip(make_zip({"a.txt": "x" * 600, "b.txt": "y" * 600}), ORIGIN, limits)


def test_rejects_zip_bomb_compression_ratio():
    bomb = make_zip({"bomb.txt": b"0" * (5 * 1024 * 1024)})
    with pytest.raises(IngestionError, match="compression ratio"):
        load_zip(bomb, ORIGIN)


def test_does_not_trust_declared_sizes():
    """A member whose real content exceeds the per-file limit is skipped even if read lazily."""
    limits = IngestLimits(max_file_bytes=10)
    snap = load_zip(make_zip({"ok.py": "x", "lying.py": "y" * 50}), ORIGIN, limits)
    assert "lying.py" not in snap.files


def test_rejects_non_zip_data():
    with pytest.raises(IngestionError, match="not a valid ZIP"):
        load_zip(b"this is not a zip", ORIGIN)


def test_rejects_archive_with_no_readable_files():
    with pytest.raises(IngestionError, match="No readable"):
        load_zip(make_zip({"image.png": b"\x89PNG"}), ORIGIN)


def test_rejects_encrypted_members():
    data = bytearray(make_zip({"a.py": "x"}, compression=zipfile.ZIP_STORED))
    # Set the "encrypted" general-purpose flag bit in the local and central headers.
    for sig in (b"PK\x03\x04", b"PK\x01\x02"):
        idx = data.find(sig)
        flag_offset = idx + (6 if sig == b"PK\x03\x04" else 8)
        data[flag_offset] |= 0x1
    with pytest.raises(IngestionError, match="Encrypted"):
        load_zip(bytes(data), ORIGIN)


def test_non_utf8_text_is_decoded_not_dropped():
    snap = load_zip(make_zip({"legacy.txt": "caf\xe9".encode("latin-1")}), ORIGIN)
    assert snap.files["legacy.txt"].text == "café"
