"""Hub verbs on ``indus skills`` (P13-27), the ``indus://skill/install`` entry point and
``trust-project`` (P13-28).

- ``browse [--source S]`` / ``search <query> [--source S]``: skills from the catalogue and taps
  (a search that finds nothing there asks skills.sh and the well-known sites);
- ``inspect <id>``: fetch into quarantine, scan, print, delete the quarantine copy;
- ``install <id> [--category C] [--name N] [--force] [-y]``: the P13-26 pipeline. ``--force``
  installs over a ``caution`` verdict and reinstalls an installed skill; it never installs a
  ``dangerous`` one. Without a terminal the confirmation needs ``-y`` (exit 2 otherwise);
- ``check-updates`` / ``update [name|--all] [--force]`` / ``audit [name]`` /
  ``uninstall <name> [-y]`` (``check`` stays the P09 file validator);
- ``tap add|remove|list`` and ``trust|untrust <repo>`` (trust only for the fixed trusted set);
- ``trust-project [path]``: load the skills of that repo (``skills.trusted_project_dirs``);
- ``publish <skill> --to github --repo owner/name`` and ``snapshot export|import`` (P24-45).

Exit codes: 0 done, 1 refused / blocked / failed, 2 a confirmation needs a terminal or ``-y``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

import click

from indus.cli.cmdkit import cli_state, err
from indus.cli.commands._support import scope_with_config
from indus.cli.prompts import ask_text, confirm, io_for
from indus.foundation.scope import Scope

if TYPE_CHECKING:
    from indus.learning.skills.hub.install import InstallResult, Preview
    from indus.learning.skills.hub.sources import SkillMeta, SourceSet

__all__ = [
    "DEEP_LINK_SCHEME",
    "deep_link_install",
    "make_sources",
    "publish_client",
    "register",
    "register_sharing",
    "slash_install",
    "slash_search_lines",
]

DEEP_LINK_SCHEME = "indus"
LIST_WIDTH = 60


def make_sources(scope: Scope) -> SourceSet:
    """The hub adapters for ``scope`` (tests replace this)."""
    from indus.learning.skills.hub.sources import build_sources

    return build_sources(scope)


def _run[T](scope: Scope, work: Callable[[SourceSet], Coroutine[Any, Any, T]]) -> T:
    """Run ``work`` on a fresh event loop (on a helper thread when this one already runs one)."""

    async def main() -> T:
        sources = make_sources(scope)
        try:
            return await work(sources)
        finally:
            await sources.aclose()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(main())
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="indus-hub") as pool:
        return pool.submit(asyncio.run, main()).result()


def _scope() -> Scope:
    return scope_with_config(cli_state().scope)


def _clip(text: str, width: int = LIST_WIDTH) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _fail(message: str, code: int = 1) -> None:
    err(message)
    raise click.exceptions.Exit(code)


def _print_rows(rows: list[SkillMeta], as_json: bool) -> None:
    if as_json:
        click.echo(
            json.dumps(
                [
                    {
                        "name": r.name,
                        "identifier": r.identifier,
                        "source": r.source,
                        "trust": r.trust,
                        "category": r.category,
                        "description": r.description,
                    }
                    for r in rows
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if not rows:
        click.echo("No skills found.")
        return
    width = max(len(r.name) for r in rows)
    for row in rows:
        click.echo(f"{row.name:<{width}}  {row.trust:<9}  {row.identifier}")
        if row.description:
            click.echo(f"{'':<{width}}  {_clip(row.description)}")
    click.echo(f"{len(rows)} skill{'s' if len(rows) != 1 else ''}.")


def _search(query: str, source: str, limit: int, as_json: bool) -> None:
    from indus.learning.skills.hub.sources import HubError, search_sources
    from indus.learning.skills.resolver import skills_setting

    scope = _scope()
    budget = skills_setting(scope, "skills.hub.search_timeout")
    seconds = float(budget) if isinstance(budget, int | float) and budget > 0 else 30.0

    async def work(sources: SourceSet) -> Any:
        return await search_sources(
            sources, query, limit=limit, source_filter=source, budget=seconds
        )

    try:
        outcome = _run(scope, work)
    except HubError as exc:
        _fail(str(exc))
        return
    _print_rows(outcome.rows, as_json)
    if outcome.timed_out and not as_json:
        err(f"No answer in time from: {', '.join(sorted(set(outcome.timed_out)))}")


# -- install ------------------------------------------------------------------------------------


def run_install(
    scope: Scope,
    identifier: str,
    *,
    category: str = "",
    name: str = "",
    force: bool = False,
    yes: bool = False,
) -> int:
    """The ``install`` verb's body; returns the exit code."""
    from indus.learning.skills.hub.install import InstallOptions, install

    io = io_for(cli_state(), "Installing a skill needs a terminal to confirm, or -y.")
    no_terminal = False

    def ask_confirm(preview: Preview) -> bool:
        nonlocal no_terminal
        answer = confirm(
            io, f"Install {preview.name!r} into skills/{preview.install_path}?", assume_yes=yes
        )
        if answer is None:
            no_terminal = True
            return False
        return answer

    def ask_name(where: str) -> str | None:
        if not io.tty:
            return None
        return ask_text(io, f"No usable name in {where}. Name for this skill")

    options = InstallOptions(
        category=category,
        name=name,
        force=force,
        confirm=ask_confirm,
        ask_name=ask_name,
        say=click.echo,
    )

    async def work(sources: SourceSet) -> InstallResult:
        return await install(scope, sources, identifier, options)

    result = _run(scope, work)
    if result.ok:
        click.echo(f"Installed {result.name}: {result.message}.")
        click.echo("Open sessions see it on their next message; new ones list it in the index.")
        return 0
    if no_terminal:
        return 2
    if result.status == "declined":
        click.echo("Nothing installed.")
        return 1
    err(f"Not installed: {result.message}")
    return 1


