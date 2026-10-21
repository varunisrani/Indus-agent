"""Cut a release: lock a version with an attempt ref, draft it, start the release workflow (P23-22).

Usage (the owner's; agents only ever pass ``--dry-run``)::

    python scripts/release/release.py --commit <sha on origin/main> [--bump patch|minor|major]
        [--canary] [--autopublish] [--lanes ci,docker,...] [--if-new] [--remote origin]
        [--repository owner/name] [--dry-run]
    python scripts/release/release.py abandon --version X.Y.Z [--remote origin] [--dry-run]

Checks, all printed (✓/✗) before anything is written:

1. the commit exists and is on ``<remote>/main``;
2. no earlier attempt of any version is open (neither abandoned nor published). Without a terminal
   this refuses; on a terminal it offers to abandon it first (default no);
3. the next version (P23-21) has no final tag yet, and the commit descends from the newest release;
4. every commit author since the newest release is in the contributor map (a canary only warns).

Then (not in a dry run): an annotated attempt ref ``rc.<N>-v<version>`` whose message is the
claim, pushed — the push is the lock, so a second cutter loses it; one draft GitHub release on the
attempt ref, with the do-not-publish warning; and a dispatch of ``release.yml`` on the default
branch with the attempt ref as input. A dry run reads only local refs (no fetch, no API call) and
says so; it prints the whole plan, the claim and the notes, and exits 1 when a check failed.

``abandon`` writes the marker ref ``abandoned-rc.<N>-v<version>`` on the attempt's commit (the
attempt ref itself stays; its number is never reused) and deletes the draft.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

if __package__ in (None, ""):  # run as a script: make the repository and src importable
    _ROOT = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(_ROOT), str(_ROOT / "src")]

from indus.cli.update.channels import Git, GitError  # noqa: E402
from indus.foundation.versions import (  # noqa: E402
    CANARY,
    MAIN,
    RELEASE_REPOSITORY,
    STABLE,
    AttemptRef,
    Version,
    parse_tag,
)
from scripts.release import changelog  # noqa: E402
from scripts.release.claims import Claim, ClaimError, draft_body, parse_lanes  # noqa: E402
from scripts.release.github import GitHubClient  # noqa: E402
from scripts.release.versioning import (  # noqa: E402
    BUMPS,
    newest_published_stable,
    next_attempt,
    next_canary,
    next_stable,
    open_attempts,
)

__all__ = ["Plan", "Releaser", "main"]

WORKFLOW = "release.yml"
ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Check:
    ok: bool
    text: str
    blocking: bool = True


@dataclass
class Plan:
    kind: str
    commit: str = ""
    version: Version | None = None
    attempt: int = 0
    previous: Version | None = None
    lanes: frozenset[str] = field(default_factory=frozenset[str])
    autopublish: bool = False
    epoch: int = 0
    checks: list[Check] = field(default_factory=list[Check])
    abandon_first: list[AttemptRef] = field(default_factory=list[AttemptRef])
    notes: str = ""
    nothing_new: bool = False

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.blocking)

    @property
    def ref(self) -> str:
        assert self.version is not None
        return AttemptRef(self.version, self.attempt).name

    def claim(self) -> Claim:
        assert self.version is not None
        return Claim(
            self.kind, str(self.version), self.attempt, self.commit, self.autopublish,
            self.lanes, self.epoch,
        )  # fmt: skip


class Releaser:
    def __init__(
        self,
        git: Git,
        github: GitHubClient | None,
        *,
        remote: str,
        repository: str,
        contributors: Path,
        now: Callable[[], datetime],
        ask: Callable[[str], bool] | None,
        say: Callable[[str], None] = print,
    ) -> None:
        self.git = git
        self.github = github
        self.remote = remote
        self.repository = repository
        self.contributors = contributors
        self.now = now
        self.ask = ask
        self.say = say

    # -- reading -----------------------------------------------------------------------------------

    def fetch(self) -> None:
        self.git.run(
            "fetch", "--quiet", "--tags", self.remote,
            f"+refs/heads/{MAIN}:refs/remotes/{self.remote}/{MAIN}", network=True,
        )  # fmt: skip

    def tag_names(self, *, local: bool) -> list[str]:
        if local:
            return [t for t in self.git.out("tag", "--list").splitlines() if t]
        listed = self.git.out("ls-remote", "--tags", self.remote)
        names = {line.split("refs/tags/", 1)[1].removesuffix("^{}") for line in
                 listed.splitlines() if "refs/tags/" in line}  # fmt: skip
        return sorted(names)

    def published(self, names: Sequence[str], *, local: bool) -> Version | None:
        if local or self.github is None:
            stables = [v for n in names if (v := parse_tag(n)) is not None and not v.is_canary]
            return max(stables, default=None)
        return newest_published_stable(self.github.list_releases())

    def _epoch_floor(self) -> int:
        raw = self.git.run(
            "for-each-ref", "--format=%(taggerdate:unix)", "refs/tags/rc.*", check=False
        ).stdout
        stamps = [int(x) for x in raw.split() if x.isdigit()]
        return max(stamps, default=0) + 1

    # -- planning ----------------------------------------------------------------------------------

    def plan(
        self,
        *,
        commit: str,
        bump: str,
        canary: bool,
        autopublish: bool,
        lanes: frozenset[str],
        dry_run: bool,
        if_new: bool,
    ) -> Plan:
        plan = Plan(CANARY if canary else STABLE, lanes=lanes, autopublish=autopublish)
        resolved = self.git.resolve(commit)
        if resolved is None:
            plan.checks.append(Check(False, f"{commit} is not a commit in this repository"))
            return plan
        plan.commit = resolved
        main_ref = f"refs/remotes/{self.remote}/{MAIN}"
        if self.git.resolve(main_ref) is None:
            plan.checks.append(Check(False, f"{self.remote}/{MAIN} is unknown here (git fetch)"))
        elif self.git.is_ancestor(resolved, main_ref):
            plan.checks.append(Check(True, f"{resolved[:12]} is on {self.remote}/{MAIN}"))
        else:
            plan.checks.append(Check(False, f"{resolved[:12]} is not on {self.remote}/{MAIN}"))
        names = self.tag_names(local=dry_run)
        on_commit = [
            n for n in names if parse_tag(n) is not None and self.git.resolve(n) == resolved
        ]
        in_flight = [
            a.name
            for a in open_attempts(names)
            if self.git.resolve(f"refs/tags/{a.name}") == resolved
        ]
        if if_new and (on_commit or in_flight):
            plan.nothing_new = True
            what = (
                f"already released ({on_commit[0]})" if on_commit else f"in flight ({in_flight[0]})"
            )
            plan.checks.append(Check(True, f"{resolved[:12]} is {what}"))
            return plan
        self._open_attempts(plan, names)
        published = self.published(names, local=dry_run)
        plan.previous = published
        plan.version = (
            next_canary(published, self.now()) if canary else next_stable(published, bump)
        )
        final = f"v{plan.version}"
        if final in names:
            plan.checks.append(Check(False, f"{final} already has a final tag (still publishing?)"))
        else:
            plan.checks.append(Check(True, f"{final} has no final tag yet"))
        if published is not None:
            descends = self.git.is_ancestor(published.tag, resolved)
            text = f"{resolved[:12]} {'descends' if descends else 'does not descend'} from "
            plan.checks.append(Check(descends, text + published.tag))
        plan.attempt = next_attempt(plan.version, names)
        plan.epoch = max(int(self.now().timestamp()), self._epoch_floor())
        self._notes(plan, published)
        return plan

    def _open_attempts(self, plan: Plan, names: Sequence[str]) -> None:
        still_open = open_attempts(names)
        if not still_open:
            plan.checks.append(Check(True, "no earlier attempt is open"))
            return
        for attempt in still_open:
            question = f"{attempt.name} is still open. Abandon it and cut a new attempt? [y/N] "
            if self.ask is not None and self.ask(question):
                plan.abandon_first.append(attempt)
                plan.checks.append(Check(True, f"{attempt.name} will be abandoned first"))
            else:
                hint = f"abandon it first: release.py abandon --version {attempt.version}"
                plan.checks.append(Check(False, f"{attempt.name} is still open; {hint}"))

    def _notes(self, plan: Plan, published: Version | None) -> None:
        since = published.tag if published is not None else None
        commits = changelog.commits_between(self.git.out, since, plan.commit)
        credits = changelog.load_contributors(self.contributors)
        missing = changelog.unmapped(commits, credits)
        if missing:
            files = ", ".join(f"{self.contributors.name}/{key}.txt" for key in missing)
            text = f"{len(missing)} commit author(s) are not in the contributor map: add {files}"
            plan.checks.append(Check(False, text, blocking=plan.kind == STABLE))
        else:
            plan.checks.append(Check(True, f"all {len(commits)} commit(s) have credited authors"))
        assert plan.version is not None
        plan.notes = changelog.render(
            str(plan.version), commits, credits, previous_tag=since, repository=self.repository
        )

    def show(self, plan: Plan, *, dry_run: bool) -> None:
        say = self.say
        say(
            "Release plan "
            + ("(dry run: local refs only, nothing is written)" if dry_run else "(live)")
        )
        for check in plan.checks:
            mark = "✓" if check.ok else ("✗" if check.blocking else "!")
            say(f"  {mark} {check.text}")
        if plan.nothing_new or plan.version is None:
            return
        say(f"  kind:        {plan.kind}")
        say(f"  version:     {plan.version} (previous: {plan.previous or 'none'})")
        say(f"  attempt ref: {plan.ref}")
        say(f"  commit:      {plan.commit}")
        say(f"  lanes:       {', '.join(sorted(plan.lanes))}")
        say(f"  autopublish: {'yes' if plan.autopublish else 'no'}")
        for attempt in plan.abandon_first:
            say(f"  first:       abandon {attempt.name} (marker {attempt.marker})")
        say(f"  then:        push {plan.ref}; draft v{plan.version}; dispatch {WORKFLOW}")
        say("  claim:       " + plan.claim().message())
        say("")
        say(plan.notes)

    # -- writing -----------------------------------------------------------------------------------

    def _tag_env(self, epoch: int) -> None:
        stamp = f"@{epoch} +0000"
        self.git.env = {**self.git.env, "GIT_COMMITTER_DATE": stamp}

    def _push_tag(self, name: str, commit: str, message: str, epoch: int | None) -> None:
        saved = self.git.env
        if epoch is not None:
            self._tag_env(epoch)
        try:
            self.git.run("tag", "--annotate", name, commit, "--message", message)
        finally:
            self.git.env = saved
        try:
            self.git.run("push", "--quiet", self.remote, f"refs/tags/{name}", network=True)
        except GitError:
            self.git.run("tag", "--delete", name, check=False)
            raise

    def execute(self, plan: Plan) -> None:
        assert self.github is not None and plan.version is not None
        for attempt in plan.abandon_first:
            self.abandon(attempt.version, dry_run=False)
        claim = plan.claim()
        try:
            self._push_tag(plan.ref, plan.commit, claim.message(), plan.epoch)
        except GitError as exc:
            raise GitError(
                f"could not claim {plan.ref}: someone else may have cut it", exc.detail
            ) from exc
        self.say(f"✓ Claimed {plan.ref}")
        self.github.create_release(
            tag=plan.ref,
            name=f"v{plan.version}",
            body=draft_body(plan.notes),
            draft=True,
            prerelease=plan.kind == CANARY,
            target=plan.commit,
        )
        self.say(f"✓ Drafted v{plan.version} on {plan.ref}")
        branch = self.github.default_branch()
        self.github.dispatch_workflow(WORKFLOW, branch, {"attempt_ref": plan.ref})
        self.say(f"✓ Started {WORKFLOW} on {branch} for {plan.ref}")

    def abandon(self, version: Version, *, dry_run: bool) -> int:
        names = self.tag_names(local=dry_run)
        mine = [a for a in open_attempts(names) if a.version == version]
        if not mine:
            self.say(f"✗ No open attempt of {version}.")
            return 1
        attempt = mine[-1]
        commit = self.git.resolve(f"refs/tags/{attempt.name}") or ""
        draft = None if self.github is None else self.github.release_by_tag(attempt.name)
        if draft is not None and draft.get("draft") is False:
            self.say(f"✗ {attempt.name} was published by hand; it cannot be abandoned.")
            return 1
        if dry_run:
            self.say(f"Would write {attempt.marker} on {commit[:12]} and delete the draft.")
            return 0
        record = json.dumps({"schema": 1, "abandons": attempt.name}, sort_keys=True)
        self._push_tag(attempt.marker, commit, record, None)
        if draft is not None and self.github is not None:
            self.github.delete_release(int(draft["id"]))
        self.say(f"✓ Abandoned {attempt.name} (marker {attempt.marker}); its number is spent.")
        return 0


# -- the command line ------------------------------------------------------------------------------


def _terminal_ask(question: str) -> bool:
    try:
        return input(question).strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="release.py", description="Cut an Indus Agent release.")
    parser.add_argument("--commit", required=True, help="the commit to release (on origin/main)")
    parser.add_argument("--bump", choices=BUMPS, default="patch")
    parser.add_argument("--canary", action="store_true", help="a canary of the next patch")
    parser.add_argument("--autopublish", action="store_true", help="publish as soon as green")
    parser.add_argument("--lanes", default=None, help="comma-separated lanes (default: all)")
    parser.add_argument("--if-new", action="store_true", help="do nothing for a released commit")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--repository", default=RELEASE_REPOSITORY)
    parser.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    return parser


def _abandon_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="release.py abandon", description="Abandon an attempt.")
    parser.add_argument("--version", required=True)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--repository", default=RELEASE_REPOSITORY)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path = ROOT,
    github: GitHubClient | None = None,
    env: dict[str, str] | None = None,
    now: Callable[[], datetime] | None = None,
    ask: Callable[[str], bool] | None = None,
    interactive: bool | None = None,
) -> int:
    args_in = list(sys.argv[1:] if argv is None else argv)
    values = dict(os.environ if env is None else env)
    clock = now or (lambda: datetime.now(UTC))
    tty = sys.stdin.isatty() and sys.stdout.isatty() if interactive is None else interactive
    asker = (ask or _terminal_ask) if tty else None
    abandoning = bool(args_in) and args_in[0] == "abandon"
    try:
        args = (_abandon_parser() if abandoning else _parser()).parse_args(args_in[abandoning:])
    except SystemExit as exc:
        return int(exc.code or 0)
    git = Git(root, values)
    if github is None and not args.dry_run:
        from scripts.release.github import RestGitHub

        github = RestGitHub(args.repository, values)
    releaser = Releaser(
        git, None if args.dry_run and github is None else github, remote=args.remote,
        repository=args.repository, contributors=root / changelog.CONTRIBUTORS_DIR, now=clock,
        ask=asker,
    )  # fmt: skip
    try:
        if abandoning:
            version = parse_tag(f"v{args.version}")
            if version is None:
                print(f"✗ {args.version} is not a release version.", file=sys.stderr)
                return 2
            if not args.dry_run:
                releaser.fetch()
            return releaser.abandon(version, dry_run=args.dry_run)
        try:
            lanes = parse_lanes(args.lanes)
        except ClaimError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 2
        if not args.dry_run:
            releaser.fetch()
        plan = releaser.plan(
            commit=args.commit, bump=args.bump, canary=args.canary, autopublish=args.autopublish,
            lanes=lanes, dry_run=args.dry_run, if_new=args.if_new,
        )  # fmt: skip
        releaser.show(plan, dry_run=args.dry_run)
        if plan.nothing_new:
            print("Nothing to release.")
            return 0
        if not plan.ok:
            print("✗ Not released: a check failed (see above).", file=sys.stderr)
            return 1
        if args.dry_run:
            return 0
        releaser.execute(plan)
        return 0
    except GitError as exc:
        print(f"✗ {exc}" + (f": {exc.detail}" if exc.detail else ""), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
