"""ffmpeg engine: turn a source video into a captioned, branded edit."""

from __future__ import annotations

import json
import re
import subprocess
import textwrap
from pathlib import Path
from typing import Callable, Optional, Sequence

Progress = Callable[[str, float], None]

ASPECTS = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)}
FITS = ("blur", "pad", "crop")
FPS = 30

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
)

# Caption size and bottom margin in libass script units (script height 288).
CAPTION_STYLE = {"9:16": (11, 70), "16:9": (14, 18), "1:1": (13, 30)}


class EngineError(RuntimeError):
    """A render step failed or the request was not valid."""


def parse_time(value) -> float:
    """Read seconds, "SS", "MM:SS" or "HH:MM:SS(.ms)" as seconds."""
    if isinstance(value, bool):
        raise EngineError(f"Bad time value: {value!r}")
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        parts = str(value).strip().split(":")
        if not 1 <= len(parts) <= 3 or not all(parts):
            raise EngineError(f"Bad time value: {value!r}")
        try:
            numbers = [float(part) for part in parts]
        except ValueError:
            raise EngineError(f"Bad time value: {value!r}") from None
        seconds = 0.0
        for number in numbers:
            seconds = seconds * 60 + number
    if seconds < 0:
        raise EngineError(f"Time cannot be negative: {value!r}")
    return seconds


def _run(cmd: Sequence[str], timeout: int = 1800) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            list(cmd), capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        raise EngineError(f"{cmd[0]} is not installed.") from None
    except subprocess.TimeoutExpired:
        raise EngineError(f"{cmd[0]} timed out.") from None
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-8:])
        raise EngineError(f"{cmd[0]} failed: {tail}")
    return result


def probe(path: Path) -> dict:
    """Read duration, size, frame rate and audio presence."""
    result = _run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        timeout=120,
    )
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise EngineError("The file has no video stream.")
    width, height = int(video["width"]), int(video["height"])
    rotation = 0
    for side in video.get("side_data_list", []) or []:
        if "rotation" in side:
            rotation = int(side["rotation"])
    rotation = rotation or int(video.get("tags", {}).get("rotate", 0) or 0)
    if abs(rotation) in (90, 270):
        width, height = height, width
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    fps = 0.0
    rate = video.get("r_frame_rate", "0/1")
    try:
        numerator, denominator = rate.split("/")
        fps = float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        pass
    return {
        "duration": duration,
        "width": width,
        "height": height,
        "fps": round(fps, 2),
        "has_audio": audio is not None,
    }


def _font() -> str:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise EngineError("No font found. Install fonts-dejavu-core.")


def _hex(color: str) -> str:
    text = (color or "").strip().lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", text):
        raise EngineError(f"Colors must look like #RRGGBB. Got: {color!r}")
    return "0x" + text.upper()


def _srt_time(seconds: float) -> str:
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _fades(duration: float, fade: float) -> tuple[str, str]:
    if fade <= 0 or duration < fade * 3:
        return "", ""
    out_at = duration - fade
    video = f",fade=t=in:st=0:d={fade},fade=t=out:st={out_at:.3f}:d={fade}"
    audio = f",afade=t=in:st=0:d={fade},afade=t=out:st={out_at:.3f}:d={fade}"
    return video, audio


