"""Google Drive access over the REST API with one stored OAuth refresh token."""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlencode

import httpx

from video_editor_mcp import config

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"

# drive.readonly reads videos the app did not create (for example Meet
# recordings). drive.file lets the app save finished edits.
SCOPES = (
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
)

FILE_FIELDS = (
    "id,name,mimeType,size,parents,createdTime,modifiedTime,webViewLink,"
    "videoMediaMetadata"
)

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
_URL_ID_RE = re.compile(r"(?:/d/|/folders/|[?&]id=)([A-Za-z0-9_-]{10,200})")

ProgressFn = Callable[[int, int], None]


class DriveError(RuntimeError):
    """A Drive or Google sign-in call failed."""


def norm_id(value: str) -> str:
    """Accept a Drive link or a bare id. Return the id."""
    text = (value or "").strip()
    match = _URL_ID_RE.search(text)
    if match:
        text = match.group(1)
    if not _ID_RE.match(text):
        raise DriveError(f"Not a valid Drive file or folder id or link: {value!r}")
    return text


def configured() -> bool:
    return bool(
        config.google_client_id()
        and config.google_client_secret()
        and config.google_refresh_token()
    )


# --- sign-in -----------------------------------------------------------

_lock = threading.Lock()
_token = {"value": None, "expires": 0.0}


