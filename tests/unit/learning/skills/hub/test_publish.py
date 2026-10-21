"""``indus skills publish`` (P24-45): checks before anything leaves, and the GitHub flow over a
``respx`` fake of the REST API (fork when the token may not push, one commit, a pull request)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from indus.foundation.scope import Scope
from indus.learning.skills.hub.publish import collect_files, prepare, publish_github
from indus.learning.skills.hub.sources import HubError
from tests.unit.learning.skills.hub._web import clean_scope

API = "api.github.com"
DANGEROUS_LINE = "rm -rf " + "/"  # built at run time: no scanner bait in the repo as one line


def _token() -> str:
    return "tok-" + secrets.token_hex(10)


@pytest.fixture
def hub_scope(scope: Scope) -> Scope:
    return clean_scope(scope)


def write_skill(
    root: Path,
    name: str = "pdf-split",
    body: str = "Run scripts/split.sh.\n",
    description: str = "Split PDFs.",
) -> Path:
    folder = root / "skills" / "docs" / name
    (folder / "scripts").mkdir(parents=True)
    front = (
        f"---\nname: {name}\n" + (f"description: {description}\n" if description else "") + "---\n"
    )
    (folder / "SKILL.md").write_text(front + f"\n# {name}\n\n{body}", encoding="utf-8")
    script = folder / "scripts" / "split.sh"
    script.write_text("#!/bin/sh\necho split\n", encoding="utf-8")
    script.chmod(0o755)
    (folder / ".git").mkdir()
    (folder / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return folder


@pytest.fixture
def web() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


# -- checks ---------------------------------------------------------------------------------------


def test_files_come_from_the_skill_folder_only(hub_scope: Scope) -> None:
    folder = write_skill(hub_scope.home)
    files, executable = collect_files(folder)
    assert sorted(files) == ["SKILL.md", "scripts/split.sh"]
    assert executable == frozenset({"scripts/split.sh"})


def test_a_link_escaping_the_folder_refuses_before_any_request(
    hub_scope: Scope, tmp_path: Path, web: respx.MockRouter
) -> None:
    folder = write_skill(hub_scope.home)
    outside = tmp_path / "secret.txt"
    outside.write_text("private\n", encoding="utf-8")
    os.symlink(outside, folder / "scripts" / "notes.txt")
    with pytest.raises(HubError, match="outside the skill folder"):
        prepare(hub_scope, "pdf-split")
    os.unlink(folder / "scripts" / "notes.txt")
    os.symlink(tmp_path, folder / "linked-dir")
    with pytest.raises(HubError, match="outside the skill folder"):
        prepare(hub_scope, str(folder))
    assert web.calls.call_count == 0


def test_a_link_inside_the_folder_is_fine(hub_scope: Scope) -> None:
    folder = write_skill(hub_scope.home)
    os.symlink(folder / "scripts" / "split.sh", folder / "scripts" / "alias.sh")
    plan = prepare(hub_scope, "pdf-split")
    assert plan.files["scripts/alias.sh"] == plan.files["scripts/split.sh"]


def test_dangerous_or_undescribed_skills_are_not_published(
    hub_scope: Scope, tmp_path: Path
) -> None:
    write_skill(hub_scope.home, "wiper", body=f"    {DANGEROUS_LINE}\n")
    with pytest.raises(HubError, match="dangerous"):
        prepare(hub_scope, "wiper")
    write_skill(hub_scope.home, "plain", description="")
    with pytest.raises(HubError, match="description"):
        prepare(hub_scope, "plain")
    with pytest.raises(HubError, match="no skill called"):
        prepare(hub_scope, "missing")


# -- GitHub ---------------------------------------------------------------------------------------


def _github(web: respx.MockRouter, *, push: bool) -> dict[str, Any]:
    seen: dict[str, Any] = {"blobs": [], "tree": None, "pull": None, "ref": None}
    base, tree = "a" * 40, "b" * 40
    web.get(host=API, path="/repos/acme/skills").respond(
        json={"default_branch": "trunk", "permissions": {"push": push}}
    )
    web.post(host=API, path="/repos/acme/skills/forks").respond(
        202, json={"full_name": "me/skills"}
    )
    web.get(host=API, path="/repos/me/skills").respond(json={"full_name": "me/skills"})
    web.get(host=API, path="/repos/acme/skills/git/ref/heads/trunk").respond(
        json={"object": {"sha": base}}
    )
    web.get(host=API, path=f"/repos/acme/skills/git/commits/{base}").respond(
        json={"tree": {"sha": tree}}
    )
    head = "acme/skills" if push else "me/skills"

    def blob(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen["blobs"].append(base64.b64decode(body["content"]))
        return httpx.Response(201, json={"sha": f"{len(seen['blobs']):040d}"})

    def trees(request: httpx.Request) -> httpx.Response:
        seen["tree"] = json.loads(request.content)
        return httpx.Response(201, json={"sha": "c" * 40})

    def refs(request: httpx.Request) -> httpx.Response:
        seen["ref"] = json.loads(request.content)
        return httpx.Response(201, json={})

    def pulls(request: httpx.Request) -> httpx.Response:
        seen["pull"] = json.loads(request.content)
        return httpx.Response(201, json={"html_url": "https://github.com/acme/skills/pull/7"})

    web.post(host=API, path=f"/repos/{head}/git/blobs").mock(side_effect=blob)
    web.post(host=API, path=f"/repos/{head}/git/trees").mock(side_effect=trees)
    web.post(host=API, path=f"/repos/{head}/git/commits").respond(201, json={"sha": "d" * 40})
    web.post(host=API, path=f"/repos/{head}/git/refs").mock(side_effect=refs)
    web.post(host=API, path="/repos/acme/skills/pulls").mock(side_effect=pulls)
    return seen


def _publish(scope: Scope, token: str, **options: Any) -> str:
    plan = prepare(scope, "pdf-split")

    async def go() -> str:
        async with httpx.AsyncClient() as client:
            return await publish_github(plan, "acme/skills", token=token, client=client, **options)

    return asyncio.run(go())


def test_pull_request_from_a_fork(hub_scope: Scope, web: respx.MockRouter) -> None:
    write_skill(hub_scope.home)
    seen = _github(web, push=False)
    token = _token()
    where = _publish(hub_scope, token)
    assert where == "https://github.com/acme/skills/pull/7"
    paths = sorted(e["path"] for e in seen["tree"]["tree"])
    assert paths == ["skills/pdf-split/SKILL.md", "skills/pdf-split/scripts/split.sh"]
    modes = {e["path"]: e["mode"] for e in seen["tree"]["tree"]}
    assert modes["skills/pdf-split/scripts/split.sh"] == "100755"
    assert seen["tree"]["base_tree"] == "b" * 40
    assert seen["ref"]["ref"] == "refs/heads/indus/add-skill-pdf-split"
    assert seen["pull"]["head"] == "me:indus/add-skill-pdf-split"
    assert seen["pull"]["base"] == "trunk"
    for call in web.calls:
        assert token not in str(call.request.url)
        assert call.request.headers["authorization"] == f"Bearer {token}"
        assert call.request.headers["x-github-api-version"] == "2022-11-28"


def test_branch_only_in_the_repo_itself(hub_scope: Scope, web: respx.MockRouter) -> None:
    write_skill(hub_scope.home)
    seen = _github(web, push=True)
    where = _publish(hub_scope, _token(), pr=False, path="catalog", branch="add/pdf")
    assert where == "https://github.com/acme/skills/tree/add/pdf"
    assert seen["pull"] is None
    assert all(e["path"].startswith("catalog/pdf-split/") for e in seen["tree"]["tree"])


def test_refusals_are_clear(hub_scope: Scope, web: respx.MockRouter) -> None:
    write_skill(hub_scope.home)
    web.get(host=API, path="/repos/acme/skills").respond(404, json={"message": "Not Found"})
    with pytest.raises(HubError, match="not found"):
        _publish(hub_scope, _token())
    with pytest.raises(HubError, match="owner/name"):
        asyncio.run(
            publish_github(
                prepare(hub_scope, "pdf-split"), "nope", token="t", client=httpx.AsyncClient()
            )
        )


def test_existing_branch_is_reported(hub_scope: Scope, web: respx.MockRouter) -> None:
    write_skill(hub_scope.home)
    _github(web, push=True)
    web.post(host=API, path="/repos/acme/skills/git/refs").respond(422, json={"message": "exists"})
    with pytest.raises(HubError, match="already exists"):
        _publish(hub_scope, _token())