def _video_chain(fit: str, w: int, h: int) -> str:
    if fit == "crop":
        return (
            f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},setsar=1"
        )
    if fit == "pad":
        return (
            f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
        )
    small_w, small_h = max(2, w // 8), max(2, h // 8)
    return (
        f"[0:v]split=2[a][b];"
        f"[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
        f"scale={small_w}:{small_h},boxblur=6:2,scale={w}:{h}[bg];"
        f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1"
    )


def _encode_args() -> list[str]:
    return [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
    ]


def _segment(
    source: Path, out: Path, start: float, end: float, info: dict,
    w: int, h: int, fit: str, fade: float,
) -> None:
    duration = end - start
    fade_video, fade_audio = _fades(duration, fade)
    graph = f"{_video_chain(fit, w, h)},fps={FPS},format=yuv420p{fade_video}[v]"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{start:.3f}", "-i", str(source)]
    if info["has_audio"]:
        graph += (
            ";[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
            f"{fade_audio}[a]"
        )
        audio_map = "[a]"
    else:
        cmd += ["-f", "lavfi", "-t", f"{duration:.3f}", "-i",
                "anullsrc=r=48000:cl=stereo"]
        audio_map = "1:a"
    cmd += ["-t", f"{duration:.3f}", "-filter_complex", graph,
            "-map", "[v]", "-map", audio_map, *_encode_args(), str(out)]
    _run(cmd)


def _card(path: Path, w: int, h: int, spec: dict, workdir: Path) -> float:
    """Render a title or end card. Return its length in seconds."""
    font = _font()
    seconds = float(spec.get("seconds") or 3.0)
    if not 0.5 <= seconds <= 15:
        raise EngineError("A card must last between 0.5 and 15 seconds.")
    background = _hex(spec.get("bg") or "#111111")
    foreground = _hex(spec.get("fg") or "#FFFFFF")
    title = (spec.get("title") or "").strip()
    subtitle = (spec.get("subtitle") or "").strip()
    if not title and not subtitle:
        raise EngineError("A card needs a title or a subtitle.")

    size = int(min(w, h) * 0.085)
    sub_size = max(24, int(size * 0.5))
    title_lines = textwrap.wrap(title, max(8, int(w * 0.84 / (size * 0.6)))) if title else []
    sub_lines = textwrap.wrap(subtitle, max(10, int(w * 0.84 / (sub_size * 0.6)))) if subtitle else []
    title_height, sub_height = int(size * 1.25), int(sub_size * 1.35)
    gap = int(size * 0.5) if title_lines and sub_lines else 0
    block = len(title_lines) * title_height + gap + len(sub_lines) * sub_height
    y = (h - block) // 2

    filters = []
    index = 0
    for lines, font_size, line_height in (
        (title_lines, size, title_height),
        (sub_lines, sub_size, sub_height),
    ):
        if lines is sub_lines:
            y += gap
        for line in lines:
            text_file = workdir / f"{path.stem}_{index}.txt"
            text_file.write_text(line, encoding="utf-8")
            index += 1
            filters.append(
                f"drawtext=fontfile='{font}':textfile='{text_file}':"
                f"fontsize={font_size}:fontcolor={foreground}:"
                f"x=(w-text_w)/2:y={y}"
            )
            y += line_height
    edge = min(0.3, seconds / 4)
    filters.append(f"fade=t=in:st=0:d={edge}")
    filters.append(f"fade=t=out:st={seconds - edge:.3f}:d={edge}")

    _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c={background}:s={w}x{h}:r={FPS}",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-t", f"{seconds:.3f}", "-vf", ",".join(filters),
        *_encode_args(), "-shortest", str(path),
    ])
    return seconds


