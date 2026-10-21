"""The image publishing workflow read statically (P23-12, R10.23, R10.24). It never runs here."""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import Any

import pytest
from ruamel.yaml import YAML

from tests.unit.packaging.docker_files import ROOT, WORKFLOW

COMMIT_REF = "${{ inputs.commit || github.sha }}"
ALIASES = ("latest", "stable", "canary")


def workflow() -> dict[str, Any]:
    data = YAML(typ="safe").load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def jobs() -> dict[str, dict[str, Any]]:
    return workflow()["jobs"]


def steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return job.get("steps", [])


def all_steps() -> list[tuple[str, dict[str, Any]]]:
    return [(name, step) for name, job in jobs().items() for step in steps(job)]


def test_triggers_and_release_inputs() -> None:
    on = workflow()["on"]
    assert set(on) == {"pull_request", "push", "workflow_call"}
    assert on["push"]["branches"] == ["main"]
    inputs = on["workflow_call"]["inputs"]
    for name in ("commit", "attempt_ref", "phase"):
        assert inputs[name]["required"] is True and inputs[name]["type"] == "string"
    outputs = on["workflow_call"]["outputs"]
    assert set(outputs) == {
        "product-digest",
        "product-browser-digest",
        "sandbox-digest",
        "sandbox-browser-digest",
    }


def test_every_action_is_pinned_to_a_full_commit_sha() -> None:
    uses = [step["uses"] for _, step in all_steps() if "uses" in step]
    assert uses
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}", ref), ref


def test_every_checkout_takes_the_commit_under_test() -> None:
    """R10.24: in a release run the workflow's own SHA is not the admitted commit."""
    checkouts = [
        (name, step)
        for name, step in all_steps()
        if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert {name for name, _ in checkouts} == {"lint", "build", "push", "manifest"}
    for name, step in checkouts:
        assert step["with"]["ref"] == COMMIT_REF, name
        assert step["with"]["persist-credentials"] is False, name
    assert workflow()["env"]["COMMIT"] == COMMIT_REF


def test_release_inputs_are_validated_before_building() -> None:
    lint = steps(jobs()["lint"])
    check = next(s for s in lint if s.get("name") == "Check the release inputs")
    assert check["if"] == "${{ inputs.phase }}"
    assert "[0-9a-f]{40}" in check["run"] and "rc\\." in check["run"]
    assert jobs()["build"]["needs"] == "lint"


def test_no_job_moves_a_channel_alias() -> None:
    """R10.23: latest / stable / canary (and the sandbox's slim / browser aliases) move only in
    the publication controller, from a receipt-bound digest."""
    text = WORKFLOW.read_text(encoding="utf-8")
    runs = "\n".join(step.get("run", "") for _, step in all_steps())
    for alias in ALIASES:
        assert not re.search(rf":{alias}\b", text), alias
        assert not re.search(rf"\btag=[\"']?{alias}\b", runs), alias
    # every tag the workflow writes starts from `main` or the attempt ref
    assignments = re.findall(r"\b(?:tag|base)=([^\n;]+)", runs)
    assert len(assignments) >= 6
    for value in assignments:
        assert value.startswith(('"$ATTEMPT_REF"', "main", '"$base-', '"$tag-')), value


def test_publishing_never_rebuilds() -> None:
    for name in ("push", "manifest", "digests"):
        runs = "\n".join(step.get("run", "") for step in steps(jobs()[name]))
        assert not re.search(r"docker build(\s|$)|buildx build", runs), name
    push = steps(jobs()["push"])
    assert any(s.get("uses", "").startswith("actions/download-artifact@") for s in push)
    pushing = next(s for s in push if s.get("name") == "Push the tested bytes")
    assert "sha256sum -c image.tar.sha256" in pushing["run"]
    assert "skopeo copy" in pushing["run"] and "docker-archive:image.tar" in pushing["run"]


def test_release_publish_skips_build_and_lint() -> None:
    assert jobs()["lint"]["if"] == "${{ inputs.phase != 'publish' }}"
    assert jobs()["build"]["if"] == "${{ inputs.phase != 'publish' }}"
    for name in ("push", "manifest", "digests"):
        condition = jobs()[name]["if"]
        assert "github.event_name == 'push' || inputs.phase == 'publish'" in condition
        assert "!failure()" in condition and "!cancelled()" in condition


def test_only_publishing_jobs_may_write_packages() -> None:
    assert workflow()["permissions"] == {"contents": "read"}
    for name, job in jobs().items():
        perms = job.get("permissions", {})
        if name in ("push", "manifest"):
            assert perms == {"contents": "read", "packages": "write"}, name
        else:
            assert "packages" not in perms, name


def test_pull_requests_never_log_in_or_push() -> None:
    for name in ("lint", "build"):
        runs = "\n".join(step.get("run", "") for step in steps(jobs()[name]))
        assert "login" not in runs and "skopeo copy" not in runs and "docker push" not in runs
    keep = next(s for s in steps(jobs()["build"]) if s.get("name") == "Keep the tested image")
    assert keep["if"] == "${{ github.event_name == 'push' || inputs.phase == 'test' }}"


def test_matrix_covers_every_image_variant_and_native_arch() -> None:
    build = jobs()["build"]
    matrix = build["strategy"]["matrix"]
    assert matrix["image"] == ["product", "sandbox"]
    assert matrix["variant"] == ["slim", "browser"]
    assert matrix["arch"] == ["amd64", "arm64"]
    runners = {entry["arch"]: entry["runner"] for entry in matrix["include"]}
    assert runners == {"amd64": "ubuntu-24.04", "arm64": "ubuntu-24.04-arm"}
    assert build["runs-on"] == "${{ matrix.runner }}"
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "qemu" not in text.lower()
    push = jobs()["push"]["strategy"]["matrix"]
    assert (push["image"], push["variant"], push["arch"]) == (
        matrix["image"],
        matrix["variant"],
        matrix["arch"],
    )


def test_image_tests_run_against_the_built_tag() -> None:
    runs = "\n".join(step.get("run", "") for step in steps(jobs()["build"]))
    assert "pytest -q -m docker tests/docker" in runs
    assert "INDUS_TEST_IMAGE=" in runs and "INDUS_TEST_SANDBOX_IMAGE=" in runs
    assert "scripts/docker/pins.py fetch uv" in runs


def test_registry_coordinates_come_from_the_pins() -> None:
    runs = "\n".join(step.get("run", "") for _, step in all_steps())
    assert "ghcr.io/" not in runs
    assert (
        runs.count('scripts/docker/pins.py" image') + runs.count("scripts/docker/pins.py image")
        == 2
    )


def test_release_concurrency_never_cancels() -> None:
    concurrency = workflow()["concurrency"]
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    assert "inputs.attempt_ref" in concurrency["group"] and "inputs.phase" in concurrency["group"]


def test_actionlint_is_clean_when_installed() -> None:
    actionlint = shutil.which("actionlint")
    if actionlint is None:
        pytest.skip("actionlint is not installed (the Docker workflow's lint job runs it)")
    done = subprocess.run(
        [actionlint, str(WORKFLOW)], capture_output=True, text=True, cwd=ROOT, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr
