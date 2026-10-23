"""install.sh core: flags, manifest and frames, the ladder, signals and receipts (P23-03,
R10.15)."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.fakes.install.fake_tools import INSTALL_SH, ROOT, Sandbox
from tests.integration.install.stage_protocol import parse_manifest, parse_stage_frame

pytestmark = pytest.mark.platforms("posix")

LADDER = [
    "prerequisites",
    "repository",
    "runtime",
    "dependencies",
    "config",
    "products",
    "setup",
    "gateway",
    "complete",
]
STAGE_PROTOCOL_TS = ROOT / "apps" / "desktop" / "electron" / "installer" / "stageProtocol.ts"
FAKE_INSTALLER = ROOT / "apps" / "desktop" / "testing" / "fakeInstaller.mjs"


@pytest.fixture
def box(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def _desktop_args(box: Sandbox) -> list[str]:
    """What P20-09's runner passes (`manifestArgs` / `stageArgs`)."""
    return ["--non-interactive", "--indus-home", str(box.indus_home)]


def _last_json(stdout: str) -> Any:
    return json.loads(stdout.strip().splitlines()[-1])


# -- the built script -----------------------------------------------------------------------------


def _build_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "indus_install_build", ROOT / "scripts" / "install" / "build.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_install_sh_equals_the_built_output() -> None:
    assert INSTALL_SH.read_text(encoding="utf-8") == _build_module().build_sh()
    assert os.access(INSTALL_SH, os.X_OK)


def test_build_check_fails_on_a_stale_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build = _build_module()
    fake_root = tmp_path / "repo"
    (fake_root / "scripts").mkdir(parents=True)
    (fake_root / "scripts" / "install.sh").write_text(build.build_sh() + "# hand edit\n")
    monkeypatch.setattr(build, "ROOT", fake_root)
    assert build.main(["--check"]) == 1
    assert build.main([]) == 0
    assert build.main(["--check"]) == 0


