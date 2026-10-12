"""The GitHub Actions flow against fake GitHub and Magic Hour APIs (no network, no credits)."""

import copy
import json
import re

import httpx
import pytest
from rich.console import Console

from pod2video import actions, showconfig
from pod2video import feed as feed_ops
from pod2video import moments as moment_ops
from pod2video.feed import Episode
from pod2video.github import GitHub
from pod2video.models import Moment
from tests.test_pipeline import FakeAPI, _ffmpeg_with_libass, episode, use_fake  # noqa: F401  (fixture)

REPO = "me/show"
FEED = "https://example.com/feed.xml"


# -- pure helpers -------------------------------------------------------------------------


def test_parse_render_command():
    assert actions.parse_render_command("/render 1 3,4") == ([1, 3, 4], {})
    assert actions.parse_render_command('thanks!\n/render 2 A=Bob B="Alice Smith"') == ([2], {"A": "Bob", "B": "Alice Smith"})
    with pytest.raises(actions.ActionError, match="which clips"):
        actions.parse_render_command("/render")
    with pytest.raises(actions.ActionError, match="not a clip number"):
        actions.parse_render_command("/render all")


def test_meta_round_trip_survives_markdown():
    meta = {"episode_title": "Ep 1 --> the <end>", "release_id": 7}
    body = "text\n" + actions.encode_meta(meta) + "\nmore"
    assert actions.decode_meta(body) == meta
    with pytest.raises(actions.ActionError):
        actions.decode_meta("no metadata here")


def test_show_config_resolves_paths_and_rejects_typos(tmp_path):
    (tmp_path / "pod2video.yml").write_text(
        f"feed: {FEED}\nhosts:\n  - name: Alice\n    photo: hosts/alice.jpg\n  - name: Bob\n    description: beanie\n"
        "speaker_map: {A: Alice, B: Bob}\nllm_model: claude-sonnet-5-5\n"
    )
    cfg = showconfig.load(tmp_path / "pod2video.yml")
    alice, bob = cfg.host_models()
    assert alice.photo == tmp_path / "hosts/alice.jpg" and bob.description == "beanie"
    assert cfg.speaker_map_arg() == "A=Alice,B=Bob"
    assert cfg.settings().llm_model == "claude-sonnet-5-5"
    (tmp_path / "bad.yml").write_text(f"feed: {FEED}\nhosts: [{{name: A, photo: a.jpg}}]\nmax_creditz: 1\n")
    with pytest.raises(ValueError, match="max_creditz"):
        showconfig.load(tmp_path / "bad.yml")


def test_check_setup_explains_what_is_missing(tmp_path):
    from pod2video.config import Settings

    (tmp_path / "pod2video.yml").write_text(f"feed: {actions.EXAMPLE_FEED}\nhosts:\n  - name: Alice\n    photo: hosts/alice.jpg\n")
    cfg = showconfig.load(tmp_path / "pod2video.yml")
    with pytest.raises(actions.ActionError) as err:
        actions.check_setup(cfg, Settings(_env_file=None, magic_hour_api_key="x", transcription_api_key="", llm_api_key="y"))
    message = str(err.value)
    assert "example feed" in message and "TRANSCRIPTION_API_KEY" in message and "alice.jpg" in message
    assert "MAGIC_HOUR_API_KEY" not in message

    (tmp_path / "hosts").mkdir()
    (tmp_path / "hosts" / "alice.jpg").write_bytes(b"x")
    (tmp_path / "pod2video.yml").write_text(f"feed: {FEED}\nhosts:\n  - name: Alice\n    photo: hosts/alice.jpg\n")
    actions.check_setup(showconfig.load(tmp_path / "pod2video.yml"), Settings(_env_file=None, magic_hour_api_key="x", transcription_api_key="y", llm_api_key="z"))


# -- fake GitHub ------------------------------------------------------------------------


