"""Video Editor MCP server.

Reads videos from Google Drive, cuts and captions them with ffmpeg, and saves
the finished edit back to Drive. The video never passes through the chat.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import secrets
import shutil
import sys
import threading
from pathlib import Path
from typing import Optional, Union

from fastmcp import FastMCP
from fastmcp.utilities.types import Image
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)

from video_editor_mcp import __version__, config, drive, engine, jobs

mcp = FastMCP("Video Editor MCP Server")

TimeValue = Union[str, float]


class SegmentIn(BaseModel):
    start: TimeValue = Field(description='Start time: seconds (83.5), "MM:SS" or "HH:MM:SS".')
    end: TimeValue = Field(description="End time. Same formats as start.")


class CaptionIn(BaseModel):
    text: str = Field(description="Caption text. Keep it to one or two short lines.")
    start: TimeValue = Field(description="When the caption appears, on the FINISHED video's clock.")
    end: TimeValue = Field(description="When the caption disappears, on the finished video's clock.")


class CardIn(BaseModel):
    title: Optional[str] = Field(default=None, description="Large text.")
    subtitle: Optional[str] = Field(default=None, description="Smaller text under the title.")
    seconds: float = Field(default=3.0, description="How long the card shows (0.5 to 15).")
    bg: str = Field(default="#111111", description="Background color as #RRGGBB.")
    fg: str = Field(default="#FFFFFF", description="Text color as #RRGGBB.")


def _json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _error(exc: Exception) -> str:
    return _json({"error": str(exc)})


# --- local copies of Drive videos ---------------------------------------

_download_lock = threading.Lock()


def _cache_path(file_id: str, info: dict) -> Path:
    stamp = re.sub(r"[^A-Za-z0-9]", "", info.get("modifiedTime", ""))
    folder = config.work_dir() / "src"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{file_id}-{stamp}.mp4"


def _cached(file_id: str, info: dict) -> Optional[Path]:
    path = _cache_path(file_id, info)
    size = int(info.get("size") or 0)
    if path.exists() and size and path.stat().st_size == size:
        return path
    return None


def _evict(folder: Path, keep: Path, need: int) -> None:
    """Delete the oldest cached videos until the new one fits."""
    files = sorted(
        (p for p in folder.glob("*") if p.is_file() and p != keep),
        key=lambda p: p.stat().st_mtime,
    )
    total = sum(p.stat().st_size for p in files) + need
    while files and total > config.max_cache_bytes():
        victim = files.pop(0)
        total -= victim.stat().st_size
        victim.unlink(missing_ok=True)


def _local_copy(file_id: str, info: dict, progress=None) -> Path:
    mime = info.get("mimeType", "")
    if not mime.startswith("video/"):
        raise engine.EngineError(f"That file is not a video (type: {mime}).")
    path = _cache_path(file_id, info)
    with _download_lock:
        if not _cached(file_id, info):
            _evict(path.parent, path, int(info.get("size") or 0))
            drive.download(file_id, path, int(info.get("size") or 0), progress)
    path.touch()
    return path


# --- tools ---------------------------------------------------------------


@mcp.tool()
async def list_folder(folder: str, name_contains: str = "", limit: int = 50) -> str:
    """List the files in a Drive folder, newest first.

    Accepts a folder id or a Drive folder link. Use name_contains to filter by
    part of the file name.
    """
    try:
        folder_id = drive.norm_id(folder)
        files = await asyncio.to_thread(drive.list_folder, folder_id, name_contains, limit)
    except Exception as exc:
        return _error(exc)
    return _json({"count": len(files), "files": files})


@mcp.tool()
async def get_file_info(file: str) -> str:
    """Get details of a Drive video: name, length, size, resolution, folder.

    Accepts a file id or a Drive link. Reads details only. It does not
    download the video.
    """
    try:
        file_id = drive.norm_id(file)
        info = await asyncio.to_thread(drive.file_info, file_id)
    except Exception as exc:
        return _error(exc)
    meta = info.get("videoMediaMetadata") or {}
    millis = meta.get("durationMillis")
    return _json(
        {
            "id": info["id"],
            "name": info.get("name"),
            "mime_type": info.get("mimeType"),
            "size_mb": round(int(info.get("size") or 0) / 1024 / 1024, 1),
            "duration_seconds": round(int(millis) / 1000, 1) if millis else None,
            "width": meta.get("width"),
            "height": meta.get("height"),
            "folder_id": (info.get("parents") or [None])[0],
            "modified": info.get("modifiedTime"),
            "link": info.get("webViewLink"),
            "cached_on_server": bool(_cached(file_id, info)),
        }
    )


@mcp.tool()
async def get_transcript(file: str, transcript_file: str = "", max_chars: int = 150000) -> str:
    """Read the transcript saved next to a video (Google Meet saves one).

    Looks in the video's folder for a file whose name starts like the video's
    and contains "Transcript". Pass transcript_file to read a specific file.
    Returns the text, plus the other candidates it found.
    """
    try:
        file_id = drive.norm_id(file)
        info = await asyncio.to_thread(drive.file_info, file_id)
        if transcript_file:
            target_id = drive.norm_id(transcript_file)
            target = await asyncio.to_thread(drive.file_info, target_id)
            candidates = []
        else:
            candidates = await asyncio.to_thread(drive.find_transcripts, info)
            if not candidates:
                return _json({"error": "No transcript found in the video's folder."})
            target = candidates[0]
        text = await asyncio.to_thread(drive.read_text, target["id"], target.get("mimeType", ""))
    except Exception as exc:
        return _error(exc)
    truncated = len(text) > max_chars
    return _json(
        {
            "transcript_name": target.get("name"),
            "transcript_id": target["id"],
            "characters": len(text),
            "truncated": truncated,
            "text": text[:max_chars],
            "other_candidates": [
                {"id": c["id"], "name": c["name"]} for c in candidates[1:5]
            ],
        }
    )


@mcp.tool()
async def prepare_video(file: str) -> str:
    """Download a Drive video to the server so frames and renders start fast.

    Starts a background job and returns a job_id. Check it with get_job. Large
    files take a minute or more. Skip this if get_file_info says the video is
    already cached.
    """
    try:
        file_id = drive.norm_id(file)
        info = await asyncio.to_thread(drive.file_info, file_id)
    except Exception as exc:
        return _error(exc)

    def work(job: jobs.Job) -> dict:
        path = _local_copy(
            file_id, info,
            lambda done, total: job.set("downloading", done / max(total, 1)),
        )
        details = engine.probe(path)
        return {"cached": True, **details}

    return _json(jobs.submit("prepare", work).to_dict())


@mcp.tool()
async def get_frames(file: str, timestamps: list[TimeValue], width: int = 640):
    """Return still frames from a video so you can see what is on screen.

    Give up to 8 timestamps (seconds or "MM:SS"). The video must be cached:
    if it is not, this starts the download and tells you to check get_job.
    """
    try:
        file_id = drive.norm_id(file)
        info = await asyncio.to_thread(drive.file_info, file_id)
        path = _cached(file_id, info)
        if path is None:
            def work(job: jobs.Job) -> dict:
                local = _local_copy(
                    file_id, info,
                    lambda done, total: job.set("downloading", done / max(total, 1)),
                )
                return {"cached": True, **engine.probe(local)}

            job = jobs.submit("prepare", work)
            return _json(
                {
                    "status": "downloading",
                    "message": "The video is not on the server yet. Download started. "
                    "Check get_job, then call get_frames again.",
                    "job_id": job.id,
                }
            )
        if not 1 <= len(timestamps) <= 8:
            return _json({"error": "Give between 1 and 8 timestamps."})
        folder = config.work_dir() / "frames"
        folder.mkdir(parents=True, exist_ok=True)
        output: list = []
        for number, stamp in enumerate(timestamps):
            seconds = engine.parse_time(stamp)
            frame = folder / f"{file_id}-{number}-{int(seconds * 1000)}.jpg"
            await asyncio.to_thread(engine.extract_frame, path, seconds, frame, width)
            output.append(f"Frame at {stamp}")
            output.append(Image(data=frame.read_bytes(), format="jpeg"))
            frame.unlink(missing_ok=True)
        return output
    except Exception as exc:
        return _error(exc)


@mcp.tool()
async def start_render(
    file: str,
    segments: list[SegmentIn],
    output_name: str,
    output_folder: str = "",
    captions: Optional[list[CaptionIn]] = None,
    aspect: str = "16:9",
    fit: str = "blur",
    intro: Optional[CardIn] = None,
    outro: Optional[CardIn] = None,
    fade: float = 0.2,
) -> str:
    """Cut a Drive video into an edit and save the MP4 back to Drive.

    Runs as a background job. Returns a job_id. Check it with get_job.

    - segments: the parts of the source to keep, in order.
    - captions: burned-in text. Times are on the FINISHED video's clock, so
      add the intro card length to every caption time when you use an intro.
    - aspect: "16:9", "9:16" or "1:1".
    - fit: how the source fills the frame. "blur" fits the whole picture over a
      blurred copy. "pad" adds black bars. "crop" fills the frame and cuts the
      edges.
    - intro / outro: title or end cards (title, subtitle, seconds, bg, fg).
    - output_folder: Drive folder id or link. Empty means the default output
      folder, or the source video's folder.
    """
    try:
        file_id = drive.norm_id(file)
        if aspect not in engine.ASPECTS:
            raise engine.EngineError(f"aspect must be one of {', '.join(engine.ASPECTS)}.")
        if fit not in engine.FITS:
            raise engine.EngineError(f"fit must be one of {', '.join(engine.FITS)}.")
        if not segments:
            raise engine.EngineError("Give at least one segment.")
        parsed = []
        for number, item in enumerate(segments, 1):
            start, end = engine.parse_time(item.start), engine.parse_time(item.end)
            if end <= start:
                raise engine.EngineError(f"Segment {number} ends before it starts.")
            parsed.append((start, end))
        caption_list = [c.model_dump() for c in (captions or [])]
        for item in caption_list:
            engine.parse_time(item["start"])
            engine.parse_time(item["end"])
        name = re.sub(r"[^\w\-. ()]", "_", output_name).strip() or "edit"
        if not name.lower().endswith(".mp4"):
            name += ".mp4"
        info = await asyncio.to_thread(drive.file_info, file_id)
        if not info.get("mimeType", "").startswith("video/"):
            raise engine.EngineError(f"That file is not a video (type: {info.get('mimeType')}).")
        if output_folder:
            folder_id = drive.norm_id(output_folder)
        else:
            folder_id = config.default_output_folder() or (info.get("parents") or [""])[0]
        if not folder_id:
            raise engine.EngineError("No output folder. Pass output_folder.")
    except Exception as exc:
        return _error(exc)

    intro_spec = intro.model_dump() if intro else None
    outro_spec = outro.model_dump() if outro else None

    def work(job: jobs.Job) -> dict:
        source = _local_copy(
            file_id, info,
            lambda done, total: job.set("downloading", 0.25 * done / max(total, 1)),
        )
        workdir = config.work_dir() / "jobs" / job.id
        try:
            result = engine.render(
                source,
                workdir / name,
                parsed,
                workdir=workdir,
                captions=caption_list,
                aspect=aspect,
                fit=fit,
                intro=intro_spec,
                outro=outro_spec,
                fade=fade,
                progress=lambda stage, fraction: job.set(stage, 0.25 + 0.65 * fraction),
            )
            uploaded = drive.upload(
                Path(result["path"]),
                name,
                folder_id,
                progress=lambda done, total: job.set("uploading", 0.9 + 0.1 * done / max(total, 1)),
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return {
            "file_name": name,
            "drive_file_id": uploaded.get("id"),
            "drive_link": uploaded.get("webViewLink"),
            "folder_id": folder_id,
            "duration_seconds": round(result["duration"], 1),
            "width": result["width"],
            "height": result["height"],
            "size_mb": round(result["size_bytes"] / 1024 / 1024, 1),
            "captions": result["captions"],
            "segments": result["segments"],
        }

    return _json(jobs.submit("render", work).to_dict())


@mcp.tool()
async def get_job(job_id: str) -> str:
    """Check a background job: status, stage, progress and the final result.

    Status is queued, running, done or error. A finished render includes the
    Drive link of the new video.
    """
    job = jobs.get(job_id.strip())
    if job is None:
        return _json({"error": "No job with that id. The server may have restarted."})
    return _json(job.to_dict())


@mcp.tool()
async def list_jobs(limit: int = 10) -> str:
    """List the most recent background jobs."""
    return _json([job.to_dict() for job in jobs.recent(limit)])


# --- one-time Google sign-in ---------------------------------------------

_pending_states: set[str] = set()


def _redirect_uri(request: Request) -> str:
    base = config.public_base_url()
    if not base:
        scheme = request.headers.get("x-forwarded-proto", "https").split(",")[0].strip()
        base = f"{scheme}://{request.headers.get('host', '')}"
    return f"{base}/oauth/callback"


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "version": __version__,
            "ffmpeg": shutil.which("ffmpeg") is not None,
            "google_ready": drive.configured(),
        }
    )


@mcp.custom_route("/oauth/start", methods=["GET"])
async def oauth_start(request: Request):
    if config.google_refresh_token():
        return PlainTextResponse("Google sign-in is already set up.", status_code=404)
    if not (config.google_client_id() and config.google_client_secret()):
        return PlainTextResponse(
            "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET first.", status_code=500
        )
    state = secrets.token_urlsafe(16)
    _pending_states.add(state)
    return RedirectResponse(drive.authorize_url(_redirect_uri(request), state))


@mcp.custom_route("/oauth/callback", methods=["GET"])
async def oauth_callback(request: Request):
    if config.google_refresh_token():
        return PlainTextResponse("Google sign-in is already set up.", status_code=404)
    state = request.query_params.get("state", "")
    code = request.query_params.get("code", "")
    if state not in _pending_states or not code:
        return PlainTextResponse("Sign-in link is not valid. Start again at /oauth/start.", status_code=400)
    _pending_states.discard(state)
    try:
        tokens = await asyncio.to_thread(drive.exchange_code, code, _redirect_uri(request))
    except Exception as exc:
        return PlainTextResponse(str(exc), status_code=500)
    refresh = tokens.get("refresh_token")
    if not refresh:
        return PlainTextResponse(
            "Google sent no refresh token. Remove this app at "
            "https://myaccount.google.com/permissions and start again at /oauth/start.",
            status_code=400,
        )
    page = (
        "<!doctype html><meta charset=utf-8><title>Video Editor MCP</title>"
        "<body style='font-family:sans-serif;max-width:640px;margin:40px auto'>"
        "<h2>Google sign-in worked</h2>"
        "<p>Copy this value. In Railway, add a variable named "
        "<b>GOOGLE_REFRESH_TOKEN</b> and paste it as the value.</p>"
        f"<pre style='white-space:pre-wrap;word-break:break-all;background:#eee;padding:12px'>{refresh}</pre>"
        "<p>After you save the variable, this page stops working. "
        "Do not share the value.</p></body>"
    )
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


# --- server entry point --------------------------------------------------

_OPEN_PATHS = {"/health", "/oauth/start", "/oauth/callback"}


class _TokenGate:
    """ASGI middleware that requires a shared secret on every HTTP request.

    The server has a public URL and no OAuth of its own. Set MCP_AUTH_TOKEN to
    turn the gate on. The health check and the one-time Google sign-in pages
    stay open. The sign-in pages shut off once GOOGLE_REFRESH_TOKEN is set.
    """

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in _OPEN_PATHS:
            await self.app(scope, receive, send)
            return

        presented = ""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                presented = value.decode("latin-1").removeprefix("Bearer ").strip()
                break
            if name == b"x-auth-token":
                presented = value.decode("latin-1").strip()
                break

        if not hmac.compare_digest(presented, self.token):
            body = b'{"error":"unauthorized"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)


def build_app():
    """The HTTP app, wrapped in the token gate when a token is set."""
    app = mcp.http_app(path="/mcp")
    token = config.auth_token()
    return _TokenGate(app, token) if token else app


def main():
    """Run the MCP server."""
    transport = "stdio"
    if "--transport" in sys.argv:
        idx = sys.argv.index("--transport")
        if idx + 1 < len(sys.argv):
            transport = sys.argv[idx + 1]

    if transport == "stdio":
        mcp.run(transport="stdio")
        return

    import uvicorn

    port = 8000
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    print(f"video-editor-mcp {__version__}", file=sys.stderr, flush=True)
    print(f"ffmpeg: {'found' if shutil.which('ffmpeg') else 'MISSING'}", file=sys.stderr, flush=True)
    if drive.configured():
        print("google: ready", file=sys.stderr, flush=True)
    else:
        print(
            "google: not signed in. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET, "
            "then open /oauth/start once.",
            file=sys.stderr, flush=True,
        )
    if config.auth_token():
        print("auth: token required", file=sys.stderr, flush=True)
    else:
        print(
            f"auth: OPEN. Anyone with the URL can call this server. "
            f"Set {config.TOKEN_ENV} to require a token.",
            file=sys.stderr, flush=True,
        )

    uvicorn.run(build_app(), host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
