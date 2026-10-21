"""An in-memory GitHub for the release tooling tests (P23-22, P23-23).

Implements ``scripts.release.github.GitHubClient``: releases (drafts included) with assets,
workflow dispatches and runs. ``fail_once(op)`` makes the next call of that operation raise, so a
test can break the controller at any step and run it again. Every mutating call is appended to
``calls`` so tests can check order and count.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


class InjectedFailure(RuntimeError):
    pass


@dataclass
class FakeGitHub:
    repository: str = "owner/indus-agent"
    default: str = "main"
    releases: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    assets: dict[int, dict[str, bytes]] = field(default_factory=dict[int, dict[str, bytes]])
    runs: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    dispatches: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    calls: list[str] = field(default_factory=list[str])
    asset_writes: int = 0
    _next_id: int = 100
    _failures: dict[str, int] = field(default_factory=dict[str, int])
    published_clock: int = 0

    # -- failure injection -------------------------------------------------------------------------

    def fail_once(self, op: str) -> None:
        self._failures[op] = self._failures.get(op, 0) + 1

    def _gate(self, op: str) -> None:
        if self._failures.get(op):
            self._failures[op] -= 1
            raise InjectedFailure(f"injected failure in {op}")

    # -- reads -------------------------------------------------------------------------------------

    def default_branch(self) -> str:
        return self.default

    def list_releases(self) -> list[dict[str, Any]]:
        self._gate("list_releases")
        return [copy.deepcopy(r) for r in self.releases]

    def release_by_tag(self, tag: str) -> dict[str, Any] | None:
        for row in self.releases:
            if row["tag_name"] == tag:
                return copy.deepcopy(row)
        return None

    def release(self, release_id: int) -> dict[str, Any] | None:
        for row in self.releases:
            if row["id"] == release_id:
                return copy.deepcopy(row)
        return None

    def workflow_runs(self, workflow: str) -> list[dict[str, Any]]:
        return [copy.deepcopy(r) for r in self.runs if r.get("workflow") == workflow]

    # -- release writes ----------------------------------------------------------------------------

    def _row(self, release_id: int) -> dict[str, Any]:
        for row in self.releases:
            if row["id"] == release_id:
                return row
        raise KeyError(release_id)

    def create_release(
        self,
        *,
        tag: str,
        name: str,
        body: str,
        draft: bool,
        prerelease: bool,
        target: str,
    ) -> dict[str, Any]:
        self._gate("create_release")
        self.calls.append(f"create_release:{tag}")
        row = {
            "id": self._next_id,
            "tag_name": tag,
            "name": name,
            "body": body,
            "draft": draft,
            "prerelease": prerelease,
            "target_commitish": target,
            "published_at": None,
        }
        self._next_id += 1
        self.releases.append(row)
        self.assets[row["id"]] = {}
        return copy.deepcopy(row)

    def update_release(self, release_id: int, **fields: Any) -> dict[str, Any]:
        op = "publish_release" if fields.get("draft") is False else "update_release"
        self._gate(op)
        row = self._row(release_id)
        if row["draft"] is False and row.get("published_at"):
            raise RuntimeError("published releases are immutable")
        self.calls.append(f"{op}:{release_id}")
        row.update(fields)
        if fields.get("draft") is False:
            self.published_clock += 1
            row["published_at"] = f"2026-10-21T07:00:{self.published_clock:02d}Z"
        return copy.deepcopy(row)

    def delete_release(self, release_id: int) -> None:
        self._gate("delete_release")
        self.calls.append(f"delete_release:{release_id}")
        self.releases = [r for r in self.releases if r["id"] != release_id]
        self.assets.pop(release_id, None)

    # -- assets ------------------------------------------------------------------------------------

    def _release_for(self, tag: str, *, create: bool) -> dict[str, Any] | None:
        for row in self.releases:
            if row["tag_name"] == tag:
                return row
        if not create:
            return None
        made = self.create_release(
            tag=tag, name=tag, body="", draft=False, prerelease=True, target=self.default
        )
        return self._row(made["id"])

    def read_asset(self, release_tag: str, name: str) -> bytes | None:
        row = self._release_for(release_tag, create=False)
        if row is None:
            return None
        return self.assets.get(row["id"], {}).get(name)

    def replace_asset(self, release_tag: str, name: str, data: bytes) -> None:
        self._gate("replace_asset")
        row = self._release_for(release_tag, create=True)
        assert row is not None
        self.calls.append(f"replace_asset:{release_tag}/{name}")
        self.asset_writes += 1
        self.assets.setdefault(row["id"], {})[name] = data

    def upload_asset(self, release_id: int, name: str, data: bytes) -> None:
        self._gate("upload_asset")
        self.calls.append(f"upload_asset:{release_id}/{name}")
        current = self.assets.setdefault(release_id, {})
        if name in current:
            raise RuntimeError(f"asset {name} already exists")
        current[name] = data

    def asset_names(self, release_id: int) -> list[str]:
        return sorted(self.assets.get(release_id, {}))

    def read_release_asset(self, release_id: int, name: str) -> bytes | None:
        return self.assets.get(release_id, {}).get(name)

    # -- workflows ---------------------------------------------------------------------------------

    def dispatch_workflow(self, workflow: str, ref: str, inputs: dict[str, str]) -> None:
        self._gate("dispatch_workflow")
        self.calls.append(f"dispatch:{workflow}@{ref}")
        self.dispatches.append({"workflow": workflow, "ref": ref, "inputs": dict(inputs)})

    def add_run(self, workflow: str, attempt_ref: str, status: str, conclusion: str | None) -> None:
        self.runs.append(
            {
                "workflow": workflow,
                "name": f"Release {attempt_ref}",
                "status": status,
                "conclusion": conclusion,
            }
        )
