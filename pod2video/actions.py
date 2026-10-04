"""The GitHub Actions flow.

`preview` (scheduled, or run by hand): finds the episode, picks candidate moments, renders
free previews, uploads them to a draft release, and opens an issue listing the candidates.

`render` (an owner comments `/render 1 4` on that issue): renders the chosen clips with
Magic Hour, uploads them to the same release, and replies with what it cost.

Everything the render step needs from the preview step travels with the issue and the
release (moments.json, transcript.json, metadata in the issue body), so it does not depend
on the Actions cache surviving.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any

from rich.console import Console

from pod2video import feed as feed_ops
from pod2video import speakers as speaker_ops
from pod2video.github import GitHub
from pod2video.models import Transcript
from pod2video.pipeline import Pipeline, RunOptions
from pod2video.showconfig import ShowConfig
from pod2video.transcribe import file_sha256

LABEL = "pod2video"
OUT = Path("out")
_META = re.compile(r"<!-- pod2video-meta:([A-Za-z0-9+/=]+) -->")


class ActionError(RuntimeError):
    pass


# -- small pure helpers (tested directly) --------------------------------------------------


def episode_marker(feed: str, guid: str) -> str:
    return "pod2video-episode:" + hashlib.sha1(f"{feed}\n{guid}".encode()).hexdigest()[:12]


def encode_meta(meta: dict[str, Any]) -> str:
    return f"<!-- pod2video-meta:{base64.b64encode(json.dumps(meta).encode()).decode()} -->"


def decode_meta(body: str) -> dict[str, Any]:
    match = _META.search(body or "")
    if not match:
        raise ActionError("this issue has no pod2video metadata (was it opened by the preview workflow?)")
    return json.loads(base64.b64decode(match.group(1)))


def parse_render_command(text: str) -> tuple[list[int], dict[str, str]]:
    """`/render 1 3,4 A=Alice B="Bob Smith"` -> ([1, 3, 4], {"A": "Alice", "B": "Bob Smith"})."""
    line = next((ln.strip() for ln in (text or "").splitlines() if ln.strip().startswith("/render")), None)
    if line is None:
        raise ActionError("no /render command found")
    picks: list[int] = []
    mapping: dict[str, str] = {}
    for token in shlex.split(line)[1:]:
        if "=" in token:
            label, _, name = token.partition("=")
            mapping[label.strip()] = name.strip()
            continue
        for part in filter(None, token.split(",")):
            if not part.isdigit():
                raise ActionError(f"'{part}' is not a clip number; use e.g. /render 1 3")
            picks.append(int(part))
    if not picks:
        raise ActionError("say which clips to render, e.g. /render 1 3")
    return sorted(set(picks)), mapping


def _money(usd: float) -> str:
    return f"${usd:,.2f}"


def _clock(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _cell(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def _run_url() -> str | None:
    server, repo, run = (os.environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{server}/{repo}/actions/runs/{run}" if server and repo and run else None


def preview_issue_body(
    *,
    report: dict[str, Any],
    assets: dict[str, str],
    speaker_rows: list[tuple[str, float, str, str]],
    suggested: list[int],
    meta: dict[str, Any],
) -> str:
    plan = report["plan"]
    rate = plan["credits_per_usd"]
    lines = [
        f"### {report['episode']}",
        f"{report['podcast'] or ''} · [episode audio]({meta['audio_url']})",
        "",
        f"**{len(report['clips'])} candidate clips.** The previews are free: the layout, cuts and captions are final, "
        "but the faces only animate in the real render.",
        "",
        "| # | Clip | Starts | Length | Preview | Est. credits |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for clip in report["clips"]:
        title = f"**{_cell(clip['title'])}**" + (f"<br><sub>{_cell(clip['reason'])}</sub>" if clip.get("reason") else "")
        link = assets.get(Path(clip["file"]).name) if clip.get("file") else None
        lines.append(
            f"| {clip['index']} | {title} | {_clock(clip['start'])} | {clip['duration']:.0f}s | "
            f"{f'[▶ watch]({link})' if link else '—'} | {clip['credits_estimate']:,} |"
        )
    estimate = sum(c["credits_estimate"] for c in report["clips"] if c["index"] in suggested)
    command = "/render " + " ".join(map(str, suggested))
    lines += [
        "",
        f"**To render**, reply with the clips you want (suggested: `{command}`, about {estimate:,} credits, "
        f"≈ {_money(estimate / rate)} on {plan['name']}):",
        "",
        "```",
        command,
        "```",
    ]
    if speaker_rows:
        lines += [
            "",
            "**Who is who?** Check this before rendering: the camera follows these labels.",
            "",
            "| Label | Talk time | Sample line | Host |",
            "| --- | --- | --- | --- |",
        ]
        lines += [f"| {label} | {secs / 60:.1f} min | {_cell(sample)} | {host or '—'} |" for label, secs, sample, host in speaker_rows]
        swapped = " ".join(f"{label}={host}" for label, host in _swapped(speaker_rows))
        if swapped:
            lines += ["", f"If the hosts are the wrong way round, add the mapping: `{command} {swapped}`"]
    totals = report["totals"]
    spent = [f"{name} {_money(usd)}" for name, usd in (("transcription", totals["transcription_usd"]), ("moment selection", totals["llm_usd"])) if usd]
    footer = "Spent on this preview: " + (", ".join(spent) if spent else "nothing new (cached)") + ". No Magic Hour credits."
    if _run_url():
        footer += f" [Run log]({_run_url()})"
    lines += ["", f"<sub>{footer}</sub>", encode_meta(meta), f"<!-- {meta['marker']} -->"]
    return "\n".join(lines)


def _swapped(rows: list[tuple[str, float, str, str]]) -> list[tuple[str, str]]:
    """For two mapped hosts, the mapping with them exchanged (the most likely mistake)."""
    mapped = [(label, host) for label, _, _, host in rows if host]
    if len(mapped) != 2:
        return []
    (a, host_a), (b, host_b) = mapped
    return sorted([(a, host_b), (b, host_a)])


def render_comment(report: dict[str, Any], assets: dict[str, str], release_url: str | None) -> str:
    plan = report["plan"]
    picked = [c for c in report["clips"] if c["picked"]]
    ok = [c for c in picked if c["status"] == "complete"]
    credits = report["totals"]["magichour_credits"]
    head = "✅" if len(ok) == len(picked) else "⚠️"
    lines = [
        f"{head} Rendered {len(ok)} of {len(picked)} clip(s): **{credits:,} Magic Hour credits** "
        f"(≈ {_money(report['totals']['magichour_usd'])} on {plan['name']}).",
        "",
        "| # | Clip | Length | Credits | Download |",
        "| --- | --- | --- | --- | --- |",
    ]
    for c in picked:
        link = assets.get(Path(c["file"]).name) if c.get("file") else None
        cell = f"[⬇ mp4]({link})" if link else f"failed: {_cell((c.get('error') or '')[:200])}"
        lines.append(f"| {c['index']} | {_cell(c['title'])} | {c['duration']:.0f}s | {c['credits']:,} | {cell} |")
    extra = [f"[All files (draft release)]({release_url})"] if release_url else []
    if _run_url():
        extra.append(f"[Run log]({_run_url()})")
    if any(c["status"] == "error" for c in picked):
        extra.append("Magic Hour refunds failed jobs; reply `/render` again to retry (finished renders are reused).")
    lines += ["", " · ".join(extra)]
    return "\n".join(lines)


# -- flows ------------------------------------------------------------------------------


def _options(cfg: ShowConfig, **overrides: Any) -> RunOptions:
    base = dict(
        hosts=cfg.host_models(),
        out=OUT,
        clips=cfg.clips,
        candidates=cfg.candidates,
        min_len=cfg.min_len,
        max_len=cfg.max_len,
        layout=cfg.layout,
        style=cfg.style,
        yes=True,
        interactive=False,
        podcast_name=cfg.podcast_name,
        avatar=cfg.avatar,
        generation_mode=cfg.generation_mode,
        plan=cfg.plan,
        max_credits=cfg.max_credits_per_run,
    )
    return RunOptions(**{**base, **overrides})


def preview(cfg: ShowConfig, gh: GitHub, *, episode: str = "latest", force: bool = False, console: Console | None = None) -> int | None:
    """Returns the issue number, or None when the episode was already handled."""
    console = console or Console()
    settings = cfg.settings()
    episodes = feed_ops.load_feed(cfg.feed)
    ep = feed_ops.pick_episode(episodes, episode)
    marker = episode_marker(cfg.feed, ep.guid)
    existing = gh.find_issue(LABEL, marker)
    if existing and not force:
        console.print(f"'{ep.title}' already has issue #{existing['number']}; nothing to do.")
        return None

    opts = _options(cfg, feed=cfg.feed, episode=ep.guid, preview=True, speaker_map=cfg.speaker_map_arg(), logo=cfg.logo_path())
    pipeline = Pipeline(opts, settings, console)
    report = pipeline.run()
    episode_dir = pipeline.episode_dir

    release = gh.create_release(
        tag=f"pod2video-{marker.rsplit(':', 1)[1]}",
        name=f"Clips: {report['episode']}"[:120],
        body=f"pod2video clips for **{report['episode']}**. Previews first; rendered clips are added here.",
    )
    assets: dict[str, str] = {}
    files = [Path(c["file"]) for c in report["clips"] if c.get("file")]
    files += [episode_dir / "moments.json", episode_dir / "transcript.json"]
    for path in files:
        if path.exists():
            assets[path.name] = gh.upload_asset(release, path)["browser_download_url"]
    if "moments.json" not in assets:  # --segment or --moments runs keep moments elsewhere
        moments = episode_dir / "moments.json"
        moments.write_text(json.dumps({"moments": [_moment_dict(c) for c in report["clips"]]}, indent=2))
        assets[moments.name] = gh.upload_asset(release, moments)["browser_download_url"]

    speaker_rows: list[tuple[str, float, str, str]] = []
    if pipeline.transcript and pipeline.layout != "single":
        names = {label: name for name, label in pipeline.label_for_host.items()}
        speaker_rows = [
            (label, secs, speaker_ops.sample_line(pipeline.transcript, label), names.get(label, ""))
            for label, secs in pipeline.transcript.speakers_by_talk_time()
        ]
    meta = {
        "v": 1,
        "marker": marker,
        "feed": cfg.feed,
        "guid": ep.guid,
        "audio_url": ep.audio_url,
        "logo_url": ep.podcast_image,
        "episode_title": report["episode"],
        "podcast_name": report["podcast"],
        "release_id": release["id"],
        "speaker_map": {label: name for name, label in pipeline.label_for_host.items()},
    }
    suggested = [c["index"] for c in report["clips"]][: cfg.clips]
    body = preview_issue_body(report=report, assets=assets, speaker_rows=speaker_rows, suggested=suggested, meta=meta)
    gh.ensure_label(LABEL, description="pod2video episode clips")
    if existing:
        gh.update_issue(existing["number"], body=body)
        gh.comment(existing["number"], "🔁 Previews regenerated; the list above is updated.")
        return existing["number"]
    issue = gh.create_issue(f"🎙️ Clips: {report['episode']}"[:200], body, [LABEL])
    console.print(f"Opened issue #{issue['number']}: {issue.get('html_url')}")
    return issue["number"]


def _moment_dict(clip: dict[str, Any]) -> dict[str, Any]:
    return {k: clip[k] for k in ("start", "end", "title", "reason", "hook") if k in clip}


def render(cfg: ShowConfig, gh: GitHub, event: dict[str, Any], *, console: Console | None = None) -> dict[str, Any]:
    console = console or Console()
    issue, comment = event["issue"], event["comment"]
    number = issue["number"]
    gh.react(comment["id"], "eyes")
    try:
        picks, mapping = parse_render_command(comment["body"])
        meta = decode_meta(issue["body"])
        settings = cfg.settings()
        release = gh.get_release(meta["release_id"])
        work = OUT / ".render-inputs"
        moments = gh.download_asset(release, "moments.json", work / "moments.json")
        audio = feed_ops.download(meta["audio_url"], settings.cache_dir, progress=False)
        _seed_transcript(gh, release, audio, settings.cache_dir, settings.transcription_provider, work)

        logo = cfg.logo_path()
        if logo == "feed":
            logo = str(feed_ops.download(meta["logo_url"], settings.cache_dir, progress=False)) if meta.get("logo_url") else None
        speaker_map = mapping or meta.get("speaker_map") or cfg.speaker_map or {}
        opts = _options(
            cfg,
            audio=audio,
            podcast_name=meta.get("podcast_name") or cfg.podcast_name,
            episode_title=meta["episode_title"],
            moments_file=moments,
            pick=picks,
            speaker_map=",".join(f"{k}={v}" for k, v in speaker_map.items()) or None,
            logo=logo,
        )
        report = Pipeline(opts, settings, console).run()
    except Exception as exc:
        gh.comment(number, f"❌ Render failed: {str(exc)[:1500]}" + (f"\n\n[Run log]({_run_url()})" if _run_url() else ""))
        gh.react(comment["id"], "confused")
        raise

    assets: dict[str, str] = {}
    for clip in report["clips"]:
        if clip["picked"] and clip.get("file"):
            path = Path(clip["file"])
            assets[path.name] = gh.upload_asset(release, path)["browser_download_url"]
    gh.comment(number, render_comment(report, assets, release.get("html_url")))
    gh.react(comment["id"], "rocket" if all(c["status"] == "complete" for c in report["clips"] if c["picked"]) else "confused")
    return report


def _seed_transcript(gh: GitHub, release: dict[str, Any], audio: Path, cache_dir: Path, provider: str, work: Path) -> None:
    """Reuse the preview's transcript so a render never pays to transcribe again."""
    names = {a["name"] for a in release.get("assets", [])}
    if "transcript.json" not in names:
        return
    transcript = Transcript.model_validate_json(gh.download_asset(release, "transcript.json", work / "transcript.json").read_text())
    digest = file_sha256(audio)
    if transcript.audio_sha256 != digest:
        return  # the publisher replaced the file; transcribe it again
    target = cache_dir / "transcripts" / f"{digest[:24]}.{provider}.json"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(transcript.model_dump_json())


def github_from_env() -> GitHub:
    return GitHub(os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GITHUB_REPOSITORY", ""))


def load_event() -> dict[str, Any]:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not Path(path).exists():
        raise ActionError("GITHUB_EVENT_PATH is not set; `render` runs inside GitHub Actions on an issue comment")
    return json.loads(Path(path).read_text())
