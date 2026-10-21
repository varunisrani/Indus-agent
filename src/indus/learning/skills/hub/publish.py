"""``indus skills publish``: share a local skill on GitHub (P24-45).

``prepare`` checks the skill before anything leaves the machine: a ``SKILL.md`` whose frontmatter
has a name and a description; files collected from the skill folder only (a link whose target is
outside the folder refuses the whole publish; ``.git``, caches and hidden files are skipped; at
most ``MAX_FILES`` files and ``MAX_TOTAL_BYTES``); a scan that is not ``dangerous``.

``publish_github`` then uses the person's own GitHub token (sent as a header, never in a URL or a
message) with the REST API pinned to ``2022-11-28``. It makes exactly one commit holding the skill
under ``<path>/<name>/`` on a new branch — in the target repo when the token may push there, else
in the person's fork (created when missing) — through the Git data API (blobs, tree, commit, ref),
and opens a pull request unless ``pr=False``.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import httpx

from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from indus.learning.skills.format import SKILL_FILE
from indus.learning.skills.hub.lock import HUB_DIR
from indus.learning.skills.hub.sources import HubError, frontmatter_of
from indus.safety.content_scan import ScanReport, scan_tree

__all__ = [
    "API_VERSION",
    "GITHUB_API",
    "MAX_FILES",
    "MAX_TOTAL_BYTES",
    "PublishPlan",
    "collect_files",
    "find_skill",
    "prepare",
    "publish_github",
]

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
API_VERSION = "2022-11-28"
MAX_FILES = 200
MAX_TOTAL_BYTES = 10 * 1024 * 1024
FORK_WAIT_TRIES = 10
FORK_WAIT_S = 1.5
_CLOCK = SystemClock()  # waiting for GitHub to finish a fork is real time
_SKIPPED = frozenset({".git", "__pycache__", ".DS_Store", "node_modules", ".venv"})
_REPO = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


@dataclass(frozen=True)
class PublishPlan:
    name: str
    folder: Path
    files: Mapping[str, bytes]
    executable: frozenset[str]
    report: ScanReport


def find_skill(scope: Scope, ref: str) -> Path:
    """A skill folder: a path holding ``SKILL.md``, else the one installed skill named ``ref``."""
    candidate = Path(ref).expanduser()
    if (candidate / SKILL_FILE).is_file():
        return candidate.resolve()
    root = scope.home / "skills"
    found = (
        [
            p.parent
            for p in root.rglob(SKILL_FILE)
            if p.parent.name == ref and HUB_DIR not in p.relative_to(root).parts
        ]
        if root.is_dir()
        else []
    )
    if not found:
        raise HubError(f"no skill called {ref!r} (give its folder, or a name under skills/)")
    if len(found) > 1:
        listed = ", ".join(str(p.relative_to(root)) for p in found)
        raise HubError(f"several skills are called {ref!r}: {listed}; give the folder")
    return found[0].resolve()


def collect_files(folder: Path) -> tuple[dict[str, bytes], frozenset[str]]:
    """Every file of the skill folder (see the module notes) and which are executable."""
    root = folder.resolve()
    files: dict[str, bytes] = {}
    executable: set[str] = set()
    total = 0
    for current, dirs, names in os.walk(root, followlinks=False):
        here = Path(current)
        kept_dirs: list[str] = []
        for name in sorted(dirs):
            path = here / name
            if path.is_symlink():
                _check_link(path, root)
                continue  # a folder link inside the skill: its files are published from there
            if name not in _SKIPPED and not name.startswith("."):
                kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(names):
            if name in _SKIPPED or name.startswith("."):
                continue
            path = here / name
            if path.is_symlink():
                _check_link(path, root)
            rel = path.relative_to(root).as_posix()
            data = path.read_bytes()
            total += len(data)
            if len(files) >= MAX_FILES or total > MAX_TOTAL_BYTES:
                raise HubError(
                    f"the skill has more than {MAX_FILES} files or "
                    f"{MAX_TOTAL_BYTES // (1024 * 1024)} MiB; not published"
                )
            files[rel] = data
            if os.access(path, os.X_OK):
                executable.add(rel)
    return files, frozenset(executable)


def _check_link(path: Path, root: Path) -> None:
    target = path.resolve()
    if target != root and not target.is_relative_to(root):
        raise HubError(
            f"{path.relative_to(root).as_posix()} is a link to a place outside the skill folder; "
            "nothing outside the skill is ever published"
        )


def prepare(scope: Scope, ref: str) -> PublishPlan:
    """The checked skill, ready to publish (see the module notes)."""
    folder = find_skill(scope, ref)
    text = (folder / SKILL_FILE).read_text(encoding="utf-8-sig")
    front = frontmatter_of(text)
    name = front.get("name")
    name = name.strip() if isinstance(name, str) and name.strip() else folder.name
    description = front.get("description")
    if not isinstance(description, str) or not description.strip():
        raise HubError("SKILL.md needs a description in its frontmatter before it is shared")
    if not re.match(r"^[a-z0-9][a-z0-9._-]{0,63}$", name):
        raise HubError(f"the skill name {name!r} is not a plain slug (a-z, 0-9, . _ -)")
    files, executable = collect_files(folder)
    if SKILL_FILE not in files:
        raise HubError("the skill folder has no SKILL.md")
    report = scan_tree(folder)
    if report.verdict == "dangerous":
        raise HubError("the scan rates this skill dangerous; it is not published")
    return PublishPlan(name, folder, files, executable, report)


# -- GitHub ---------------------------------------------------------------------------------------


class _GitHub:
    def __init__(self, client: httpx.AsyncClient, token: str) -> None:
        self.client = client
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }

    async def call(
        self, method: str, path: str, body: Mapping[str, Any] | None = None
    ) -> httpx.Response:
        try:
            return await self.client.request(
                method, f"{GITHUB_API}{path}", headers=self.headers, json=body
            )
        except httpx.HTTPError as exc:
            raise HubError(f"GitHub could not be reached ({type(exc).__name__})") from None

    async def json(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        ok: tuple[int, ...] = (200, 201),
    ) -> dict[str, Any]:
        resp = await self.call(method, path, body)
        if resp.status_code not in ok:
            raise HubError(_why(resp, f"{method} {path.split('?', 1)[0]}"))
        try:
            data = resp.json()
        except ValueError:
            raise HubError(f"GitHub sent an unreadable answer to {method} {path}") from None
        return cast(dict[str, Any], data) if isinstance(data, dict) else {}


def _why(resp: httpx.Response, what: str) -> str:
    message = ""
    try:
        data = resp.json()
        if isinstance(data, dict):
            message = str(cast(dict[str, Any], data).get("message") or "")
    except ValueError:
        message = ""
    hint = {
        401: "the GitHub token was refused",
        403: "the GitHub token may not do this",
        404: "not found (or the token cannot see it)",
        422: "GitHub refused the change",
    }.get(resp.status_code, f"HTTP {resp.status_code}")
    return f"{what}: {hint}" + (f" ({message[:200]})" if message else "")


async def _fork(gh: _GitHub, repo: str) -> str:
    data = await gh.json("POST", f"/repos/{repo}/forks", {}, ok=(200, 201, 202))
    fork = str(data.get("full_name") or "")
    if not _REPO.match(fork):
        raise HubError("GitHub did not say where the fork is")
    for _ in range(FORK_WAIT_TRIES):
        if (await gh.call("GET", f"/repos/{fork}")).status_code == 200:
            return fork
        await _CLOCK.sleep(FORK_WAIT_S)
    raise HubError(f"the fork {fork} did not become ready in time; try again in a minute")


async def publish_github(
    plan: PublishPlan,
    repo: str,
    *,
    token: str,
    client: httpx.AsyncClient,
    path: str = "skills",
    branch: str = "",
    pr: bool = True,
    say: Callable[[str], None] = lambda _line: None,
) -> str:
    """Publish ``plan`` (see the module notes); the pull request (or branch) address."""
    if not _REPO.match(repo):
        raise HubError(f"--repo must be owner/name, not {repo!r}")
    folder = "/".join(p for p in path.replace("\\", "/").split("/") if p and p != ".")
    if ".." in folder.split("/"):
        raise HubError("--path may not leave the repository")
    branch = branch or f"indus/add-skill-{plan.name}"
    if not _BRANCH.match(branch) or ".." in branch:
        raise HubError(f"{branch!r} is not a usable branch name")
    gh = _GitHub(client, token)
    target = await gh.json("GET", f"/repos/{repo}")
    default = str(target.get("default_branch") or "main")
    permissions = target.get("permissions")
    can_push = isinstance(permissions, dict) and bool(cast(dict[str, Any], permissions).get("push"))
    head_repo = repo
    if not can_push:
        say(f"No push rights on {repo}: using your fork.")
        head_repo = await _fork(gh, repo)
    ref = await gh.json("GET", f"/repos/{repo}/git/ref/heads/{default}")
    obj = ref.get("object")
    base_sha = str(cast(dict[str, Any], obj).get("sha") or "") if isinstance(obj, dict) else ""
    if not base_sha:
        raise HubError(f"could not read the tip of {repo}@{default}")
    commit = await gh.json("GET", f"/repos/{repo}/git/commits/{base_sha}")
    tree_obj = commit.get("tree")
    base_tree = (
        str(cast(dict[str, Any], tree_obj).get("sha") or "") if isinstance(tree_obj, dict) else ""
    )
    entries: list[dict[str, str]] = []
    prefix = f"{folder}/{plan.name}" if folder else plan.name
    for rel in sorted(plan.files):
        blob = await gh.json(
            "POST",
            f"/repos/{head_repo}/git/blobs",
            {"content": base64.b64encode(plan.files[rel]).decode("ascii"), "encoding": "base64"},
        )
        entries.append(
            {
                "path": f"{prefix}/{rel}",
                "mode": "100755" if rel in plan.executable else "100644",
                "type": "blob",
                "sha": str(blob.get("sha") or ""),
            }
        )
    tree = await gh.json(
        "POST", f"/repos/{head_repo}/git/trees", {"base_tree": base_tree, "tree": entries}
    )
    made = await gh.json(
        "POST",
        f"/repos/{head_repo}/git/commits",
        {"message": f"Add the {plan.name} skill", "tree": tree.get("sha"), "parents": [base_sha]},
    )
    resp = await gh.call(
        "POST",
        f"/repos/{head_repo}/git/refs",
        {"ref": f"refs/heads/{branch}", "sha": made.get("sha")},
    )
    if resp.status_code == 422:
        raise HubError(f"the branch {branch!r} already exists in {head_repo}; pass --branch")
    if resp.status_code not in (200, 201):
        raise HubError(_why(resp, "creating the branch"))
    branch_url = f"https://github.com/{head_repo}/tree/{branch}"
    if not pr:
        return branch_url
    owner = head_repo.split("/", 1)[0]
    pull = await gh.json(
        "POST",
        f"/repos/{repo}/pulls",
        {
            "title": f"Add skill: {plan.name}",
            "head": f"{owner}:{branch}" if head_repo != repo else branch,
            "base": default,
            "body": (
                f"Adds the `{plan.name}` skill under `{prefix}/`.\n\n"
                f"Scanned before sharing: {plan.report.verdict} ({plan.report.scanner})."
            ),
        },
    )
    return str(pull.get("html_url") or branch_url)