def test_sourcing_runs_nothing(box: Sandbox) -> None:
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{INSTALL_SH}" --stage config; echo sourced; type ins_main >/dev/null',
        ],
        env=box.env(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "sourced\n"
    assert not box.indus_home.exists()


# -- manifest -------------------------------------------------------------------------------------


def test_manifest_json(box: Sandbox) -> None:
    result = box.run("--manifest", *_desktop_args(box))
    assert result.returncode == 0, result.stderr
    document = _last_json(result.stdout)
    assert document["protocol_version"] == 1
    assert [s["name"] for s in document["stages"]] == LADDER
    for stage in document["stages"]:
        assert set(stage) == {"name", "title", "category", "needs_user_input"}
        assert stage["needs_user_input"] is (stage["name"] in ("setup", "gateway"))
    stages = parse_manifest(result.stdout)
    assert isinstance(stages, list) and [s["name"] for s in stages] == LADDER
    assert not box.indus_home.exists()  # the manifest writes nothing


def test_manifest_with_desktop_names_the_desktop(box: Sandbox) -> None:
    plain = _last_json(box.run("--manifest").stdout)
    desktop = _last_json(box.run("--manifest", "--include-desktop").stdout)
    assert [s["name"] for s in desktop["stages"]] == LADDER
    titles = {s["name"]: s["title"] for s in desktop["stages"]}
    assert "desktop" in titles["products"]
    assert {s["name"]: s["title"] for s in plain["stages"]}["products"] != titles["products"]


def _node_strip_types() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    probe = subprocess.run(
        [node, "--experimental-strip-types", "--no-warnings", "-e", "0"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("this node cannot strip TypeScript types")
    return node


def _desktop_parse(tmp_path: Path, kind: str, stdout: str) -> Any:
    node = _node_strip_types()
    driver = tmp_path / "drive.mjs"
    driver.write_text(
        "const mod = await import(process.argv[2]);\n"
        'let data = ""; process.stdin.on("data", (c) => (data += c));\n'
        'process.stdin.on("end", () => { const out = process.argv[3] === "manifest" ?\n'
        "  mod.parseManifest(data) : mod.parseStageFrame(data);\n"
        "  process.stdout.write(JSON.stringify(out)); });\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            node,
            "--experimental-strip-types",
            "--no-warnings",
            str(driver),
            str(STAGE_PROTOCOL_TS),
            kind,
        ],
        input=stdout,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_desktop_parser_reads_the_real_manifest_and_frames(
    box: Sandbox, tmp_path: Path
) -> None:
    manifest = box.run("--manifest", *_desktop_args(box)).stdout
    parsed = _desktop_parse(tmp_path, "manifest", manifest)
    assert [s["name"] for s in parsed["stages"]] == LADDER
    assert parsed["stages"] == parse_manifest(manifest)
    frame_out = box.run("--stage", "setup", "--json", *_desktop_args(box)).stdout
    frame = _desktop_parse(tmp_path, "frame", frame_out)
    assert frame == parse_stage_frame(frame_out)
    assert frame["ok"] is True and frame["skipped"] is True and frame["stage"] == "setup"


def test_same_shapes_as_the_p20_fake_installer(box: Sandbox, tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    env = {**os.environ, "INDUS_FAKE_INSTALLER_STATE": str(tmp_path / "fake")}
    env.pop("INDUS_FAKE_INSTALLER_SCENARIO", None)
    fake_manifest = subprocess.run(
        [node, str(FAKE_INSTALLER), "--manifest", "--non-interactive"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout
    fake_frame = subprocess.run(
        [node, str(FAKE_INSTALLER), "--stage", "setup", "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout
    ours_manifest = box.run("--manifest", *_desktop_args(box)).stdout
    ours_frame = box.run("--stage", "setup", "--json", *_desktop_args(box)).stdout
    theirs = parse_manifest(fake_manifest)
    ours = parse_manifest(ours_manifest)
    assert isinstance(theirs, list) and isinstance(ours, list)
    assert {frozenset(s) for s in theirs} == {frozenset(s) for s in ours}
    assert _last_json(fake_manifest).keys() == _last_json(ours_manifest).keys()
    theirs_frame, ours_frame_parsed = parse_stage_frame(fake_frame), parse_stage_frame(ours_frame)
    assert theirs_frame is not None and ours_frame_parsed is not None
    assert {k: v for k, v in theirs_frame.items() if k != "reason"} == {
        k: v for k, v in ours_frame_parsed.items() if k != "reason"
    }
    assert _last_json(fake_frame).keys() == _last_json(ours_frame).keys()


# -- flags ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "code", "message"),
    [
        (["--bogus"], 1, "unknown option: --bogus"),
        (["-Bogus"], 1, "unknown option: -Bogus"),
        (["--dir"], 2, "--dir needs a value"),
        (["--stage", "--json"], 2, "--stage needs a value"),
        (["--channel="], 2, "--channel needs a value"),
        (["--indus-home", ""], 2, "--indus-home needs a value"),
        (["--channel", "nightly"], 2, "--channel must be stable, canary or main"),
        (["-Channel", "beta"], 2, "--channel must be stable, canary or main"),
        (["--json=yes"], 2, "--json takes no value"),
        (["--commit", "not-a-sha"], 2, "--commit must be a hex commit id"),
    ],
)
def test_flag_errors(box: Sandbox, args: list[str], code: int, message: str) -> None:
    result = box.run(*args)
    assert result.returncode == code
    assert message in result.stderr
    assert not box.indus_home.exists()


def test_channel_from_the_environment_is_checked(box: Sandbox) -> None:
    result = box.run("--manifest", env=box.env(INDUS_INSTALL_CHANNEL="nightly"))
    assert result.returncode == 2
    assert "--channel must be stable, canary or main, not nightly" in result.stderr


def test_powershell_spellings_and_help(box: Sandbox) -> None:
    result = box.run(
        "-Stage", "gateway", "-Json", "-NonInteractive", "-IndusHome", str(box.indus_home)
    )
    assert result.returncode == 0, result.stderr
    assert parse_stage_frame(result.stdout) == {
        "ok": True,
        "stage": "gateway",
        "skipped": True,
        "reason": "needs user input (--non-interactive)",
    }
    shown = box.run("--help")
    assert shown.returncode == 0 and "--channel stable|canary|main" in shown.stdout


# -- single stages --------------------------------------------------------------------------------


def _with_package_venv(box: Sandbox) -> Path:
    """A checkout dir whose .venv is this test run's environment (the real package)."""
    checkout = box.indus_home / "indus-agent"
    checkout.mkdir(parents=True)
    venv = Path(os.environ.get("VIRTUAL_ENV") or ROOT / ".venv")
    if not (venv / "bin" / "python").exists():
        pytest.skip("no project virtualenv to stand in for the installed one")
    (checkout / ".venv").symlink_to(venv)
    return checkout


def test_stage_config_json_frame(box: Sandbox) -> None:
    _with_package_venv(box)
    result = box.run("--stage", "config", "--json", *_desktop_args(box))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1] == '{"ok":true,"stage":"config","skipped":false}'
    assert (box.indus_home / "config.yaml").is_file()
    assert (box.indus_home / ".env").stat().st_mode & 0o777 == 0o600


def test_a_failing_stage_frame_carries_the_reason(box: Sandbox) -> None:
    result = box.run("--stage", "config", "--json", *_desktop_args(box))
    assert result.returncode == 1
    frame = parse_stage_frame(result.stdout)
    assert frame is not None and frame["ok"] is False and frame["stage"] == "config"
    assert frame["reason"] == "the Python environment is missing; run the dependencies stage first"


def test_unknown_stage(box: Sandbox) -> None:
    result = box.run("--stage", "nope", "--json")
    assert result.returncode == 2
    assert parse_stage_frame(result.stdout) == {
        "ok": False,
        "stage": "nope",
        "skipped": False,
        "reason": "unknown stage: nope",
    }


def test_termux_is_refused_with_a_pointer(box: Sandbox) -> None:
    result = box.run("--stage", "prerequisites", "--json", env=box.env(TERMUX_VERSION="0.118"))
    assert result.returncode == 1
    frame = parse_stage_frame(result.stdout)
    assert frame is not None and frame["ok"] is False
    reason = str(frame["reason"])
    assert "Termux" in reason and "APT channel" in reason and "pkg install indus-agent" in reason


def test_reason_strings_are_escaped(box: Sandbox) -> None:
    prelude = "ins_stage_config() { ins_fail $'quote \" back\\\\ tab\\t bell\\a' scripted; }"
    result = box.run("--stage", "config", "--json", prelude=prelude)
    frame = json.loads(result.stdout.strip().splitlines()[-1])
    assert frame["reason"] == 'quote " back\\ tab\t bell\x07'


def test_child_output_goes_to_the_log(box: Sandbox) -> None:
    prelude = "ins_stage_config() { ins_run 'Talking' sh -c 'echo child-output; echo oops >&2'; }"
    quiet = box.run("--stage", "config", "--json", prelude=prelude)
    assert quiet.returncode == 0
    assert "child-output" not in quiet.stdout + quiet.stderr
    log = (box.indus_home / "logs" / "install.log").read_text()
    assert "==> Talking" in log and "child-output" in log and "oops" in log
    loud = box.run("--stage", "config", "--json", "--verbose", prelude=prelude)
    assert "child-output" in loud.stdout
    in_ci = box.run("--stage", "config", "--json", prelude=prelude, env=box.env(CI="true"))
    assert "child-output" in in_ci.stdout
    assert "\x1b[" not in quiet.stdout + loud.stdout  # no colour off a terminal


def test_a_failed_command_names_its_cause(box: Sandbox) -> None:
    prelude = (
        "ins_stage_config() { ins_run 'Fetching' "
        "sh -c 'echo noise; echo \"fatal: no route\"; exit 3'"
        ' || ins_fail "fetch failed$(ins_cause)" scripted; }'
    )
    result = box.run("--stage", "config", "--json", prelude=prelude)
    frame = parse_stage_frame(result.stdout)
    assert frame is not None and frame["reason"] == "fetch failed: fatal: no route"
    assert "full log:" in result.stderr


# -- the ladder, signals and receipts -------------------------------------------------------------


def _receipts(box: Sandbox) -> list[dict[str, Any]]:
    folder = box.indus_home / "logs" / "install_receipts"
    if not folder.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))]


ALL_NOOP = "\n".join(f"ins_stage_{name}() {{ :; }}" for name in LADDER)


def test_a_full_ladder_writes_one_success_receipt(box: Sandbox) -> None:
    result = box.run("--non-interactive", prelude=ALL_NOOP)
    assert result.returncode == 0, result.stderr
    for index, name in enumerate(LADDER, 1):
        assert f"[{index}/9] " in result.stdout, name
    assert "skipped (needs user input (--non-interactive))" in result.stdout
    (receipt,) = _receipts(box)
    assert receipt["installer"] == "sh" and receipt["outcome"] == "success"
    assert set(receipt) == {"id", "installer", "outcome", "started_at", "finished_at"}
    assert len(receipt["id"]) == 32
    assert not list((box.indus_home / "logs" / "install_receipts").glob(".*"))


def test_a_failed_stage_writes_its_class(box: Sandbox) -> None:
    prelude = ALL_NOOP + "\nins_stage_runtime() { ins_fail 'scripted' download_failed; }"
    result = box.run("--non-interactive", prelude=prelude)
    assert result.returncode == 1
    (receipt,) = _receipts(box)
    assert receipt["outcome"] == "failed"
    assert receipt["failed_stage"] == "runtime"
    assert receipt["failure_class"] == "download_failed"
    assert "[4/9]" not in result.stdout


@pytest.fixture
def gated(box: Sandbox) -> Iterator[tuple[Sandbox, Path, Path]]:
    """A three-stage ladder whose middle stage waits for the test (two FIFOs, no sleeps)."""
    started = box.root / "started.fifo"
    verdict = box.root / "verdict.fifo"
    os.mkfifo(started)
    os.mkfifo(verdict)
    yield box, started, verdict


def _gated_prelude(started: Path, verdict: Path) -> str:
    return (
        "INS_STAGES='prerequisites config complete'\n"
        "ins_stage_prerequisites() { :; }\n"
        "ins_stage_config() {\n"
        f'    echo started >"{started}"\n'
        f'    read -r verdict <"{verdict}"\n'
        '    [ "$verdict" = pass ] || ins_fail "scripted failure" scripted_failure\n'
        "}\n"
        'ins_stage_complete() { : >"$INDUS_FAKE_TOOLS_STATE/complete-ran"; }\n'
    )


def _signal_mid_stage(
    box: Sandbox, started: Path, verdict: Path, sig: signal.Signals, answer: str
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(
        ["bash", str(box.harness(_gated_prelude(started, verdict))), "--non-interactive"],
        env=box.env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        with started.open(encoding="utf-8") as reader:
            assert reader.readline().strip() == "started"
        proc.send_signal(sig)  # the installer only, as `kill -INT <pid>` would
        with verdict.open("w", encoding="utf-8") as writer:
            writer.write(answer + "\n")
        out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def test_sigint_during_a_stage_that_succeeds_continues(gated: tuple[Sandbox, Path, Path]) -> None:
    box, started, verdict = gated
    result = _signal_mid_stage(box, started, verdict, signal.SIGINT, "pass")
    assert result.returncode == 0, result.stderr
    assert (box.state / "complete-ran").exists()
    (receipt,) = _receipts(box)
    assert receipt["outcome"] == "success"


def test_sigint_then_failure_is_interrupted_and_dies_by_the_signal(
    gated: tuple[Sandbox, Path, Path],
) -> None:
    box, started, verdict = gated
    result = _signal_mid_stage(box, started, verdict, signal.SIGINT, "fail")
    assert result.returncode in (-signal.SIGINT, 130)
    assert not (box.state / "complete-ran").exists()
    (receipt,) = _receipts(box)
    assert receipt["outcome"] == "interrupted"
    assert receipt["failed_stage"] == "config"
    assert receipt["failure_class"] == "interrupted"


def test_sigterm_after_a_passing_stage_stops_before_the_next(
    gated: tuple[Sandbox, Path, Path],
) -> None:
    box, started, verdict = gated
    result = _signal_mid_stage(box, started, verdict, signal.SIGTERM, "pass")
    assert result.returncode in (-signal.SIGTERM, 143)
    assert not (box.state / "complete-ran").exists()
    (receipt,) = _receipts(box)
    assert receipt["outcome"] == "interrupted" and receipt["failed_stage"] == "complete"


def test_second_receipt_write_is_a_no_op(box: Sandbox) -> None:
    script = (
        f'source "{INSTALL_SH}"\n'
        f'ins_parse_args --indus-home "{box.indus_home}"\n'
        "INS_RUN_ID=0123456789abcdef0123456789abcdef\n"
        "INS_STARTED_AT=2026-10-21T00:00:00Z\n"
        "ins_receipt failed config scripted_failure\n"
        "ins_receipt success\n"
        "ins_receipt interrupted config interrupted\n"
    )
    result = subprocess.run(
        ["bash", "-c", script],
        env=box.env(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    (receipt,) = _receipts(box)
    assert receipt == {
        "id": "0123456789abcdef0123456789abcdef",
        "installer": "sh",
        "outcome": "failed",
        "failed_stage": "config",
        "failure_class": "scripted_failure",
        "started_at": "2026-10-21T00:00:00Z",
        "finished_at": receipt["finished_at"],
    }


def test_a_stage_run_writes_no_receipt(box: Sandbox) -> None:
    box.run("--stage", "setup", "--json", *_desktop_args(box))
    assert _receipts(box) == []
