"""P23-23: the shape of ``release.yml`` and ``canary.yml`` (R10.18, R10.23, R10.24).

The workflows never run here; these tests read them as data. They pin what the release process
relies on: dispatch only from the default branch, the admitted commit (not ``github.sha``)
checked out by every job that builds or tests, a strict gate over every job, publication only
after the gate, actions pinned by full SHA and no workflow input pasted into a shell script.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest

from indus.foundation import yamlio
from scripts.release.claims import JOB_LANES

ROOT = Path(__file__).resolve().parents[3]
RELEASE = ROOT / ".github/workflows/release.yml"
CANARY = ROOT / ".github/workflows/canary.yml"
ADMITTED = "${{ needs.admit.outputs.commit }}"
SCRIPT_JOBS = {"admit", "upload", "gate", "publish"}
"""Jobs that run only the release scripts (from the default branch) and build nothing."""
AFTER_GATE = {"docker-publish"}
"""Jobs that run after the gate: they push what the gated jobs built (nothing is rebuilt)."""


def load(path: Path) -> dict[str, Any]:
    data = yamlio.load_safe(path)
    assert isinstance(data, dict)
    return data


def triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    raw = cast(dict[Any, Any], workflow)
    found = raw["on"] if "on" in raw else raw.get(True)  # YAML 1.1 reads a bare `on` as true
    assert isinstance(found, dict)
    return found


@pytest.fixture(scope="module")
def release() -> dict[str, Any]:
    return load(RELEASE)


@pytest.fixture(scope="module")
def jobs(release: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return release["jobs"]


def checkout_refs(job: dict[str, Any]) -> list[str]:
    return [
        str((step.get("with") or {}).get("ref", ""))
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]


def test_release_is_dispatch_only_from_the_default_branch(release: dict[str, Any]) -> None:
    on = triggers(release)
    assert set(on) == {"workflow_dispatch"}
    attempt = on["workflow_dispatch"]["inputs"]["attempt_ref"]
    assert attempt["required"] is True and attempt["type"] == "string"
    assert release["run-name"] == "Release ${{ inputs.attempt_ref }}"  # the controller reads it
    assert release["concurrency"]["cancel-in-progress"] is False
    assert release["permissions"] == {"contents": "read"}
    admit = release["jobs"]["admit"]
    step = next(s for s in admit["steps"] if s.get("id") == "admit")
    assert "publish.py admit" in step["run"] and "--default-branch" in step["run"]
    assert step["env"]["DEFAULT_BRANCH"] == "${{ github.event.repository.default_branch }}"


def test_lane_jobs_match_the_lane_table(jobs: dict[str, dict[str, Any]]) -> None:
    lane_jobs = set(jobs) - SCRIPT_JOBS - AFTER_GATE
    assert lane_jobs | {"upload"} == set(JOB_LANES)
    for name in lane_jobs:
        job = jobs[name]
        assert job["needs"] == "admit" or "admit" in job["needs"], name
        assert job["if"] == f"fromJSON(needs.admit.outputs.jobs)['{name}']", name


def test_every_build_job_uses_the_admitted_commit(jobs: dict[str, dict[str, Any]]) -> None:
    for name, job in jobs.items():
        if name in SCRIPT_JOBS:
            continue
        if "uses" in job:  # a called workflow gets the commit as an input
            assert job["with"]["commit"] == ADMITTED, name
            continue
        refs = checkout_refs(job)
        assert refs and all(ref == ADMITTED for ref in refs), name
        assert "github.sha" not in str(job), name


def test_script_jobs_never_build_the_default_branch(jobs: dict[str, dict[str, Any]]) -> None:
    for name in SCRIPT_JOBS:
        text = str(jobs[name])
        assert "pnpm" not in text and "docker build" not in text and "pytest" not in text, name
        refs = checkout_refs(jobs[name])
        if name == "upload":  # its second checkout is the admitted commit's installers only
            assert refs[1:] == [ADMITTED]
            refs = refs[:1]
        assert all(ref == "${{ github.sha }}" for ref in refs), name


INSTALLERS = ("scripts/install.sh", "scripts/install.ps1", "scripts/install.cmd")


def test_upload_attaches_the_installers_from_the_admitted_commit(
    jobs: dict[str, dict[str, Any]],
) -> None:
    """F-1 (D-16.960): the install guide and ``install.cmd`` download the installers from
    ``releases/latest/download``, so every release carries them, taken from the admitted commit
    (a sparse checkout of just those files) and listed in the manifest like any artefact."""
    steps = jobs["upload"]["steps"]
    names = [step.get("name") for step in steps]
    checkout = steps[names.index("Check out the installers (admitted commit)")]
    options = checkout["with"]
    assert options["ref"] == ADMITTED and options["persist-credentials"] is False
    assert options["sparse-checkout"].split() == list(INSTALLERS)
    assert options["sparse-checkout-cone-mode"] is False
    add = steps[names.index("Add the installers")]
    for path in INSTALLERS:
        assert f"{options['path']}/{path}" in add["run"]
        assert (ROOT / path).is_file(), path
    assert '"$RUNNER_TEMP/artefacts/"' in add["run"]
    upload = steps[names.index("Upload with the manifest")]
    assert names.index("Collect the tested artefacts") < names.index("Add the installers")
    assert names.index("Add the installers") < names.index("Upload with the manifest")
    assert '--dir "$RUNNER_TEMP/artefacts"' in upload["run"]
    shipped = {Path(path).name for path in INSTALLERS}
    guide = (ROOT / "docs/user/getting-started/install.md").read_text(encoding="utf-8")
    shim = (ROOT / "scripts/install.cmd").read_text(encoding="utf-8")
    wanted = set(re.findall(r"releases/latest/download/([\w.]+)", guide + shim))
    assert wanted and wanted <= shipped


def test_every_release_carries_the_dashboard_pages(jobs: dict[str, dict[str, Any]]) -> None:
    """F-3 (wave 46): ``web/bundle.py`` fetches ``indus-web-<version>.tar.gz`` from the release
    of HEAD's tag, so every release builds the pages from the admitted commit (no lane: it always
    runs), sweeps them with the clean-room artefact guard, keeps the tarball, and ``upload``
    attaches it with the manifest after the installers' kin."""
    from indus.web.bundle import RELEASE_ASSET

    assert JOB_LANES["web-pages"] == frozenset()
    job = jobs["web-pages"]
    assert job["env"]["VERSION"] == "${{ needs.admit.outputs.version }}"
    steps = job["steps"]
    names = [step.get("name") for step in steps]
    build = steps[names.index("Build the pages")]["run"]
    assert "pnpm install --frozen-lockfile" in build and "pnpm --filter @indus/web build" in build
    pack = steps[names.index("Pack the pages")]["run"]
    tarball = '"$RUNNER_TEMP/web-pages/' + RELEASE_ASSET.format(version="${VERSION}") + '"'
    assert f"tar -C apps/web/dist -czf {tarball} ." in pack
    sweep = " ".join(steps[names.index("Sweep the pages")]["run"].split())
    assert "tooling.checks artifacts --require-ref" in sweep
    assert "--artifact apps/web/dist" in sweep and f"--artifact {tarball}" in sweep
    assert (
        "INDUS_REF="
        in steps[names.index("Fetch the pinned reference (outside the workspace)")]["run"]
    )
    assert names.index("Build the pages") < names.index("Pack the pages")
    assert names.index("Pack the pages") < names.index("Sweep the pages")
    assert names.index("Sweep the pages") < names.index("Keep the pages")
    kept = steps[names.index("Keep the pages")]["with"]
    assert kept["name"] == "web-pages" and kept["if-no-files-found"] == "error"
    upload = jobs["upload"]
    assert "web-pages" in upload["needs"]
    unames = [step.get("name") for step in upload["steps"]]
    collect = upload["steps"][unames.index("Collect the dashboard pages")]["with"]
    assert collect == {"name": "web-pages", "path": "${{ runner.temp }}/artefacts"}
    assert unames.index("Collect the dashboard pages") < unames.index("Upload with the manifest")


