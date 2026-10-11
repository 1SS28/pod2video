"""Full pipeline runs against a mocked Magic Hour API (no network, no credits)."""

import io
import json
import math
from pathlib import Path

import httpx
import pytest
from rich.console import Console

from core import compose
from core.magichour import MagicHour
from pod2video import pipeline as pl
from pod2video.config import Settings
from pod2video.models import Host
from pod2video.transcribe import file_sha256
from tests.conftest import make_transcript


def _ffmpeg_with_libass() -> bool:
    try:
        return compose.supports_captions()
    except compose.FFmpegError:
        return False


pytestmark = pytest.mark.skipif(not _ffmpeg_with_libass(), reason="needs ffmpeg with libass")

CREATOR_MONTHLY = {
    "credits": 100_000, "tier": "creator",
    "subscription": {"name": "Creator", "status": "active", "price": {"amount": 1900, "currency": "usd"},
                     "discount": None, "billing_interval": "month", "current_period_end": None, "cancel_at_period_end": False},
}


@pytest.fixture
def episode(tmp_path):
    from PIL import Image

    audio = tmp_path / "episode.wav"
    compose.run_ffmpeg(["-f", "lavfi", "-i", "sine=frequency=300:duration=40", "-ac", "2", audio])
    script = [("A", "Welcome back. " * 6), ("B", "Here is the thing. " * 8), ("A", "Totally agree with that. " * 5),
              ("B", "And one more point. " * 6)]
    transcript = make_transcript(script).model_copy(update={"audio_sha256": file_sha256(audio)})
    cache = tmp_path / "cache"
    (cache / "transcripts").mkdir(parents=True)
    (cache / "transcripts" / f"{transcript.audio_sha256[:24]}.assemblyai.json").write_text(transcript.model_dump_json())

    photos = []
    for name, colour in (("alice", (200, 80, 120)), ("bob", (60, 120, 220))):
        path = tmp_path / f"{name}.jpg"
        Image.new("RGB", (800, 800), colour).save(path)
        photos.append(path)
    rendered = compose.still_video(photos[0], 30.0, tmp_path / "mh_output.mp4").read_bytes()
    png = io.BytesIO()
    Image.new("RGB", (1024, 1024), (240, 200, 60)).save(png, format="PNG")
    return audio, cache, photos, rendered, png.getvalue()


class FakeAPI:
    def __init__(self, rendered: bytes, png: bytes, account=CREATOR_MONTHLY, faces=1):
        self.rendered, self.png, self.account, self.faces = rendered, png, account, faces
        self.log: list[tuple[str, str, bytes]] = []

    def count(self, suffix: str) -> int:
        return sum(1 for m, u, _ in self.log if m == "POST" and u.endswith(suffix))

    def bodies(self, suffix: str) -> list[dict]:
        return [json.loads(b) for m, u, b in self.log if m == "POST" and u.endswith(suffix)]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.log.append((request.method, url, request.content if request.method == "POST" else b""))
        if url.endswith("/v1/account"):
            return httpx.Response(200, json=self.account)
        if url.endswith("/v1/files/upload-urls"):
            ext = json.loads(request.content)["items"][0]["extension"]
            n = len(self.log)
            return httpx.Response(200, json={"items": [{"upload_url": f"https://videos.magichour.ai/u{n}.{ext}?sig=1", "expires_at": "x", "file_path": f"api-assets/u{n}.{ext}"}]})
        if request.method == "PUT":
            return httpx.Response(200)
        if url.endswith("/v1/ai-talking-photo"):
            return httpx.Response(200, json={"id": f"job{self.count('/v1/ai-talking-photo')}", "credits_charged": 999})
        if "/v1/video-projects/" in url:
            job = url.rsplit("/", 1)[-1]
            return httpx.Response(200, json={
                "id": job, "status": "complete", "credits_charged": 400, "fps": 25, "width": 720, "height": 720,
                "error": None, "downloads": [{"url": f"https://videos.magichour.ai/out/{job}.mp4", "expires_at": "x"}],
            })
        if url.endswith(("/v1/ai-image-editor", "/v1/ai-image-generator")):
            return httpx.Response(200, json={"id": "img1", "credits_charged": 50})
        if "/v1/image-projects/" in url:
            return httpx.Response(200, json={"id": "img1", "status": "complete", "credits_charged": 50, "error": None,
                                             "downloads": [{"url": "https://images.magichour.ai/out/img1.png", "expires_at": "x"}]})
        if url.endswith("/v1/face-detection"):
            return httpx.Response(200, json={"id": "fd1", "credits_charged": 0})
        if "/v1/face-detection/" in url:
            return httpx.Response(200, json={"id": "fd1", "status": "complete", "credits_charged": 0,
                                             "faces": [{"path": "f", "url": "u"}] * self.faces})
        if url.startswith("https://videos.magichour.ai/out/"):
            return httpx.Response(200, content=self.rendered)
        if url.startswith("https://images.magichour.ai/out/"):
            return httpx.Response(200, content=self.png)
        return httpx.Response(404, json={"code": "not_found", "message": url})