def deep_link_install(
    scope: Scope,
    url: str,
    *,
    confirm_install: Callable[[Preview], bool],
    say: Callable[[str], None] = lambda _line: None,
) -> InstallResult:
    """Handle ``indus://skill/install?identifier=…`` (the desktop's entry point, P20).

    Runs the normal pipeline in the caller's own profile: the scan always runs, the confirmation
    always happens, and parameters such as ``profile``, ``force`` or a scan switch are ignored.
    ``category`` and ``name`` are honoured.
    """
    from indus.learning.skills.hub.install import InstallOptions, InstallResult, install

    parts = urlsplit(url)
    if parts.scheme != DEEP_LINK_SCHEME or parts.netloc != "skill" or parts.path != "/install":
        return InstallResult("failed", message=f"not a skill install link: {url}")
    query = parse_qs(parts.query)
    identifier = (query.get("identifier") or [""])[0].strip()
    if not identifier:
        return InstallResult("failed", message="the link names no skill (identifier=…)")
    options = InstallOptions(
        category=(query.get("category") or [""])[0],
        name=(query.get("name") or [""])[0],
        confirm=confirm_install,
        say=say,
    )

    async def work(sources: SourceSet) -> InstallResult:
        return await install(scope, sources, identifier, options)

    return _run(scope, work)


# -- chat: /skills search | install --------------------------------------------------------------


def slash_search_lines(scope: Scope, query: str, limit: int = 10) -> list[str]:
    """``/skills search <query>`` as lines for a chat surface."""
    from indus.learning.skills.hub.sources import HubError, search_sources

    async def work(sources: SourceSet) -> Any:
        return await search_sources(sources, query, limit=limit)

    try:
        rows = _run(scope, work).rows
    except HubError as exc:
        return [str(exc)]
    if not rows:
        return [f"No hub skills match {query!r}."]
    lines = [f"  {r.identifier}  ({r.trust})  {_clip(r.description, 50)}" for r in rows]
    return [*lines, "/skills install <identifier> installs one (scanned first)."]


