"""execute_code in script mode: RPC, limits, env scrub, approval, output (P10-08)."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.backends import local as local_backend
from indus.backends.contract import BackendFlags
from indus.tools.builtin import read_file as read_file_module
from indus.tools.builtin.code import ExecuteCodeTool, launch
from indus.tools.builtin.code.settings import STDOUT_INLINE_BYTES
from indus.tools.registry import ToolRegistry
from tests.fakes.broker import FakeBroker, Reply
from tests.fakes.clock import FakeClock, checkpoint
from tests.unit.tools.builtin._file_env import FakeRegistry, ShellEnv
from tests.unit.tools.builtin.code._rig import call, code_scope, fake_tools, run


def staged(tmp_path: Path) -> list[str]:
    return sorted(os.listdir(tmp_path / "t"))


def test_script_reads_a_file_through_rpc(tmp_path: Path) -> None:
    scope = code_scope(tmp_path)
    registry, seen = fake_tools(tmp_path)
    target = tmp_path / "notes.txt"
    target.write_text("hello from the file")
    code = (
        "from indus_tools import read_file\n"
        f"r = read_file({str(target)!r})\n"
        "print(r['content'].upper())\n"
    )
    result = run(call(ExecuteCodeTool(), scope, code, registry=registry, cwd=tmp_path))
    assert result["status"] == "success"
    assert result["output"] == "HELLO FROM THE FILE\n"
    assert result["tool_calls_made"] == 1 and result["exit_code"] == 0
    assert seen == [("read_file", {"path": str(target), "offset": 1})]
    assert staged(tmp_path) == []  # private dir and socket gone


def test_script_uses_the_real_read_file_tool(tmp_path: Path) -> None:
    scope = code_scope(tmp_path)
    registry = ToolRegistry()
    read_file_module.register_tools(registry)
    target = tmp_path / "real.txt"
    target.write_text("alpha\nbeta\n")
    code = (
        "from indus_tools import read_file\n"
        f"r = read_file({str(target)!r})\n"
        "print('beta' in r['content'], sorted(r)[:1])\n"
    )
    result = run(call(ExecuteCodeTool(), scope, code, registry=registry, cwd=tmp_path))
    assert result["status"] == "success", result
    assert result["output"].startswith("True")


def test_fifty_first_call_is_refused_inside_the_script(tmp_path: Path) -> None:
    scope = code_scope(tmp_path)
    registry, seen = fake_tools(tmp_path)
    code = (
        "from indus_tools import terminal, ToolCallRefused\n"
        "for i in range(50):\n"
        "    terminal(f'echo {i}')\n"
        "try:\n"
        "    terminal('echo 51')\n"
        "except ToolCallRefused as exc:\n"
        "    print('refused:', exc)\n"
    )
    result = run(call(ExecuteCodeTool(), scope, code, registry=registry, cwd=tmp_path))
    assert result["status"] == "success"
    assert result["tool_calls_made"] == 50 and len(seen) == 50
    assert "refused: This execute_code call already made its 50 tool calls" in result["output"]


def test_wrong_token_is_rejected(tmp_path: Path) -> None:
    scope = code_scope(tmp_path)
    registry, seen = fake_tools(tmp_path)
    code = (
        "import os\n"
        "os.environ['INDUS_RPC_TOKEN'] = 'forged'\n"
        "import indus_tools\n"
        "try:\n"
        "    indus_tools.read_file('/etc/hostname')\n"
        "except indus_tools.ToolCallRefused as exc:\n"
        "    print('refused:', exc)\n"
    )
    result = run(call(ExecuteCodeTool(), scope, code, registry=registry, cwd=tmp_path))
    assert result["output"] == "refused: Unauthorised tool request.\n"
    assert seen == [] and result["tool_calls_made"] == 0


def test_child_env_has_no_credentials(tmp_path: Path) -> None:
    fake_key = "sk" + "-" + "proj" + "Q" * 24
    scope = code_scope(
        tmp_path,
        env={
            "OPENAI_API_KEY": fake_key,
            "MY_SERVICE_TOKEN": "a" + "1" * 15,
            "DATABASE_PASSWORD": "pw" + "9" * 10,
            "STRIPE_APIKEY": "k" * 12,
            "UNRELATED_SETTING": "kept-out",
            "LANG": "C.UTF-8",
        },
    )
    registry, _ = fake_tools(tmp_path)
    code = "import os\nprint('\\n'.join(sorted(os.environ)))\n"
    result = run(call(ExecuteCodeTool(), scope, code, registry=registry, cwd=tmp_path))
    names = set(result["output"].split())
    assert not {n for n in names if "KEY" in n or "PASSWORD" in n} - set()
    assert "MY_SERVICE_TOKEN" not in names and "UNRELATED_SETTING" not in names
    assert {"PATH", "LANG", "INDUS_RPC_SOCKET", "INDUS_RPC_TOKEN", "PYTHONUTF8"} <= names
    assert fake_key not in result["output"]


def test_printed_secret_values_are_masked(tmp_path: Path) -> None:
    secret = "zq" + "8" * 30
    scope = code_scope(tmp_path, secrets={"SERVICE_TOKEN": secret})
    registry, _ = fake_tools(tmp_path)
    code = f"value = {secret[:10]!r} + {secret[10:]!r}\nprint('value is', value)\n"
    result = run(call(ExecuteCodeTool(), scope, code, registry=registry, cwd=tmp_path))
    assert result["output"].startswith("value is")
    assert secret not in result["output"]


def test_long_output_is_cut_and_spilled(tmp_path: Path) -> None:
    scope = code_scope(tmp_path)
    registry, _ = fake_tools(tmp_path)
    code = "for i in range(6000):\n    print(f'row {i:05d}')\n"
    result = run(call(ExecuteCodeTool(), scope, code, registry=registry, cwd=tmp_path))
    full = "".join(f"row {i:05d}\n" for i in range(6000))
    assert len(full.encode()) == 60_000
    output = result["output"]
    assert result["stdout_truncated"] is True
    assert result["stdout_bytes_total"] == 60_000
    assert result["stdout_bytes_omitted"] == 60_000 - STDOUT_INLINE_BYTES
    assert output.startswith("row 00000\n") and output.endswith("row 05999\n")
    assert "bytes of output left out here" in output
    head, _, tail = output.partition("\n\n[...")
    assert len(head.encode()) <= int(STDOUT_INLINE_BYTES * 0.4)
    path = Path(result["full_output_path"])
    assert path.parent == scope.home / "cache" / "exec"
    assert path.read_text() == full
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "read_file" in result["note"]


def test_failing_script_shows_stderr_and_a_hint(tmp_path: Path) -> None:
    scope = code_scope(tmp_path)
    registry, _ = fake_tools(tmp_path)
    code = "print('before')\nimport surely_not_installed_pkg\n"
    result = run(call(ExecuteCodeTool(), scope, code, registry=registry, cwd=tmp_path))
    assert result["status"] == "error" and result["exit_code"] == 1
    assert result["error"] == "The script exited with code 1."
    assert result["output"].startswith("before\n\n--- stderr ---\n")
    assert "ModuleNotFoundError" in result["output"]
    assert "'surely_not_installed_pkg' is not installed" in result["hint"]


def test_stderr_is_not_shown_on_success(tmp_path: Path) -> None:
    scope = code_scope(tmp_path)
    registry, _ = fake_tools(tmp_path)
    code = "import sys\nprint('ok')\nprint('noise', file=sys.stderr)\n"
    result = run(call(ExecuteCodeTool(), scope, code, registry=registry, cwd=tmp_path))
    assert result["output"] == "ok\n"


def test_tool_errors_are_reported(tmp_path: Path) -> None:
    scope = code_scope(tmp_path)
    registry, _ = fake_tools(tmp_path)
    code = "from indus_tools import read_file\nr = read_file('/no/such/file')\nprint(sorted(r))\n"
    result = run(call(ExecuteCodeTool(), scope, code, registry=registry, cwd=tmp_path))
    assert result["status"] == "success"
    assert result["output"] == "['error']\n"
    assert result["tool_errors"] == [{"tool": "read_file", "error": "no such file: /no/such/file"}]


def test_misrouted_command_points_to_terminal(tmp_path: Path) -> None:
    scope = code_scope(tmp_path)
    text = asyncio.run(ExecuteCodeTool()({"command": "ls -la"}, scope=scope))
    body = json.loads(text)
    assert body["status"] == "error" and "terminal(command=...)" in body["error"]
    assert staged(tmp_path) == []


@pytest.mark.parametrize(("args", "words"), [({"code": 5}, "got int"), ({"code": "  "}, "No code")])
def test_bad_code_argument(tmp_path: Path, args: dict[str, Any], words: str) -> None:
    scope = code_scope(tmp_path)
    body = json.loads(asyncio.run(ExecuteCodeTool()(args, scope=scope)))
    assert words in body["error"]


def test_no_scope_runs_nothing() -> None:
    body = json.loads(asyncio.run(ExecuteCodeTool()({"code": "print(1)"})))
    assert "No profile" in body["error"]


# -- approval (E9) ---------------------------------------------------------------------------------


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 19, tzinfo=UTC))


def test_manual_approval_denied_spawns_nothing(tmp_path: Path) -> None:
    scope = code_scope(tmp_path, {"approvals": {"mode": "manual"}})
    registry, seen = fake_tools(tmp_path)
    broker = FakeBroker(_clock(), default=Reply(choice="deny", reason="not now"))
    marker = tmp_path / "ran.txt"
    # The long path sits on a line of its own: the approval text may wrap or shorten a long line
    # (any base temp folder), so the check reads the short line that does the work (W46 RS11).
    code = f"target = {str(marker)!r}\nopen(target, 'w').write('ran')\n"
    result = run(
        call(
            ExecuteCodeTool(),
            scope,
            code,
            registry=registry,
            cwd=tmp_path,
            broker=broker,
            run_context="interactive",
        )
    )
    assert result["status"] == "error"
    assert "denied" in result["error"] and "not now" in result["error"]
    assert not marker.exists() and staged(tmp_path) == [] and seen == []
    (request,) = broker.requests
    assert request.kind == "approval"
    assert "INDUS_SCRIPT" in request.command
    assert "\nopen(target, 'w').write('ran')\n" in request.command
    assert [c.value for c in request.choices] == ["once", "session", "deny"]


def test_session_approval_is_asked_once(tmp_path: Path) -> None:
    scope = code_scope(tmp_path, {"approvals": {"mode": "manual"}})
    registry, _ = fake_tools(tmp_path)
    broker = FakeBroker(_clock(), default=Reply(choice="session"))
    tool = ExecuteCodeTool()
    first = run(
        call(
            tool,
            scope,
            "print(1)",
            registry=registry,
            cwd=tmp_path,
            broker=broker,
            run_context="interactive",
        )
    )
    second = run(
        call(
            tool,
            scope,
            "print(2)",
            registry=registry,
            cwd=tmp_path,
            broker=broker,
            run_context="interactive",
        )
    )
    assert (
        first["output"] == "1\n" and first["approval"] == "The user approved running this script."
    )
    assert second["output"] == "2\n" and "approval" not in second
    assert len(broker.requests) == 1


def test_nobody_to_ask_blocks_by_default(tmp_path: Path) -> None:
    scope = code_scope(tmp_path, {"approvals": {"mode": "manual"}})
    registry, _ = fake_tools(tmp_path)
    result = run(
        call(
            ExecuteCodeTool(),
            scope,
            "print(1)",
            registry=registry,
            cwd=tmp_path,
            run_context="single_query",
        )
    )
    assert "approvals.single_query_mode: approve" in result["error"]
    assert result["user_summary"].startswith("Blocked")
    allowed = code_scope(
        tmp_path / "b", {"approvals": {"mode": "manual", "single_query_mode": "approve"}}
    )
    ok = run(
        call(
            ExecuteCodeTool(),
            allowed,
            "print(1)",
            registry=registry,
            cwd=tmp_path,
            run_context="single_query",
        )
    )
    assert ok["output"] == "1\n"


def test_isolated_backend_needs_no_approval(tmp_path: Path) -> None:
    class Sandbox(ShellEnv):
        @property
        def flags(self) -> BackendFlags:
            return BackendFlags(isolated=True, host_local=True)

    scope = code_scope(tmp_path, {"approvals": {"mode": "manual"}})
    registry, _ = fake_tools(tmp_path)
    result = run(
        call(
            ExecuteCodeTool(),
            scope,
            "print(3)",
            registry=registry,
            cwd=tmp_path,
            environments=FakeRegistry(Sandbox(str(tmp_path)), backend="local"),
        )
    )
    assert result["output"] == "3\n"


# -- cwd and interpreter ---------------------------------------------------------------------------


def test_project_mode_runs_in_the_session_directory(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    scope = code_scope(tmp_path)
    registry, _ = fake_tools(tmp_path)
    result = run(
        call(ExecuteCodeTool(), scope, "import os\nprint(os.getcwd())", registry=registry, cwd=work)
    )
    assert result["output"].strip() == str(work)


def test_strict_mode_runs_in_its_private_directory(tmp_path: Path) -> None:
    scope = code_scope(tmp_path, {"code_execution": {"mode": "strict"}})
    registry, _ = fake_tools(tmp_path)
    code = (
        "import os, sys\nprint(os.getcwd())\nprint(sys.executable)\n"
        "print(oct(os.stat('.').st_mode & 0o777))"
    )
    result = run(call(ExecuteCodeTool(), scope, code, registry=registry, cwd=tmp_path / "x"))
    where, python, mode = result["output"].split()
    assert Path(where).parent == tmp_path / "t" and Path(where).name.startswith("indus-code-")
    assert python == sys.executable and mode == "0o700"
    assert staged(tmp_path) == []


def _fake_python(root: Path, body: str) -> Path:
    exe = root / "bin" / "python3"
    exe.parent.mkdir(parents=True)
    exe.write_text(f"#!/bin/sh\n{body}\n")
    exe.chmod(0o755)
    return exe


def test_interpreter_choice(tmp_path: Path) -> None:
    good = _fake_python(tmp_path / "good", f'exec {sys.executable} "$@"')
    broken = _fake_python(tmp_path / "broken", "exit 1")
    launch.forget_probes()
    pick = lambda env: asyncio.run(  # noqa: E731
        launch.choose_interpreter(code_scope(tmp_path, env=env), "project")
    )
    assert pick({"VIRTUAL_ENV": str(tmp_path / "good")}) == str(good)
    assert pick({"VIRTUAL_ENV": str(tmp_path / "broken")}) == sys.executable
    assert pick({"VIRTUAL_ENV": str(tmp_path / "missing")}) == sys.executable
    assert pick({"VIRTUAL_ENV": "", "CONDA_PREFIX": str(tmp_path / "good")}) == str(good)
    strict = asyncio.run(
        launch.choose_interpreter(
            code_scope(tmp_path, env={"VIRTUAL_ENV": str(good.parent.parent)}), "strict"
        )
    )
    assert strict == sys.executable
    assert broken.exists()


# -- stopping --------------------------------------------------------------------------------------


async def _until(task: asyncio.Future[Any], ready: Path, seconds: float = 30.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while not ready.exists():
        assert not task.done(), task.result()
        assert loop.time() < end, f"{ready} never appeared"
        await asyncio.wait({task}, timeout=0.01)


def test_interrupt_kills_the_child(tmp_path: Path) -> None:
    scope = code_scope(tmp_path)
    registry, _ = fake_tools(tmp_path)
    ready = tmp_path / "ready"
    pid_file = tmp_path / "pid"
    cancel = threading.Event()
    code = (
        "import os, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        f"open({str(ready)!r}, 'w').write('1')\n"
        "time.sleep(600)\n"
    )

    async def scenario() -> dict[str, Any]:
        task = asyncio.ensure_future(
            call(ExecuteCodeTool(), scope, code, registry=registry, cwd=tmp_path, cancel=cancel)
        )
        await _until(task, ready)
        cancel.set()
        return await asyncio.wait_for(task, 30)

    result = run(scenario())
    assert result["status"] == "interrupted"
    assert result["output"].endswith("[execution interrupted]")
    pid = int(pid_file.read_text())
    assert (
        not Path(f"/proc/{pid}").exists()
        or "Z" in Path(f"/proc/{pid}/stat").read_text().split(")")[1][:3]
    )
    assert staged(tmp_path) == []


def test_timeout_sends_sigterm_then_sigkill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _clock()
    scope = code_scope(tmp_path, {"code_execution": {"timeout": 300}}, clock=clock)
    registry, _ = fake_tools(tmp_path)
    ready, termed = tmp_path / "ready", tmp_path / "termed"
    signals: list[tuple[int, float]] = []
    original = local_backend._signal_group  # pyright: ignore[reportPrivateUsage]

    def recording(pgid: int | None, leader_start: int | None, sig: int) -> None:
        signals.append((sig, clock.monotonic()))
        original(pgid, leader_start, sig)

    monkeypatch.setattr(local_backend, "_signal_group", recording)
    code = (
        "import signal, time\n"
        f"signal.signal(signal.SIGTERM, lambda *a: open({str(termed)!r}, 'w').write('TERM'))\n"
        f"open({str(ready)!r}, 'w').write('1')\n"
        "while True:\n"
        "    time.sleep(0.05)\n"
    )

    async def scenario() -> dict[str, Any]:
        task = asyncio.ensure_future(
            call(ExecuteCodeTool(), scope, code, registry=registry, cwd=tmp_path)
        )
        await _until(task, ready)
        started = clock.monotonic()
        clock.advance(299)
        await _settle(task)
        assert not task.done() and not termed.exists()
        clock.advance(2)  # past 300 s: the ordered kill starts
        await _until(task, termed)
        term_at = clock.monotonic()
        for _ in range(400):
            if task.done():
                break
            clock.advance(0.25)
            await _settle(task)
        assert task.done()
        result = task.result()
        result["_term_after"] = term_at - started
        return result

    result = run(scenario())
    assert result["status"] == "timeout"
    assert "ran past its 300s limit" in result["error"] and "300s limit" in result["output"]
    kinds = [sig for sig, _ in signals]
    assert signal.SIGKILL in kinds
    (kill_at,) = [at for sig, at in signals if sig == signal.SIGKILL]
    assert kill_at - 300 >= 5.0  # SIGKILL only after the 5 s grace
    assert termed.read_text() == "TERM"
    assert staged(tmp_path) == []


async def _settle(task: asyncio.Future[Any]) -> None:
    for _ in range(5):
        await checkpoint()
        await asyncio.wait({task}, timeout=0.01)
