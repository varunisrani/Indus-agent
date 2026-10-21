"""The GitHub calls the release tooling makes, behind one protocol (P23-22, P23-23).

``GitHubClient`` is what ``release.py`` and ``publish.py`` use; tests pass the in-memory
``tests/fakes/release/fake_github.FakeGitHub``. ``RestGitHub`` talks to the REST API with the token
from ``GH_TOKEN`` or ``GITHUB_TOKEN`` (names only; the value is never printed or stored). Agents
never construct it: they only run ``--dry-run``, which makes no API call at all.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any, Protocol

__all__ = ["API", "GitHubClient", "GitHubError", "RestGitHub"]

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
TOKEN_ENV = ("GH_TOKEN", "GITHUB_TOKEN")


class GitHubError(RuntimeError):
    pass


class GitHubClient(Protocol):
    def default_branch(self) -> str: ...

    def list_releases(self) -> list[dict[str, Any]]: ...

    def release_by_tag(self, tag: str) -> dict[str, Any] | None: ...

    def release(self, release_id: int) -> dict[str, Any] | None: ...

    def create_release(
        self, *, tag: str, name: str, body: str, draft: bool, prerelease: bool, target: str
    ) -> dict[str, Any]: ...

    def update_release(self, release_id: int, **fields: Any) -> dict[str, Any]: ...

    def delete_release(self, release_id: int) -> None: ...

    def read_asset(self, release_tag: str, name: str) -> bytes | None: ...

    def replace_asset(self, release_tag: str, name: str, data: bytes) -> None: ...

    def upload_asset(self, release_id: int, name: str, data: bytes) -> None: ...

    def asset_names(self, release_id: int) -> list[str]: ...

    def read_release_asset(self, release_id: int, name: str) -> bytes | None: ...

    def dispatch_workflow(self, workflow: str, ref: str, inputs: dict[str, str]) -> None: ...

    def workflow_runs(self, workflow: str) -> list[dict[str, Any]]: ...


class RestGitHub:
    """The REST API for ``owner/name``; drafts are found by listing (get-by-tag hides them)."""

    def __init__(self, repository: str, env: Mapping[str, str], *, timeout: float = 60.0) -> None:
        token = next((env[name] for name in TOKEN_ENV if env.get(name)), "")
        if not token:
            raise GitHubError(f"set {' or '.join(TOKEN_ENV)} to a token that can write releases")
        self.repository = repository
        self._token = token
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"RestGitHub({self.repository!r})"

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: object = None,
        raw: bytes | None = None,
        accept: str = "application/vnd.github+json",
        content_type: str = "application/json",
    ) -> bytes:
        data = raw if raw is not None else None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", accept)
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as answer:  # noqa: S310
                return answer.read()
        except urllib.error.HTTPError as exc:
            raise GitHubError(f"{method} {url.split('?')[0]} answered {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise GitHubError(f"{method} {url.split('?')[0]} failed: {exc.reason}") from exc

    def _json(self, method: str, path: str, body: object = None) -> Any:
        raw = self._request(method, f"{API}/repos/{self.repository}{path}", body=body)
        return json.loads(raw) if raw.strip() else None

    def default_branch(self) -> str:
        return str(self._json("GET", "")["default_branch"])

    def list_releases(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._json("GET", f"/releases?per_page=100&page={page}")
            if not batch:
                return rows
            rows.extend(batch)
            page += 1

    def release_by_tag(self, tag: str) -> dict[str, Any] | None:
        return next((r for r in self.list_releases() if r.get("tag_name") == tag), None)

    def release(self, release_id: int) -> dict[str, Any] | None:
        try:
            return self._json("GET", f"/releases/{release_id}")
        except GitHubError:
            return None

    def create_release(
        self, *, tag: str, name: str, body: str, draft: bool, prerelease: bool, target: str
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/releases",
            {
                "tag_name": tag,
                "name": name,
                "body": body,
                "draft": draft,
                "prerelease": prerelease,
                "target_commitish": target,
                "make_latest": "false",
            },
        )

    def update_release(self, release_id: int, **fields: Any) -> dict[str, Any]:
        return self._json("PATCH", f"/releases/{release_id}", fields)

    def delete_release(self, release_id: int) -> None:
        self._json("DELETE", f"/releases/{release_id}")

    def _assets(self, release_id: int) -> list[dict[str, Any]]:
        return list(self._json("GET", f"/releases/{release_id}/assets?per_page=100") or [])

    def asset_names(self, release_id: int) -> list[str]:
        return sorted(str(a["name"]) for a in self._assets(release_id))

    def read_release_asset(self, release_id: int, name: str) -> bytes | None:
        for asset in self._assets(release_id):
            if asset.get("name") == name:
                url = f"{API}/repos/{self.repository}/releases/assets/{asset['id']}"
                return self._request("GET", url, accept="application/octet-stream")
        return None

    def read_asset(self, release_tag: str, name: str) -> bytes | None:
        row = self.release_by_tag(release_tag)
        return None if row is None else self.read_release_asset(int(row["id"]), name)

    def replace_asset(self, release_tag: str, name: str, data: bytes) -> None:
        row = self.release_by_tag(release_tag)
        if row is None:
            row = self.create_release(
                tag=release_tag, name=release_tag, body="Channel records (machine-written).",
                draft=False, prerelease=True, target=self.default_branch(),
            )  # fmt: skip
        release_id = int(row["id"])
        for asset in self._assets(release_id):
            if asset.get("name") == name:
                self._json("DELETE", f"/releases/assets/{asset['id']}")
        self.upload_asset(release_id, name, data)

    def upload_asset(self, release_id: int, name: str, data: bytes) -> None:
        query = urllib.parse.urlencode({"name": name})
        url = f"{UPLOADS}/repos/{self.repository}/releases/{release_id}/assets?{query}"
        self._request("POST", url, raw=data, content_type="application/octet-stream")

    def dispatch_workflow(self, workflow: str, ref: str, inputs: dict[str, str]) -> None:
        self._json(
            "POST", f"/actions/workflows/{workflow}/dispatches", {"ref": ref, "inputs": inputs}
        )

    def workflow_runs(self, workflow: str) -> list[dict[str, Any]]:
        data = self._json("GET", f"/actions/workflows/{workflow}/runs?per_page=100") or {}
        return [
            {
                "workflow": workflow,
                "name": run.get("display_title") or run.get("name"),
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
            }
            for run in data.get("workflow_runs", [])
        ]