def slash_install(
    scope: Scope,
    identifier: str,
    *,
    confirm_install: Callable[[str], bool] | None,
    say: Callable[[str], None],
) -> InstallResult:
    """``/skills install <id>``: the same pipeline; it always asks, and refuses when the surface
    cannot ask (a chat platform without buttons, a cron run)."""
    from indus.learning.skills.hub.install import InstallOptions, InstallResult, install

    if confirm_install is None:
        return InstallResult(
            "declined",
            message="installing needs a confirmation this surface cannot ask for; run "
            f"`indus skills install {identifier}` in a terminal",
        )
    ask = confirm_install

    def confirm_preview(preview: Preview) -> bool:
        return ask(
            f"Install {preview.name!r} ({preview.bundle.trust}) into skills/{preview.install_path}?"
        )

    options = InstallOptions(confirm=confirm_preview, say=say)

    async def work(sources: SourceSet) -> InstallResult:
        return await install(scope, sources, identifier, options)

    return _run(scope, work)


# -- registration -------------------------------------------------------------------------------


def register(group: click.Group) -> None:
    """Add the hub verbs to the ``skills`` group."""

    @group.command("browse")
    @click.option("--source", default="all", help="A source id, or owner/repo for one tap.")
    @click.option("--limit", default=50, show_default=True, help="At most this many rows.")
    @click.option("--json", "as_json", is_flag=True, help="Print the rows as JSON.")
    def browse_command(source: str, limit: int, as_json: bool) -> None:
        """List hub skills from the catalogue and your taps."""
        _search("", source, limit, as_json)

    @group.command("search")
    @click.argument("query")
    @click.option("--source", default="all", help="A source id, or owner/repo for one tap.")
    @click.option("--limit", default=20, show_default=True, help="At most this many rows.")
    @click.option("--json", "as_json", is_flag=True, help="Print the rows as JSON.")
    def search_command(query: str, source: str, limit: int, as_json: bool) -> None:
        """Search the hub for skills."""
        _search(query, source, limit, as_json)

    @group.command("inspect")
    @click.argument("identifier")
    @click.option("--name", default="", help="Name to check it under (for unnamed URL skills).")
    def inspect_command(identifier: str, name: str) -> None:
        """Fetch and scan a hub skill without installing it."""
        from indus.learning.skills.hub.install import inspect
        from indus.learning.skills.hub.sources import HubError

        scope = _scope()

        async def work(sources: SourceSet) -> Preview:
            return await inspect(scope, sources, identifier, name=name)

        try:
            preview = _run(scope, work)
        except (HubError, ValueError) as exc:
            _fail(str(exc))
            return
        click.echo(preview.render())
        click.echo("Nothing was installed.")

    @group.command("install")
    @click.argument("identifier")
    @click.option("--category", default="", help="Category folder to install into.")
    @click.option("--name", default="", help="Install under this name.")
    @click.option("--force", is_flag=True, help="Install over a caution verdict; reinstall.")
    @click.option("--yes", "-y", is_flag=True, help="Do not ask first.")
    def install_command(identifier: str, category: str, name: str, force: bool, yes: bool) -> None:
        """Install a skill from the hub (scanned first)."""
        code = run_install(_scope(), identifier, category=category, name=name, force=force, yes=yes)
        if code:
            raise click.exceptions.Exit(code)

    @group.command("check-updates")
    def check_updates_command() -> None:
        """Check hub skills for upstream changes (``check`` validates skill files)."""
        from indus.learning.skills.hub.install import check_updates

        scope = _scope()
        rows = _run(scope, lambda sources: check_updates(scope, sources))
        if not rows:
            click.echo("No hub skills installed.")
            return
        width = max(len(r.name) for r in rows)
        for row in rows:
            note = f"  ({row.detail})" if row.detail else ""
            click.echo(f"{row.name:<{width}}  {row.source:<10}  {row.status}{note}")
        waiting = sum(1 for r in rows if r.status == "update_available")
        click.echo(f"{waiting} update{'s' if waiting != 1 else ''} available.")

    @group.command("update")
    @click.argument("name", required=False)
    @click.option("--all", "every", is_flag=True, help="Update every hub skill.")
    @click.option("--force", is_flag=True, help="Overwrite skills you edited.")
    def update_command(name: str | None, every: bool, force: bool) -> None:
        """Reinstall hub skills whose upstream changed (your edits are kept)."""
        from indus.learning.skills.hub.install import update

        if not name and not every:
            _fail("Name a skill to update, or pass --all.", 2)
        scope = _scope()
        report = _run(
            scope, lambda sources: update(scope, sources, name, force=force, say=click.echo)
        )
        for done in report.updated:
            click.echo(f"Updated {done}.")
        for kept in report.kept_local:
            click.echo(f"Kept {kept}: you edited it (update {kept} --force overwrites).")
        for failed, why in report.failed:
            err(f"Not updated {failed}: {why}")
        if not (report.updated or report.kept_local or report.failed):
            click.echo("Everything is up to date.")
        if report.failed:
            raise click.exceptions.Exit(1)

    @group.command("audit")
    @click.argument("name", required=False)
    def audit_command(name: str | None) -> None:
        """Scan every hub skill again."""
        from indus.learning.skills.hub.install import audit
        from indus.safety.content_scan import render_report

        rows = audit(_scope(), name)
        if not rows:
            click.echo(
                "No hub skills installed." if name is None else f"{name!r} is not a hub skill."
            )
            return
        dangerous = False
        for row in rows:
            if row.report is None:
                click.echo(f"{row.name}: {row.detail}")
                continue
            dangerous = dangerous or row.report.verdict == "dangerous"
            click.echo(render_report(row.report))
        if dangerous:
            raise click.exceptions.Exit(1)

    @group.command("uninstall")
    @click.argument("name")
    @click.option("--yes", "-y", is_flag=True, help="Do not ask first.")
    def uninstall_command(name: str, yes: bool) -> None:
        """Remove a skill the hub installed."""
        from indus.learning.skills.hub.install import uninstall
        from indus.learning.skills.hub.lock import HubLock
        from indus.learning.skills.hub.sources import HubError

        scope = _scope()
        if HubLock(scope.home / "skills").get(name) is None:
            _fail(f"{name!r} was not installed from the hub.")
        io = io_for(cli_state(), "Removing a skill needs a terminal to confirm, or -y.")
        answer = confirm(io, f"Remove the hub skill {name!r}?", assume_yes=yes)
        if answer is None:
            raise click.exceptions.Exit(2)
        if not answer:
            click.echo("Nothing changed.")
            raise click.exceptions.Exit(1)
        try:
            click.echo(uninstall(scope, name).capitalize() + ".")
        except HubError as exc:
            _fail(str(exc))

    @group.group("tap")
    def tap_group() -> None:
        """GitHub repos searched for skills."""

    @tap_group.command("list")
    def tap_list() -> None:
        """List your taps."""
        from indus.learning.skills.hub.lock import Taps

        rows = Taps(_scope().home / "skills").list()
        if not rows:
            click.echo("No taps. `indus skills tap add owner/repo` adds one.")
            return
        for row in rows:
            click.echo(f"{row['repo']}  {row['path']}  {row['trust']}")

    @tap_group.command("add")
    @click.argument("repo")
    @click.option("--path", default="skills/", show_default=True, help="Folder holding skills.")
    def tap_add(repo: str, path: str) -> None:
        """Search a GitHub repo for skills (community trust)."""
        from indus.learning.skills.hub.lock import Taps

        if repo.count("/") != 1 or not all(repo.split("/")):
            _fail(f"{repo!r} is not owner/repo.", 2)
        added = Taps(_scope().home / "skills").add(repo, path)
        click.echo(f"Added {repo}." if added else f"{repo} is already a tap.")

    @tap_group.command("remove")
    @click.argument("repo")
    def tap_remove(repo: str) -> None:
        """Stop searching a repo."""
        from indus.learning.skills.hub.lock import Taps

        if not Taps(_scope().home / "skills").remove(repo):
            _fail(f"{repo} is not a tap.")
        click.echo(f"Removed {repo}.")

    def _set_trust(repo: str, trust: str) -> None:
        from indus.learning.skills.hub.lock import Taps
        from indus.learning.skills.hub.sources import TRUSTED_TAPS

        allowed = {t.lower() for t in TRUSTED_TAPS}
        if trust == "trusted" and repo.lower() not in allowed:
            listed = ", ".join(sorted(TRUSTED_TAPS)) or "none in this build"
            _fail(f"{repo} cannot be trusted; only these publishers can: {listed}.")
        taps = Taps(_scope().home / "skills")
        if trust == "trusted" and not any(t["repo"] == repo for t in taps.list()):
            taps.add(repo)
        if not taps.set_trust(repo, "trusted" if trust == "trusted" else "community"):
            _fail(f"{repo} is not a tap.")
        click.echo(f"{repo} is now {trust}.")

    @group.command("trust")
    @click.argument("repo")
    def trust_command(repo: str) -> None:
        """Raise a tap to trusted (only publishers on the built-in list)."""
        _set_trust(repo, "trusted")

    @group.command("untrust")
    @click.argument("repo")
    def untrust_command(repo: str) -> None:
        """Put a tap back to community trust."""
        _set_trust(repo, "community")

    @group.command("trust-project")
    @click.argument("path", required=False, type=click.Path(file_okay=False, path_type=Path))
    def trust_project_command(path: Path | None) -> None:
        """Load the skills of this repo (its .indus/skills and .agents/skills)."""
        from indus.learning.skills import project_trust

        scope = _scope()
        try:
            root = project_trust.trust_project(scope, path.resolve() if path else None)
        except ValueError as exc:
            _fail(str(exc))
            return
        state = project_trust.project_skills(scope_with_config(scope), root)
        held = len(state.quarantined)
        click.echo(f"Trusted {root}: {state.found} project skill{'s' if state.found != 1 else ''}.")
        if held:
            click.echo(f"{held} quarantined by the content scan (they stay unloaded).")

    register_sharing(group)


