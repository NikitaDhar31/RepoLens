"""Central settings and secret lookup.

Secrets are read from environment variables first, then Streamlit secrets.
They are never logged or rendered.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class IngestLimits:
    """Resource limits applied to every import, regardless of source."""

    max_archive_bytes: int = 100 * 1024 * 1024  # compressed archive / download
    max_total_uncompressed_bytes: int = 300 * 1024 * 1024
    max_members: int = 20_000  # entries in the archive, including skipped ones
    max_text_files: int = 5_000  # text files actually kept for analysis
    max_file_bytes: int = 1 * 1024 * 1024  # larger files are skipped, not read
    max_compression_ratio: float = 200.0  # per-member zip-bomb guard
    download_timeout_s: float = 60.0


DEFAULT_LIMITS = IngestLimits()

DEFAULT_MODEL = "claude-opus-5-5"


def get_secret(name: str) -> str | None:
    """Return a secret from the environment or Streamlit secrets, else None."""
    value = os.environ.get(name)
    if value:
        return value.strip()
    try:
        import streamlit as st

        if name in st.secrets:
            secret = st.secrets[name]
            return str(secret).strip() or None
    except Exception:  # no secrets.toml, or not running under Streamlit
        pass
    return None


def get_model() -> str:
    return os.environ.get("REPOLENS_MODEL", DEFAULT_MODEL)