def test_gate_needs_every_job_and_always_runs(jobs: dict[str, dict[str, Any]]) -> None:
    gate = jobs["gate"]
    assert gate["if"] == "always()"
    assert set(gate["needs"]) == set(jobs) - {"gate", "publish"} - AFTER_GATE
    step = gate["steps"][-1]
    assert step["run"] == "python3 scripts/release/claims.py gate"
    assert step["env"] == {
        "NEEDS": "${{ toJSON(needs) }}",
        "LANES": "${{ needs.admit.outputs.lanes }}",
    }


def test_publication_comes_only_after_the_gate(jobs: dict[str, dict[str, Any]]) -> None:
    publish = jobs["publish"]
    assert set(publish["needs"]) == {"admit", "gate", "docker-publish"}
    condition = " ".join(str(publish["if"]).split())
    assert "!cancelled()" in condition and "needs.gate.result == 'success'" in condition
    assert "needs.docker-publish.result" in condition and "failure" not in condition
    pushed = jobs["docker-publish"]
    assert set(pushed["needs"]) == {"admit", "gate"}
    assert pushed["if"] == "${{ success() && fromJSON(needs.admit.outputs.jobs)['docker'] }}"
    assert publish["environment"] == "release"
    assert publish["concurrency"]["cancel-in-progress"] is False
    assert "publish.py publish" in publish["steps"][-1]["run"]
    upload = jobs["upload"]
    assert "!contains(needs.*.result, 'failure')" in upload["if"]
    assert "publish.py upload" in upload["steps"][-1]["run"]


