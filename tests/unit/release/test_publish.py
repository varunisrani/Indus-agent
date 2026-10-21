"""P23-23: admit, the strict gate, the upload and the publication controller (R10.18, R10.23,
R10.24). Real git (a bare "origin"), ``FakeGitHub`` and ``FakeRegistry``; nothing is published."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from indus.cli.update.channels import Git
from indus.foundation.versions import RELEASE_IMAGE
from scripts.release import publish
from scripts.release.claims import IMAGES, draft_body
from scripts.release.publish import Controller, PublishError, admit, gate
from scripts.release.versioning import read_channel_record
from tests.fakes.release.fake_github import FakeGitHub, InjectedFailure
from tests.fakes.release.fake_registry import FakeRegistry, RegistryFailure
from tests.unit.release._repo import FIXED_EPOCH, Remote

DISPATCH = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main"}


@pytest.fixture
def remote(tmp_path: Path) -> Remote:
    return Remote(tmp_path)


def digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


class Release:
    """A claim the release workflow ran: attempt ref, draft, run, manifest, pushed images."""

    def __init__(
        self,
        remote: Remote,
        gh: FakeGitHub,
        registry: FakeRegistry,
        version: str,
        *,
        state: str = "success",
        autopublish: bool = False,
        lanes: tuple[str, ...] = ("ci", "docker"),
        kind: str = "stable",
    ) -> None:
        self.commit = remote.commit(f"feat: work for {version}")
        self.ref = remote.claim(
            version, self.commit, autopublish=autopublish, lanes=lanes, kind=kind
        )
        self.version = version
        draft = gh.create_release(
            tag=self.ref, name=f"v{version}", body=draft_body(f"notes {version}"), draft=True,
            prerelease=kind == "canary", target=self.commit,
        )  # fmt: skip
        self.release_id = draft["id"]
        self.manifest = json.dumps({"app.zip": "ab" * 32}).encode()
        gh.upload_asset(self.release_id, "manifest.json", self.manifest)
        if state == "running":
            gh.add_run("release.yml", self.ref, "in_progress", None)
        else:
            gh.add_run("release.yml", self.ref, "completed", state)
        self.digests = {}
        if "docker" in lanes:
            for image in IMAGES:
                self.digests[image.name] = digest(f"{image.name}-{version}")
                registry.push(image.repository, image.tag_for(self.ref), self.digests[image.name])


def controller(remote: Remote, gh: FakeGitHub, registry: FakeRegistry) -> Controller:
    return Controller(Git(remote.clone, remote.env), gh, registry, say=lambda _l: None)


def fakes() -> tuple[FakeGitHub, FakeRegistry]:
    gh = FakeGitHub()
    return gh, FakeRegistry(events=gh.calls)  # one ordered log across both services


# -- admit -----------------------------------------------------------------------------------------


def test_admit_outputs_the_claimed_commit(remote: Remote) -> None:
    sha = remote.commit("feat: one")
    ref = remote.claim("0.0.1", sha, lanes=("ci", "nix"))
    later = remote.commit("feat: main moved on")  # github.sha would be this one
    out = admit(Git(remote.clone, remote.env), DISPATCH, ref, "main")
    assert out["commit"] == sha != later
    assert out["version"] == "0.0.1" and out["tag"] == "v0.0.1" and out["kind"] == "stable"
    assert json.loads(out["lanes"]) == ["ci", "nix"]
    jobs = json.loads(out["jobs"])
    assert jobs["ci"] is True and jobs["docker"] is False and jobs["upload"] is True
    assert out["epoch"] == str(FIXED_EPOCH)


def test_admit_refuses_a_run_that_is_not_the_default_branch_dispatch(remote: Remote) -> None:
    sha = remote.commit("feat: one")
    ref = remote.claim("0.0.1", sha)
    git = Git(remote.clone, remote.env)
    with pytest.raises(PublishError, match="workflow_dispatch"):
        admit(git, {**DISPATCH, "GITHUB_EVENT_NAME": "push"}, ref, "main")
    with pytest.raises(PublishError, match="dispatched from main"):
        admit(git, {**DISPATCH, "GITHUB_REF": f"refs/tags/{ref}"}, ref, "main")
    with pytest.raises(PublishError, match="not an attempt ref"):
        admit(git, DISPATCH, "v0.0.1", "main")


def test_admit_refuses_bad_claims(remote: Remote) -> None:
    first = remote.commit("feat: one")
    git = Git(remote.clone, remote.env)
    remote.git("tag", "rc.1-v0.0.1", first)  # lightweight
    remote.git("push", "--quiet", "origin", "refs/tags/rc.1-v0.0.1")
    with pytest.raises(PublishError, match="annotated"):
        admit(git, DISPATCH, "rc.1-v0.0.1", "main")
    second = remote.commit("feat: two")
    other = remote.claim("0.0.2", first, attempt=1)  # claim names first, tag is on first: fine
    assert admit(git, DISPATCH, other, "main")["commit"] == first
    from scripts.release.claims import Claim

    lying = Claim("stable", "0.0.3", 1, first, False, frozenset({"ci"}), FIXED_EPOCH)
    remote.tag("rc.1-v0.0.3", second, lying.message(), epoch=FIXED_EPOCH)
    with pytest.raises(PublishError, match="another version, attempt or commit"):
        admit(git, DISPATCH, "rc.1-v0.0.3", "main")
    skewed = Claim("stable", "0.0.4", 1, second, False, frozenset({"ci"}), FIXED_EPOCH)
    remote.tag("rc.1-v0.0.4", second, skewed.message(), epoch=FIXED_EPOCH + 5)
    with pytest.raises(PublishError, match="epoch"):
        admit(git, DISPATCH, "rc.1-v0.0.4", "main")
    side = remote.commit("feat: not on main", push=False)
    remote.claim("0.0.5", side)
    with pytest.raises(PublishError, match="not on origin/main"):
        admit(git, DISPATCH, "rc.1-v0.0.5", "main")


# -- gate ------------------------------------------------------------------------------------------


def needs(**results: str) -> dict[str, dict[str, str]]:
    base = {job: "success" for job in ("admit", "ci", "docker", "nix", "install-e2e",
            "desktop-mac", "desktop-win", "desktop-linux", "upgrade", "upload")}  # fmt: skip
    base.update(results)
    return {job: {"result": result} for job, result in base.items()}


def test_gate_counts_a_skipped_selected_job_as_failed() -> None:
    every = ["ci", "docker", "nix", "install-e2e", "desktop-mac", "desktop-win",
             "desktop-linux", "upgrade"]  # fmt: skip
    assert gate(needs(), every) == []
    assert gate(needs(nix="skipped"), every) == ["nix=skipped (expected success)"]
    assert gate(needs(ci="failure"), every) == ["ci=failure (expected success)"]
    some = ["ci", "docker"]
    off = {j: "skipped" for j in ("nix", "install-e2e", "desktop-mac", "desktop-win",
                                   "desktop-linux", "upgrade")}  # fmt: skip
    assert gate(needs(**off), some) == []
    assert gate(needs(**{**off, "nix": "success"}), some) == ["nix=success (expected skipped)"]
    missing = needs(**off)
    del missing["upload"]
    assert gate(missing, some) == ["upload=missing (expected success)"]
    assert gate(needs(**off, admit="failure"), some)[0].startswith("admit=failure")


def test_gate_command_reads_needs_and_lanes(tmp_path: Path) -> None:
    env = {"NEEDS": json.dumps(needs(upgrade="skipped")), "LANES": json.dumps(["ci", "upgrade"])}
    assert publish.main(["gate"], root=tmp_path, env=env) == 1
    env["LANES"] = json.dumps(["ci", "docker", "nix", "install-e2e", "desktop-mac",
                               "desktop-win", "desktop-linux"])  # fmt: skip
    assert publish.main(["gate"], root=tmp_path, env=env) == 0


# -- upload ----------------------------------------------------------------------------------------


def test_upload_sends_artefacts_and_manifest_once(remote: Remote, tmp_path: Path) -> None:
    gh = FakeGitHub()
    sha = remote.commit("feat: one")
    gh.create_release(tag="rc.1-v0.0.1", name="v0.0.1", body="", draft=True, prerelease=False,
                      target=sha)  # fmt: skip
    built = tmp_path / "artefacts"
    (built / "mac").mkdir(parents=True)
    (built / "mac" / "Indus.dmg").write_bytes(b"dmg bytes")
    (built / "indus.AppImage").write_bytes(b"appimage bytes")
    args = ["upload", "--ref", "rc.1-v0.0.1", "--dir", str(built)]
    assert publish.main(args, root=remote.clone, env=remote.env, github=gh) == 0
    release_id = gh.releases[0]["id"]
    assert gh.asset_names(release_id) == ["Indus.dmg", "indus.AppImage", "manifest.json"]
    manifest = json.loads(gh.read_release_asset(release_id, "manifest.json") or b"{}")
    assert manifest["Indus.dmg"] == hashlib.sha256(b"dmg bytes").hexdigest()
    assert publish.main(args, root=remote.clone, env=remote.env, github=gh) == 0  # a rerun: no-op
    (built / "indus.AppImage").write_bytes(b"rebuilt bytes")
    assert publish.main(args, root=remote.clone, env=remote.env, github=gh) == 1  # never replaced


# -- the controller --------------------------------------------------------------------------------


def test_release_is_made_public_after_the_aliases(remote: Remote) -> None:
    gh, registry = fakes()
    made = Release(remote, gh, registry, "0.0.1", autopublish=True)
    assert controller(remote, gh, registry).run(None) == 0
    calls = gh.calls
    public = calls.index(f"publish_release:{made.release_id}")
    aliases = [i for i, c in enumerate(calls) if c.startswith("alias:")]
    record = calls.index("replace_asset:channels/channels.json")
    assert aliases and max(aliases) < record < public
    assert calls[-1] == f"publish_release:{made.release_id}"
    receipt = remote.message("v0.0.1")
    assert receipt["commit"] == made.commit and receipt["attempt_ref"] == made.ref
    assert receipt["manifest_sha256"] == hashlib.sha256(made.manifest).hexdigest()
    assert receipt["images"] == made.digests
    assert registry.digest(RELEASE_IMAGE, "stable") == made.digests["product"]
    assert registry.digest(RELEASE_IMAGE, "latest-browser") == made.digests["product-browser"]
    record_now, _ = read_channel_record(gh)
    assert record_now.stable is not None and record_now.stable.version == "0.0.1"
    published = gh.release(made.release_id)
    assert published is not None
    assert published["tag_name"] == "v0.0.1" and published["draft"] is False
    assert "Do not publish" not in published["body"] and "notes 0.0.1" in published["body"]


def test_rerun_after_alias_failure_completes_without_duplicates(remote: Remote) -> None:
    gh, registry = fakes()
    made = Release(remote, gh, registry, "0.0.1", autopublish=True)
    registry.fail_once("tag")
    with pytest.raises(RegistryFailure):
        controller(remote, gh, registry).run(None)
    assert gh.releases[0]["draft"] is True  # nothing went public
    assert "v0.0.1" in remote.remote_tags()  # the receipt came first
    assert controller(remote, gh, registry).run(None) == 0
    tags = [line for line in remote.git("ls-remote", "--tags", "origin").splitlines()
            if line.endswith("refs/tags/v0.0.1")]  # fmt: skip
    assert len(tags) == 1
    assert gh.calls.count("replace_asset:channels/channels.json") == 1
    assert gh.calls.count(f"publish_release:{made.release_id}") == 1
    assert gh.releases[0]["draft"] is False
    # a third run finds everything done and changes nothing
    before = list(gh.calls)
    assert controller(remote, gh, registry).run(None) == 0
    assert gh.calls == before


def test_rerun_after_a_failure_making_it_public(remote: Remote) -> None:
    gh, registry = fakes()
    Release(remote, gh, registry, "0.0.1", autopublish=True)
    gh.fail_once("publish_release")
    with pytest.raises(InjectedFailure):
        controller(remote, gh, registry).run(None)
    assert controller(remote, gh, registry).run(None) == 0
    assert gh.releases[0]["draft"] is False and gh.asset_writes == 1  # one record write


def test_older_green_claim_is_published_first(remote: Remote) -> None:
    gh, registry = fakes()
    older = Release(remote, gh, registry, "0.0.1")  # green, held (no autopublish)
    newer = Release(remote, gh, registry, "0.0.2", autopublish=True)
    assert controller(remote, gh, registry).run(None) == 0
    order = [c for c in gh.calls if c.startswith("publish_release:")]
    assert order == [f"publish_release:{older.release_id}", f"publish_release:{newer.release_id}"]
    record, _ = read_channel_record(gh)
    assert record.stable is not None and record.stable.version == "0.0.2"
    assert registry.digest(RELEASE_IMAGE, "stable") == newer.digests["product"]


def test_running_older_claim_blocks_newer(remote: Remote) -> None:
    gh, registry = fakes()
    Release(remote, gh, registry, "0.0.1", state="running")
    Release(remote, gh, registry, "0.0.2", autopublish=True)
    assert controller(remote, gh, registry).run(None) == 0
    assert all(r["draft"] is True for r in gh.releases)
    assert "v0.0.2" not in remote.remote_tags()


def test_held_green_claim_waits_until_requested(remote: Remote) -> None:
    gh, registry = fakes()
    made = Release(remote, gh, registry, "0.0.1")
    assert controller(remote, gh, registry).run(None) == 0
    assert gh.releases[0]["draft"] is True
    assert controller(remote, gh, registry).run("0.0.1") == 0
    assert gh.releases[0]["draft"] is False and gh.releases[0]["id"] == made.release_id


def test_failed_and_abandoned_claims_are_skipped(remote: Remote) -> None:
    gh, registry = fakes()
    Release(remote, gh, registry, "0.0.1", state="failure")
    abandoned = Release(remote, gh, registry, "0.0.2", autopublish=True)
    remote.tag(f"abandoned-{abandoned.ref}", abandoned.commit, '{"abandons": 1}')
    good = Release(remote, gh, registry, "0.0.3", autopublish=True)
    assert controller(remote, gh, registry).run(None) == 0
    assert [c for c in gh.calls if c.startswith("publish_release:")] == [
        f"publish_release:{good.release_id}"
    ]


def test_receipt_with_other_content_is_never_replaced(remote: Remote) -> None:
    gh, registry = fakes()
    made = Release(remote, gh, registry, "0.0.1", autopublish=True)
    remote.tag("v0.0.1", made.commit, '{"schema": 1, "version": "0.0.1"}')
    with pytest.raises(PublishError, match="never replaced"):
        controller(remote, gh, registry).run(None)
    assert gh.releases[0]["draft"] is True


def test_canary_moves_only_the_canary_alias(remote: Remote) -> None:
    gh, registry = fakes()
    made = Release(remote, gh, registry, "0.0.1-canary.20261021T064100Z", autopublish=True,
                   kind="canary")  # fmt: skip
    assert controller(remote, gh, registry).run(None) == 0
    assert registry.digest(RELEASE_IMAGE, "canary") == made.digests["product"]
    assert registry.digest(RELEASE_IMAGE, "stable") is None
    record, _ = read_channel_record(gh)
    assert record.canary is not None and record.stable is None


def test_claim_without_docker_binds_no_images(remote: Remote) -> None:
    gh, registry = fakes()
    Release(remote, gh, registry, "0.0.1", autopublish=True, lanes=("ci",))
    assert controller(remote, gh, registry).run(None) == 0
    assert remote.message("v0.0.1")["images"] == {}
    assert not any(c.startswith("alias:") for c in gh.calls)
