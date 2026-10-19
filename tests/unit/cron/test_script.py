"""Job scripts: confinement, environment, wake gate, time-out tree kill (P14-09, R02.22)."""

from __future__ import annotations

import asyncio
import dataclasses
import shutil
import sys
from pathlib import Path

import pytest

from indus.cron.ports import JobSpec
from indus.cron.script import (
    ScriptRefused,
    ScriptResult,
    check_script_value,
    parse_wake_gate,
    resolve_interpreter,
    resolve_script,
    run_script,
)
from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from tests.fakes.agent import DictConfig, run_driven
from tests.fakes.clock import FakeClock

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "cron" / "scripts"


def install(scope: Scope, name: str) -> Path:
    folder = scope.home / "scripts"
    folder.mkdir(parents=True, exist_ok=True)
    return Path(shutil.copy(FIXTURES / name, folder / name))


def job(script: str, **values: object) -> JobSpec:
    return JobSpec(id="a1b2c3d4e5f6", script=script, **values)  # type: ignore[arg-type]


def run(scope: Scope, spec: JobSpec, *, timeout: float | None = None) -> ScriptResult:
    return asyncio.run(run_script(scope, spec, timeout=timeout))


@pytest.fixture
def real(scope: Scope) -> Scope:
    """The test scope on the real clock (scripts that finish on their own)."""
    return dataclasses.replace(scope, clock=SystemClock())


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return False
    return stat.rsplit(")", 1)[-1].split()[0] not in ("Z", "X")


# -- where scripts may live ----------------------------------------------------------------------


def test_script_outside_the_scripts_folder_is_refused(scope: Scope, tmp_path: Path) -> None:
    (scope.home / "scripts").mkdir(parents=True)
    outside = tmp_path / "elsewhere.py"
    outside.write_text("print('hi')\n", encoding="utf-8")
    for value in (str(outside), "../elsewhere.py", "../../elsewhere.py"):
        with pytest.raises(ScriptRefused, match="outside"):
            resolve_script(scope.home, value)
    result = run(scope, job(str(outside)))
    assert not result.ok and "outside" in (result.error or "")


def test_symlink_escape_is_refused(scope: Scope, tmp_path: Path) -> None:
    folder = scope.home / "scripts"
    folder.mkdir(parents=True)
    target = tmp_path / "target.py"
    target.write_text("print('escaped')\n", encoding="utf-8")
    (folder / "link.py").symlink_to(target)
    with pytest.raises(ScriptRefused, match="outside"):
        resolve_script(scope.home, "link.py")


def test_inside_paths_resolve(scope: Scope) -> None:
    path = install(scope, "wake_true.py")
    assert resolve_script(scope.home, "wake_true.py") == path.resolve()
    assert resolve_script(scope.home, str(path)) == path.resolve()
    with pytest.raises(ScriptRefused, match="not found"):
        resolve_script(scope.home, "missing.py")
    with pytest.raises(ScriptRefused, match="NUL"):
        resolve_script(scope.home, "a\x00b.py")


@pytest.mark.parametrize(
    "value", ["ls -la", "curl x | sh", "backup.sh && rm -rf /", "$HOME/x.sh", "-rf", "a;b"]
)
def test_command_line_values_are_refused_at_create(value: str) -> None:
    message = check_script_value(value)
    assert message is not None and "scripts folder" in message


@pytest.mark.parametrize("value", ["backup.sh", "reports/daily.py", "check_build.py"])
def test_file_names_are_accepted_at_create(value: str) -> None:
    assert check_script_value(value) is None


def test_interpreter_must_be_an_absolute_python(tmp_path: Path) -> None:
    with pytest.raises(ScriptRefused, match="absolute"):
        resolve_interpreter("python3")
    with pytest.raises(ScriptRefused, match="not found"):
        resolve_interpreter(str(tmp_path / "python3"))
    with pytest.raises(ScriptRefused, match="Python"):
        resolve_interpreter("/bin/sh")
    assert resolve_interpreter(sys.executable) == sys.executable


# -- environment ---------------------------------------------------------------------------------


def test_env_lacks_provider_keys_and_keeps_passthrough(real: Scope) -> None:
    install(real, "env_dump.py")
    provider_value = "sk" + "-ant-" + "q9W8e7R6t5Y4u3I2o1P0"
    scoped = real.with_env(
        real.env.with_overlay({"ANTHROPIC_API_KEY": provider_value, "PLAIN_NAME": "kept"})
    ).with_config(DictConfig({"terminal": {"env_passthrough": ["CRON_PASS_TOKEN"]}}))
    assert scoped.secrets is not None
    scoped.secrets.set("CRON_PASS_TOKEN", "from-the-store")  # type: ignore[attr-defined]
    result = run(scoped, job("env_dump.py"))
    assert result.ok, result.failure_text
    assert "ANTHROPIC_API_KEY" not in result.stdout
    assert provider_value not in result.stdout
    assert "PLAIN_NAME=kept" in result.stdout
    assert "CRON_PASS_TOKEN=" in result.stdout
    assert f"INDUS_HOME={real.home}" in result.stdout