class FakeGitHub:
    def __init__(self):
        self.issues: dict[int, dict] = {}
        self.comments: list[tuple[int, str]] = []
        self.reactions: list[tuple[int, str]] = []
        self.releases: dict[int, dict] = {}
        self.blobs: dict[int, bytes] = {}
        self.auth_on_blob: list[bool] = []
        self._next = 100

    def _id(self) -> int:
        self._next += 1
        return self._next

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url, method = request.url, request.method
        if url.host == "objects.example.com":  # where asset downloads redirect to
            self.auth_on_blob.append("authorization" in request.headers)
            return httpx.Response(200, content=self.blobs[int(url.path.rsplit("/", 1)[1])])
        if url.host == "uploads.github.com":
            release = self.releases[int(url.path.split("/")[5])]
            asset_id = self._id()
            self.blobs[asset_id] = request.read()
            name = url.params["name"]
            asset = {"id": asset_id, "name": name, "browser_download_url": f"https://github.com/{REPO}/releases/download/untagged/{name}"}
            release["assets"].append(asset)
            return httpx.Response(201, json=asset)
        rest = url.path.removeprefix(f"/repos/{REPO}")
        raw = request.read()
        payload = json.loads(raw) if raw else {}
        if rest == "/labels":
            return httpx.Response(201, json={})
        if rest == "/issues" and method == "GET":
            return httpx.Response(200, json=list(self.issues.values()))
        if rest == "/issues" and method == "POST":
            number = len(self.issues) + 1
            self.issues[number] = {**payload, "number": number, "html_url": f"https://github.com/{REPO}/issues/{number}"}
            return httpx.Response(201, json=self.issues[number])
        if m := re.fullmatch(r"/issues/(\d+)", rest):
            self.issues[int(m[1])].update(payload)
            return httpx.Response(200, json=self.issues[int(m[1])])
        if m := re.fullmatch(r"/issues/(\d+)/comments", rest):
            self.comments.append((int(m[1]), payload["body"]))
            return httpx.Response(201, json={"id": self._id()})
        if m := re.fullmatch(r"/issues/comments/(\d+)/reactions", rest):
            self.reactions.append((int(m[1]), payload["content"]))
            return httpx.Response(201, json={})
        if rest == "/releases" and method == "POST":
            rid = self._id()
            self.releases[rid] = {
                "id": rid, "name": payload["name"], "draft": payload["draft"], "assets": [],
                "html_url": f"https://github.com/{REPO}/releases/{rid}",
                "upload_url": f"https://uploads.github.com/repos/{REPO}/releases/{rid}/assets{{?name,label}}",
            }
            return httpx.Response(201, json=self.releases[rid])
        if m := re.fullmatch(r"/releases/(\d+)", rest):
            return httpx.Response(200, json=copy.deepcopy(self.releases[int(m[1])]))
        if m := re.fullmatch(r"/releases/assets/(\d+)", rest):
            asset_id = int(m[1])
            if method == "DELETE":
                for release in self.releases.values():
                    release["assets"] = [a for a in release["assets"] if a["id"] != asset_id]
                return httpx.Response(204)
            return httpx.Response(302, headers={"Location": f"https://objects.example.com/blob/{asset_id}"})
        return httpx.Response(404, json={"message": f"{method} {url}"})

    def assets(self) -> set[str]:
        return {a["name"] for r in self.releases.values() for a in r["assets"]}


# -- end to end -------------------------------------------------------------------------


