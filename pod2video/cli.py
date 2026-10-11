"""pod2video command line."""

from __future__ import annotations

import re
import sys
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx
import typer
from rich.console import Console
from rich.table import Table

from pod2video.config import Settings

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Turn audio podcasts into vertical talking-photo clips with Magic Hour.")
console = Console()


class LayoutChoice(str, Enum):
    auto = "auto"
    split = "split"
    speaker_cut = "speaker-cut"
    single = "single"


class ModeChoice(str, Enum):
    realistic = "realistic"
    prompted = "prompted"


class AvatarChoice(str, Enum):
    photo = "photo"
    cartoon = "cartoon"


def _parse_host(value: str):
    """`Alice=photos/alice.jpg`, or `Alice=desc:curly red hair, round glasses` for a generated avatar."""
    from pod2video.models import Host

    name, sep, spec = value.partition("=")
    name, spec = name.strip(), spec.strip()
    if not sep or not name or not spec:
        raise typer.BadParameter(f'expected NAME=PHOTO or NAME=desc:DESCRIPTION, got "{value}"')
    if spec.lower().startswith("desc:"):
        return Host(name=name, description=spec[5:].strip())
    return Host(name=name, photo=Path(spec).expanduser())


def _parse_pick(value: str | None) -> list[int]:
    if not value:
        return []
    try:
        return [int(x) for x in value.replace(" ", "").split(",") if x]
    except ValueError as exc:
        raise typer.BadParameter(f"--pick takes clip numbers like 1,3,4 (got '{value}')") from exc


def _seconds(text: str) -> float:
    parts = [float(p) for p in text.split(":")]
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def _parse_segment(value: str) -> tuple[float, float]:
    match = re.fullmatch(r"\s*([\d:.]+)\s*-\s*([\d:.]+)\s*", value)
    if not match:
        raise typer.BadParameter(f"expected START-END like 95-130 or 1:35-2:10, got '{value}'")
    start, end = _seconds(match.group(1)), _seconds(match.group(2))
    if end <= start:
        raise typer.BadParameter(f"segment end must be after start: '{value}'")
    return start, end