def test_docker_lane_tests_then_pushes_by_attempt_ref(jobs: dict[str, dict[str, Any]]) -> None:
    """``docker.yml``'s release interface (P23-12): ``commit``, ``attempt_ref`` and ``phase`` —
    ``test`` in the gated lane, ``publish`` (push the kept archives) after the gate."""
    called = load(ROOT / ".github/workflows/docker.yml")
    inputs = triggers(called)["workflow_call"]["inputs"]
    for name, phase in (("docker", "test"), ("docker-publish", "publish")):
        job = jobs[name]
        assert job["uses"] == "./.github/workflows/docker.yml"
        assert set(job["with"]) == set(inputs)
        assert job["with"]["commit"] == ADMITTED
        assert job["with"]["attempt_ref"] == "${{ needs.admit.outputs.attempt-ref }}"
        assert job["with"]["phase"] == phase
        assert "latest" not in str(job) and "stable" not in str(job)


def build_step(job: dict[str, Any]) -> dict[str, Any]:
    """The step that runs the desktop packaging script (``dist:unsigned`` or ``dist-<os>.mjs``)."""
    return next(
        s
        for s in job["steps"]
        if "dist:unsigned" in str(s.get("run", "")) or "scripts/dist-" in str(s.get("run", ""))
    )


def test_desktop_lanes_use_the_packaging_scripts(jobs: dict[str, dict[str, Any]]) -> None:
    """P20-38 packaging into ``apps/desktop/build/release``. Linux stays unsigned on
    ``dist:unsigned`` and takes no secret (P23-16); the macOS (``dist-mac.mjs``, P23-15) and
    Windows (``dist-win.mjs``, P23-16) signing lanes are checked on their own."""
    for name in ("desktop-mac", "desktop-win", "desktop-linux"):
        keep = jobs[name]["steps"][-1]
        assert keep["with"]["path"] == "apps/desktop/build/release/*.*"
    linux = build_step(jobs["desktop-linux"])
    assert "pnpm --filter @indus/desktop run dist:unsigned" in linux["run"]
    assert "secrets." not in str(jobs["desktop-linux"])


