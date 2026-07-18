"""
source_paths.py

Single source of truth for where the raw manifest and canonical topic
taxonomy live on disk. Every ingestion/graph-building script imports these
two paths from here instead of hardcoding or re-deriving them -- same
"one canonical place" pattern as db_env.py for DB credentials.

Both default to the exact files currently in use:

    MANIFEST_PATH  1000_manifest_final_slugs_filled.json on the Desktop
                   checkout (NOT the data/1000_manifest_final.json copy
                   committed in this repo -- that one is a stale fallback).
    TAXONOMY_PATH  topic_tags_taxonomy_v2.json, currently sitting in a
                   WhatsApp Desktop transfer folder. This is a fragile
                   location (WhatsApp's transfer cache can be cleared) --
                   move this file somewhere durable when convenient and
                   override via TOPIC_TAXONOMY_PATH in .env instead of
                   editing this file.

Override either at runtime without touching code:
    MANIFEST_PATH=path/to/manifest.json
    TOPIC_TAXONOMY_PATH=path/to/topic_tags_taxonomy_v2.json
"""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_MANIFEST = r"C:\Users\asus\OneDrive\Desktop\dsa-recommendation\1000_manifest_final_slugs_filled.json"
_DEFAULT_TAXONOMY = (
    r"C:\Users\asus\AppData\Local\Packages\5319275A.WhatsAppDesktop_cv1g1gvanyjgm"
    r"\LocalState\sessions\B706724C32AC44077F4BE384444E5D3F20F08258"
    r"\transfers\2026-29\topic_tags_taxonomy_v2.json"
)

MANIFEST_PATH = Path(os.environ.get("MANIFEST_PATH", _DEFAULT_MANIFEST))
TAXONOMY_PATH = Path(os.environ.get("TOPIC_TAXONOMY_PATH", _DEFAULT_TAXONOMY))


def require(path: Path, label: str) -> Path:
    """Fail loudly and early if a configured source file doesn't exist,
    instead of letting some downstream step misinterpret a FileNotFoundError."""
    if not path.exists():
        raise FileNotFoundError(
            f"{label} not found at: {path}\n"
            f"Set the correct path via an environment variable, e.g.\n"
            f"  $env:MANIFEST_PATH = 'C:\\path\\to\\manifest.json'"
        )
    return path