@app.command()
def run(
    host: list[str] = typer.Option(..., "--host", help='"Alice=photos/alice.jpg", or "Alice=desc:curly red hair, glasses" for a generated cartoon. Repeat per host.'),
    feed: Optional[str] = typer.Option(None, "--feed", help="Podcast RSS feed URL."),
    episode: str = typer.Option("latest", "--episode", help="latest, a number (1 = latest), a GUID, or part of the title."),
    audio: Optional[Path] = typer.Option(None, "--audio", help="Local audio file instead of a feed."),
    clips: int = typer.Option(3, "--clips", min=1, max=20, help="Clips to render with Magic Hour."),
    candidates: Optional[int] = typer.Option(None, "--candidates", min=1, max=20, help="Moments to select and preview (default: 2 x --clips)."),
    pick: Optional[str] = typer.Option(None, "--pick", help="Candidate numbers to render, e.g. 1,3,4 (default: ask, or the top --clips)."),
    min_len: float = typer.Option(15, "--min-len", help="Minimum clip length in seconds."),
    max_len: float = typer.Option(30, "--max-len", help="Maximum clip length in seconds (prompted mode allows up to 45)."),
    layout: LayoutChoice = typer.Option(LayoutChoice.auto, "--layout", help="speaker-cut (default for 2+ hosts; only the speaker is animated), split (all hosts on screen, costs more), or single."),
    style: str = typer.Option("bold", "--style", help="Caption style preset: bold, clean, minimal."),
    out: Path = typer.Option(Path("out"), "--out", help="Output directory."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Select moments and show the cost on each plan; do not render."),
    preview: bool = typer.Option(False, "--preview", help="Render every candidate with still images instead of Magic Hour (free)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask before spending Magic Hour credits."),
    speaker_map: Optional[str] = typer.Option(None, "--speaker-map", help='Skip the prompt: "A=Alice,B=Bob".'),
    speakers: Optional[int] = typer.Option(None, "--speakers", help="Expected number of speakers (hint for diarization)."),
    segment: list[str] = typer.Option([], "--segment", help="Use a fixed time range instead of LLM selection, e.g. 95-130. Repeatable."),
    moments: Optional[Path] = typer.Option(None, "--moments", help="Use moments from a JSON file (e.g. an edited moments.json)."),
    reselect: bool = typer.Option(False, "--reselect", help="Ask the LLM again even if moments.json matches."),
    podcast_name: Optional[str] = typer.Option(None, "--podcast-name", help="Override the podcast name shown on screen."),
    episode_title: Optional[str] = typer.Option(None, "--episode-title", help="Override the episode title shown on screen."),
    logo: Optional[str] = typer.Option(None, "--logo", help='Logo image path, or "feed" to use the podcast artwork.'),
    avatar: AvatarChoice = typer.Option(AvatarChoice.photo, "--avatar", help="photo: animate the photos as given. cartoon: make a cartoon of each photo once (~50 credits per host) and reuse it."),
    avatar_prompt: Optional[str] = typer.Option(None, "--avatar-prompt", help="Custom prompt for cartoon avatars."),
    generation_mode: ModeChoice = typer.Option(ModeChoice.prompted, "--generation-mode", help="Talking Photo mode: prompted (~24 credits/s, up to 45 s per job) or realistic (48 credits/s, best likeness)."),
    max_resolution: Optional[int] = typer.Option(None, "--max-resolution", help="Cap the Talking Photo output's longer edge (does not change credits)."),
    plan: Optional[str] = typer.Option(None, "--plan", help="Your Magic Hour plan for cost reporting, e.g. creator-annual (default: read from your account; see `pod2video plans`)."),
    per_month: float = typer.Option(4, "--per-month", help="Episodes you make per month, for the plan recommendation."),
    concurrency: Optional[int] = typer.Option(None, "--concurrency", min=1, max=16, help="Magic Hour jobs at once (default: what your plan allows)."),
    max_credits: Optional[int] = typer.Option(None, "--max-credits", min=0, help="Refuse to start if the run would spend more than this many credits."),
) -> None:
    """Make vertical clips from a podcast episode."""
    from pod2video.pipeline import Pipeline, PipelineError, RunOptions

    if bool(feed) == bool(audio):
        raise typer.BadParameter("pass exactly one of --feed or --audio")
    settings = Settings()
    try:
        opts = RunOptions(
            hosts=[_parse_host(h) for h in host],
            out=out,
            feed=feed,
            episode=episode,
            audio=audio,
            clips=clips,
            candidates=candidates,
            pick=_parse_pick(pick),
            min_len=min_len,
            max_len=max_len,
            layout=layout.value,
            style=style,
            dry_run=dry_run,
            preview=preview,
            yes=yes,
            interactive=sys.stdin.isatty(),
            speaker_map=speaker_map,
            speakers_expected=speakers,
            segments=[_parse_segment(s) for s in segment],
            moments_file=moments,
            reselect=reselect,
            podcast_name=podcast_name,
            episode_title=episode_title,
            logo=logo,
            avatar=avatar.value,
            avatar_prompt=avatar_prompt,
            generation_mode=generation_mode.value,
            max_resolution=max_resolution,
            plan=plan or settings.magic_hour_plan,
            per_month=per_month,
            concurrency=concurrency,
            max_credits=max_credits,
        )
        report = Pipeline(opts, settings, console).run()
    except (PipelineError, ValueError, RuntimeError, httpx.HTTPError) as exc:
        console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(2) from exc
    if any(c["status"] == "error" for c in report["clips"]):
        raise typer.Exit(1)


@app.command()
def plans(
    clips_per_month: int = typer.Option(13, "--clips-per-month", help="Clips you expect to render per month (13 = weekly show, 3 clips each)."),
    clip_length: float = typer.Option(23, "--clip-length", help="Average clip length in seconds."),
    hosts: int = typer.Option(2, "--hosts", help="Hosts per show."),
    layout: LayoutChoice = typer.Option(LayoutChoice.auto, "--layout"),
    generation_mode: ModeChoice = typer.Option(ModeChoice.prompted, "--generation-mode"),
) -> None:
    """Compare Magic Hour plans for your volume and recommend the cheapest."""
    from core.magichour import TALKING_PHOTO_CREDITS_PER_SECOND
    from core.plans import PLANS, detect_plan, rank_plans

    rate = TALKING_PHOTO_CREDITS_PER_SECOND[generation_mode.value]
    chosen = "single" if hosts == 1 else ("speaker-cut" if layout == LayoutChoice.auto else layout.value)
    # speaker-cut bills ~3% over clip length (settle pauses between a host's moments; simulated);
    # split animates every host for the whole clip.
    billed = clip_length * (hosts if chosen == "split" else 1.03 if chosen == "speaker-cut" else 1.0)
    per_clip = billed * rate
    monthly = round(per_clip * clips_per_month)
    console.print(
        f"{clips_per_month} clips/month x {clip_length:g}s, {hosts} host(s), {chosen}, {generation_mode.value}: "
        f"~{per_clip:,.0f} credits per clip, ~{monthly:,} credits per month."
    )

    yours = None
    settings = Settings()
    if settings.magic_hour_api_key:
        from core.magichour import MagicHour, MagicHourError

        try:
            with MagicHour(settings.magic_hour_api_key) as mh:
                yours = detect_plan(mh.account())
        except MagicHourError:
            pass

    ranked = rank_plans(monthly)
    ranked = [pc for pc in ranked if pc[0].self_serve] + [pc for pc in ranked if not pc[0].self_serve]
    table = Table(title="Monthly cost for this volume, cheapest first")
    for col, justify in (("Plan", "left"), ("Per month", "right"), ("Per clip", "right"), ("Note", "left")):
        table.add_column(col, justify=justify)
    for n, (p, cost) in enumerate(ranked):
        name = p.name + (" ← yours" if yours and yours.key == p.key else "")
        style = "bold green" if n == 0 else ("dim" if not p.self_serve else "")
        table.add_row(name, f"${cost:,.2f}", f"${cost / clips_per_month:,.2f}", p.note, style=style)
    table.add_row(PLANS["free"].name, "—", "—", PLANS["free"].note, style="dim")
    console.print(table)
    console.print(
        "Subscription credits never expire, so a quiet month banks credits. Higher plans add credits and "
        "concurrency, not Talking Photo quality (720p on every paid plan)."
    )


@app.command()
def avatars(
    host: list[str] = typer.Option(..., "--host", help='"Alice=photos/alice.jpg" or "Alice=desc:curly red hair, glasses".'),
    avatar_prompt: Optional[str] = typer.Option(None, "--avatar-prompt", help="Custom prompt for the cartoon."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask before spending credits."),
) -> None:
    """Make (or show cached) cartoon avatars once, so previews and runs can use them."""
    from rich.prompt import Confirm

    from core.costs import CostLedger
    from core.magichour import MagicHour, MagicHourError
    from pod2video.avatars import AVATAR_CREDITS_ESTIMATE, Avatars, check_face

    settings = Settings()
    store = Avatars(settings.cache_dir, style="cartoon", prompt=avatar_prompt)
    hosts = [_parse_host(h) for h in host]
    pending = store.pending(hosts)
    for h in hosts:
        if h not in pending:
            console.print(f"{h.name}: {store.cached(h)} (already made)")
    if not pending:
        return
    if not settings.magic_hour_api_key:
        console.print("[red]MAGIC_HOUR_API_KEY is not set[/]")
        raise typer.Exit(2)
    estimate = AVATAR_CREDITS_ESTIMATE * len(pending)
    if not yes and not Confirm.ask(f"Make {len(pending)} avatar(s) for ~{estimate} credits?", default=True, console=console):
        raise typer.Exit(1)
    ledger = CostLedger()
    try:
        with MagicHour(settings.magic_hour_api_key, ledger=ledger) as mh:
            for h in pending:
                path = store.generate(h, mh)
                faces = check_face(path, mh, settings.cache_dir)
                note = "" if faces else "  [yellow](no face detected: try --avatar-prompt)[/]"
                console.print(f"{h.name}: {path}{note}")
    except MagicHourError as exc:
        console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(2) from exc
    console.print(f"Spent {ledger.magichour_credits():,} credits.")


actions_app = typer.Typer(no_args_is_help=True, help="Run inside GitHub Actions (see template/README.md).")
app.add_typer(actions_app, name="actions")


@actions_app.command("preview")
def actions_preview(
    config: Path = typer.Option(Path("pod2video.yml"), "--config", help="The show's config file."),
    episode: str = typer.Option("latest", "--episode", help="latest, a number (1 = latest), or part of the title."),
    force: bool = typer.Option(False, "--force", help="Preview again even if the episode already has an issue."),
) -> None:
    """New episode -> free previews in a draft release + an issue listing the candidates."""
    from pod2video import actions, showconfig

    try:
        actions.preview(showconfig.load(config), actions.github_from_env(), episode=episode, force=force, console=console)
    except (RuntimeError, ValueError, FileNotFoundError, httpx.HTTPError) as exc:
        console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(1) from exc


@actions_app.command("render")
def actions_render(
    config: Path = typer.Option(Path("pod2video.yml"), "--config", help="The show's config file."),
) -> None:
    """Handle a `/render 1 4` comment: render those clips and reply with the files and the cost."""
    from pod2video import actions, showconfig

    try:
        report = actions.render(showconfig.load(config), actions.github_from_env(), actions.load_event(), console=console)
    except (RuntimeError, ValueError, FileNotFoundError, httpx.HTTPError) as exc:
        console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(1) from exc
    if any(c["status"] == "error" for c in report["clips"] if c["picked"]):
        raise typer.Exit(1)


@app.command()
def episodes(
    feed: str = typer.Option(..., "--feed", help="Podcast RSS feed URL."),
    limit: int = typer.Option(10, "--limit", help="How many to list."),
) -> None:
    """List a feed's episodes (newest first) with the numbers --episode accepts."""
    import time

    from pod2video.feed import load_feed

    items = load_feed(feed)
    table = Table(title=items[0].podcast_title)
    table.add_column("#", justify="right")
    table.add_column("Published")
    table.add_column("Title")
    for i, ep in enumerate(items[:limit], start=1):
        published = time.strftime("%Y-%m-%d", time.localtime(ep.published)) if ep.published else "?"
        table.add_row(str(i), published, ep.title)
    console.print(table)


@app.command()
def doctor() -> None:
    """Check ffmpeg, API keys, and the Magic Hour credit balance."""
    from core import compose
    from core.magichour import MagicHour, MagicHourError

    settings = Settings()
    ok = True

    def line(good: bool, label: str, detail: str) -> None:
        nonlocal ok
        ok &= good
        console.print(f"{'[green]✓[/]' if good else '[red]✗[/]'} {label}: {detail}")

    try:
        binary = compose.ffmpeg_bin()
        captions = compose.supports_captions()
        line(captions, "ffmpeg", f"{binary}" + ("" if captions else " (no libass: install ffmpeg-full or set FFMPEG_BIN)"))
    except compose.FFmpegError as exc:
        line(False, "ffmpeg", str(exc))

    line(bool(settings.transcription_api_key), "TRANSCRIPTION_API_KEY", f"{'set' if settings.transcription_api_key else 'missing'} (provider: {settings.transcription_provider})")
    line(bool(settings.llm_api_key and settings.llm_model), "LLM", f"model={settings.llm_model or 'missing'}, key {'set' if settings.llm_api_key else 'missing'}")
    if settings.magic_hour_api_key:
        try:
            with MagicHour(settings.magic_hour_api_key) as mh:
                account = mh.account()
            from core.plans import detect_plan

            plan = detect_plan(account)
            rate = f", {plan.credits_per_usd:,.0f} credits/$" if plan and plan.credits_per_usd else ""
            line(True, "MAGIC_HOUR_API_KEY", f"{plan.name if plan else account.get('tier', '?')}{rate}, {account.get('credits', 0):,} credits available")
        except MagicHourError as exc:
            line(False, "MAGIC_HOUR_API_KEY", str(exc))
    else:
        line(False, "MAGIC_HOUR_API_KEY", "missing")
    console.print(f"Cache: {settings.cache_dir}")
    if not ok:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