def use_fake(monkeypatch, api: FakeAPI) -> None:
    transport = httpx.MockTransport(api)
    monkeypatch.setattr(pl, "MagicHour", lambda key, **kw: MagicHour(key, transport=transport, poll_interval=0, **kw))


def options(tmp_path, photos, **overrides):
    base = dict(
        hosts=[Host(name="Alice", photo=photos[0]), Host(name="Bob", photo=photos[1])],
        audio=tmp_path / "episode.wav", out=tmp_path / "out", segments=[(1.0, 30.0)],
        speaker_map="A=Alice,B=Bob", yes=True, interactive=False, podcast_name="Test Pod",
    )
    return pl.RunOptions(**{**base, **overrides})


def test_speaker_cut_animates_only_the_speaker_in_one_job_per_host(tmp_path, episode, monkeypatch):
    audio, cache, photos, rendered, png = episode
    api = FakeAPI(rendered, png)
    use_fake(monkeypatch, api)
    settings = Settings(magic_hour_api_key="sk-test", pod2video_cache_dir=cache, _env_file=None)
    report = pl.Pipeline(options(tmp_path, photos), settings, Console(quiet=True)).run()

    [clip] = report["clips"]
    assert clip["status"] == "complete", clip["error"]
    assert report["layout"] == "speaker-cut" and report["generation_mode"] == "prompted"

    # One Talking Photo job per host; together they cover the clip about once, not once per host.
    bodies = api.bodies("/v1/ai-talking-photo")
    assert len(bodies) == 2
    assert all(b["style"]["generation_mode"] == "prompted" for b in bodies)
    billed = sum(b["end_seconds"] - b["start_seconds"] for b in bodies)
    assert clip["duration"] <= billed < clip["duration"] * 1.15
    spans = sorted(s for r in clip["renders"] for s in r["spans"])
    assert spans[0][0] == 0 and spans[-1][1] == pytest.approx(clip["duration"], abs=0.01)
    assert all(a[1] == pytest.approx(b[0], abs=0.01) for a, b in zip(spans, spans[1:]))  # shots tile the clip

    # Pricing comes from the account: Creator monthly, $19 for 12,000 credits.
    assert report["plan"]["key"] == "creator-monthly"
    assert report["totals"]["magichour_credits"] == 800
    assert report["totals"]["magichour_usd"] == pytest.approx(800 * 19 / 12000, abs=1e-4)
    assert report["cost_by_plan"]["creator-annual"] == pytest.approx(0.8)

    out = Path(clip["file"])
    assert compose.probe_duration(out) == pytest.approx(clip["duration"], abs=0.1)
    assert api.count("/v1/face-detection") == 2  # free preflight, one per host image

    # A rerun reuses the renders and the face checks: no new jobs, no new credits.
    api.log.clear()
    out.unlink()
    report2 = pl.Pipeline(options(tmp_path, photos), settings, Console(quiet=True)).run()
    assert report2["clips"][0]["status"] == "complete"
    assert report2["totals"]["magichour_credits"] == 0
    assert api.count("/v1/ai-talking-photo") == 0 and api.count("/v1/face-detection") == 0