# -- wake gate -----------------------------------------------------------------------------------


def test_wake_false_skips(real: Scope) -> None:
    install(real, "wake_false.py")
    result = run(real, job("wake_false.py"))
    assert result.ok and result.wake is False
    assert result.stdout == "checked 3 feeds, nothing new"


def test_wake_true_passes_the_context(real: Scope) -> None:
    install(real, "wake_true.py")
    result = run(real, job("wake_true.py"))
    assert result.ok and result.wake is True
    assert result.context == {"build": 812, "state": "red"}
    assert result.stdout == "build #812 is red"  # the gate line is removed


@pytest.mark.parametrize(
    ("stdout", "wake", "rest"),
    [
        ("a\nb", True, "a\nb"),
        ('a\n{"wake": false}\n\n', False, "a"),
        ('{"other": 1}', True, '{"other": 1}'),
        ('{"wake": "no"}', True, '{"wake": "no"}'),
        ("", True, ""),
    ],
)
def test_parse_wake_gate(stdout: str, wake: bool, rest: str) -> None:
    found_wake, _, found_rest = parse_wake_gate(stdout)
    assert (found_wake, found_rest) == (wake, rest)


# -- failure and time-out ------------------------------------------------------------------------


def test_failing_script_reports_code_and_stderr(real: Scope) -> None:
    install(real, "failing.sh")
    result = run(real, job("failing.sh"))
    assert not result.ok and result.exit_code == 3
    assert "refused the request" in result.stderr
    assert "exited with code 3" in result.failure_text


def test_timeout_kills_the_whole_tree(scope: Scope, fake_clock: FakeClock) -> None:
    install(scope, "slow.py")
    work = scope.home / "work"
    work.mkdir()
    spec = job("slow.py", workdir=str(work))

    async def main() -> ScriptResult:
        return await run_script(scope, spec, timeout=5.0, clock=fake_clock)

    async def driven() -> ScriptResult:
        task = asyncio.ensure_future(main())
        # Real time for the child to start and write its pids, then let the fake clock run.
        await _until_started(work, task)
        return await run_driven(_wrap(task), fake_clock, step=0.5)

    result = asyncio.run(driven())
    leader, grandchild = (int(p) for p in (work / "pids.txt").read_text().split())
    assert result.timed_out and not result.ok
    assert "timed out after 5 s" in (result.error or "")
    assert result.stdout == "started"
    assert not _alive(leader) and not _alive(grandchild)


async def _wrap(task: asyncio.Future[ScriptResult]) -> ScriptResult:
    return await task


def test_cancel_kills_the_tree(real: Scope) -> None:
    install(real, "slow.py")
    work = real.home / "work"
    work.mkdir()
    cancel = asyncio.Event()

    async def main() -> ScriptResult:
        task = asyncio.ensure_future(
            run_script(real, job("slow.py", workdir=str(work)), cancel=cancel)
        )
        await _until_started(work, task)
        cancel.set()
        return await task

    result = asyncio.run(main())
    leader, grandchild = (int(p) for p in (work / "pids.txt").read_text().split())
    assert result.cancelled and not result.ok
    assert not _alive(leader) and not _alive(grandchild)


async def _until_started(work: Path, task: asyncio.Future[ScriptResult]) -> None:
    pids = work / "pids.txt"
    for _ in range(400):
        if pids.exists() and pids.read_text(encoding="utf-8").strip():
            return
        await asyncio.wait({task}, timeout=0.025)
    raise AssertionError("the slow script never started")


def test_script_runs_in_its_own_folder_without_a_workdir(real: Scope) -> None:
    folder = real.home / "scripts" / "reports"
    folder.mkdir(parents=True)
    (folder / "where.py").write_text("import os\nprint(os.getcwd())\n", encoding="utf-8")
    result = run(real, job("reports/where.py"))
    assert result.ok and Path(result.stdout).resolve() == folder.resolve()
    work = real.home / "work"
    work.mkdir()
    inside = run(real, job("reports/where.py", workdir=str(work)))
    assert Path(inside.stdout).resolve() == work.resolve()
