"""``write_file``: guards, approval, the stale-write rule and verified writes (P05-18)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.protocol.envelope import ErrorObject
from indus.protocol.events import CancelReason
from indus.protocol.requests_port import AnswerModel, RequestUnsupported, RequestWithdrawn
from indus.protocol.server_requests import ApprovalAnswer, ApprovalChoice, ApprovalRequest
from indus.tools.builtin import read_tracking
from indus.tools.builtin.patch import patch
from indus.tools.builtin.read_file import read_file
from indus.tools.builtin.write_file import (
    MISSING_CONTENT,
    is_tool_display_text,
    register_tools,
    write_file,
)
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import DictConfig
from tests.unit.tools.builtin._file_env import FakeRegistry, ShellEnv


@pytest.fixture(autouse=True)
def fresh_trackers() -> Iterator[None]:
    read_tracking.reset_all()
    yield
    read_tracking.reset_all()


@pytest.fixture
def user_scope(scope: Scope, tmp_path: Path) -> Scope:
    home = tmp_path / "user"
    home.mkdir()
    return scope.with_env(scope.env.with_overlay({"HOME": str(home)}))


class ApprovalPort:
    """Answers approval asks with a scripted outcome and records what was asked."""

    def __init__(self, reply: ApprovalChoice | BaseException) -> None:
        self.reply = reply
        self.asked: list[dict[str, Any]] = []

    async def ask(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        timeout: float | None = None,
        turn_id: str | None = None,
    ) -> AnswerModel:
        assert kind == "approval"
        ApprovalRequest(request_id="r1", session_id="s1", **payload)  # a valid request
        self.asked.append({"payload": dict(payload), "timeout": timeout, "turn_id": turn_id})
        if isinstance(self.reply, BaseException):
            raise self.reply
        return ApprovalAnswer(choice=self.reply)

    def cancel_all(self, reason: CancelReason) -> int:
        return 0

    def human_wait_seconds(self) -> float:
        return 0.0


def write(scope: Scope, path: object, content: object = "new\n", **kwargs: Any) -> dict[str, Any]:
    args: dict[str, Any] = {"path": path}
    if content is not None:
        args["content"] = content
    return json.loads(asyncio.run(write_file(args, scope=scope, task_id="t1", **kwargs)))


def read(scope: Scope, path: Path, **args: Any) -> dict[str, Any]:
    return json.loads(
        asyncio.run(read_file({"path": str(path), **args}, scope=scope, task_id="t1"))
    )


# -- the stale-write rule ----------------------------------------------------------------------


def test_new_file_is_written_with_parents_and_verified(scope: Scope, tmp_path: Path) -> None:
    target = tmp_path / "pkg" / "mod.py"
    body = write(scope, str(target), "print('hi')\n")
    assert target.read_text() == "print('hi')\n"
    assert body["verified"] is True and body["dirs_created"] is True
    assert body["bytes_written"] == len("print('hi')\n")
    assert body["files_modified"] == [str(target)]
    assert list(body)[:3] == ["bytes_written", "dirs_created", "verified"]


def test_existing_file_never_read_is_refused(scope: Scope, tmp_path: Path) -> None:
    target = tmp_path / "keep.txt"
    target.write_text("theirs\n")
    body = write(scope, str(target))
    assert body["stale_write_blocked"] is True
    assert "NOT changed" in body["error"]
    assert target.read_text() == "theirs\n"


def test_partial_read_is_not_enough(scope: Scope, tmp_path: Path) -> None:
    target = tmp_path / "long.txt"
    target.write_text("".join(f"{i}\n" for i in range(10)))
    read(scope, target, limit=3)
    assert write(scope, str(target))["stale_write_blocked"] is True


def test_full_read_then_write_is_allowed(scope: Scope, tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("old\n")
    read(scope, target)
    assert write(scope, str(target), "replaced\n")["verified"] is True
    assert target.read_text() == "replaced\n"


def test_file_changed_on_disk_after_the_read_is_refused(scope: Scope, tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("old\n")
    read(scope, target)
    target.write_text("someone else\n")
    body = write(scope, str(target))
    assert body["stale_write_blocked"] is True and "changed on disk" in body["error"]
    assert target.read_text() == "someone else\n"


def test_own_write_then_rewrite_is_allowed(scope: Scope, tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    write(scope, str(target), "one\n")
    assert write(scope, str(target), "two\n")["verified"] is True
    assert target.read_text() == "two\n"


def test_terminal_writes_make_no_baseline_and_other_tasks_have_none(
    scope: Scope, tmp_path: Path
) -> None:
    target = tmp_path / "f.txt"
    write(scope, str(target), "mine\n")
    target.write_text("shell wrote this\n")  # e.g. `echo ... > f.txt` in the terminal
    assert write(scope, str(target))["stale_write_blocked"] is True
    other = json.loads(
        asyncio.run(write_file({"path": str(target), "content": "x"}, scope=scope, task_id="t2"))
    )
    assert other["stale_write_blocked"] is True


def test_patch_from_a_baseline_keeps_it_and_blind_patch_does_not(
    scope: Scope, tmp_path: Path
) -> None:
    target = tmp_path / "f.py"
    target.write_text("alpha = 1\nbeta = 2\n")
    read(scope, target)
    edit = {"path": str(target), "old_string": "alpha = 1", "new_string": "alpha = 10"}
    asyncio.run(patch(edit, scope=scope, task_id="t1"))
    assert write(scope, str(target), "whole\n")["verified"] is True

    other = tmp_path / "g.py"
    other.write_text("gamma = 3\n")
    edit = {"path": str(other), "old_string": "gamma = 3", "new_string": "gamma = 30"}
    asyncio.run(patch(edit, scope=scope, task_id="t1"))
    body = write(scope, str(other))
    assert body["stale_write_blocked"] is True and "own patch" in body["error"]


# -- guards ----------------------------------------------------------------------------------------


def test_system_and_credential_paths_are_refused(user_scope: Scope, tmp_path: Path) -> None:
    assert "system path" in write(user_scope, "/etc/indus-test.conf")["error"]
    ssh = tmp_path / "user" / ".ssh"
    ssh.mkdir()
    assert "credential folder" in write(user_scope, "~/.ssh/authorized_keys")["error"]
    (tmp_path / "innocent").symlink_to(ssh / "id_rsa")
    assert "credential folder" in write(user_scope, str(tmp_path / "innocent"))["error"]
    assert not (ssh / "id_rsa").exists()
    indus_config = user_scope.home / "config.yaml"
    assert "Indus" in write(user_scope, str(indus_config))["error"]


def test_ssh_config_asks_for_approval(user_scope: Scope) -> None:
    port = ApprovalPort(ApprovalChoice.ONCE)
    body = write(user_scope, "~/.ssh/config", "Host x\n", broker=port, turn_id="turn1")
    assert body["verified"] is True
    assert "ProxyCommand" in port.asked[0]["payload"]["description"]


def test_binary_documents_are_refused(scope: Scope, tmp_path: Path) -> None:
    assert "document" in write(scope, str(tmp_path / "r.docx"))["error"]
    assert write(scope, str(tmp_path / "new.pdf"), "%PDF-1.4\n")["verified"] is True
    (tmp_path / "old.pdf").write_bytes(b"%PDF-1.4 binary")
    assert "binary file" in write(scope, str(tmp_path / "old.pdf"))["error"]


@pytest.mark.parametrize("name", ["AGENTS.md", "claude.md", "PERSONA.md", ".cursorrules"])
def test_instruction_files_ask_and_write_once_approved(
    scope: Scope, tmp_path: Path, name: str
) -> None:
    port = ApprovalPort(ApprovalChoice.ONCE)
    body = write(scope, str(tmp_path / name), "rules\n", broker=port, turn_id="turn1")
    assert body["verified"] is True
    payload = port.asked[0]["payload"]
    assert payload["choices"] == ["once", "deny"]
    assert port.asked[0]["turn_id"] == "turn1"
    assert port.asked[0]["timeout"] == 300.0


@pytest.mark.parametrize(
    ("reply", "words"),
    [
        (ApprovalChoice.DENY, "denied by the user"),
        (RequestWithdrawn("r1", CancelReason.TIMED_OUT), "silence is not consent"),
        (RequestWithdrawn("r1", CancelReason.INTERRUPTED), "was not answered"),
        (RequestUnsupported("approval", ErrorObject(code=1, message="no")), "cannot show it"),
    ],
)
def test_instruction_file_refusals_are_worded_apart(
    scope: Scope, tmp_path: Path, reply: Any, words: str
) -> None:
    body = write(scope, str(tmp_path / "AGENTS.md"), "x", broker=ApprovalPort(reply))
    assert words in body["error"]
    assert not (tmp_path / "AGENTS.md").exists()


def test_instruction_file_without_a_surface_is_blocked(scope: Scope, tmp_path: Path) -> None:
    assert "nobody here can answer" in write(scope, str(tmp_path / "CLAUDE.md"))["error"]


def test_configured_instruction_globs(scope: Scope, tmp_path: Path) -> None:
    configured = scope.with_config(
        DictConfig({"security": {"protected_instruction_files": ["*.rules"]}})
    )
    assert "nobody here" in write(configured, str(tmp_path / "team.rules"))["error"]


def test_read_output_echo_and_stub_text_are_refused(scope: Scope, tmp_path: Path) -> None:
    echoed = "1|import os\n2|\n3|print(os.getcwd())\n"
    assert "display text" in write(scope, str(tmp_path / "a.py"), echoed)["error"]
    assert is_tool_display_text(echoed)
    assert not is_tool_display_text("1|one value only\n")
    assert not is_tool_display_text("a | b\nc | d\n")


def test_missing_content_explains_dropped_arguments(scope: Scope, tmp_path: Path) -> None:
    body = write(scope, str(tmp_path / "x.txt"), None)
    assert body["error"] == MISSING_CONTENT


# -- shape keeping and hints -----------------------------------------------------------------------


def test_crlf_and_bom_are_kept(scope: Scope, tmp_path: Path) -> None:
    target = tmp_path / "win.txt"
    target.write_bytes(b"\xef\xbb\xbfone\r\ntwo\r\n")
    read(scope, target)
    write(scope, str(target), "uno\ndos\n")
    assert target.read_bytes() == b"\xef\xbb\xbfuno\r\ndos\r\n"


def test_rewrite_hint_for_mostly_unchanged_big_files(scope: Scope, tmp_path: Path) -> None:
    target = tmp_path / "big.py"
    original = "".join(f"value_{i} = {i}\n" for i in range(2000))
    target.write_text(original)
    read(scope, target)
    body = write(scope, str(target), original.replace("value_7 = 7", "value_7 = 70"))
    assert "use patch" in body["hint"]


def test_baseline_after_own_write_matches_disk(scope: Scope, tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    write(scope, str(target), "abc")
    tracker = read_tracking.tracker_for(scope, "t1")
    assert tracker.baseline(str(target)) == hashlib.sha256(b"abc").hexdigest()


def test_write_through_a_shell_environment(scope: Scope, tmp_path: Path) -> None:
    registry = FakeRegistry(ShellEnv(str(tmp_path), noise="banner\n"))
    body = write(scope, "remote/out.txt", "over the wire\n", environments=registry)
    assert body["verified"] is True
    assert (tmp_path / "remote" / "out.txt").read_text() == "over the wire\n"


# -- lint after edit (P05-20 integration) -------------------------------------------------------


def test_lint_gate_refuses_a_new_data_file_that_does_not_parse(
    scope: Scope, tmp_path: Path
) -> None:
    target = tmp_path / "conf.yaml"
    body = write(scope, str(target), "key: [unclosed\n")
    assert "not valid YAML" in body["error"] and not target.exists()


def test_lint_gate_lets_an_already_broken_file_be_repaired_in_steps(
    scope: Scope, tmp_path: Path
) -> None:
    target = tmp_path / "c.json"
    target.write_text('{"a": \n')
    read(scope, target)
    body = write(scope, str(target), '{"a": [\n')
    assert body["verified"] is True and body["lint"]["status"] == "preexisting"


def test_lint_after_reports_an_introduced_python_error(scope: Scope, tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    body = write(scope, str(target), "def f(:\n")
    assert body["verified"] is True
    assert body["lint"]["status"] == "introduced" and body["lint"]["linter"] == "python"
    assert "lint" not in write(scope, str(target), "def f():\n    return 1\n")


def test_register_tools() -> None:
    registry = ToolRegistry()
    register_tools(registry)
    entry = registry.get("write_file")
    assert entry is not None and entry.traits.writer and entry.traits.mutating
    assert entry.is_async
