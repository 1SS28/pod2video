import pytest

from core import compose
from pod2video.models import Word


def _has_ffmpeg() -> bool:
    try:
        compose.ffmpeg_bin()
        return True
    except compose.FFmpegError:
        return False


def words(*items):
    return [Word(text=t, start=a, end=b, speaker=s) for t, a, b, s in items]


def test_caption_chunks_break_on_punctuation_pause_and_speaker():
    ws = words(
        ("Hello", 0.0, 0.3, "A"), ("there.", 0.4, 0.7, "A"),
        ("How", 0.8, 1.0, "A"), ("are", 1.1, 1.2, "A"),
        ("Fine", 2.5, 2.8, "A"),  # after a long pause
        ("Me", 2.9, 3.0, "B"),  # speaker change
    )
    chunks = compose.caption_chunks(ws, max_words=3, max_chars=20)
    assert [[w.text for w in c] for c in chunks] == [["Hello", "there."], ["How", "are"], ["Fine"], ["Me"]]


def test_build_ass_has_header_tags_and_word_highlights():
    panes = compose.layout_panes("split", 2)
    overlay = compose.Overlay(
        duration=3.0, layout="split", panes=panes, host_names=["Alice", "Bob"],
        words=words(("Hi", 0.0, 0.4, "A"), ("{you}", 0.5, 0.9, "A")),
        active=[[(0.0, 1.0)], []], podcast_name="Pod", episode_title="Ep 1",
    )
    ass = compose.build_ass(overlay, compose.CAPTION_STYLES["bold"])
    assert "PlayResY: 1920" in ass
    assert ",Header,,0,0,0,,{\\pos(48,40)}Pod" in ass
    assert "ALICE" in ass and "BOB" in ass
    captions = [line for line in ass.splitlines() if ",Caption," in line]
    assert len(captions) == 2  # one event per highlighted word
    assert "(YOU)" in captions[1]  # braces are escaped so they cannot inject ASS tags
    assert all(f"\\pos(540,{panes[1].y})" in c for c in captions)


def test_layout_panes():
    top, bottom = compose.layout_panes("split", 2)
    assert top.y == compose.HEADER and bottom.y + bottom.h == compose.HEIGHT
    [square] = compose.layout_panes("single", 1)
    assert (square.w, square.h) == (960, 960)  # 720p Talking Photo output upscales 1.33x, not 2.4x


def test_crop_photo_matches_pane_aspect(tmp_path):
    from PIL import Image

    src = tmp_path / "wide.jpg"
    Image.new("RGB", (1600, 900), (200, 100, 50)).save(src)
    out = compose.crop_photo(src, tmp_path / "out.jpg", aspect=1080 / 1720)
    with Image.open(out) as img:
        assert abs(img.width / img.height - 1080 / 1720) < 0.01


def _libass() -> bool:
    return _has_ffmpeg() and compose.supports_captions()


def _pixel(video, t, x, y):
    """RGB of one pixel of the frame at time t."""
    import subprocess

    raw = subprocess.run(
        [compose.ffmpeg_bin(), "-v", "error", "-ss", str(t), "-i", str(video), "-frames:v", "1",
         "-vf", f"format=rgb24,crop=1:1:{x}:{y}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout
    return tuple(raw[:3])


@pytest.mark.skipif(not _libass(), reason="needs ffmpeg with libass")
def test_render_clip_places_each_shot_from_the_right_render(tmp_path):
    """Speaker-cut: two condensed renders are cut apart and laid on the timeline in order."""
    from PIL import Image

    red, blue = tmp_path / "red.png", tmp_path / "blue.png"
    Image.new("RGB", (512, 512), (230, 20, 20)).save(red)
    Image.new("RGB", (512, 512), (20, 20, 230)).save(blue)
    alice = compose.still_video(red, 2.5, tmp_path / "alice.mp4")  # her two shots back to back + pad
    bob = compose.still_video(blue, 1.0, tmp_path / "bob.mp4")
    mix = tmp_path / "mix.wav"
    compose.run_ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-ac", "2", mix])
    [pane] = compose.layout_panes("speaker-cut", 1)
    placements = [
        compose.Placement(render=0, pane=pane, start=0.0, end=1.0, offset=0.0),
        compose.Placement(render=1, pane=pane, start=1.0, end=2.0, offset=0.0),
        compose.Placement(render=0, pane=pane, start=2.0, end=3.0, offset=1.25),
    ]
    overlay = compose.Overlay(duration=3.0, layout="speaker-cut", panes=[pane, pane], host_names=["A", "B"])
    out = compose.render_clip(
        renders=[alice, bob], placements=placements, mix_audio=mix, overlay=overlay,
        style=compose.CAPTION_STYLES["bold"], dest=tmp_path / "final.mp4",
    )
    assert abs(compose.probe_duration(out) - 3.0) < 0.1
    centre = (pane.x + pane.w // 2, pane.y + pane.h // 2)
    for t, want in ((0.5, "red"), (1.5, "blue"), (2.5, "red")):
        r, g, b = _pixel(out, t, *centre)
        assert (r > 150 and b < 90) if want == "red" else (b > 150 and r < 90), (t, (r, g, b))


@pytest.mark.skipif(not _libass(), reason="needs ffmpeg with libass")
def test_render_clip_produces_vertical_video(tmp_path):
    from PIL import Image

    photo = tmp_path / "p.png"
    Image.new("RGB", (512, 512), (50, 120, 200)).save(photo)
    video = compose.still_video(photo, 2.0, tmp_path / "host.mp4")
    mix = tmp_path / "mix.wav"
    compose.run_ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-ac", "2", mix])
    panes = compose.layout_panes("single", 1)
    overlay = compose.Overlay(
        duration=2.0, layout="single", panes=panes, host_names=["Alice"], hook="A hook",
        words=words(("Hello", 0.1, 0.5, "A"), ("world", 0.6, 1.0, "A")), podcast_name="Pod", episode_title="Ep",
    )
    out = compose.render_clip(
        renders=[video], placements=[compose.Placement(0, panes[0], 0.0, 2.0)], mix_audio=mix, overlay=overlay,
        style=compose.CAPTION_STYLES["bold"], dest=tmp_path / "final.mp4",
    )
    assert abs(compose.probe_duration(out) - 2.0) < 0.1
