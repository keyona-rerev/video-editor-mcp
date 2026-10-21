import pytest

from video_editor_mcp import engine


def test_parse_time_formats():
    assert engine.parse_time(12) == 12.0
    assert engine.parse_time("83.5") == 83.5
    assert engine.parse_time("1:05") == 65.0
    assert engine.parse_time("01:01:05.5") == 3665.5


@pytest.mark.parametrize("bad", ["", "a:b", "1:2:3:4", -1, True, "1::2"])
def test_parse_time_rejects_bad_values(bad):
    with pytest.raises(engine.EngineError):
        engine.parse_time(bad)


def test_probe(sample_video):
    info = engine.probe(sample_video)
    assert round(info["duration"]) == 20
    assert (info["width"], info["height"]) == (1280, 720)
    assert info["has_audio"] is True


@pytest.mark.parametrize("aspect,size", [("9:16", (1080, 1920)), ("16:9", (1920, 1080)), ("1:1", (1080, 1080))])
def test_render_with_cards_and_captions(sample_video, tmp_path, aspect, size):
    out = tmp_path / "out" / "edit.mp4"
    steps = []
    result = engine.render(
        sample_video,
        out,
        [(2, 6), (10, 14)],
        workdir=tmp_path / "work",
        captions=[
            {"text": "Club Alpha starts now", "start": 3.5, "end": 6},
            {"text": "Join the next class", "start": "0:09", "end": "0:11"},
        ],
        aspect=aspect,
        fit="blur",
        intro={"title": "Club Alpha", "subtitle": "Learn to build with AI", "seconds": 2.5},
        outro={"title": "Join Club Alpha", "subtitle": "rerev.io", "seconds": 3, "bg": "#0B3D2E"},
        progress=lambda stage, fraction: steps.append((stage, fraction)),
    )
    assert out.exists()
    assert (result["width"], result["height"]) == size
    assert result["duration"] == pytest.approx(2.5 + 4 + 4 + 3, abs=0.6)
    assert result["has_audio"] is True
    assert result["captions"] == 2
    assert steps[-1] == ("done", 1.0)


@pytest.mark.parametrize("fit", ["pad", "crop"])
def test_render_fit_modes(sample_video, tmp_path, fit):
    result = engine.render(
        sample_video, tmp_path / "e.mp4", [(0, 3)],
        workdir=tmp_path / "w", aspect="9:16", fit=fit,
    )
    assert (result["width"], result["height"]) == (1080, 1920)


def test_render_without_audio_source(tmp_path):
    import subprocess

    silent = tmp_path / "silent.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc2=size=640x360:rate=30:duration=6", "-pix_fmt", "yuv420p", str(silent)],
        check=True,
    )
    result = engine.render(silent, tmp_path / "e.mp4", [(0, 4)], workdir=tmp_path / "w")
    assert result["has_audio"] is True
    assert result["duration"] == pytest.approx(4, abs=0.4)


def test_segment_past_end_is_rejected(sample_video, tmp_path):
    with pytest.raises(engine.EngineError, match="starts at"):
        engine.render(sample_video, tmp_path / "e.mp4", [(25, 30)], workdir=tmp_path / "w")


def test_segment_end_is_clamped(sample_video, tmp_path):
    result = engine.render(sample_video, tmp_path / "e.mp4", [(15, 99)], workdir=tmp_path / "w")
    assert result["duration"] == pytest.approx(5, abs=0.4)


def test_bad_inputs(sample_video, tmp_path):
    with pytest.raises(engine.EngineError):
        engine.render(sample_video, tmp_path / "e.mp4", [], workdir=tmp_path / "w")
    with pytest.raises(engine.EngineError):
        engine.render(sample_video, tmp_path / "e.mp4", [(0, 3)], workdir=tmp_path / "w", aspect="4:3")
    with pytest.raises(engine.EngineError):
        engine.render(sample_video, tmp_path / "e.mp4", [(0, 3)], workdir=tmp_path / "w",
                      intro={"title": "x", "bg": "red"})
    with pytest.raises(engine.EngineError, match="before it starts"):
        engine.render(sample_video, tmp_path / "e.mp4", [(0, 3)], workdir=tmp_path / "w",
                      captions=[{"text": "x", "start": 2, "end": 1}])


def test_extract_frame(sample_video, tmp_path):
    frame = engine.extract_frame(sample_video, 5, tmp_path / "f.jpg", 320)
    assert frame.exists() and frame.stat().st_size > 1000
