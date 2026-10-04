import json

import httpx
import pytest

from core.costs import CostLedger
from core.magichour import MagicHour, MagicHourError, MagicHourJobFailed, estimate_talking_photo_credits


class FakeMagicHour:
    """Records requests and replays the documented response shapes."""

    def __init__(self, statuses, final_credits=480, error=None):
        self.requests = []
        self.statuses = list(statuses)
        self.final_credits = final_credits
        self.error = error
        self.uploads = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if request.method == "POST" and url.endswith("/v1/files/upload-urls"):
            item = json.loads(request.content)["items"][0]
            self.uploads += 1
            name = f"api-assets/id/{self.uploads}.{item['extension']}"
            return httpx.Response(200, json={"items": [{
                "upload_url": f"https://videos.magichour.ai/{name}?auth=1",
                "expires_at": "2026-10-04T00:00:00Z",
                "file_path": name,
            }]})
        if request.method == "PUT" and url.startswith("https://videos.magichour.ai/"):
            return httpx.Response(200)
        if request.method == "POST" and url.endswith("/v1/ai-talking-photo"):
            return httpx.Response(200, json={"id": "job123", "credits_charged": 500})
        if request.method == "GET" and "/v1/video-projects/job123" in url:
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            body = {
                "id": "job123", "status": status, "credits_charged": self.final_credits,
                "fps": 25, "width": 720, "height": 576, "error": None, "downloads": [],
            }
            if status == "complete":
                body["downloads"] = [{"url": "https://videos.magichour.ai/out/job123.mp4", "expires_at": "x"}]
            if status == "error":
                body["error"] = self.error
            return httpx.Response(200, json=body)
        if request.method == "GET" and url == "https://videos.magichour.ai/out/job123.mp4":
            return httpx.Response(200, content=b"fake-mp4")
        if request.method == "GET" and url.endswith("/v1/account"):
            return httpx.Response(200, json={"credits": 1000, "tier": "creator"})
        return httpx.Response(404, json={"code": "not_found", "message": url})


@pytest.fixture
def files(tmp_path):
    photo = tmp_path / "alice.jpg"
    photo.write_bytes(b"jpg")
    voice = tmp_path / "voice.wav"
    voice.write_bytes(b"wav")
    return photo, voice


def client(fake, ledger=None):
    return MagicHour("sk-test", ledger=ledger, transport=httpx.MockTransport(fake), poll_interval=0)


def test_talking_photo_end_to_end_logs_final_credits(tmp_path, files):
    photo, voice = files
    fake = FakeMagicHour(["queued", "rendering", "complete"], final_credits=480)
    ledger = CostLedger(tmp_path / "ledger.jsonl")
    with client(fake, ledger) as mh:
        result = mh.run_talking_photo(
            image=photo, audio=voice, start_seconds=0, end_seconds=10, label="clip 1 / Alice",
            max_resolution=512, dest=tmp_path / "out.mp4",
        )

    assert result.output_path.read_bytes() == b"fake-mp4"
    create = next(r for r in fake.requests if str(r.url).endswith("/v1/ai-talking-photo"))
    body = json.loads(create.content)
    assert body["assets"] == {"image_file_path": "api-assets/id/1.jpg", "audio_file_path": "api-assets/id/2.wav"}
    assert body["start_seconds"] == 0 and body["end_seconds"] == 10
    assert body["max_resolution"] == 512
    assert body["style"] == {"generation_mode": "realistic"}
    assert create.headers["authorization"] == "Bearer sk-test"

    # Pre-signed storage URLs never receive the API key.
    for put in (r for r in fake.requests if r.method == "PUT"):
        assert "authorization" not in put.headers

    [entry] = ledger.entries
    assert (entry.status, entry.credits, entry.meta["credits_estimate"]) == ("complete", 480, 500)
    assert ledger.totals()["magichour_credits"] == 480
    assert (tmp_path / "ledger.jsonl").read_text().count("job123") >= 2


def test_uploads_are_deduplicated(files):
    photo, voice = files
    fake = FakeMagicHour(["complete"])
    with client(fake) as mh:
        assert mh.upload(photo) == mh.upload(photo)
    assert fake.uploads == 1


def test_failed_job_is_logged_with_refund(tmp_path, files):
    photo, voice = files
    fake = FakeMagicHour(
        ["rendering", "error"], final_credits=0,
        error={"code": "no_source_face", "message": "Please use an image with a detectable face"},
    )
    ledger = CostLedger()
    with client(fake, ledger) as mh, pytest.raises(MagicHourJobFailed, match="detectable face"):
        mh.run_talking_photo(image=photo, audio=voice, start_seconds=0, end_seconds=5, dest=tmp_path / "x.mp4")
    [entry] = ledger.entries
    assert entry.status == "error" and entry.credits == 0 and "detectable face" in entry.error
    assert len(ledger.failures()) == 1


def test_rejects_clips_over_the_mode_limit(files):
    photo, voice = files
    with client(FakeMagicHour(["complete"])) as mh, pytest.raises(MagicHourError, match="limited to 45s"):
        mh.talking_photo(image=photo, audio=voice, start_seconds=0, end_seconds=50, generation_mode="prompted")


def test_api_errors_surface_code_and_message(files):
    def handler(request):
        return httpx.Response(402, json={"code": "insufficient_credits", "message": "Purchase credits"})

    photo, voice = files
    with MagicHour("sk", transport=httpx.MockTransport(handler)) as mh, pytest.raises(MagicHourError) as err:
        mh.account()
    assert err.value.status_code == 402 and err.value.code == "insufficient_credits"


def test_credit_estimate():
    assert estimate_talking_photo_credits(30.2) == 31 * 48
    assert estimate_talking_photo_credits(30.0, "prompted") == 600
