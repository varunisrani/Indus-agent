"""The release workflow's steps and the publication controller (P23-23).

Subcommands (``python scripts/release/publish.py <cmd>``), each run by ``release.yml``:

- ``admit``: prove the run before anything builds. It must be a ``workflow_dispatch`` on the
  default branch; the attempt ref must be an annotated tag object on ``origin`` (local and remote
  object and peeled commit equal), its claim must match its own name, its epoch must equal its
  tagger time, and its commit must be on ``origin/main``. Writes the outputs every later job uses:
  ``commit`` (the admitted commit — never ``github.sha``, which is main's tip, R10.24),
  ``version``, ``tag``, ``kind``, ``epoch``, ``lanes`` and ``jobs`` (job → runs?) as JSON;
- ``gate``: strict aggregate gate over ``toJSON(needs)``: a job whose lanes are all on must be
  ``success``; it may be ``skipped`` only when a lane it needs was off in the claim (R10.18);
- ``upload``: the built artefacts plus ``manifest.json`` (name → SHA-256) onto the draft, once;
  a name already there with other bytes stops the run (tested bytes only, R10.23);
- ``publish``: the controller. Claims are resolved oldest version first; a still-running older
  claim blocks every newer one; a green claim goes out when it asked for autopublish, when it is
  the requested version, or when a later claim is green too (that flushes the older first).
  Publishing one claim is four steps, each checking what is already there so a rerun after any
  failure finishes the job without doing anything twice:

  1. the final receipt tag ``v<version>`` (annotated) binding attempt ref, attempt object, commit,
     the manifest's SHA-256 and the image digests;
  2. the GHCR aliases (``stable`` + ``latest``, or ``canary``) moved to the receipt's digests;
  3. the channel record (compare-and-swap, never backwards; P23-21);
  4. the draft retargeted onto the final tag, warnings stripped, and made public — last.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

if __package__ in (None, ""):  # run as a script: make the repository and src importable
    _ROOT = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(_ROOT), str(_ROOT / "src")]

from indus.cli.update.channels import Git, GitError  # noqa: E402
from indus.foundation.versions import (  # noqa: E402
    MAIN,
    RELEASE_REPOSITORY,
    AttemptRef,
    parse_attempt_ref,
    parse_marker_ref,
    parse_version,
    tag_record_text,
)
from scripts.release.claims import (  # noqa: E402
    ALIASES,
    IMAGES,
    Claim,
    ClaimError,
    ImageSpec,
    gate,
    jobs_to_run,
    strip_warnings,
)
from scripts.release.github import GitHubClient  # noqa: E402
from scripts.release.versioning import (  # noqa: E402
    ChannelEntry,
    read_channel_record,
    update_channel_record,
)

__all__ = [
    "MANIFEST",
    "ClaimState",
    "Controller",
    "PublishError",
    "Registry",
    "admit",
    "gate",
    "main",
    "plan_publication",
]

WORKFLOW = "release.yml"
MANIFEST = "manifest.json"
ROOT = Path(__file__).resolve().parents[2]


class PublishError(RuntimeError):
    pass


# -- admit -----------------------------------------------------------------------------------------


def _remote_refs(git: Git, ref: str) -> dict[str, str]:
    listed = git.run("ls-remote", "origin", ref, f"{ref}^{{}}", network=True).stdout
    found: dict[str, str] = {}
    for line in listed.splitlines():
        sha, _tab, name = line.partition("\t")
        if name:
            found[name.strip()] = sha.strip()
    return found


def admit(
    git: Git, env: Mapping[str, str], attempt_ref: str, default_branch: str
) -> dict[str, str]:
    """Verify the run and the claim (see the module notes); return the job outputs."""
    if env.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        raise PublishError("a release run must be a workflow_dispatch")
    if env.get("GITHUB_REF") != f"refs/heads/{default_branch}":
        raise PublishError(f"a release run must be dispatched from {default_branch}")
    parsed = parse_attempt_ref(attempt_ref)
    if parsed is None:
        raise PublishError(f"{attempt_ref!r} is not an attempt ref (rc.<N>-v<version>)")
    ref = f"refs/tags/{attempt_ref}"
    git.run("fetch", "--quiet", "--force", "origin", f"+{ref}:{ref}",
            f"+refs/heads/{MAIN}:refs/remotes/origin/{MAIN}", network=True)  # fmt: skip
    local_object = git.out("rev-parse", ref)
    if git.out("cat-file", "-t", local_object) != "tag":
        raise PublishError(f"{attempt_ref} is not an annotated tag")
    commit = git.out("rev-parse", f"{ref}^{{commit}}")
    remote = _remote_refs(git, ref)
    if remote.get(ref) != local_object or remote.get(f"{ref}^{{}}") != commit:
        raise PublishError(f"{attempt_ref} on origin is not the object fetched (it moved)")
    try:
        claim = Claim.from_message(git.out("tag", "--list", attempt_ref, "--format=%(contents)"))
    except ClaimError as exc:
        raise PublishError(f"{attempt_ref}: {exc}") from exc
    if (claim.version, claim.attempt, claim.commit) != (
        str(parsed.version),
        parsed.attempt,
        commit,
    ):
        raise PublishError(f"{attempt_ref}'s claim names another version, attempt or commit")
    tagger = git.out("for-each-ref", "--format=%(taggerdate:unix)", ref)
    if not tagger.isdigit() or int(tagger) != claim.epoch:
        raise PublishError(f"{attempt_ref}'s epoch is not its tagger time")
    if not git.is_ancestor(commit, f"refs/remotes/origin/{MAIN}"):
        raise PublishError(f"{commit[:12]} is not on origin/{MAIN}")
    return {
        "attempt-ref": attempt_ref,
        "attempt-object": local_object,
        "commit": commit,
        "version": claim.version,
        "tag": f"v{claim.version}",
        "kind": claim.kind,
        "epoch": str(claim.epoch),
        "autopublish": "true" if claim.autopublish else "false",
        "lanes": json.dumps(sorted(claim.lanes)),
        "jobs": json.dumps(jobs_to_run(claim.lanes), sort_keys=True),
    }


# -- the registry ----------------------------------------------------------------------------------


class Registry(Protocol):
    def digest(self, repository: str, tag: str) -> str | None: ...

    def tag(self, repository: str, digest: str, tag: str) -> None: ...


class BuildxRegistry:
    """GHCR through ``docker buildx imagetools`` (log in first); aliases are new tags on an
    existing manifest, so nothing is rebuilt or re-pushed."""

    def __init__(self, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run):
        self._run = run

    def digest(self, repository: str, tag: str) -> str | None:
        done = self._run(
            ["docker", "buildx", "imagetools", "inspect", f"{repository}:{tag}",
             "--format", "{{json .Manifest.Digest}}"],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        if done.returncode != 0:
            return None
        value = json.loads(done.stdout or "null")
        return value if isinstance(value, str) else None

    def tag(self, repository: str, digest: str, tag: str) -> None:
        done = self._run(
            ["docker", "buildx", "imagetools", "create", "--tag", f"{repository}:{tag}",
             f"{repository}@{digest}"],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        if done.returncode != 0:
            raise PublishError(f"could not tag {repository}:{tag}: {done.stderr.strip()}")


# -- the controller --------------------------------------------------------------------------------


@dataclass
class ClaimState:
    attempt: AttemptRef
    claim: Claim
    object: str
    state: str  # running | green | failed | abandoned | published
    release: dict[str, Any] | None


def plan_publication(states: Sequence[ClaimState], requested: str | None) -> list[ClaimState]:
    """Which green claims go out now, oldest first (see the module notes)."""
    ordered = sorted(states, key=lambda s: (s.attempt.version.key, s.attempt.attempt))
    chosen: list[ClaimState] = []
    for index, current in enumerate(ordered):
        if current.state in ("abandoned", "failed", "published"):
            continue
        if current.state == "running":
            break
        later_green = False
        for later in ordered[index + 1 :]:
            if later.state == "running":
                break
            if later.state == "green":
                later_green = True
                break
        if current.claim.autopublish or current.claim.version == requested or later_green:
            chosen.append(current)
            continue
        break  # a held green claim keeps everything newer waiting
    return chosen


class Controller:
    def __init__(
        self,
        git: Git,
        github: GitHubClient,
        registry: Registry,
        *,
        say: Callable[[str], None] = print,
        images: Sequence[ImageSpec] = IMAGES,
    ) -> None:
        self.git = git
        self.github = github
        self.registry = registry
        self.say = say
        self.images = images

    # -- discovery ---------------------------------------------------------------------------------

    def _remote_tags(self) -> dict[str, str]:
        listed = self.git.run("ls-remote", "--tags", "origin", network=True).stdout
        tags: dict[str, str] = {}
        for line in listed.splitlines():
            sha, _tab, name = line.partition("\t")
            if name.startswith("refs/tags/") and not name.endswith("^{}"):
                tags[name.removeprefix("refs/tags/")] = sha
        return tags

    def discover(self) -> list[ClaimState]:
        self.git.run("fetch", "--quiet", "--tags", "--force", "origin", network=True)
        tags = self._remote_tags()
        cleared = {m for name in tags if (m := parse_marker_ref(name)) is not None}
        runs = self.github.workflow_runs(WORKFLOW)
        found: list[ClaimState] = []
        for name, obj in tags.items():
            attempt = parse_attempt_ref(name)
            if attempt is None:
                continue
            try:
                claim = Claim.from_message(
                    self.git.out("tag", "--list", name, "--format=%(contents)")
                )
            except ClaimError:
                continue  # not one of ours: never published
            release = self.github.release_by_tag(name) or self.github.release_by_tag(
                f"v{attempt.version}"
            )
            found.append(
                ClaimState(
                    attempt, claim, obj, self._state(attempt, release, runs, cleared), release
                )
            )
        return found

    def _state(
        self,
        attempt: AttemptRef,
        release: dict[str, Any] | None,
        runs: Sequence[Mapping[str, Any]],
        cleared: set[AttemptRef],
    ) -> str:
        if attempt in cleared:
            return "abandoned"
        if release is not None and release.get("draft") is False:
            return "published"
        mine = [r for r in runs if r.get("name") == f"Release {attempt.name}"]
        if not mine or any(r.get("status") != "completed" for r in mine):
            return "running"
        if release is None:
            return "failed"  # green without its draft: nothing to publish
        return "green" if mine[-1].get("conclusion") == "success" else "failed"

    # -- one publication ---------------------------------------------------------------------------

    def _manifest_digest(self, release: Mapping[str, Any]) -> str:
        data = self.github.read_release_asset(int(release["id"]), MANIFEST)
        if data is None:
            raise PublishError(f"the draft has no {MANIFEST}; the upload job did not finish")
        return hashlib.sha256(data).hexdigest()

    def _image_digests(self, cs: ClaimState) -> dict[str, str]:
        if "docker" not in cs.claim.lanes:
            return {}
        digests: dict[str, str] = {}
        for image in self.images:
            digest = self.registry.digest(image.repository, image.tag_for(cs.attempt.name))
            if digest is None:
                raise PublishError(
                    f"{image.repository}:{image.tag_for(cs.attempt.name)} is missing"
                )
            digests[image.name] = digest
        return digests

    def _receipt(self, cs: ClaimState, manifest: str, images: Mapping[str, str]) -> None:
        tag = f"v{cs.claim.version}"
        record = {
            "schema": 1,
            "version": cs.claim.version,
            "commit": cs.claim.commit,
            "attempt_ref": cs.attempt.name,
            "attempt_object": cs.object,
            "manifest_sha256": manifest,
            "images": dict(sorted(images.items())),
        }
        ref = f"refs/tags/{tag}"
        if self.git.resolve(ref) is None:
            remote = _remote_refs(self.git, ref)
            if ref in remote:
                self.git.run("fetch", "--quiet", "origin", f"+{ref}:{ref}", network=True)
        if self.git.resolve(ref) is not None:
            text = self.git.out("tag", "--list", tag, "--format=%(contents)")
            try:
                found = json.loads(tag_record_text(text))
            except ValueError:
                found = None
            if found != record or self.git.out("rev-parse", f"{ref}^{{commit}}") != cs.claim.commit:
                raise PublishError(f"{tag} exists with other content; a receipt is never replaced")
            if ref not in _remote_refs(self.git, ref):
                self.git.run("push", "--quiet", "origin", ref, network=True)
            self.say(f"  ✓ receipt {tag} already written")
            return
        message = json.dumps(record, sort_keys=True, separators=(",", ":"))
        self.git.run("tag", "--annotate", tag, cs.claim.commit, "--message", message)
        self.git.run("push", "--quiet", "origin", ref, network=True)
        self.say(f"  ✓ receipt {tag} written")

    def _aliases(self, cs: ClaimState, images: Mapping[str, str]) -> None:
        for image in self.images:
            digest = images.get(image.name)
            if digest is None:
                continue
            for word in ALIASES[cs.claim.kind]:
                tag = image.tag_for(word)
                if self.registry.digest(image.repository, tag) == digest:
                    continue
                self.registry.tag(image.repository, digest, tag)
                self.say(f"  ✓ {image.repository}:{tag} → {digest[:19]}")

    def _record(self, cs: ClaimState, images: Mapping[str, str], published_at: str) -> None:
        record, digest = read_channel_record(self.github)
        current = record.get(cs.claim.kind)
        entry = ChannelEntry(
            cs.claim.version, f"v{cs.claim.version}", cs.claim.commit, dict(images), published_at
        )
        if current is not None and current.version == entry.version:
            self.say(f"  ✓ {cs.claim.kind} record already names {entry.version}")
            return
        update_channel_record(self.github, cs.claim.kind, entry, expected_digest=digest)
        self.say(f"  ✓ {cs.claim.kind} record → {entry.version}")

    def _make_public(self, cs: ClaimState, release: Mapping[str, Any]) -> None:
        final = f"v{cs.claim.version}"
        release_id = int(release["id"])
        current = self.github.release(release_id) or dict(release)
        if current.get("draft") is False:
            if current.get("tag_name") != final:
                raise PublishError(f"release {release_id} went public on the wrong tag")
            self.say(f"  ✓ {final} already public")
            return
        if current.get("tag_name") != final or strip_warnings(
            str(current.get("body") or "")
        ) != str(current.get("body") or ""):
            self.github.update_release(
                release_id,
                tag_name=final,
                target_commitish=cs.claim.commit,
                body=strip_warnings(str(current.get("body") or "")),
                draft=True,
            )
        self.github.update_release(release_id, draft=False)
        after = self.github.release(release_id) or {}
        if after.get("draft") is not False or after.get("tag_name") != final:
            raise PublishError(f"release {release_id} did not become public on {final}")
        self.say(f"  ✓ {final} is public")

    def _superseded(self, cs: ClaimState) -> bool:
        """The channel already names a newer release (aliases and record must not go back)."""
        record, _digest = read_channel_record(self.github)
        current = record.get(cs.claim.kind)
        newer = None if current is None else parse_version(current.version)
        mine = parse_version(cs.claim.version)
        return newer is not None and mine is not None and newer > mine

    def publish_one(self, cs: ClaimState) -> None:
        if cs.release is None:
            raise PublishError(f"{cs.attempt.name} has no draft release")
        self.say(f"Publishing {cs.claim.version} ({cs.attempt.name})")
        manifest = self._manifest_digest(cs.release)
        images = self._image_digests(cs)
        self._receipt(cs, manifest, images)
        if self._superseded(cs):
            self.say(f"  ! the {cs.claim.kind} channel already names a newer release")
        else:
            self._aliases(cs, images)
            when = datetime.fromtimestamp(cs.claim.epoch, UTC).isoformat()
            self._record(cs, images, published_at=when)
        self._make_public(cs, cs.release)

    def run(self, requested: str | None) -> int:
        states = self.discover()
        chosen = plan_publication(states, requested)
        if not chosen:
            self.say("Nothing to publish now.")
            return 0
        for cs in chosen:
            self.publish_one(cs)
        return 0


# -- the command line ------------------------------------------------------------------------------


def _write_outputs(values: Mapping[str, str], env: Mapping[str, str]) -> None:
    target = env.get("GITHUB_OUTPUT")
    lines = "".join(f"{k}={v}\n" for k, v in values.items())
    if target:
        with open(target, "a", encoding="utf-8") as out:
            out.write(lines)
    else:
        sys.stdout.write(lines)


def _upload(github: GitHubClient, ref: str, directory: Path) -> None:
    release = github.release_by_tag(ref)
    if release is None or release.get("draft") is not True:
        raise PublishError(f"no draft release on {ref}")
    files = sorted(p for p in directory.rglob("*") if p.is_file())
    manifest = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    if len(manifest) != len(files):
        raise PublishError("two artefacts share a file name")
    payload = {**{p.name: p.read_bytes() for p in files}}
    payload[MANIFEST] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    present = set(github.asset_names(int(release["id"])))
    for name, data in payload.items():
        if name in present:
            existing = github.read_release_asset(int(release["id"]), name)
            if existing != data:
                raise PublishError(f"{name} is already on the draft with other bytes")
            continue
        github.upload_asset(int(release["id"]), name, data)


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path = ROOT,
    env: Mapping[str, str] | None = None,
    github: GitHubClient | None = None,
    registry: Registry | None = None,
) -> int:
    values = dict(os.environ if env is None else env)
    parser = argparse.ArgumentParser(prog="publish.py", description="Release workflow steps.")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("admit")
    one.add_argument("--ref", required=True)
    one.add_argument("--default-branch", default=MAIN)
    sub.add_parser("gate")
    up = sub.add_parser("upload")
    up.add_argument("--ref", required=True)
    up.add_argument("--dir", required=True, type=Path)
    pub = sub.add_parser("publish")
    pub.add_argument("--version", default="")
    for p in (one, up, pub):
        p.add_argument(
            "--repository", default=values.get("GITHUB_REPOSITORY") or RELEASE_REPOSITORY
        )
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:
        return int(exc.code or 0)
    git = Git(root, values)
    try:
        if args.command == "gate":
            from scripts.release.claims import main as gate_main

            return gate_main(["gate"], values)
        if args.command == "admit":
            _write_outputs(admit(git, values, args.ref, args.default_branch), values)
            return 0
        if github is None:
            from scripts.release.github import RestGitHub

            github = RestGitHub(args.repository, values)
        if args.command == "upload":
            _upload(github, args.ref, args.dir)
            return 0
        controller = Controller(git, github, registry or BuildxRegistry())
        return controller.run(args.version or None)
    except (PublishError, GitError) as exc:
        detail = getattr(exc, "detail", "")
        print(f"✗ {exc}" + (f": {detail}" if detail else ""), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
