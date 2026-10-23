"""P24-21: the shape of the optional Termux lane (``termux.yml``), read as data."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

from indus.foundation import yamlio

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/termux.yml"
SECRETS = {"TERMUX_APT_SIGNING_KEY", "TERMUX_APT_SIGNING_PASSPHRASE"}
# The tag when dispatched; the admitted commit when release.yml's optional lane calls it.
CHECKOUT_REF = "${{ inputs.commit || format('refs/tags/{0}', inputs.tag) }}"


def load() -> dict[str, Any]:
    data = yamlio.load_safe(WORKFLOW)
    assert isinstance(data, dict)
    return cast(dict[str, Any], data)


def triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    raw = cast(dict[Any, Any], workflow)
    found = raw["on"] if "on" in raw else raw.get(True)  # YAML 1.1 reads a bare `on` as true
    assert isinstance(found, dict)
    return cast(dict[str, Any], found)


def steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], job.get("steps", []))


def test_the_lane_is_manual_or_called_only() -> None:
    workflow = load()
    on = triggers(workflow)
    assert set(on) == {"workflow_dispatch", "workflow_call"}  # never on push or pull request
    for kind in on.values():
        tag = kind["inputs"]["tag"]
        assert tag["required"] is True and tag["type"] == "string"
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_release_lane_names_the_admitted_commit_with_the_tag() -> None:
    """W49 integration: release.yml's opt-in ``termux`` lane passes the admitted commit; the
    build names it with the (not yet pushed) tag locally, refusing a tag that names another
    commit, before build.sh exports the tag's tree."""
    called = triggers(load())["workflow_call"]["inputs"]
    assert called["commit"] == {
        "description": called["commit"]["description"],
        "required": False,
        "type": "string",
        "default": "",
    }
    assert "commit" not in triggers(load())["workflow_dispatch"]["inputs"]
    build = steps(load()["jobs"]["build"])
    names = [s.get("name") for s in build]
    tag = build[names.index("Name the commit with the tag")]
    assert tag["if"] == "inputs.commit != ''"
    assert 'git tag "$TAG" HEAD' in tag["run"]
    assert 'test "$(git rev-parse "$TAG^{commit}")" = "$(git rev-parse HEAD)"' in tag["run"]
    assert "push" not in tag["run"]
    assert names.index("Name the commit with the tag") < names.index("Build and sign")


def test_build_runs_on_arm_in_the_protected_environment() -> None:
    jobs = load()["jobs"]
    build = jobs["build"]
    assert build["needs"] == "checks"
    assert build["runs-on"] == "ubuntu-24.04-arm"
    assert build["environment"] == "release"
    assert "environment" not in jobs["checks"]  # only the signing job sees the secrets


def test_secrets_by_name_only_and_never_echoed() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert set(re.findall(r"secrets\.([A-Z_]+)", text)) == SECRETS
    build = load()["jobs"]["build"]
    [sign] = [s for s in steps(build) if s.get("name") == "Build and sign"]
    assert {f"${{{{ secrets.{name} }}}}" for name in SECRETS} == set(sign["env"].values())
    script = str(sign["run"])
    for name in SECRETS:
        assert f'echo "${name}' not in script and f"echo ${name}" not in script
    assert "umask 077" in script and 'rm -f "$RUNNER_TEMP/apt-signing-key.asc"' in script


def test_exit_3_fails_the_lane() -> None:
    build = load()["jobs"]["build"]
    [sign] = [s for s in steps(build) if s.get("name") == "Build and sign"]
    script = str(sign["run"])
    assert "bash scripts/termux/build.sh" in script
    assert 'if [ "$code" -eq 3 ]' in script and 'exit "$code"' in script
    assert "|| true" not in script and "continue-on-error" not in build
    upload = steps(build)[-1]
    assert str(upload["uses"]).startswith("actions/upload-artifact@")
    assert upload["with"]["if-no-files-found"] == "error"


def test_checks_run_the_termux_tests_and_the_plan() -> None:
    checks = load()["jobs"]["checks"]
    text = " ".join(str(s.get("run", "")) for s in steps(checks))
    for name in ("test_termux_versions.py", "test_termux_repo.py", "test_termux_build_script.py"):
        assert name in text
        assert (ROOT / "tests/unit/release" / name).is_file()
    assert "build.sh --tag" in text and "--dry-run" in text


def test_actions_are_pinned_and_checkouts_drop_credentials() -> None:
    for job in load()["jobs"].values():
        for step in steps(job):
            uses = step.get("uses")
            if uses is None:
                continue
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", str(uses)), uses
            if str(uses).startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False
                assert step["with"]["ref"] == CHECKOUT_REF


def test_no_expression_is_pasted_into_a_script() -> None:
    for job in load()["jobs"].values():
        for step in steps(job):
            assert "${{" not in str(step.get("run", "")), step.get("name")
