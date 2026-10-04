"""RSS feed parsing, episode selection, and cached audio downloads."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import httpx

USER_AGENT = "pod2video/0.1"


class FeedError(RuntimeError):
    pass


@dataclass
class Episode:
    podcast_title: str
    title: str
    guid: str
    audio_url: str
    published: float | None  # unix time
    podcast_image: str | None
    feed_url: str


def _http() -> httpx.Client:
    return httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0), headers={"User-Agent": USER_AGENT})


def load_feed(url: str) -> list[Episode]:
    """Fetch a feed; episodes newest first."""
    with _http() as client:
        response = client.get(url)
        response.raise_for_status()
    return parse_feed(response.content, url)


def parse_feed(content: bytes | str, url: str) -> list[Episode]:
    parsed = feedparser.parse(content)
    if parsed.bozo and not parsed.entries:
        raise FeedError(f"could not parse feed {url}: {parsed.bozo_exception}")
    feed = parsed.feed
    podcast_title = feed.get("title", "Podcast")
    image = (feed.get("image") or {}).get("href") or (feed.get("itunes_image") or {}).get("href")

    episodes = []
    for entry in parsed.entries:
        audio = next(
            (e.get("href") for e in entry.get("enclosures", []) if str(e.get("type", "")).startswith("audio") or e.get("href", "").lower().endswith((".mp3", ".m4a", ".wav", ".aac", ".ogg"))),
            None,
        )
        if not audio:
            continue
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        episodes.append(
            Episode(
                podcast_title=podcast_title,
                title=entry.get("title", "Untitled episode"),
                guid=entry.get("id") or audio,
                audio_url=audio,
                published=time.mktime(published) if published else None,
                podcast_image=image,
                feed_url=url,
            )
        )
    if not episodes:
        raise FeedError(f"no audio episodes found in {url}")
    episodes.sort(key=lambda e: e.published or 0, reverse=True)
    return episodes


def pick_episode(episodes: list[Episode], selector: str) -> Episode:
    """`latest`, a number (1 = latest, 2 = the one before...), a GUID, or part of the title."""
    selector = selector.strip()
    if selector.lower() == "latest":
        return episodes[0]
    if selector.isdigit():
        index = int(selector)
        if not 1 <= index <= len(episodes):
            raise FeedError(f"episode number must be between 1 and {len(episodes)}")
        return episodes[index - 1]
    for episode in episodes:
        if episode.guid == selector:
            return episode
    matches = [e for e in episodes if selector.lower() in e.title.lower()]
    if not matches:
        raise FeedError(f"no episode matches '{selector}' (try `pod2video episodes --feed ...`)")
    return matches[0]


def download(url: str, cache_dir: Path, *, progress: bool = True) -> Path:
    """Download once into the cache; later runs reuse the file."""
    suffix = Path(urlparse(url).path).suffix.lower() or ".mp3"
    if len(suffix) > 5:
        suffix = ".bin"
    dest = cache_dir / "downloads" / f"{hashlib.sha1(url.encode()).hexdigest()[:16]}{suffix}"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    from rich.progress import BarColumn, DownloadColumn, Progress, TransferSpeedColumn

    with _http() as client, client.stream("GET", url) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length") or 0) or None
        with Progress("[progress.description]{task.description}", BarColumn(), DownloadColumn(), TransferSpeedColumn(), disable=not progress, transient=True) as bar:
            task = bar.add_task("Downloading", total=total)
            with tmp.open("wb") as fh:
                for chunk in response.iter_bytes(1 << 16):
                    fh.write(chunk)
                    bar.advance(task, len(chunk))
    tmp.replace(dest)
    return dest