WIN_SECRETS = (
    # the names apps/desktop/scripts/sign-win.mjs reads (and scripts/release/msix.mjs, P24-22)
    "WIN_CERTIFICATE",
    "WIN_CERTIFICATE_PASSWORD",
    "AZURE_TRUSTED_SIGNING_ENDPOINT",
    "AZURE_TRUSTED_SIGNING_ACCOUNT",
    "AZURE_TRUSTED_SIGNING_PROFILE",
    "AZURE_TENANT_ID",
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
)


def test_windows_lane_signs_with_the_release_environment_secrets(
    jobs: dict[str, dict[str, Any]],
) -> None:
    """P23-16: the Windows lane runs in the protected ``release`` environment; each secret reaches
    only the build step, under its own name, through ``env``; dependencies install earlier without
    them; the build is ``dist-win.mjs`` with plain node (unsigned with one line when absent)."""
    job = jobs["desktop-win"]
    assert job["environment"] == "release"
    build = build_step(job)
    assert build["run"].strip() == "node scripts/dist-win.mjs"
    assert build["working-directory"] == "apps/desktop"
    env = build["env"]
    for name in WIN_SECRETS:
        assert env[name] == f"${{{{ secrets.{name} }}}}", name
    dlib = " ".join(str(env["INDUS_AZURE_SIGNING_DLIB"]).split())
    assert dlib.startswith("${{ secrets.AZURE_TRUSTED_SIGNING_ENDPOINT != '' && format(")
    assert "Azure.CodeSigning.Dlib.dll" in dlib and dlib.endswith("|| '' }}")
    assert not [k for k in env if k.startswith(("CSC_", "WIN_CSC_"))]
    steps = job["steps"]
    install = next(s for s in steps if "pnpm install" in str(s.get("run", "")))
    assert steps.index(install) < steps.index(build)
    for step in steps:
        run = str(step.get("run", ""))
        assert "secrets." not in run
        assert not re.search(r"\b(echo|printf)\b", run)
        assert "set -x" not in run
        if step is not build:
            assert "secrets." not in str(step), step.get("name")
    client = next(s for s in steps if "Trusted.Signing.Client" in str(s.get("run", "")))
    assert steps.index(client) < steps.index(build)
    signer = (ROOT / "apps/desktop/scripts/sign-win.mjs").read_text(encoding="utf-8")
    for name in (*WIN_SECRETS, "INDUS_AZURE_SIGNING_DLIB"):
        assert f'"{name}"' in signer, name


def test_linux_lane_checks_its_sums(jobs: dict[str, dict[str, Any]]) -> None:
    """P23-16: the Linux artefacts ship unsigned with SHA-256 sums, checked before they are kept."""
    steps = jobs["desktop-linux"]["steps"]
    check = next(s for s in steps if "checksums.mjs check" in str(s.get("run", "")))
    assert "apps/desktop/build/release/SHA256SUMS-linux.txt" in check["run"]
    assert steps.index(build_step(jobs["desktop-linux"])) < steps.index(check) < len(steps) - 1


def test_optional_lanes_exist_but_never_run_by_default(jobs: dict[str, dict[str, Any]]) -> None:
    """W49 integration: ``desktop-msix`` (P24-22) and ``termux`` (P24-21) are lanes like any other
    (gated, judged against the claim) but optional, so a claim without them skips both."""
    from scripts.release.claims import DEFAULT_LANES, OPTIONAL_LANES, jobs_to_run

    assert {"desktop-msix", "termux"} == OPTIONAL_LANES
    assert not OPTIONAL_LANES & DEFAULT_LANES
    default = jobs_to_run(DEFAULT_LANES)
    for name in OPTIONAL_LANES:
        assert name in jobs and default[name] is False, name
        assert name in jobs["gate"]["needs"], name
    assert "desktop-msix" in jobs["upload"]["needs"]  # its bundle and feed go onto the draft


