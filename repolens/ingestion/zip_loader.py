"""Safe, in-memory ZIP ingestion.

Both import methods end here: GitHub imports download the repository's zipball
and pass the bytes in, uploads pass the uploaded file's bytes. Nothing is
extracted to disk and nothing from the archive is ever executed.
"""

from __future__ import annotations

import io
import logging
import re
import stat
import zipfile
from typing import BinaryIO

from repolens.config import DEFAULT_LIMITS, IngestLimits
from repolens.ingestion.models import (
    IngestionError,
    RepoOrigin,
    RepoSnapshot,
    SkippedSummary,
    SourceFile,
)

log = logging.getLogger(__name__)

IGNORED_DIRS = {
    ".git", "node_modules", "dist", "build", "out", ".venv", "venv", "env",
    "__pycache__", ".next", ".nuxt", "target", "vendor", ".idea", ".vscode",
    "coverage", ".mypy_cache", ".pytest_cache", ".tox", ".gradle", "bin", "obj",
    ".terraform", "site-packages", ".cache", ".parcel-cache", ".turbo",
}

BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svgz", ".pdf",
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".jar", ".war", ".exe",
    ".dll", ".so", ".dylib", ".bin", ".o", ".a", ".class", ".pyc", ".pyo",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".mov", ".avi",
    ".wav", ".ogg", ".webm", ".psd", ".sqlite", ".db", ".docx", ".xlsx", ".pptx",
    ".odt", ".ods", ".parquet", ".pkl", ".npy", ".h5", ".onnx", ".pt",
}

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def safe_member_path(name: str) -> str | None:
    """Normalise an archive member name, or return None if it is unsafe.

    Rejects absolute paths, drive letters, parent-directory segments and NUL
    bytes. Backslashes are treated as separators so Windows-made archives
    cannot smuggle ``..\\`` segments past the check.
    """
    if not name or "\x00" in name:
        return None
    normalised = name.replace("\\", "/")
    if normalised.startswith("/") or _DRIVE_RE.match(normalised):
        return None
    parts = [p for p in normalised.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def _looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _common_root(paths: list[str]) -> str | None:
    """GitHub zipballs wrap everything in ``owner-repo-sha/``; detect that."""
    if not paths:
        return None
    first = {p.split("/", 1)[0] for p in paths}
    if len(first) == 1 and all("/" in p for p in paths):
        return first.pop()
    return None


def load_zip(
    source: bytes | BinaryIO,
    origin: RepoOrigin,
    limits: IngestLimits = DEFAULT_LIMITS,
) -> RepoSnapshot:
    """Read a repository archive into a :class:`RepoSnapshot`.

    Raises :class:`IngestionError` for malformed archives or limit breaches.
    """
    data = source if isinstance(source, bytes) else source.read(limits.max_archive_bytes + 1)
    if len(data) > limits.max_archive_bytes:
        raise IngestionError(
            f"Archive is larger than the {limits.max_archive_bytes // (1024 * 1024)} MB limit."
        )
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise IngestionError("The file is not a valid ZIP archive.")

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise IngestionError("The ZIP archive could not be read.") from exc

    with zf:
        infos = zf.infolist()
        if len(infos) > limits.max_members:
            raise IngestionError(
                f"Archive has {len(infos)} entries; the limit is {limits.max_members}."
            )

        declared_total = sum(i.file_size for i in infos)
        if declared_total > limits.max_total_uncompressed_bytes:
            raise IngestionError("Archive expands beyond the allowed uncompressed size.")

        skipped = SkippedSummary()
        candidates: list[tuple[zipfile.ZipInfo, str]] = []
        for info in infos:
            if info.is_dir():
                continue
            path = safe_member_path(info.filename)
            if path is None:
                skipped.unsafe_path += 1
                log.warning("Skipped unsafe archive member")
                continue
            if _is_symlink(info):
                skipped.symlink += 1
                continue
            if info.flag_bits & 0x1:
                raise IngestionError("Encrypted ZIP archives are not supported.")
            if (
                info.compress_size > 0
                and info.file_size > 1024 * 1024
                and info.file_size / info.compress_size > limits.max_compression_ratio
            ):
                raise IngestionError(
                    "Archive contains a member with a suspicious compression ratio."
                )
            candidates.append((info, path))

        root = _common_root([p for _, p in candidates])
        snapshot = RepoSnapshot(origin=origin, skipped=skipped)
        actual_total = 0

        for info, path in candidates:
            if root:
                path = path[len(root) + 1 :]
            snapshot.all_paths.append(path)
            parts = path.split("/")
            if any(p in IGNORED_DIRS for p in parts[:-1]):
                skipped.ignored_dir += 1
                continue
            ext = "." + parts[-1].rsplit(".", 1)[-1].lower() if "." in parts[-1] else ""
            if ext in BINARY_EXTENSIONS:
                skipped.binary += 1
                continue
            if info.file_size > limits.max_file_bytes:
                skipped.too_large += 1
                continue
            if len(snapshot.files) >= limits.max_text_files:
                skipped.over_file_limit += 1
                continue

            # Never trust declared sizes: cap the actual read as well.
            with zf.open(info) as fh:
                raw = fh.read(limits.max_file_bytes + 1)
            if len(raw) > limits.max_file_bytes:
                skipped.too_large += 1
                continue
            actual_total += len(raw)
            if actual_total > limits.max_total_uncompressed_bytes:
                raise IngestionError("Archive expands beyond the allowed uncompressed size.")
            if _looks_binary(raw):
                skipped.binary += 1
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("latin-1")
            snapshot.files[path] = SourceFile(path=path, text=text, size=len(raw))

    if not snapshot.files:
        raise IngestionError("No readable source files were found in the archive.")
    snapshot.all_paths.sort()
    return snapshot