@pytest.fixture
def show(tmp_path, episode, monkeypatch):  # noqa: F811
    audio, cache, photos, rendered, png = episode
    monkeypatch.chdir(tmp_path)
    for key, value in {
        "POD2VIDEO_CACHE_DIR": str(cache), "MAGIC_HOUR_API_KEY": "sk-test", "TRANSCRIPTION_API_KEY": "stt-test",
        "LLM_API_KEY": "llm-test", "LLM_MODEL": "claude-opus-5-5",
    }.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "pod2video.yml").write_text(
        f"feed: {FEED}\nhosts:\n  - name: Alice\n    photo: {photos[0].name}\n  - name: Bob\n    photo: {photos[1].name}\n"
        "clips: 1\ncandidates: 2\nspeaker_map: {A: Alice, B: Bob}\nmax_credits_per_run: 5000\n"
    )
    ep = Episode(podcast_title="Test Pod", title="Ep 7: Shipping", guid="ep7", audio_url="https://cdn.example.com/ep7.wav",
                 published=None, podcast_image=None, feed_url=FEED)
    monkeypatch.setattr(feed_ops, "load_feed", lambda url: [ep])
    monkeypatch.setattr(feed_ops, "download", lambda url, cache_dir, progress=True: audio)
    monkeypatch.setattr(
        moment_ops, "select_moments",
        lambda *a, **k: [Moment(start=1, end=12, title="First take", reason="hooky"), Moment(start=14, end=26, title="Second take", reason="quotable")],
    )
    mh = FakeAPI(rendered, png)
    use_fake(monkeypatch, mh)
    fake_gh = FakeGitHub()
    gh = GitHub("ghs_test", REPO, transport=httpx.MockTransport(fake_gh))
    return showconfig.load(tmp_path / "pod2video.yml"), gh, fake_gh, mh, cache


pytestmark = pytest.mark.skipif(not _ffmpeg_with_libass(), reason="needs ffmpeg with libass")


def test_preview_opens_an_issue_and_render_replies_with_clips(show):
    cfg, gh, fake_gh, mh, cache = show
    quiet = Console(quiet=True)

    number = actions.preview(cfg, gh, console=quiet)
    issue = fake_gh.issues[number]
    assert issue["labels"] == ["pod2video"] and "Ep 7: Shipping" in issue["title"]
    body = issue["body"]
    assert "First take" in body and "Second take" in body and body.count("▶ watch") == 2
    assert "/render 1" in body and "Who is who?" in body and "A=Bob B=Alice" in body
    assert {"moments.json", "transcript.json"} <= fake_gh.assets()
    assert sum(1 for name in fake_gh.assets() if name.endswith("_preview.mp4")) == 2
    assert mh.count("/v1/ai-talking-photo") == 0  # previews are free
    [release] = fake_gh.releases.values()
    assert release["draft"] is True

    assert actions.preview(cfg, gh, console=quiet) is None  # same episode again: nothing to do

    # The render job runs on a fresh machine: no cached transcript, only what the issue and release carry.
    for cached in (cache / "transcripts").glob("*.json"):
        cached.unlink()
    event = {"issue": {"number": number, "body": body}, "comment": {"id": 42, "body": "/render 2"}}
    report = actions.render(cfg, gh, event, console=quiet)

    [picked] = [c for c in report["clips"] if c["picked"]]
    assert picked["index"] == 2 and picked["status"] == "complete"
    assert mh.count("/v1/ai-talking-photo") == 2  # speaker-cut: one job per host
    assert any(name.startswith("clip_02_") and not name.endswith("_preview.mp4") for name in fake_gh.assets())
    [(issue_number, reply)] = fake_gh.comments
    assert issue_number == number and "Rendered 1 of 1" in reply and "800 Magic Hour credits" in reply
    assert fake_gh.reactions == [(42, "eyes"), (42, "rocket")]
    assert fake_gh.auth_on_blob and not any(fake_gh.auth_on_blob)  # the token never leaves api.github.com


def test_render_over_budget_is_refused_and_reported(show):
    cfg, gh, fake_gh, mh, cache = show
    quiet = Console(quiet=True)
    number = actions.preview(cfg, gh, console=quiet)
    cfg.max_credits_per_run = 10
    event = {"issue": {"number": number, "body": fake_gh.issues[number]["body"]}, "comment": {"id": 7, "body": "/render 1"}}
    with pytest.raises(Exception, match="budget"):
        actions.render(cfg, gh, event, console=quiet)
    assert mh.count("/v1/ai-talking-photo") == 0
    [(_, reply)] = fake_gh.comments
    assert reply.startswith("❌") and "budget" in reply
    assert (7, "confused") in fake_gh.reactions