def test_msix_lane_packs_the_windows_lanes_own_apps(jobs: dict[str, dict[str, Any]]) -> None:
    """P24-22 wired in: the Windows lane keeps its unpacked apps only when ``desktop-msix`` is on,
    under a name the upload never collects; the MSIX lane packs those (nothing rebuilt, no package
    install), signs with the Windows secret names only in its build step, and keeps the bundle and
    the App Installer feed as a ``desktop-*`` artefact."""
    win = jobs["desktop-win"]["steps"]
    kept = next(s for s in win if s.get("name") == "Keep the unpacked apps (desktop-msix lane)")
    assert kept["if"] == "fromJSON(needs.admit.outputs.jobs)['desktop-msix']"
    assert kept["with"]["name"] == "win-unpacked"  # not collected by the upload's `desktop-*`
    assert win.index(kept) < len(win) - 1
    job = jobs["desktop-msix"]
    assert job["needs"] == ["admit", "desktop-win"]
    assert job["environment"] == "release"
    steps = job["steps"]
    collect = next(s for s in steps if str(s.get("uses", "")).startswith("actions/download-"))
    assert collect["with"] == {"name": "win-unpacked", "path": "apps/desktop/build/release"}
    assert "pnpm" not in str(job) and "dist-win" not in str(job)
    build = next(s for s in steps if "scripts/release/msix.mjs build" in str(s.get("run", "")))
    assert "--store" not in build["run"]
    env = build["env"]
    assert env["VERSION"] == "${{ needs.admit.outputs.version }}"
    for name in WIN_SECRETS:
        assert env[name] == f"${{{{ secrets.{name} }}}}", name
    assert (
        env["INDUS_AZURE_SIGNING_DLIB"]
        == jobs["desktop-win"]["steps"][[s.get("name") for s in win].index("Build and sign")][
            "env"
        ]["INDUS_AZURE_SIGNING_DLIB"]
    )
    for step in steps:
        if step is not build:
            assert "secrets." not in str(step), step.get("name")
        assert not re.search(r"\b(echo|printf)\b", str(step.get("run", "")))
    keep = steps[-1]
    assert keep["with"]["name"] == "desktop-msix"
    assert keep["with"]["path"].splitlines() == [
        "${{ runner.temp }}/msix/*.msixbundle",
        "${{ runner.temp }}/msix/*.appinstaller",
    ]
    msix = (ROOT / "scripts/release/msix.mjs").read_text(encoding="utf-8")
    for name in ("WIN_CERTIFICATE", "WIN_CERTIFICATE_PASSWORD", "INDUS_AZURE_SIGNING_DLIB"):
        assert f'"{name}"' in msix, name


def test_termux_lane_calls_its_workflow_on_the_admitted_commit(
    jobs: dict[str, dict[str, Any]],
) -> None:
    """P24-21 wired in: the optional ``termux`` lane calls ``termux.yml`` with the release tag and
    the admitted commit (the tag is named locally there); its secrets stay in that workflow's
    ``release`` environment, so none is passed from here."""
    job = jobs["termux"]
    assert job["uses"] == "./.github/workflows/termux.yml"
    called = triggers(load(ROOT / ".github/workflows/termux.yml"))["workflow_call"]["inputs"]
    assert set(job["with"]) == set(called)
    assert job["with"] == {"tag": "${{ needs.admit.outputs.tag }}", "commit": ADMITTED}
    assert "secrets" not in job


MAC_SECRETS = (
    "MAC_CERTIFICATE",
    "MAC_CERTIFICATE_PASSWORD",
    "APPLE_ID",
    "APPLE_TEAM_ID",
    "APPLE_APP_SPECIFIC_PASSWORD",
    "APPLE_API_KEY",
    "APPLE_API_KEY_ID",
    "APPLE_API_ISSUER",
)


