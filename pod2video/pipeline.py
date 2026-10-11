"""End-to-end pipeline: fetch -> transcribe -> map speakers -> select candidate moments -> pick
-> prep audio -> animate (Magic Hour) -> compose -> report.

Cost model (see README "Cost"): in the default `speaker-cut` layout only the host on screen is
animated, and each host's on-screen moments in a clip are joined into one Talking Photo job, so a
clip bills roughly its own length no matter how many hosts there are. Previews are free; only the
picked clips are rendered.

Every artefact lands in `<out>/<episode-slug>/`, and expensive steps are cached so a rerun never
pays twice: transcripts (by audio hash), moments (`moments.json`), avatars (by source), and
Talking Photo outputs (by input hashes + settings).
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from core import compose
from core.costs import CostLedger, LedgerEntry
from core.llm import LLM
from core.magichour import TALKING_PHOTO_MAX_SECONDS, MagicHour, MagicHourError, estimate_talking_photo_credits
from core.plans import DEFAULT_PLAN, PLANS, Plan, custom_plan, detect_plan, rank_plans
from pod2video import audio as audio_ops
from pod2video import avatars as avatar_ops
from pod2video import feed as feed_ops
from pod2video import moments as moment_ops
from pod2video import speakers
from pod2video.config import Settings
from pod2video.models import Host, Moment, Transcript, Word
from pod2video.transcribe import TranscriptionError, file_sha256, transcribe

Layout = Literal["auto", "split", "speaker-cut", "single"]
MIN_SPEECH_TO_ANIMATE = 0.5  # seconds; a host this quiet in `split` gets a still image instead of a paid job
MIN_SHOT = 1.5  # seconds; shortest shot in `speaker-cut`
PUNCH_IN = 1.12  # zoom for every other shot of the same host ("second camera")
SINGLE_SHOT = 5.0  # seconds; a single host gets a virtual cut at a pause about this often
SETTLE_PAD = 0.25  # seconds of silence between a host's moments inside one condensed render


class PipelineError(RuntimeError):
    pass


class RunOptions(BaseModel):
    hosts: list[Host]
    out: Path = Path("out")
    feed: str | None = None
    episode: str = "latest"
    audio: Path | None = None
    clips: int = Field(default=3, ge=1, le=20)  # clips to render with Magic Hour
    candidates: int | None = Field(default=None, ge=1, le=20)  # moments to select and preview; default 2 x clips
    pick: list[int] = Field(default_factory=list)  # candidate numbers to render; default the top `clips`
    min_len: float = Field(default=15.0, gt=0)
    max_len: float = Field(default=30.0, gt=0)
    layout: Layout = "auto"
    style: str = "bold"
    dry_run: bool = False
    preview: bool = False  # still photos instead of Magic Hour (free check of layout and captions)
    yes: bool = False  # skip the spend confirmation
    interactive: bool = True
    speaker_map: str | None = None
    speakers_expected: int | None = None
    segments: list[tuple[float, float]] = Field(default_factory=list)
    moments_file: Path | None = None
    reselect: bool = False
    podcast_name: str | None = None
    episode_title: str | None = None
    logo: str | None = None  # a path, or "feed" for the podcast artwork
    generation_mode: Literal["realistic", "prompted"] = "prompted"
    max_resolution: int | None = None
    concurrency: int | None = Field(default=None, ge=1, le=16)  # default: what the plan allows
    plan: str = "auto"  # a key of core.plans.PLANS, or auto (read from the Magic Hour account)
    avatar: Literal["photo", "cartoon"] = "photo"
    avatar_prompt: str | None = None
    per_month: float = Field(default=4.0, gt=0)  # episodes per month, for the plan recommendation
    max_credits: int | None = Field(default=None, ge=0)  # refuse to start a run that would spend more

    @property
    def candidate_count(self) -> int:
        return self.candidates or min(max(self.clips * 2, self.clips), 10)


@dataclass
class Source:
    audio: Path
    podcast_name: str
    episode_title: str
    store_key: str
    logo: Path | None
    origin: str


@dataclass
class Render:
    """One Talking Photo job (or a free still) covering some of a host's time in a clip."""

    host_index: int
    host: Host
    label: str  # "clip 2 / Alice", unique per render
    stem: str  # file name stem inside the clip directory
    spans: list[tuple[float, float]]  # clip-time stretches this render is shown for
    offsets: list[float]  # where each span starts inside the render
    seconds: float  # length of the render = billed seconds
    paid: bool = True
    audio: Path | None = None
    video: Path | None = None
    status: str = "planned"  # planned | cached | complete | still | error
    job_id: str | None = None
    credits: int | None = None
    error: str | None = None
    elapsed: float | None = None


@dataclass
class ClipPlan:
    index: int  # candidate number (1 = the model's top pick)
    moment: Moment
    dir: Path
    renders: list[Render]
    words: list[Word] = field(default_factory=list)  # clip-relative
    host_intervals: list[list[tuple[float, float]]] = field(default_factory=list)  # who speaks when
    on_screen: list[list[tuple[float, float]]] = field(default_factory=list)  # speaker-cut shots per host
    picked: bool = False
    mix: Path | None = None
    output: Path | None = None
    status: str = "planned"
    error: str | None = None

    @property
    def label(self) -> str:
        return f"clip {self.index}"

    def credits_estimate(self, mode: str) -> int:
        return sum(estimate_talking_photo_credits(r.seconds, mode) for r in self.renders if r.paid)

    def paid_jobs(self) -> int:
        return sum(1 for r in self.renders if r.paid)