def _write_srt(path: Path, captions: Sequence[dict], total: float) -> int:
    lines = []
    count = 0
    for item in sorted(captions, key=lambda c: parse_time(c["start"])):
        start = parse_time(item["start"])
        end = min(parse_time(item["end"]), total)
        text = " ".join(str(item["text"]).split())
        if not text:
            continue
        if end <= start:
            raise EngineError(f"Caption {text[:30]!r} ends before it starts.")
        count += 1
        lines += [str(count), f"{_srt_time(start)} --> {_srt_time(end)}", text, ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return count


def render(
    source: Path,
    out_path: Path,
    segments: Sequence[tuple[float, float]],
    *,
    workdir: Path,
    captions: Sequence[dict] = (),
    aspect: str = "16:9",
    fit: str = "blur",
    intro: Optional[dict] = None,
    outro: Optional[dict] = None,
    fade: float = 0.2,
    progress: Optional[Progress] = None,
) -> dict:
    """Cut segments from the source, join them, add cards and burn captions.

    Caption times are on the finished video's clock, so they include the intro
    card. Returns details of the finished file.
    """
    source, out_path, workdir = Path(source), Path(out_path), Path(workdir)
    if aspect not in ASPECTS:
        raise EngineError(f"aspect must be one of {', '.join(ASPECTS)}.")
    if fit not in FITS:
        raise EngineError(f"fit must be one of {', '.join(FITS)}.")
    if not segments:
        raise EngineError("Give at least one segment.")
    if not 0 <= fade <= 1:
        raise EngineError("fade must be between 0 and 1 second.")

    def report(stage: str, fraction: float) -> None:
        if progress:
            progress(stage, max(0.0, min(1.0, fraction)))

    info = probe(source)
    clean = []
    for number, (start, end) in enumerate(segments, 1):
        if start >= info["duration"]:
            raise EngineError(
                f"Segment {number} starts at {start:.1f}s. "
                f"The video is {info['duration']:.1f}s long."
            )
        end = min(end, info["duration"])
        if end - start < 0.5:
            raise EngineError(f"Segment {number} is shorter than 0.5 seconds.")
        clean.append((start, end))

    width, height = ASPECTS[aspect]
    workdir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    pieces: list[Path] = []
    total = 0.0
    steps = len(clean) + (1 if intro else 0) + (1 if outro else 0) + 1
    finished = 0

    if intro:
        report("intro card", 0)
        piece = workdir / "part_intro.mp4"
        total += _card(piece, width, height, intro, workdir)
        pieces.append(piece)
        finished += 1
    for number, (start, end) in enumerate(clean, 1):
        report(f"cutting segment {number} of {len(clean)}", finished / steps)
        piece = workdir / f"part_{number:03d}.mp4"
        _segment(source, piece, start, end, info, width, height, fit, fade)
        total += end - start
        pieces.append(piece)
        finished += 1
    if outro:
        report("end card", finished / steps)
        piece = workdir / "part_outro.mp4"
        total += _card(piece, width, height, outro, workdir)
        pieces.append(piece)
        finished += 1

    report("joining", finished / steps)
    listing = workdir / "parts.txt"
    listing.write_text("".join(f"file '{p}'\n" for p in pieces), encoding="utf-8")
    joined = workdir / "joined.mp4"
    _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat",
          "-safe", "0", "-i", str(listing), "-c", "copy", str(joined)])

    caption_count = 0
    if captions:
        report("burning captions", 0.9)
        srt = workdir / "captions.srt"
        caption_count = _write_srt(srt, captions, total)
    if caption_count:
        size, margin = CAPTION_STYLE[aspect]
        style = (
            f"Fontname=DejaVu Sans,Fontsize={size},Bold=1,"
            "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
            f"BorderStyle=1,Outline=3,Shadow=0,Alignment=2,MarginV={margin}"
        )
        _run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(joined),
            "-vf", f"subtitles={srt}:force_style='{style}'",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "copy",
            "-movflags", "+faststart", str(out_path),
        ])
    else:
        _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(joined),
              "-c", "copy", "-movflags", "+faststart", str(out_path)])

    report("done", 1.0)
    result = probe(out_path)
    result.update(
        {
            "path": str(out_path),
            "size_bytes": out_path.stat().st_size,
            "captions": caption_count,
            "segments": len(clean),
        }
    )
    return result


def extract_frame(source: Path, seconds: float, out: Path, width: int = 640) -> Path:
    """Save one still frame as a JPEG."""
    _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{seconds:.3f}", "-i", str(source), "-frames:v", "1",
        "-vf", f"scale={int(width)}:-2", "-q:v", "4", str(out),
    ], timeout=300)
    if not Path(out).exists():
        raise EngineError(f"No frame at {seconds:.1f}s. The video may be shorter.")
    return Path(out)