def test_split_animates_every_host_for_the_whole_clip(tmp_path, episode, monkeypatch):
    audio, cache, photos, rendered, png = episode
    api = FakeAPI(rendered, png)
    use_fake(monkeypatch, api)
    settings = Settings(magic_hour_api_key="sk-test", pod2video_cache_dir=cache, _env_file=None)
    report = pl.Pipeline(options(tmp_path, photos, layout="split"), settings, Console(quiet=True)).run()
    [clip] = report["clips"]
    assert clip["status"] == "complete", clip["error"]
    bodies = api.bodies("/v1/ai-talking-photo")
    assert len(bodies) == 2
    assert all(b["end_seconds"] == pytest.approx(clip["duration"], abs=0.01) for b in bodies)


def test_pick_renders_only_chosen_candidates(tmp_path, episode, monkeypatch):
    audio, cache, photos, rendered, png = episode
    api = FakeAPI(rendered, png)
    use_fake(monkeypatch, api)
    settings = Settings(magic_hour_api_key="sk-test", pod2video_cache_dir=cache, _env_file=None)
    opts = options(tmp_path, photos, segments=[(1.0, 12.0), (14.0, 26.0)], pick=[2])
    report = pl.Pipeline(opts, settings, Console(quiet=True)).run()
    first, second = report["clips"]
    assert (first["status"], second["status"]) == ("not picked", "complete")
    assert first["file"] is None and Path(second["file"]).exists()


def test_cartoon_avatar_is_made_once_and_reused(tmp_path, episode, monkeypatch):
    audio, cache, photos, rendered, png = episode
    api = FakeAPI(rendered, png)
    use_fake(monkeypatch, api)
    settings = Settings(magic_hour_api_key="sk-test", pod2video_cache_dir=cache, _env_file=None)
    hosts = [Host(name="Alice", photo=photos[0]), Host(name="Bob", description="tall, beard, green beanie")]
    report = pl.Pipeline(options(tmp_path, photos, hosts=hosts, avatar="cartoon"), settings, Console(quiet=True)).run()
    assert report["clips"][0]["status"] == "complete", report["clips"][0]["error"]
    assert api.count("/v1/ai-image-editor") == 1  # Alice's photo -> cartoon
    [generated] = api.bodies("/v1/ai-image-generator")  # Bob from his description
    assert "green beanie" in generated["style"]["prompt"]
    avatar_credits = sum(e["credits"] for e in report["entries"] if e["kind"].startswith("image_"))
    assert avatar_credits == 100

    api.log.clear()
    pl.Pipeline(options(tmp_path, photos, hosts=hosts, avatar="cartoon"), settings, Console(quiet=True)).run()
    assert api.count("/v1/ai-image-editor") == 0 and api.count("/v1/ai-image-generator") == 0


def test_no_face_stops_before_spending(tmp_path, episode, monkeypatch):
    audio, cache, photos, rendered, png = episode
    api = FakeAPI(rendered, png, faces=0)
    use_fake(monkeypatch, api)
    settings = Settings(magic_hour_api_key="sk-test", pod2video_cache_dir=cache, _env_file=None)
    with pytest.raises(pl.PipelineError, match="cannot find a face"):
        pl.Pipeline(options(tmp_path, photos), settings, Console(quiet=True)).run()
    assert api.count("/v1/ai-talking-photo") == 0


def test_dry_run_estimates_and_compares_plans_without_rendering(tmp_path, episode, monkeypatch):
    audio, cache, photos, rendered, png = episode
    api = FakeAPI(rendered, png)
    use_fake(monkeypatch, api)
    settings = Settings(magic_hour_api_key="sk-test", pod2video_cache_dir=cache, _env_file=None)
    report = pl.Pipeline(options(tmp_path, photos, dry_run=True), settings, Console(quiet=True)).run()
    assert not any(m == "POST" for m, _, _ in api.log)  # only the read-only account lookup
    [clip] = report["clips"]
    expected = sum(math.ceil(r["seconds"]) * 24 for r in clip["renders"] if r["paid"])
    assert report["estimate"]["credits"] == clip["credits_estimate"] == expected
    assert clip["status"] == "planned"
    assert report["cost_by_plan"]["pack"] == pytest.approx(expected / 400, abs=1e-4)


def test_chunk_spans_respects_the_job_limit():
    spans = [(0.0, 20.0), (25.0, 40.0), (50.0, 60.0)]
    assert pl.chunk_spans(spans, limit=45, pad=0.25) == [[(0.0, 20.0), (25.0, 40.0)], [(50.0, 60.0)]]
    assert pl.chunk_spans(spans, limit=300, pad=0.25) == [spans]