def slugify(text: str, limit: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "episode"


def resolve_layout(layout: str, n_hosts: int) -> str:
    if layout == "auto":
        return "single" if n_hosts == 1 else "speaker-cut"
    if layout == "single" and n_hosts != 1:
        raise PipelineError("--layout single takes exactly one --host")
    if layout in ("split", "speaker-cut") and n_hosts < 2:
        raise PipelineError(f"--layout {layout} needs two or more --host entries (use single for one host)")
    if layout == "split" and n_hosts > 3:
        raise PipelineError("split supports up to 3 hosts; use --layout speaker-cut")
    return layout


def chunk_spans(spans: list[tuple[float, float]], limit: float, pad: float) -> list[list[tuple[float, float]]]:
    """Group consecutive spans into renders no longer than `limit` seconds (including pads)."""
    chunks: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    length = 0.0
    for a, b in spans:
        add = (b - a) + (pad if current else 0.0)
        if current and length + add > limit:
            chunks.append(current)
            current, length, add = [], 0.0, b - a
        current.append((a, b))
        length += add
    if current:
        chunks.append(current)
    return chunks


class Pipeline:
    def __init__(self, opts: RunOptions, settings: Settings, console: Console | None = None):
        self.opts = opts
        self.settings = settings
        self.console = console or Console()
        self.layout = resolve_layout(opts.layout, len(opts.hosts))
        if opts.style not in compose.CAPTION_STYLES:
            raise PipelineError(f"unknown --style '{opts.style}' (choose from {', '.join(compose.CAPTION_STYLES)})")
        if opts.min_len > opts.max_len:
            raise PipelineError("--min-len must not exceed --max-len")
        limit = TALKING_PHOTO_MAX_SECONDS[opts.generation_mode]
        if opts.max_len > limit:
            raise PipelineError(
                f"--max-len {opts.max_len:.0f}s exceeds the {opts.generation_mode} Talking Photo limit of {limit:.0f}s"
            )
        if opts.plan != "auto" and (opts.plan not in PLANS or opts.plan == "free"):
            raise PipelineError(f"unknown --plan '{opts.plan}' (choose from auto, {', '.join(k for k in PLANS if k != 'free')})")
        self.style = compose.CAPTION_STYLES[opts.style]
        self.ledger = CostLedger()
        self.avatars = avatar_ops.Avatars(settings.cache_dir, style=opts.avatar, prompt=opts.avatar_prompt)
        self.pricing: Plan = PLANS[DEFAULT_PLAN]
        self.pricing_source = "assumed"
        self.balance: int | None = None
        self.episode_dir: Path | None = None
        self.source: Source | None = None
        self.transcript: Transcript | None = None
        self.label_for_host: dict[str, str] = {}
        self.plans: list[ClipPlan] = []
        self.images: dict[str, Path] = {}

    # -- public ---------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        with self.ledger.stage("fetch"):
            self.source = self._fetch()
        self.episode_dir = self.opts.out / slugify(self.source.episode_title)
        self.episode_dir.mkdir(parents=True, exist_ok=True)
        self.ledger.path = self.episode_dir / "ledger.jsonl"
        self.console.rule(f"[bold]{self.source.podcast_name or 'pod2video'}[/] · {self.source.episode_title}")

        with self.ledger.stage("transcribe"):
            self.transcript = self._transcribe()
        with self.ledger.stage("map_speakers"):
            self._map_speakers()
        with self.ledger.stage("select_moments"):
            moments = self._select_moments()
        if not moments:
            raise PipelineError("no moments selected")
        self._plan(moments)
        self._resolve_pricing()
        self._choose_picks()
        self._show_estimate()

        if self.opts.dry_run:
            self._show_plan_choices(self._picked_estimate())
            self.console.print("[cyan]Dry run: nothing was sent to Magic Hour.[/]")
            return self._report(started)

        with self.ledger.stage("prepare_audio"):
            self._prepare_audio()
        with self.ledger.stage("prepare_images"):
            images = self._prepare_images()
            if not self.opts.preview:
                self._confirm_spend(images)
                images = self._make_avatars(images)
                self._check_faces(images)
        self.images = images
        with self.ledger.stage("animate"):
            self._animate(images)
        with self.ledger.stage("compose"):
            self._compose()
        return self._report(started)

    @property
    def picked(self) -> list[ClipPlan]:
        return [p for p in self.plans if p.picked]

    # -- source, transcript, speakers ------------------------------------------------------

    def _fetch(self) -> Source:
        opts, cache = self.opts, self.settings.cache_dir
        if opts.audio:
            if not opts.audio.exists():
                raise PipelineError(f"audio file not found: {opts.audio}")
            name = opts.podcast_name or ""
            logo = Path(opts.logo) if opts.logo and opts.logo != "feed" else None
            return Source(
                audio=opts.audio,
                podcast_name=name,
                episode_title=opts.episode_title or opts.audio.stem.replace("_", " "),
                store_key=f"podcast:{name}" if name else f"file:{opts.audio.resolve()}",
                logo=logo,
                origin=str(opts.audio),
            )
        if not opts.feed:
            raise PipelineError("pass --feed <rss_url> or --audio <file>")
        episodes = feed_ops.load_feed(opts.feed)
        episode = feed_ops.pick_episode(episodes, opts.episode)
        self.console.print(f"Episode: [bold]{episode.title}[/]")
        audio_path = feed_ops.download(episode.audio_url, cache)
        logo = None
        if opts.logo == "feed" and episode.podcast_image:
            logo = feed_ops.download(episode.podcast_image, cache, progress=False)
        elif opts.logo:
            logo = Path(opts.logo)
        return Source(
            audio=audio_path,
            podcast_name=opts.podcast_name or episode.podcast_title,
            episode_title=opts.episode_title or episode.title,
            store_key=f"feed:{opts.feed}",
            logo=logo,
            origin=episode.audio_url,
        )

    def _transcribe(self) -> Transcript | None:
        s = self.settings
        try:
            transcript = transcribe(
                self.source.audio,
                provider_name=s.transcription_provider,
                api_key=s.transcription_api_key,
                cache_dir=s.cache_dir,
                ledger=self.ledger,
                usd_per_hour=s.transcription_usd_per_hour,
                speakers_expected=self.opts.speakers_expected,
            )
        except TranscriptionError as exc:
            if not s.transcription_api_key and self.opts.segments and self.layout == "single":
                self.console.print("[yellow]No TRANSCRIPTION_API_KEY: continuing without captions.[/]")
                return None
            raise PipelineError(f"transcription failed: {exc}") from exc
        (self.episode_dir / "transcript.json").write_text(transcript.model_dump_json(indent=1))
        cached = self.ledger.entries[-1].status == "cached"
        self.console.print(
            f"Transcript: {len(transcript.words):,} words, {transcript.duration / 60:.1f} min"
            f"{' (cached)' if cached else ''}"
        )
        return transcript

    def _map_speakers(self) -> None:
        if self.layout == "single" or not self.transcript:
            return
        explicit = speakers.parse_map(self.opts.speaker_map, self.opts.hosts) if self.opts.speaker_map else None
        mapping = speakers.resolve(
            self.transcript,
            self.opts.hosts,
            store_key=self.source.store_key,
            cache_dir=self.settings.cache_dir,
            console=self.console,
            explicit=explicit,
            interactive=self.opts.interactive,
        )
        self.label_for_host = {name: label for label, name in mapping.items()}

    def _speaker_names(self) -> dict[str, str]:
        return {label: name for name, label in self.label_for_host.items()}

    # -- moments ------------------------------------------------------------------------

    def _select_moments(self) -> list[Moment]:
        opts, transcript = self.opts, self.transcript
        if opts.segments:
            moments = []
            for i, (a, b) in enumerate(opts.segments, start=1):
                if transcript:
                    a, b = moment_ops.snap_to_words(transcript, a, b)
                moments.append(Moment(start=a, end=b, title=f"Segment {i}", reason="--segment"))
            return moments
        if opts.moments_file:
            return self._load_moments(opts.moments_file)

        cache_path = self.episode_dir / "moments.json"
        params = {
            "candidates": opts.candidate_count,
            "min_len": opts.min_len,
            "max_len": opts.max_len,
            "model": self.settings.llm_model,
            # Host names are deliberately not part of the key: fixing a wrong speaker map must
            # keep the same moments and clip numbers, or `--pick` would render the wrong clip.
            "transcript": transcript.audio_sha256 if transcript else None,
        }
        if cache_path.exists() and not opts.reselect:
            cached = json.loads(cache_path.read_text())
            if cached.get("params") == params:
                self.console.print(f"Moments: reusing {cache_path} (pass --reselect to ask the LLM again)")
                return [Moment.model_validate(m) for m in cached["moments"]]

        if not transcript:
            raise PipelineError("moment selection needs a transcript (set TRANSCRIPTION_API_KEY) or use --segment")
        llm = LLM(
            self.settings.llm_model,
            self.settings.llm_api_key,
            provider=self.settings.llm_provider,
            effort=self.settings.llm_effort,
            base_url=self.settings.llm_base_url,
            usd_per_mtok_in=self.settings.llm_usd_per_mtok_in,
            usd_per_mtok_out=self.settings.llm_usd_per_mtok_out,
        )
        with self.console.status(f"Selecting moments with {llm.model}..."):
            moments = moment_ops.select_moments(
                transcript,
                llm,
                count=opts.candidate_count,
                min_len=opts.min_len,
                max_len=opts.max_len,
                speaker_names=self._speaker_names(),
                podcast=self.source.podcast_name,
                episode=self.source.episode_title,
                ledger=self.ledger,
            )
        cache_path.write_text(json.dumps({"params": params, "moments": [m.model_dump() for m in moments]}, indent=2))
        if len(moments) < opts.clips:
            self.console.print(f"[yellow]Only {len(moments)} usable moment(s) found (asked for {opts.clips}).[/]")
        return moments

    def _load_moments(self, path: Path) -> list[Moment]:
        data = json.loads(path.read_text())
        items = data["moments"] if isinstance(data, dict) else data
        moments = []
        for i, item in enumerate(items, start=1):
            moment = Moment.model_validate({"title": f"Clip {i}", **item})
            if self.transcript:
                moment.start, moment.end = moment_ops.snap_to_words(self.transcript, moment.start, moment.end)
            moments.append(moment)
        self.console.print(f"Moments: {len(moments)} loaded from {path}")
        return moments

    # -- planning -------------------------------------------------------------------------

    def _plan(self, moments: list[Moment]) -> None:
        limit = TALKING_PHOTO_MAX_SECONDS[self.opts.generation_mode]
        for i, moment in enumerate(moments, start=1):
            if moment.duration > limit:
                raise PipelineError(f"clip {i} is {moment.duration:.1f}s, over the {limit:.0f}s Talking Photo limit")
            words: list[Word] = []
            if self.transcript:
                for w in self.transcript.words_between(moment.start, moment.end):
                    words.append(
                        Word(
                            text=w.text,
                            start=max(0.0, w.start - moment.start),
                            end=min(moment.duration, w.end - moment.start),
                            speaker=w.speaker,
                        )
                    )
            intervals = [
                audio_ops.speaker_intervals(
                    self.transcript.words, self.label_for_host[h.name], clip_start=moment.start, clip_end=moment.end
                )
                if self.layout != "single"
                else []
                for h in self.opts.hosts
            ]
            plan = ClipPlan(index=i, moment=moment, dir=self.episode_dir / f"clip_{i:02d}", renders=[], words=words, host_intervals=intervals)
            plan.renders = self._plan_renders(plan, limit)
            self.plans.append(plan)

    def _plan_renders(self, plan: ClipPlan, limit: float) -> list[Render]:
        d, i = plan.moment.duration, plan.index
        hosts = self.opts.hosts
        if self.layout == "single":
            return [Render(0, hosts[0], f"clip {i} / {hosts[0].name}", slugify(hosts[0].name), [(0.0, d)], [0.0], d)]
        if self.layout == "split":
            return [
                Render(
                    h, host, f"clip {i} / {host.name}", slugify(host.name), [(0.0, d)], [0.0], d,
                    paid=sum(b - a for a, b in plan.host_intervals[h]) >= MIN_SPEECH_TO_ANIMATE,
                )
                for h, host in enumerate(hosts)
            ]
        # speaker-cut: animate each host only while on screen, one condensed render per host
        plan.on_screen = audio_ops.shot_list(plan.host_intervals, d, min_shot=MIN_SHOT)
        renders = []
        for h, (host, spans) in enumerate(zip(hosts, plan.on_screen)):
            chunks = chunk_spans(spans, limit, SETTLE_PAD)
            for k, chunk in enumerate(chunks, start=1):
                offsets = audio_ops.span_offsets(chunk, SETTLE_PAD)
                seconds = offsets[-1] + (chunk[-1][1] - chunk[-1][0])
                suffix = f" #{k}" if len(chunks) > 1 else ""
                renders.append(
                    Render(h, host, f"clip {i} / {host.name}{suffix}", slugify(host.name + suffix), chunk, offsets, round(seconds, 3))
                )
        return renders

    def _choose_picks(self) -> None:
        n = len(self.plans)
        if self.opts.pick:
            bad = [k for k in self.opts.pick if not 1 <= k <= n]
            if bad:
                raise PipelineError(f"--pick {bad} out of range: there are {n} candidates")
            chosen = set(self.opts.pick)
        elif self.opts.segments or self.opts.moments_file or self.opts.preview:
            chosen = set(range(1, n + 1))  # chosen by hand already, or free to preview
        else:
            chosen = set(range(1, min(self.opts.clips, n) + 1))
        for plan in self.plans:
            plan.picked = plan.index in chosen
        self._show_candidates()
        ask = self.opts.interactive and not (self.opts.yes or self.opts.dry_run or self.opts.preview or self.opts.pick)
        if ask and not (self.opts.segments or self.opts.moments_file) and n > len(chosen):
            answer = Prompt.ask(
                "Which clips should Magic Hour render? (numbers, comma-separated)",
                default=",".join(map(str, sorted(chosen))),
                console=self.console,
            )
            try:
                chosen = {int(x) for x in answer.replace(" ", "").split(",") if x}
            except ValueError as exc:
                raise PipelineError(f"not a list of clip numbers: {answer}") from exc
            if not chosen or any(not 1 <= k <= n for k in chosen):
                raise PipelineError(f"pick numbers between 1 and {n}")
            for plan in self.plans:
                plan.picked = plan.index in chosen

    def _picked_estimate(self) -> int:
        return sum(p.credits_estimate(self.opts.generation_mode) for p in self.picked)

    # -- pricing --------------------------------------------------------------------------

    def _resolve_pricing(self) -> None:
        s = self.settings
        if s.magic_hour_credits_per_usd:
            self.pricing, self.pricing_source = custom_plan(s.magic_hour_credits_per_usd), "MAGIC_HOUR_CREDITS_PER_USD"
        elif self.opts.plan != "auto":
            self.pricing, self.pricing_source = PLANS[self.opts.plan], "--plan"
        elif s.magic_hour_api_key:
            try:
                with MagicHour(s.magic_hour_api_key) as mh:
                    account = mh.account()
                self.balance = account.get("credits")
                detected = detect_plan(account)
                if detected:
                    self.pricing, self.pricing_source = detected, "your Magic Hour account"
            except MagicHourError as exc:
                self.console.print(f"[yellow]Could not read your Magic Hour plan ({exc}); assuming {self.pricing.name}.[/]")
        if self.pricing_source == "assumed":
            self.pricing_source = "assumed; pass --plan to set yours"
        self.ledger.credits_per_usd = self.pricing.credits_per_usd

    def _usd(self, credits: float) -> float:
        return credits / self.pricing.credits_per_usd

    def _show_candidates(self) -> None:
        mode = self.opts.generation_mode
        table = Table(title=f"Candidate clips ({self.layout} layout, {mode} mode)")
        for col, justify in (("#", "right"), ("Render", "center"), ("Title", "left"), ("Starts", "right"), ("Length", "right"), ("Jobs", "right"), ("Est. credits", "right")):
            table.add_column(col, justify=justify)
        for p in self.plans:
            table.add_row(
                str(p.index),
                "✓" if p.picked else "",
                p.moment.title,
                _clock(p.moment.start),
                f"{p.moment.duration:.1f}s",
                str(p.paid_jobs()),
                f"{p.credits_estimate(mode):,}",
            )
        self.console.print(table)

    def _pending_avatars(self) -> list[str]:
        return [h.name for h in self.avatars.pending(self.opts.hosts)]

    def _show_estimate(self) -> None:
        credits = self._picked_estimate()
        avatars = self._pending_avatars()
        if avatars and not self.opts.preview:
            once = avatar_ops.AVATAR_CREDITS_ESTIMATE * len(avatars)
            self.console.print(
                f"Plus ~{once} credits once for cartoon avatar(s) ({', '.join(avatars)}); reused for every later episode."
            )
        what = "Previews are free" if self.opts.preview else "Estimated Magic Hour cost"
        self.console.print(
            f"{what}: rendering the {len(self.picked)} ticked clip(s) with Talking Photo would be [bold]{credits:,} credits[/] "
            f"(~${self._usd(credits):,.2f} on {self.pricing.name}, {self.pricing_source})"
        )

    def _show_plan_choices(self, credits: int) -> None:
        """This run's cost on every plan, and which plan fits `per_month` runs like it."""
        table = Table(title="This run on each plan")
        table.add_column("Plan")
        table.add_column("This run", justify="right")
        table.add_column("Credits/$", justify="right")
        table.add_column("Note")
        for plan in PLANS.values():
            if plan.credits_per_usd is None:
                continue
            marker = " ←" if plan.key == self.pricing.key else ""
            table.add_row(plan.name + marker, f"${credits / plan.credits_per_usd:,.2f}", f"{plan.credits_per_usd:,.0f}", plan.note)
        self.console.print(table)
        monthly = round(credits * self.opts.per_month)
        ranked = rank_plans(monthly, include_usage=False)[:3]
        options = "; ".join(f"{p.name} ${cost:,.2f}/mo" + (f" ({p.note})" if p.note else "") for p, cost in ranked)
        self.console.print(
            f"At {self.opts.per_month:g} episodes a month (~{monthly:,} credits), the cheapest self-serve plans are: {options}."
        )

    # -- paid-step helpers ------------------------------------------------------------------

    def _prepare_audio(self) -> None:
        for p in self.picked:
            m = p.moment
            p.mix = audio_ops.cut(self.source.audio, m.start, m.end, p.dir / "mix.wav")
            mono = audio_ops.cut(self.source.audio, m.start, m.end, p.dir / "mono.wav", mono=True, rate=audio_ops.ANIMATION_RATE)
            voices: dict[int, Path] = {}
            for r in p.renders:
                if self.layout == "single":
                    r.audio = mono
                    continue
                if not r.paid:
                    continue
                if r.host_index not in voices:
                    voices[r.host_index] = audio_ops.isolate(
                        mono, p.host_intervals[r.host_index], p.dir / f"{slugify(r.host.name)}_voice.wav"
                    )
                if self.layout == "split":
                    r.audio = voices[r.host_index]
                else:
                    r.audio = audio_ops.condense(voices[r.host_index], r.spans, p.dir / f"{r.stem}_render.wav", pad=SETTLE_PAD)

    def _prepare_images(self) -> dict[str, Path | None]:
        """Cropped image per host; None where a paid avatar still has to be generated."""
        pane = compose.layout_panes(self.layout, len(self.opts.hosts))
        images: dict[str, Path | None] = {}
        for host, p in zip(self.opts.hosts, pane):
            if host.photo and not host.photo.exists():
                raise PipelineError(f"photo not found for {host.name}: {host.photo}")
            source = self.avatars.cached(host)
            if source is None and self.opts.preview:
                source = host.photo or avatar_ops.placeholder(host.name, self.episode_dir / "photos" / f"{slugify(host.name)}_placeholder.png")
                self.console.print(f"[dim]{host.name}: cartoon avatar not made yet; previewing with {'the photo' if host.photo else 'a placeholder'}.[/]")
            images[host.name] = self._crop(host, source, p) if source else None
        return images

    def _crop(self, host: Host, source: Path, pane: compose.Pane) -> Path:
        dest = self.episode_dir / "photos" / f"{slugify(host.name)}_{self.layout}.jpg"
        return compose.crop_photo(source, dest, pane.aspect)

    def _confirm_spend(self, images: dict[str, Path | None]) -> None:
        mode = self.opts.generation_mode
        pending = [
            (p, r) for p in self.picked for r in p.renders
            if r.paid and not (images[r.host.name] and self._cached_render(p, r, images[r.host.name]))
        ]
        avatars = [h for h, img in images.items() if img is None]
        credits = sum(estimate_talking_photo_credits(r.seconds, mode) for _, r in pending)
        credits += avatar_ops.AVATAR_CREDITS_ESTIMATE * len(avatars)
        reused = sum(p.paid_jobs() for p in self.picked) - len(pending)
        if reused:
            self.console.print(f"Reusing {reused} render(s) from a previous run (no charge).")
        if not pending and not avatars:
            return
        if not self.settings.magic_hour_api_key:
            raise PipelineError("MAGIC_HOUR_API_KEY is not set (or use --preview / --dry-run)")
        if self.balance is None:
            try:
                with MagicHour(self.settings.magic_hour_api_key) as mh:
                    self.balance = mh.account().get("credits")
            except MagicHourError as exc:
                raise PipelineError(f"could not reach Magic Hour: {exc}") from exc
        parts = [f"{len(pending)} Talking Photo job(s)"]
        if avatars:
            parts.append(f"{len(avatars)} cartoon avatar(s), made once and reused ({', '.join(avatars)})")
        message = f"About to run {' + '.join(parts)}: ~{credits:,} credits (~${self._usd(credits):,.2f} on {self.pricing.name})."
        if self.balance is not None:
            message += f" Balance: {self.balance:,} credits."
        self.console.print(message)
        if self.opts.max_credits is not None and credits > self.opts.max_credits:
            raise PipelineError(
                f"this run needs ~{credits:,} credits, over the budget of {self.opts.max_credits:,} (--max-credits). "
                "Pick fewer or shorter clips, or raise the budget."
            )
        if self.balance is not None and self.balance < credits:
            raise PipelineError(
                f"not enough credits: need ~{credits:,}, have {self.balance:,}. "
                "Pick fewer clips (--pick), shorten --max-len, or use --generation-mode prompted."
            )
        if not self.opts.yes:
            if not self.opts.interactive:
                raise PipelineError("refusing to spend credits non-interactively without --yes")
            if not Confirm.ask("Spend these credits?", default=True, console=self.console):
                raise PipelineError("cancelled")

    def _make_avatars(self, images: dict[str, Path | None]) -> dict[str, Path]:
        missing = [h for h in self.opts.hosts if images[h.name] is None]
        if not missing:
            return images
        panes = compose.layout_panes(self.layout, len(self.opts.hosts))
        with MagicHour(self.settings.magic_hour_api_key, ledger=self.ledger) as mh:
            for host in missing:
                with self.console.status(f"Making a cartoon avatar for {host.name}..."):
                    avatar = self.avatars.generate(host, mh)
                images[host.name] = self._crop(host, avatar, panes[self.opts.hosts.index(host)])
                self.console.print(f"  [green]✓[/] avatar for {host.name}: {avatar}")
        return images

    def _check_faces(self, images: dict[str, Path]) -> None:
        """Free preflight: fail before spending credits if Magic Hour cannot find a face."""
        with MagicHour(self.settings.magic_hour_api_key) as mh:
            for name, image in images.items():
                try:
                    faces = avatar_ops.check_face(image, mh, self.settings.cache_dir)
                except MagicHourError as exc:
                    self.console.print(f"[yellow]Face check skipped for {name}: {exc}[/]")
                    continue
                if faces == 0:
                    raise PipelineError(
                        f"Magic Hour cannot find a face in {name}'s image ({image}). "
                        "Use a clear, front-facing photo (or a different --avatar-prompt)."
                    )

    def _animate(self, images: dict[str, Path]) -> None:
        renders = [(p, r) for p in self.picked for r in p.renders]
        paid = sum(1 for _, r in renders if r.paid)
        workers = self.opts.concurrency or self.pricing.concurrency or 3
        what = "still previews" if self.opts.preview else f"Talking Photo job(s) ({workers} at a time)"
        self.console.print(f"Animating: {paid} {what}")
        mh = None if self.opts.preview else MagicHour(self.settings.magic_hour_api_key, ledger=self.ledger)
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(self._animate_one, mh, p, r, images[r.host.name]): r for p, r in renders}
                for future in as_completed(futures):
                    render = futures[future]
                    try:
                        future.result()
                    except Exception as exc:  # one failed job must not sink the other clips
                        render.status, render.error = "error", str(exc)
                        self.console.print(f"  [red]✗ {render.label}: {exc}[/]")
        finally:
            if mh:
                mh.close()

    def _render_signature(self, render: Render, photo: Path) -> dict[str, Any]:
        return {
            "audio": file_sha256(render.audio),
            "photo": file_sha256(photo),
            "mode": self.opts.generation_mode,
            "max_resolution": self.opts.max_resolution,
        }

    def _cached_render(self, plan: ClipPlan, render: Render, photo: Path) -> dict[str, Any] | None:
        """Metadata of a previous render made from identical inputs, if its video is still on disk."""
        dest = plan.dir / f"{render.stem}.mp4"
        meta_path = dest.with_suffix(".json")
        if not (render.audio and dest.exists() and meta_path.exists()):
            return None
        meta = json.loads(meta_path.read_text())
        return meta if meta.get("signature") == self._render_signature(render, photo) else None

    def _animate_one(self, mh: MagicHour | None, plan: ClipPlan, render: Render, photo: Path) -> None:
        if not render.paid or self.opts.preview:
            dest = plan.dir / f"{render.stem}.preview.mp4"
            compose.still_video(photo, render.seconds, dest)
            render.video, render.status = dest, "still"
            return

        dest = plan.dir / f"{render.stem}.mp4"
        audio_seconds = audio_ops.wav_duration(render.audio)
        meta = self._cached_render(plan, render, photo)
        if meta:
            render.video, render.status, render.job_id = dest, "cached", meta.get("job_id")
            self.ledger.add(
                LedgerEntry(
                    provider="magichour", kind="talking_photo", label=render.label, job_id=render.job_id,
                    status="cached", credits=0, seconds=round(audio_seconds, 3),
                    meta={"reused_from_previous_run": True, "original_credits": meta.get("credits")},
                )
            )
            self.console.print(f"  [dim]↺ {render.label}: reused previous render[/]")
            return

        def on_status(status: str, project: dict[str, Any]) -> None:
            if status not in ("complete", "error", "canceled"):
                self.console.print(f"  [dim]… {render.label}: {status}[/]")

        result = mh.run_talking_photo(
            image=photo,
            audio=render.audio,
            start_seconds=0.0,
            end_seconds=round(audio_seconds - 0.001, 3),
            name=f"pod2video {self.source.episode_title[:40]} - {render.label}",
            generation_mode=self.opts.generation_mode,
            max_resolution=self.opts.max_resolution,
            label=render.label,
            dest=dest,
            on_status=on_status,
        )
        render.video, render.status, render.job_id, render.credits = dest, "complete", result.id, result.credits_charged
        render.elapsed = result.elapsed_seconds
        dest.with_suffix(".json").write_text(
            json.dumps({"signature": self._render_signature(render, photo), "job_id": result.id, "credits": result.credits_charged}, indent=2)
        )
        self.console.print(f"  [green]✓ {render.label}[/]: {result.credits_charged:,} credits, {result.elapsed_seconds:.0f}s")

    def _compose(self) -> None:
        panes = compose.layout_panes(self.layout, len(self.opts.hosts))
        if not compose.supports_captions():
            raise PipelineError(
                f"{compose.ffmpeg_bin()} has no libass, so captions cannot be burned in. "
                "Install an ffmpeg with libass (macOS: brew install ffmpeg-full) or set FFMPEG_BIN."
            )
        for p in self.picked:
            failed = [r for r in p.renders if r.status == "error"]
            if failed:
                p.status, p.error = "error", "; ".join(f"{r.label}: {r.error}" for r in failed)
                continue
            placements = self._placements(p, panes)
            backdrops = None
            if self.layout in compose.CARD_LAYOUTS:
                made = {
                    h.name: compose.make_backdrop(self.images[h.name], self.episode_dir / "photos" / f"{slugify(h.name)}_backdrop.jpg")
                    for h in self.opts.hosts
                }
                backdrops = [made[r.host.name] for r in p.renders]
            overlay = compose.Overlay(
                duration=p.moment.duration,
                layout=self.layout,
                panes=panes,
                host_names=[h.name for h in self.opts.hosts],
                words=p.words,
                active=p.on_screen if self.layout == "speaker-cut" else p.host_intervals,
                podcast_name=self.source.podcast_name,
                episode_title=self.source.episode_title,
                hook=_hook_text(p.moment),
                has_logo=self.source.logo is not None,
            )
            tag = "_preview" if self.opts.preview else ""
            dest = self.episode_dir / f"clip_{p.index:02d}_{slugify(p.moment.title, 40)}{tag}.mp4"
            try:
                compose.render_clip(
                    renders=[r.video for r in p.renders],
                    placements=placements,
                    mix_audio=p.mix,
                    overlay=overlay,
                    style=self.style,
                    dest=dest,
                    logo=self.source.logo,
                    backdrops=backdrops,
                )
            except compose.FFmpegError as exc:
                p.status, p.error = "error", str(exc)
                self.console.print(f"  [red]✗ {p.label}: compose failed[/]\n{exc}")
                continue
            p.output, p.status = dest, "complete"
            self.console.print(f"  [green]✓[/] {dest}")

    def _placements(self, plan: ClipPlan, panes: list[compose.Pane]) -> list[compose.Placement]:
        """Shots in time order. Every other appearance of a host is punched in tighter, like a
        second camera; a single host gets virtual cuts at pauses so long takes still feel edited."""
        shots = []
        for n, r in enumerate(plan.renders):
            for (a, b), off in zip(r.spans, r.offsets):
                pieces = _cuts_at_pauses(plan.words, a, b) if self.layout == "single" else [(a, b)]
                shots += [(pa, pb, n, off + (pa - a), r.host_index) for pa, pb in pieces]
        seen: dict[int, int] = {}
        placements = []
        for a, b, n, off, host in sorted(shots):
            k = seen.get(host, 0)
            seen[host] = k + 1
            placements.append(
                compose.Placement(render=n, pane=panes[host], start=a, end=b, offset=off, zoom=PUNCH_IN if k % 2 else 1.0)
            )
        return placements

    # -- report -----------------------------------------------------------------------

    def _report(self, started: float) -> dict[str, Any]:
        ledger = self.ledger
        mode = self.opts.generation_mode
        clips = []
        for p in self.plans:
            credits = ledger.magichour_credits(label_prefix=f"{p.label} /")
            clips.append(
                {
                    "index": p.index,
                    "picked": p.picked,
                    "title": p.moment.title,
                    "reason": p.moment.reason,
                    "hook": p.moment.hook,
                    "start": p.moment.start,
                    "end": p.moment.end,
                    "duration": round(p.moment.duration, 2),
                    "status": p.status if p.picked and not self.opts.dry_run else ("planned" if p.picked else "not picked"),
                    "file": str(p.output) if p.output else None,
                    "error": p.error,
                    "credits_estimate": p.credits_estimate(mode),
                    "credits": credits,
                    "usd": round(self._usd(credits), 4),
                    "renders": [
                        {"host": r.host.name, "label": r.label, "seconds": r.seconds, "spans": r.spans, "paid": r.paid,
                         "status": r.status, "job_id": r.job_id, "credits": r.credits, "render_seconds": r.elapsed, "error": r.error}
                        for r in p.renders
                    ],
                }
            )
        estimate = self._picked_estimate()
        actual = ledger.magichour_credits()
        basis = estimate if self.opts.dry_run or self.opts.preview else actual
        report = {
            "podcast": self.source.podcast_name if self.source else None,
            "episode": self.source.episode_title if self.source else None,
            "source": self.source.origin if self.source else None,
            "layout": self.layout,
            "style": self.opts.style,
            "generation_mode": mode,
            "max_resolution": self.opts.max_resolution,
            "dry_run": self.opts.dry_run,
            "preview": self.opts.preview,
            "plan": {"key": self.pricing.key, "name": self.pricing.name, "credits_per_usd": round(self.pricing.credits_per_usd, 2), "source": self.pricing_source},
            "estimate": {"credits": estimate, "usd": round(self._usd(estimate), 4)},
            "cost_by_plan": {
                plan.key: round(basis / plan.credits_per_usd, 4) for plan in PLANS.values() if plan.credits_per_usd
            },
            "clips": clips,
            "wall_seconds": round(time.monotonic() - started, 1),
            **ledger.to_dict(),
        }
        if self.episode_dir:
            (self.episode_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))
        self._print_summary(report)
        return report

    def _print_summary(self, report: dict[str, Any]) -> None:
        if report["dry_run"]:
            t = report["totals"]
            spent = [f"{name} ${usd:,.2f}" for name, usd in (("LLM", t["llm_usd"]), ("transcription", t["transcription_usd"])) if usd]
            self.console.print(("Spent so far: " + ", ".join(spent) + ". " if spent else "") + f"Report: {self.episode_dir / 'report.json'}")
            return
        rendered = [c for c in report["clips"] if c["picked"]]
        table = Table(title="Results")
        for col, justify in (("#", "right"), ("Title", "left"), ("Length", "right"), ("MH credits", "right"), ("≈ $", "right"), ("Status", "left")):
            table.add_column(col, justify=justify)
        for c in rendered:
            status = c["status"]
            colour = {"complete": "green", "error": "red"}.get(status, "white")
            table.add_row(str(c["index"]), c["title"], f"{c['duration']:.1f}s", f"{c['credits']:,}", f"{c['usd']:.2f}", f"[{colour}]{status}[/]")
        self.console.print(table)

        t = report["totals"]

        def money(v: float | None) -> str:
            return "n/a" if v is None else f"${v:,.4f}" if 0 < v < 0.01 else f"${v:,.2f}"

        plan = report["plan"]
        lines = [
            f"Magic Hour: {t['magichour_credits']:,} credits ≈ {money(t['magichour_usd'])} on {plan['name']} "
            f"({plan['credits_per_usd']:,.0f} credits/$, {plan['source']})",
            f"LLM: {money(t['llm_usd'])}   Transcription: {money(t['transcription_usd'])}   Total: {money(t['total_usd'])}",
            "Time: " + ", ".join(f"{k} {v:.0f}s" for k, v in report["stage_seconds"].items()) + f" (wall {report['wall_seconds']:.0f}s)",
        ]
        if t["magichour_credits"]:
            cheapest = sorted(
                ((PLANS[k].name, usd) for k, usd in report["cost_by_plan"].items() if PLANS[k].self_serve and k != plan["key"]),
                key=lambda kv: kv[1],
            )[:2]
            lines.append("Same run on other plans: " + ", ".join(f"{name} {money(usd)}" for name, usd in cheapest))
        failures = [e for e in report["entries"] if e["status"] in ("error", "canceled", "timeout")]
        for c in rendered:
            if c["error"]:
                lines.append(f"[red]Clip {c['index']} failed: {c['error'][:300]}[/]")
        if failures:
            lines.append(f"[red]{len(failures)} failed API call(s); see ledger.jsonl (Magic Hour refunds failed jobs).[/]")
        if self.episode_dir:
            lines.append(f"Report: {self.episode_dir / 'report.json'}")
        for line in lines:
            self.console.print(line)


def _clock(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _hook_text(moment: Moment) -> str:
    """The clip title as an on-screen hook, unless it is a placeholder like "Clip 2"."""
    return "" if re.fullmatch(r"(Clip|Segment) \d+", moment.title) else moment.title


def _cuts_at_pauses(words: list[Word], start: float, end: float) -> list[tuple[float, float]]:
    """Split [start, end] at pauses between words, roughly every SINGLE_SHOT seconds."""
    cuts, last = [start], start
    for prev, nxt in zip(words, words[1:]):
        middle = (prev.end + nxt.start) / 2
        if nxt.start - prev.end >= 0.2 and middle - last >= SINGLE_SHOT and end - middle >= 3.0 and start < middle < end:
            cuts.append(middle)
            last = middle
    return [(round(a, 3), round(b, 3)) for a, b in zip(cuts, cuts[1:] + [end])]