# -- publish and snapshots (P24-45) ---------------------------------------------------------------


def publish_client(scope: Scope) -> Any:
    """The HTTP client publishing uses (tests replace this): the SSRF-safe one."""
    import httpx

    from indus.safety.safe_transport import safe_client

    return safe_client(scope, timeout=httpx.Timeout(30.0, connect=10.0))


def register_sharing(group: click.Group) -> None:
    """``publish`` and ``snapshot export|import`` on the ``skills`` group."""

    @group.command("publish")
    @click.argument("skill")
    @click.option(
        "--to", "target", type=click.Choice(["github"]), default="github", show_default=True
    )
    @click.option("--repo", required=True, help="The GitHub repository: owner/name.")
    @click.option(
        "--path", "folder", default="skills", show_default=True, help="Folder in the repo."
    )
    @click.option("--branch", default="", help="Branch to create (default indus/add-skill-<name>).")
    @click.option("--no-pr", is_flag=True, help="Only push the branch; open no pull request.")
    @click.option("--yes", "-y", is_flag=True, help="Do not ask first.")
    def publish_command(
        skill: str, target: str, repo: str, folder: str, branch: str, no_pr: bool, yes: bool
    ) -> None:
        """Share a local skill on GitHub (a branch and a pull request)."""
        from indus.learning.skills.hub.publish import prepare, publish_github
        from indus.learning.skills.hub.sources import HubError, github_token

        scope = _scope()
        try:
            plan = prepare(scope, skill)
        except (HubError, OSError) as exc:
            _fail(f"Not published: {exc}")
            return
        click.echo(f"Skill {plan.name}: {len(plan.files)} file(s), scan {plan.report.verdict}.")
        for rel in sorted(plan.files):
            click.echo(f"  {rel}")
        token = github_token(scope)
        if not token:
            _fail("Publishing needs your GitHub token: set GITHUB_TOKEN (`indus auth` or .env).")
            return
        io = io_for(cli_state(), "Publishing needs a terminal to confirm, or -y.")
        answer = confirm(io, f"Publish {plan.name} to {target}:{repo}?", assume_yes=yes)
        if answer is None:
            raise click.exceptions.Exit(2)
        if not answer:
            click.echo("Nothing published.")
            raise click.exceptions.Exit(1)

        async def work() -> str:
            async with publish_client(scope) as client:
                return await publish_github(
                    plan,
                    repo,
                    token=token,
                    client=client,
                    path=folder,
                    branch=branch,
                    pr=not no_pr,
                    say=click.echo,
                )

        try:
            where = asyncio.run(work())
        except HubError as exc:
            _fail(f"Not published: {exc}")
            return
        click.echo(f"Published: {where}")

    @group.group("snapshot")
    def snapshot_group() -> None:
        """Export or import the hub skills of this profile."""

    @snapshot_group.command("export")
    @click.option("--output", "-o", default="-", help="Write to this file (default: stdout).")
    def snapshot_export_command(output: str) -> None:
        """Write the installed hub skills and taps as JSON."""
        from indus.learning.skills.hub.snapshot import export_snapshot

        data = export_snapshot(_scope())
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        if output == "-":
            click.echo(text, nl=False)
            return
        Path(output).expanduser().write_text(text, encoding="utf-8")
        click.echo(f"Snapshot written: {len(data['skills'])} skill(s), {len(data['taps'])} tap(s).")

    @snapshot_group.command("import")
    @click.argument("source", type=click.Path(dir_okay=False, path_type=Path))
    @click.option("--force", is_flag=True, help="Install over a caution verdict (never dangerous).")
    @click.option("--yes", "-y", is_flag=True, help="Do not ask first.")
    def snapshot_import_command(source: Path, force: bool, yes: bool) -> None:
        """Install the skills of a snapshot again (each one scanned)."""
        from indus.learning.skills.hub.snapshot import import_snapshot, read_snapshot
        from indus.learning.skills.hub.sources import HubError

        scope = _scope()
        try:
            snapshot = read_snapshot(source.read_text(encoding="utf-8-sig"))
        except OSError as exc:
            _fail(f"Cannot read {source}: {exc.strerror or exc}")
            return
        except HubError as exc:
            _fail(str(exc))
            return
        io = io_for(cli_state(), "Importing skills needs a terminal to confirm, or -y.")
        no_terminal = False

        def ask(preview: Preview) -> bool:
            nonlocal no_terminal
            answer = confirm(io, f"Install {preview.name!r}?", assume_yes=yes)
            if answer is None:
                no_terminal = True
                return False
            return answer

        async def work(sources: SourceSet) -> Any:
            return await import_snapshot(
                scope, sources, snapshot, force=force, confirm=ask, say=click.echo
            )

        rows = _run(scope, work)
        failed = 0
        for row in rows:
            mark = "✓" if row.ok else "✗"
            note = f" — {row.message}" if row.message else ""
            click.echo(f"{mark} {row.name} ({row.status}){note}")
            failed += 0 if row.ok else 1
        click.echo(f"{len(rows) - failed} of {len(rows)} skill(s) in place.")
        if no_terminal:
            raise click.exceptions.Exit(2)
        if failed:
            raise click.exceptions.Exit(1)
