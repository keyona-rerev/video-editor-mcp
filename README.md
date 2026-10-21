# video-editor-mcp

An MCP server that edits videos stored in Google Drive. Claude sends a short cut list. The server downloads the video, cuts it with ffmpeg, adds captions and title cards, and saves the finished MP4 back to Drive. The video never passes through the chat, so a 700 MB meeting recording is no problem.

## Tools

| Tool | What it does |
| --- | --- |
| `list_folder` | List files in a Drive folder (id or link). |
| `get_file_info` | Name, length, size, resolution, folder of a Drive video. |
| `get_transcript` | Read the transcript saved next to a video (Google Meet saves one). |
| `prepare_video` | Download a video to the server ahead of time (background job). |
| `get_frames` | Return still frames so Claude can see what is on screen. |
| `start_render` | Cut segments, add captions and cards, save the MP4 to Drive (background job). |
| `get_job` / `list_jobs` | Check a background job. A finished render includes the Drive link. |

`start_render` options: `segments`, `captions` (times on the finished video's clock), `aspect` (`16:9`, `9:16`, `1:1`), `fit` (`blur`, `pad`, `crop`), `intro` and `outro` cards, `fade`, `output_folder`.

## Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `MCP_AUTH_TOKEN` | Yes | Shared secret. Callers send it as `x-auth-token` or `Authorization: Bearer`. |
| `GOOGLE_CLIENT_ID` | Yes | OAuth client ID (Web application). |
| `GOOGLE_CLIENT_SECRET` | Yes | OAuth client secret. |
| `GOOGLE_REFRESH_TOKEN` | Yes, after sign-in | Set after the one-time sign-in below. |
| `PUBLIC_BASE_URL` | No | Public https URL of the service. Used for the sign-in redirect. Derived from the request when empty. |
| `DEFAULT_OUTPUT_FOLDER_ID` | No | Drive folder for finished edits. Empty means the source video's folder. |
| `MAX_CACHE_GB` | No | Disk limit for cached source videos. Default 10. |
| `WORK_DIR` | No | Scratch folder. Default `/tmp/video-editor`. |

## Setup

1. Create a Google Cloud OAuth client (type: Web application) with the Google Drive API enabled.
2. Deploy this repo on Railway. It builds from the `Dockerfile`, which installs ffmpeg.
3. Set `MCP_AUTH_TOKEN`, `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in Railway.
4. In Google Cloud, add `https://<your-railway-domain>/oauth/callback` as an authorized redirect URI on the OAuth client.
5. Open `https://<your-railway-domain>/oauth/start` once and sign in. The page shows a refresh token. Save it as `GOOGLE_REFRESH_TOKEN` in Railway. The sign-in pages shut off once that variable exists.
6. Add the connector in Claude: URL `https://<your-railway-domain>/mcp`, header `x-auth-token` set to your `MCP_AUTH_TOKEN`.

Scopes requested: `drive.readonly` (read videos the app did not create) and `drive.file` (save finished edits).

## Local development

```
pip install -r requirements.txt pytest
python -m pytest tests -q
PYTHONPATH=src python -m video_editor_mcp.server --transport streamable-http --port 8000
```

The tests need `ffmpeg` and `ffprobe` on the path.

## Notes

- One render runs at a time. Extra renders wait in a queue.
- Jobs live in memory. A restart clears the job list. Finished videos are already in Drive.
- Whisper transcription is not built in. Google Meet recordings come with a transcript. For other videos, add a transcription step later.