def access_token() -> str:
    """Return a valid access token. Refresh it when it is near expiry."""
    with _lock:
        if _token["value"] and time.time() < _token["expires"] - 60:
            return _token["value"]
        if not configured():
            raise DriveError(
                "Google sign-in is not set up. Set GOOGLE_CLIENT_ID, "
                "GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN on the service."
            )
        response = httpx.post(
            TOKEN_URL,
            data={
                "client_id": config.google_client_id(),
                "client_secret": config.google_client_secret(),
                "refresh_token": config.google_refresh_token(),
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        if response.status_code != 200:
            raise DriveError(
                f"Google token refresh failed ({response.status_code}): "
                f"{response.text[:300]}"
            )
        body = response.json()
        _token["value"] = body["access_token"]
        _token["expires"] = time.time() + int(body.get("expires_in", 3600))
        return _token["value"]


def _headers() -> dict:
    return {"Authorization": f"Bearer {access_token()}"}


def authorize_url(redirect_uri: str, state: str) -> str:
    """URL that starts the one-time Google sign-in."""
    query = urlencode(
        {
            "client_id": config.google_client_id() or "",
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"{AUTH_URL}?{query}"


def exchange_code(code: str, redirect_uri: str) -> dict:
    """Trade the sign-in code for tokens."""
    response = httpx.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": config.google_client_id(),
            "client_secret": config.google_client_secret(),
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise DriveError(
            f"Google code exchange failed ({response.status_code}): "
            f"{response.text[:300]}"
        )
    return response.json()


# --- helpers -----------------------------------------------------------


def _check(response: httpx.Response, what: str) -> None:
    if response.status_code < 400:
        return
    try:
        message = response.json().get("error", {}).get("message", response.text)
    except Exception:
        message = response.text
    raise DriveError(f"{what} failed ({response.status_code}): {str(message)[:300]}")


def _quote(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


# --- reading -----------------------------------------------------------


def file_info(file_id: str) -> dict:
    response = httpx.get(
        f"{API}/files/{file_id}",
        params={"fields": FILE_FIELDS, "supportsAllDrives": "true"},
        headers=_headers(),
        timeout=30,
    )
    _check(response, "Reading file details")
    return response.json()


def list_folder(folder_id: str, name_contains: str = "", limit: int = 50) -> list[dict]:
    query = f"'{folder_id}' in parents and trashed = false"
    if name_contains:
        query += f" and name contains '{_quote(name_contains)}'"
    response = httpx.get(
        f"{API}/files",
        params={
            "q": query,
            "pageSize": max(1, min(limit, 100)),
            "orderBy": "modifiedTime desc",
            "fields": "files(id,name,mimeType,size,modifiedTime,webViewLink)",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        },
        headers=_headers(),
        timeout=30,
    )
    _check(response, "Listing the folder")
    return response.json().get("files", [])


def find_transcripts(info: dict) -> list[dict]:
    """Look in the video's folder for a transcript with a matching name.

    Google Meet saves "<meeting> - Recording" and "<meeting> - Transcript"
    side by side.
    """
    parents = info.get("parents") or []
    if not parents:
        return []
    name = info.get("name", "")
    base = re.split(r"\s+-\s+Recording\b", name, maxsplit=1)[0].strip()
    if not base or base == name:
        base = name.rsplit(".", 1)[0]
    found = list_folder(parents[0], name_contains="Transcript", limit=100)
    matches = [item for item in found if item["name"].startswith(base)]
    return matches or found[:10]


def read_text(file_id: str, mime_type: str) -> str:
    """Read a Google Doc or a plain text file as text."""
    if mime_type.startswith("application/vnd.google-apps."):
        response = httpx.get(
            f"{API}/files/{file_id}/export",
            params={"mimeType": "text/plain"},
            headers=_headers(),
            timeout=60,
        )
    else:
        response = httpx.get(
            f"{API}/files/{file_id}",
            params={"alt": "media", "supportsAllDrives": "true"},
            headers=_headers(),
            timeout=60,
        )
    _check(response, "Reading the transcript")
    return response.text


def download(
    file_id: str,
    dest: Path,
    expected_size: Optional[int] = None,
    progress: Optional[ProgressFn] = None,
) -> Path:
    """Stream a Drive file to disk. Skip it when a full copy already exists."""
    dest = Path(dest)
    if dest.exists() and expected_size and dest.stat().st_size == expected_size:
        return dest
    part = dest.with_name(dest.name + ".part")
    with httpx.stream(
        "GET",
        f"{API}/files/{file_id}",
        params={"alt": "media", "supportsAllDrives": "true"},
        headers=_headers(),
        timeout=httpx.Timeout(30.0, read=180.0),
        follow_redirects=True,
    ) as response:
        if response.status_code >= 400:
            response.read()
            _check(response, "Downloading the video")
        total = int(response.headers.get("content-length") or expected_size or 0)
        done = 0
        last_report = 0.0
        with open(part, "wb") as handle:
            for chunk in response.iter_bytes(1024 * 1024):
                handle.write(chunk)
                done += len(chunk)
                now = time.time()
                if progress and now - last_report > 1.0:
                    progress(done, total)
                    last_report = now
    if expected_size and part.stat().st_size != expected_size:
        part.unlink(missing_ok=True)
        raise DriveError("Download ended early. Try again.")
    part.replace(dest)
    if progress:
        progress(dest.stat().st_size, dest.stat().st_size)
    return dest


# --- writing -----------------------------------------------------------

_CHUNK = 16 * 1024 * 1024  # must be a multiple of 256 KiB


def upload(
    path: Path,
    name: str,
    folder_id: str,
    mime_type: str = "video/mp4",
    progress: Optional[ProgressFn] = None,
) -> dict:
    """Upload a file to a Drive folder with a resumable session."""
    path = Path(path)
    size = path.stat().st_size
    if size == 0:
        raise DriveError("The rendered file is empty.")
    start = httpx.post(
        UPLOAD,
        params={
            "uploadType": "resumable",
            "supportsAllDrives": "true",
            "fields": "id,name,size,webViewLink",
        },
        headers={
            **_headers(),
            "X-Upload-Content-Type": mime_type,
            "X-Upload-Content-Length": str(size),
        },
        json={"name": name, "parents": [folder_id]},
        timeout=60,
    )
    _check(start, "Starting the upload")
    session = start.headers.get("Location")
    if not session:
        raise DriveError("Google gave no upload address.")

    sent = 0
    with open(path, "rb") as handle:
        while sent < size:
            data = handle.read(_CHUNK)
            end = sent + len(data) - 1
            response = None
            for attempt in range(4):
                response = httpx.put(
                    session,
                    content=data,
                    headers={
                        "Content-Range": f"bytes {sent}-{end}/{size}",
                        "Content-Length": str(len(data)),
                        **_headers(),
                    },
                    timeout=httpx.Timeout(30.0, write=300.0, read=300.0),
                )
                if response.status_code in (200, 201, 308):
                    break
                if response.status_code >= 500:
                    time.sleep(2 * (attempt + 1))
                    continue
                _check(response, "Uploading the edit")
            else:
                _check(response, "Uploading the edit")
            sent += len(data)
            if progress:
                progress(sent, size)
            if response.status_code in (200, 201):
                return response.json()
    raise DriveError("Upload ended without a final reply from Google.")
