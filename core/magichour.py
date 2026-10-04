"""Thin Magic Hour REST client: uploads, Talking Photo, polling, downloads, credit logging.

Endpoints (https://docs.magichour.ai/api-reference/openapi.json):
  POST /v1/files/upload-urls     -> pre-signed PUT urls + `file_path` to reference in jobs
  POST /v1/ai-talking-photo      -> {id, credits_charged (estimate)}
  GET  /v1/video-projects/{id}   -> status queued|rendering|complete|error|canceled, downloads[]
  POST /v1/ai-voice-generator    -> {id, credits_charged}
  GET  /v1/audio-projects/{id}
  GET  /v1/account               -> credit balance

Credits: `credits_charged` on create is an estimate. The final value comes from the
get-details response once the job is terminal (failed jobs are refunded, and the
field is updated to reflect that).
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from core.costs import CostLedger, LedgerEntry

API_BASE = "https://api.magichour.ai"
TERMINAL_STATUSES = {"complete", "error", "canceled"}

# Talking Photo limits and pricing (docs.magichour.ai/api-reference/models, checked 2026-10).
TALKING_PHOTO_MAX_SECONDS = {"realistic": 300.0, "prompted": 45.0}
TALKING_PHOTO_CREDITS_PER_SECOND = {"realistic": 48, "prompted": 20}

_ASSET_TYPES = {
    "video": {"mp4", "m4v", "mov", "webm"},
    "audio": {"mp3", "wav", "aac", "flac", "webm", "weba", "m4a", "opus", "ogg", "oga", "aiff", "amr"},
    "image": {"png", "jpg", "jpeg", "jfif", "heic", "heif", "webp", "avif", "jp2", "tiff", "tif", "bmp"},
}


class MagicHourError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class MagicHourJobFailed(MagicHourError):
    def __init__(self, job_id: str, status: str, code: str | None, message: str):
        super().__init__(f"job {job_id} ended with status '{status}': {message}", code=code)
        self.job_id = job_id
        self.status = status


@dataclass
class JobResult:
    id: str
    status: str
    credits_charged: int | None
    output_path: Path | None = None
    downloads: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


def estimate_talking_photo_credits(seconds: float, generation_mode: str = "realistic") -> int:
    """Upper-bound estimate. Magic Hour bills on output duration (final value may differ slightly)."""
    return math.ceil(seconds) * TALKING_PHOTO_CREDITS_PER_SECOND[generation_mode]


def _asset_type(extension: str) -> str:
    for kind, extensions in _ASSET_TYPES.items():
        if extension in extensions:
            return kind
    raise MagicHourError(f"unsupported file extension for upload: .{extension}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class MagicHour:
    def __init__(
        self,
        api_key: str,
        *,
        ledger: CostLedger | None = None,
        base_url: str = API_BASE,
        timeout: float = 60.0,
        poll_interval: float = 5.0,
        max_wait: float = 30 * 60,
        transport: httpx.BaseTransport | None = None,
    ):
        if not api_key:
            raise MagicHourError("MAGIC_HOUR_API_KEY is not set")
        self.ledger = ledger or CostLedger()
        self.poll_interval = poll_interval
        self.max_wait = max_wait
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )
        # Pre-signed storage URLs must not receive the API key.
        self._storage = httpx.Client(timeout=httpx.Timeout(timeout, write=300.0), transport=transport)
        self._uploads: dict[str, str] = {}
        self._upload_lock = threading.Lock()
        self._entries: dict[str, LedgerEntry] = {}

    def close(self) -> None:
        self._http.close()
        self._storage.close()

    def __enter__(self) -> "MagicHour":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- HTTP -----------------------------------------------------------------

    def _request(self, method: str, path: str, *, retries: int = 0, **kwargs: Any) -> dict[str, Any]:
        """Send a request. `retries` applies to 429/5xx/network errors (use 0 for paid creates)."""
        attempt = 0
        while True:
            try:
                response = self._http.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if attempt < retries:
                    attempt += 1
                    time.sleep(min(2**attempt, 30))
                    continue
                raise MagicHourError(f"{method} {path} failed: {exc}") from exc
            # A 429 is rejected before any work starts, so it is safe to retry even for paid creates.
            rate_limited = response.status_code == 429 and attempt < max(retries, 3)
            server_error = response.status_code >= 500 and attempt < retries
            if rate_limited or server_error:
                attempt += 1
                delay = float(response.headers.get("retry-after", min(2**attempt, 30)))
                time.sleep(delay)
                continue
            if response.is_error:
                try:
                    body = response.json()
                except ValueError:
                    body = {"message": response.text[:500]}
                raise MagicHourError(
                    f"{method} {path} -> HTTP {response.status_code}: {body.get('message', body)}",
                    status_code=response.status_code,
                    code=body.get("code"),
                )
            return response.json()

    # -- Account & uploads ----------------------------------------------------

    def account(self) -> dict[str, Any]:
        return self._request("GET", "/v1/account", retries=2)

    def upload(self, path: str | Path) -> str:
        """Upload a local file and return the `file_path` Magic Hour jobs accept. URLs pass through."""
        if isinstance(path, str) and path.startswith(("http://", "https://")):
            return path
        path = Path(path)
        digest = _sha256(path)
        with self._upload_lock:
            if digest in self._uploads:
                return self._uploads[digest]
        extension = path.suffix.lower().lstrip(".")
        payload = {"items": [{"type": _asset_type(extension), "extension": extension}]}
        item = self._request("POST", "/v1/files/upload-urls", json=payload, retries=2)["items"][0]
        with path.open("rb") as fh:
            put = self._storage.put(item["upload_url"], content=fh)
        if put.is_error:
            raise MagicHourError(f"upload of {path.name} failed: HTTP {put.status_code} {put.text[:200]}")
        with self._upload_lock:
            self._uploads[digest] = item["file_path"]
        return item["file_path"]

    # -- Jobs -----------------------------------------------------------------

    def talking_photo(
        self,
        *,
        image: str | Path,
        audio: str | Path,
        start_seconds: float,
        end_seconds: float,
        name: str | None = None,
        generation_mode: str = "realistic",
        prompt: str | None = None,
        max_resolution: int | None = None,
        label: str = "talking_photo",
    ) -> str:
        """Create a Talking Photo job and return its id. Logs the estimated credits immediately."""
        duration = end_seconds - start_seconds
        limit = TALKING_PHOTO_MAX_SECONDS.get(generation_mode)
        if duration <= 0:
            raise MagicHourError(f"end_seconds must be greater than start_seconds (got {start_seconds}-{end_seconds})")
        if limit and duration > limit:
            raise MagicHourError(f"{generation_mode} Talking Photo jobs are limited to {limit:.0f}s (got {duration:.1f}s)")

        body: dict[str, Any] = {
            "name": name or label,
            "start_seconds": round(start_seconds, 3),
            "end_seconds": round(end_seconds, 3),
            "assets": {"image_file_path": self.upload(image), "audio_file_path": self.upload(audio)},
            "style": {"generation_mode": generation_mode},
        }
        if prompt and generation_mode == "prompted":
            body["style"]["prompt"] = prompt
        if max_resolution:
            body["max_resolution"] = int(max_resolution)

        return self._create(
            "/v1/ai-talking-photo",
            body,
            kind="talking_photo",
            label=label,
            seconds=round(duration, 3),
            meta={"generation_mode": generation_mode, "max_resolution": max_resolution},
        )

    def _create(
        self,
        path: str,
        body: dict[str, Any],
        *,
        kind: str,
        label: str,
        seconds: float | None = None,
        meta: dict[str, Any] | None = None,
    ) -> str:
        """POST a paid job (never retried except on 429) and log its estimated credits."""
        created = self._request("POST", path, json=body)
        entry = self.ledger.add(
            LedgerEntry(
                provider="magichour",
                kind=kind,
                label=label,
                job_id=created["id"],
                credits=created.get("credits_charged"),
                seconds=seconds,
                meta={"credits_estimate": created.get("credits_charged"), **(meta or {})},
            )
        )
        self._entries[created["id"]] = entry
        return created["id"]

    def voice_generate(self, *, text: str, voice_name: str, label: str = "voice_generate") -> str:
        """AI Voice Generator (0.1 credit per character). Not used by pod2video; kept for reuse.

        Returns the audio project id; pass it to `wait(job_id, kind="audio")`.
        """
        if len(text) > 1000:
            raise MagicHourError("voice generator prompts are limited to 1000 characters")
        return self._create(
            "/v1/ai-voice-generator",
            {"name": label, "style": {"prompt": text, "voice_name": voice_name}},
            kind="voice_generate",
            label=label,
            meta={"characters": len(text)},
        )

    def image_edit(
        self,
        *,
        images: list[str | Path],
        prompt: str,
        model: str | None = None,
        aspect_ratio: str = "1:1",
        resolution: str = "1k",
        label: str = "image_edit",
    ) -> str:
        """AI Image Editor. Returns the image project id; pass it to `wait(job_id, kind="image")`."""
        body: dict[str, Any] = {
            "name": label,
            "image_count": 1,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "style": {"prompt": prompt},
            "assets": {"image_file_paths": [self.upload(i) for i in images]},
        }
        if model:
            body["model"] = model
        return self._create("/v1/ai-image-editor", body, kind="image_edit", label=label, meta={"model": model})

    def image_generate(
        self,
        *,
        prompt: str,
        model: str | None = None,
        aspect_ratio: str = "1:1",
        resolution: str = "1k",
        label: str = "image_generate",
    ) -> str:
        """AI Image Generator. Returns the image project id; pass it to `wait(job_id, kind="image")`."""
        body: dict[str, Any] = {
            "name": label,
            "image_count": 1,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "style": {"prompt": prompt},
        }
        if model:
            body["model"] = model
        return self._create("/v1/ai-image-generator", body, kind="image_generate", label=label, meta={"model": model})

    def detect_faces(self, image: str | Path, *, confidence: float = 0.5) -> list[dict[str, Any]]:
        """Faces Magic Hour can see in an image (free at the time of writing). Empty list = none."""
        created = self._request(
            "POST",
            "/v1/face-detection",
            json={"confidence_score": confidence, "assets": {"target_file_path": self.upload(image)}},
        )
        started, delay = time.monotonic(), 1.0
        while True:
            result = self._request("GET", f"/v1/face-detection/{created['id']}", retries=4)
            if result["status"] == "complete":
                return result.get("faces") or []
            if result["status"] == "error":
                raise MagicHourError(f"face detection failed for {Path(str(image)).name}")
            if time.monotonic() - started > 300:
                raise MagicHourError("face detection timed out")
            time.sleep(delay)
            delay = min(delay * 1.5, 5.0)

    def get(self, job_id: str, kind: str = "video") -> dict[str, Any]:
        return self._request("GET", f"/v1/{kind}-projects/{job_id}", retries=4)

    def wait(
        self,
        job_id: str,
        kind: str = "video",
        on_status: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Poll until the job is terminal. Updates the ledger with the final credits either way."""
        started = time.monotonic()
        delay = self.poll_interval
        last_status = None
        while True:
            project = self.get(job_id, kind)
            status = project["status"]
            if status != last_status and on_status:
                on_status(status, project)
            last_status = status
            if status in TERMINAL_STATUSES:
                break
            if time.monotonic() - started > self.max_wait:
                self._record(job_id, project, status="timeout", error=f"gave up after {self.max_wait:.0f}s")
                raise MagicHourJobFailed(job_id, "timeout", None, f"still '{status}' after {self.max_wait:.0f}s")
            time.sleep(delay)
            delay = min(delay * 1.25, 20.0)

        error = project.get("error") or {}
        self._record(job_id, project, status=status, error=error.get("message"))
        if status != "complete":
            raise MagicHourJobFailed(job_id, status, error.get("code"), error.get("message") or status)
        return project

    def _record(self, job_id: str, project: dict[str, Any], *, status: str, error: str | None) -> None:
        entry = self._entries.get(job_id)
        if not entry:
            return
        self.ledger.update(
            entry,
            status=status,
            credits=project.get("credits_charged", entry.credits),
            error=error,
            meta={"fps": project.get("fps"), "width": project.get("width"), "height": project.get("height")},
        )

    def download(self, url: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        with self._storage.stream("GET", url, follow_redirects=True) as response:
            response.raise_for_status()
            with tmp.open("wb") as fh:
                for chunk in response.iter_bytes(1 << 16):
                    fh.write(chunk)
        tmp.replace(dest)
        return dest

    def download_result(self, job_id: str, kind: str, dest: Path) -> Path:
        """Wait for a job and download its first output."""
        project = self.wait(job_id, kind)
        urls = [d["url"] for d in project.get("downloads") or []]
        if not urls:
            raise MagicHourJobFailed(job_id, "complete", None, "job completed without downloads")
        return self.download(urls[0], dest)

    def run_talking_photo(self, *, dest: Path, on_status: Callable[[str, dict[str, Any]], None] | None = None, **kwargs: Any) -> JobResult:
        """Create, wait, and download a Talking Photo job. Raises MagicHourJobFailed on failure."""
        started = time.monotonic()
        job_id = self.talking_photo(**kwargs)
        project = self.wait(job_id, "video", on_status=on_status)
        urls = [d["url"] for d in project.get("downloads") or []]
        if not urls:
            raise MagicHourJobFailed(job_id, "complete", None, "job completed without downloads")
        output = self.download(urls[0], dest)
        return JobResult(
            id=job_id,
            status=project["status"],
            credits_charged=project.get("credits_charged"),
            output_path=output,
            downloads=urls,
            elapsed_seconds=time.monotonic() - started,
            raw=project,
        )