def test_mac_lane_signs_with_the_release_environment_secrets(
    jobs: dict[str, dict[str, Any]],
) -> None:
    """P23-15: the mac lane runs in the protected ``release`` environment; each secret reaches only
    the build step, under its own name, through ``env`` (never pasted into a script, never echoed);
    dependencies install in an earlier step without them; the build is ``dist-mac.mjs`` with plain
    node, which builds unsigned with one line when the secrets are absent."""
    job = jobs["desktop-mac"]
    assert job["environment"] == "release"
    build = build_step(job)
    assert build["run"].strip() == "node scripts/dist-mac.mjs"
    assert build["working-directory"] == "apps/desktop"
    passed = {k: v for k, v in build["env"].items() if "secrets." in str(v)}
    assert passed == {name: f"${{{{ secrets.{name} }}}}" for name in MAC_SECRETS}
    for step in job["steps"]:
        if step is build:
            continue
        assert "secrets." not in str(step), step.get("name")
        assert not any(name in str(step.get("env", {})) for name in MAC_SECRETS)
    install = next(s for s in job["steps"] if "pnpm install" in str(s.get("run", "")))
    assert job["steps"].index(install) < job["steps"].index(build)
    for step in job["steps"]:
        run = str(step.get("run", ""))
        assert "secrets." not in run
        assert not re.search(r"\b(echo|printf)\b", run)
        assert "set -x" not in run
    keep = job["steps"][-1]
    assert keep["with"]["path"] == "apps/desktop/build/release/*.*"
    script = (ROOT / "apps/desktop/scripts/sign-mac.mjs").read_text(encoding="utf-8")
    for name in MAC_SECRETS:
        assert f'"{name}"' in script, name


def test_desktop_build_stamp_names_the_admitted_commit(jobs: dict[str, dict[str, Any]]) -> None:
    """The build stamp (write-build-stamp.mjs) reads INDUS_DESKTOP_COMMIT, never GITHUB_SHA, which
    in a release run is the default branch's tip (R10.24)."""
    for name in ("desktop-mac", "desktop-win", "desktop-linux"):
        env = build_step(jobs[name])["env"]
        assert env["INDUS_DESKTOP_COMMIT"] == ADMITTED
        assert env["INDUS_DESKTOP_VERSION"] == "${{ needs.admit.outputs.version }}"
        assert "GITHUB_SHA" not in env
        assert "github.sha" not in str(jobs[name])
    stamp = (ROOT / "apps/desktop/scripts/write-build-stamp.mjs").read_text(encoding="utf-8")
    assert "env.INDUS_DESKTOP_COMMIT" in stamp
    assert "env.GITHUB_SHA" not in stamp
    assert 'path.join(REPO_ROOT, "src", "indus", "__init__.py")' in stamp


def _uses(workflow: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if "uses" in step:
                found.append(str(step["uses"]))
    return found


@pytest.mark.parametrize("path", [RELEASE, CANARY], ids=["release", "canary"])
def test_actions_are_pinned_by_full_sha(path: Path) -> None:
    for uses in _uses(load(path)):
        assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", uses), uses


@pytest.mark.parametrize("path", [RELEASE, CANARY], ids=["release", "canary"])
def test_no_expression_is_pasted_into_a_script(path: Path) -> None:
    for job in load(path)["jobs"].values():
        for step in job.get("steps", []):
            assert "${{" not in str(step.get("run", "")), step.get("name")


def test_canary_runs_daily_and_never_cancels() -> None:
    canary = load(CANARY)
    on = triggers(canary)
    assert set(on) == {"schedule", "workflow_dispatch"}
    assert on["schedule"] == [{"cron": "41 6 * * *"}]
    assert canary["concurrency"]["cancel-in-progress"] is False
    [job] = canary["jobs"].values()
    assert job["if"] == "github.ref_name == github.event.repository.default_branch"
    run = job["steps"][-1]["run"]
    assert "release.py --canary --autopublish" in run and "--if-new" in run
