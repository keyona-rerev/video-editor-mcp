"""Runtime configuration read from the environment."""

from __future__ import annotations

import os
from pathlib import Path

TOKEN_ENV = "MCP_AUTH_TOKEN"
CLIENT_ID_ENV = "GOOGLE_CLIENT_ID"
CLIENT_SECRET_ENV = "GOOGLE_CLIENT_SECRET"
REFRESH_TOKEN_ENV = "GOOGLE_REFRESH_TOKEN"
PUBLIC_URL_ENV = "PUBLIC_BASE_URL"
WORK_DIR_ENV = "WORK_DIR"
CACHE_GB_ENV = "MAX_CACHE_GB"
OUTPUT_FOLDER_ENV = "DEFAULT_OUTPUT_FOLDER_ID"


def _env(name: str) -> str | None:
    return os.environ.get(name, "").strip() or None


def auth_token() -> str | None:
    """The shared secret required on HTTP requests, if one is configured."""
    return _env(TOKEN_ENV)


def google_client_id() -> str | None:
    return _env(CLIENT_ID_ENV)


def google_client_secret() -> str | None:
    return _env(CLIENT_SECRET_ENV)


def google_refresh_token() -> str | None:
    return _env(REFRESH_TOKEN_ENV)


def public_base_url() -> str | None:
    """Public https URL of this service, used for the one-time Google sign-in."""
    value = _env(PUBLIC_URL_ENV)
    return value.rstrip("/") if value else None


def work_dir() -> Path:
    """Scratch folder for downloaded videos and render jobs."""
    path = Path(_env(WORK_DIR_ENV) or "/tmp/video-editor")
    path.mkdir(parents=True, exist_ok=True)
    return path


def max_cache_bytes() -> int:
    """Upper limit for cached source videos on disk."""
    return int(float(_env(CACHE_GB_ENV) or "10") * 1024**3)


def default_output_folder() -> str | None:
    """Drive folder for finished edits when a render names no folder."""
    return _env(OUTPUT_FOLDER_ENV)
