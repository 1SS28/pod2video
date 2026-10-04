"""Minimal GitHub REST client for the Actions flow: issues, comments, reactions, release assets.

Uses the workflow's GITHUB_TOKEN (needs `contents: write` for releases, `issues: write`).
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, Iterator

import httpx

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, token: str, repo: str, *, api: str = API, transport: httpx.BaseTransport | None = None):
        if not token or not repo:
            raise GitHubError("GITHUB_TOKEN and GITHUB_REPOSITORY must be set (they are, inside GitHub Actions)")
        self.repo = repo
        self._http = httpx.Client(
            base_url=api,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=httpx.Timeout(60.0, write=600.0),
            follow_redirects=True,  # httpx drops the token when a redirect leaves api.github.com
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, url: str, **kwargs: Any) -> Any:
        response = self._http.request(method, url, **kwargs)
        if response.is_error:
            raise GitHubError(f"{method} {url} -> HTTP {response.status_code}: {response.text[:300]}")
        return response.json() if response.content else None

    # -- issues --------------------------------------------------------------------

    def ensure_label(self, name: str, color: str = "FFD84D", description: str = "") -> None:
        response = self._http.post(f"/repos/{self.repo}/labels", json={"name": name, "color": color, "description": description})
        if response.is_error and response.status_code != 422:  # 422: it already exists
            raise GitHubError(f"could not create label {name}: HTTP {response.status_code}")

    def issues(self, label: str) -> Iterator[dict[str, Any]]:
        page = 1
        while True:
            batch = self._call("GET", f"/repos/{self.repo}/issues", params={"labels": label, "state": "all", "per_page": 100, "page": page})
            for issue in batch:
                if "pull_request" not in issue:
                    yield issue
            if len(batch) < 100:
                return
            page += 1

    def find_issue(self, label: str, marker: str) -> dict[str, Any] | None:
        return next((i for i in self.issues(label) if marker in (i.get("body") or "")), None)

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        return self._call("POST", f"/repos/{self.repo}/issues", json={"title": title, "body": body, "labels": labels})

    def update_issue(self, number: int, **fields: Any) -> dict[str, Any]:
        return self._call("PATCH", f"/repos/{self.repo}/issues/{number}", json=fields)

    def comment(self, number: int, body: str) -> dict[str, Any]:
        return self._call("POST", f"/repos/{self.repo}/issues/{number}/comments", json={"body": body})

    def react(self, comment_id: int, content: str) -> None:
        """Best effort: a reaction is a progress hint, never worth failing a run over."""
        try:
            self._call("POST", f"/repos/{self.repo}/issues/comments/{comment_id}/reactions", json={"content": content})
        except (GitHubError, httpx.HTTPError):
            pass

    # -- releases ------------------------------------------------------------------

    def create_release(self, *, tag: str, name: str, body: str, draft: bool = True) -> dict[str, Any]:
        return self._call("POST", f"/repos/{self.repo}/releases", json={"tag_name": tag, "name": name, "body": body, "draft": draft})

    def get_release(self, release_id: int) -> dict[str, Any]:
        return self._call("GET", f"/repos/{self.repo}/releases/{release_id}")

    def upload_asset(self, release: dict[str, Any], path: Path, *, name: str | None = None) -> dict[str, Any]:
        """Upload a file to a release, replacing an asset of the same name."""
        name = name or path.name
        for asset in release.get("assets", []):
            if asset["name"] == name:
                self._call("DELETE", f"/repos/{self.repo}/releases/assets/{asset['id']}")
        upload_url = release["upload_url"].split("{", 1)[0]
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        with path.open("rb") as fh:
            asset = self._call("POST", upload_url, params={"name": name}, content=fh, headers={"Content-Type": content_type})
        release.setdefault("assets", []).append(asset)
        return asset

    def download_asset(self, release: dict[str, Any], name: str, dest: Path) -> Path:
        asset = next((a for a in release.get("assets", []) if a["name"] == name), None)
        if asset is None:
            raise GitHubError(f"release {release.get('name')} has no asset named {name}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._http.stream(
            "GET", f"/repos/{self.repo}/releases/assets/{asset['id']}", headers={"Accept": "application/octet-stream"}
        ) as response:
            if response.is_error:
                raise GitHubError(f"download of {name} failed: HTTP {response.status_code}")
            with dest.open("wb") as fh:
                for chunk in response.iter_bytes(1 << 16):
                    fh.write(chunk)
        return dest
