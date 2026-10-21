import asyncio
import json
import shutil
from pathlib import Path

import pytest
from fastmcp import Client
from starlette.testclient import TestClient

from video_editor_mcp import config, drive, jobs, server

FILE_ID = "FILEID1234567"
FOLDER_ID = "FOLDERID123456"


@pytest.fixture
def fake_drive(monkeypatch, tmp_path, sample_video):
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    uploaded = tmp_path / "uploaded"
    uploaded.mkdir()
    calls = {"downloads": 0, "uploads": []}

    info = {
        "id": FILE_ID,
        "name": "Planning Period - 2026/10/20 18:03 CDT - Recording",
        "mimeType": "video/mp4",
        "size": str(sample_video.stat().st_size),
        "parents": [FOLDER_ID],
        "modifiedTime": "2026-10-21T01:53:42.854Z",
        "webViewLink": "https://drive.google.com/file/d/x/view",
        "videoMediaMetadata": {"durationMillis": "20000", "width": 1280, "height": 720},
    }

    def file_info(file_id):
        return info

    def download(file_id, dest, expected_size=None, progress=None):
        calls["downloads"] += 1
        shutil.copy(sample_video, dest)
        if progress:
            progress(expected_size, expected_size)
        return Path(dest)

    def upload(path, name, folder_id, mime_type="video/mp4", progress=None):
        shutil.copy(path, uploaded / name)
        calls["uploads"].append((name, folder_id))
        if progress:
            progress(1, 1)
        return {"id": "UPLOADED12345", "webViewLink": "https://drive.google.com/file/d/UPLOADED12345/view"}

    monkeypatch.setattr(drive, "file_info", file_info)
    monkeypatch.setattr(drive, "download", download)
    monkeypatch.setattr(drive, "upload", upload)
    monkeypatch.setattr(
        drive, "list_folder",
        lambda folder_id, name_contains="", limit=50: [{"id": "A" * 12, "name": "Planning Period - Transcript", "mimeType": "application/vnd.google-apps.document"}],
    )
    monkeypatch.setattr(drive, "read_text", lambda file_id, mime: "00:00:05 Keyona: Welcome to Club Alpha.")
    calls["uploaded_dir"] = uploaded
    return calls


async def _call(client, tool, args):
    result = await client.call_tool(tool, args)
    return result


def _text(result) -> dict:
    return json.loads(result.content[0].text)


async def _wait(client, job_id, timeout=240):
    for _ in range(timeout * 2):
        state = _text(await client.call_tool("get_job", {"job_id": job_id}))
        if state["status"] in ("done", "error"):
            return state
        await asyncio.sleep(0.5)
    raise AssertionError("job did not finish")


def test_full_flow(fake_drive):
    async def run():
        async with Client(server.mcp) as client:
            tools = {t.name for t in await client.list_tools()}
            assert {"list_folder", "get_file_info", "get_transcript", "prepare_video",
                    "get_frames", "start_render", "get_job", "list_jobs"} <= tools

            listing = _text(await _call(client, "list_folder", {"folder": f"https://drive.google.com/drive/folders/{FOLDER_ID}"}))
            assert listing["count"] == 1

            details = _text(await _call(client, "get_file_info", {"file": f"https://drive.google.com/file/d/{FILE_ID}/view"}))
            assert details["duration_seconds"] == 20.0
            assert details["cached_on_server"] is False

            transcript = _text(await _call(client, "get_transcript", {"file": FILE_ID}))
            assert "Welcome to Club Alpha" in transcript["text"]

            # Frames before the video is cached start a download.
            first = _text(await _call(client, "get_frames", {"file": FILE_ID, "timestamps": [1]}))
            assert first["status"] == "downloading"
            assert (await _wait(client, first["job_id"]))["status"] == "done"
            assert fake_drive["downloads"] == 1

            frames = await _call(client, "get_frames", {"file": FILE_ID, "timestamps": [1, "0:05"]})
            kinds = [c.type for c in frames.content]
            assert kinds.count("image") == 2

            started = _text(await _call(client, "start_render", {
                "file": FILE_ID,
                "segments": [{"start": "0:02", "end": 6}, {"start": 10, "end": 14}],
                "output_name": "Club Alpha Commercial",
                "captions": [{"text": "Build real AI tools", "start": 3.5, "end": 6}],
                "aspect": "9:16",
                "intro": {"title": "Club Alpha", "seconds": 2},
                "outro": {"title": "Join now", "subtitle": "rerev.io", "seconds": 2},
            }))
            state = await _wait(client, started["job_id"])
            assert state["status"] == "done", state
            assert state["result"]["drive_file_id"] == "UPLOADED12345"
            assert state["result"]["duration_seconds"] == pytest.approx(12, abs=0.6)
            assert state["result"]["folder_id"] == FOLDER_ID
            assert fake_drive["uploads"] == [("Club Alpha Commercial.mp4", FOLDER_ID)]
            assert (fake_drive["uploaded_dir"] / "Club Alpha Commercial.mp4").exists()
            # The video was downloaded once and reused.
            assert fake_drive["downloads"] == 1

            bad = _text(await _call(client, "start_render", {"file": FILE_ID, "segments": [], "output_name": "x"}))
            assert "error" in bad
            missing = _text(await _call(client, "get_job", {"job_id": "nope"}))
            assert "error" in missing

    asyncio.run(run())


def test_render_error_is_reported(fake_drive):
    async def run():
        async with Client(server.mcp) as client:
            started = _text(await _call(client, "start_render", {
                "file": FILE_ID, "segments": [{"start": 50, "end": 60}], "output_name": "late",
            }))
            state = await _wait(client, started["job_id"])
            assert state["status"] == "error"
            assert "starts at" in state["error"]

    asyncio.run(run())


def test_gate_and_sign_in_pages(monkeypatch, tmp_path):
    monkeypatch.setenv("WORK_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.delenv("GOOGLE_REFRESH_TOKEN", raising=False)

    with TestClient(server.build_app(), follow_redirects=False) as http:
        assert http.get("/health").json()["ok"] is True
        assert http.post("/mcp", json={}).status_code == 401
        assert http.post("/mcp", json={}, headers={"x-auth-token": "wrong"}).status_code == 401

        start = http.get("/oauth/start", headers={"host": "video.example.com"})
        assert start.status_code in (302, 307)
        location = start.headers["location"]
        assert location.startswith("https://accounts.google.com/")
        assert "client_id=client-id" in location
        assert "redirect_uri=https%3A%2F%2Fvideo.example.com%2Foauth%2Fcallback" in location
        assert "access_type=offline" in location
        assert "drive.readonly" in location

        assert http.get("/oauth/callback?state=bad&code=x").status_code == 400

        monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "already-set")
        assert http.get("/oauth/start").status_code == 404


def test_norm_id():
    assert drive.norm_id("https://drive.google.com/file/d/1IZDj2sih1BFxu_CAa9cBM1Q7CR1-28B5/view") == "1IZDj2sih1BFxu_CAa9cBM1Q7CR1-28B5"
    assert drive.norm_id("https://drive.google.com/drive/folders/1u0iwfwlexcX7k8gmIXj5_eSS4z5SBtDI") == "1u0iwfwlexcX7k8gmIXj5_eSS4z5SBtDI"
    assert drive.norm_id("https://drive.google.com/open?id=1IZDj2sih1BFxu_CAa9cBM1Q7CR1-28B5") == "1IZDj2sih1BFxu_CAa9cBM1Q7CR1-28B5"
    with pytest.raises(drive.DriveError):
        drive.norm_id("../../etc/passwd")
