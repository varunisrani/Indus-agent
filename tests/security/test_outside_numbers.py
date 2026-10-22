"""Outside numbers and inherited environments (SEC-6, SEC-8; wave 46).

SEC-6: text from a chat, a platform, a peer or a provider that ``str.isdigit()`` accepts but
``int()`` cannot read (``"²"``, ``"--5"``, thousands of digits) is "not a number", never an
exception. A sample of the swept spots is driven here; the helper has its own unit tests.

SEC-8: the dashboard's git calls and the doctor / locate ``--version`` probes run with an
explicit environment that holds no provider key.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from indus.cli.doctor import DoctorContext
from indus.cli.doctor import checks as doctor_checks
from indus.foundation.scope import Scope
from indus.gateway.adapters.base import clarify_callback, parse_clarify_callback
from indus.gateway.adapters.telegram.bot_api import _chat  # pyright: ignore[reportPrivateUsage]
from indus.gateway.adapters.yuanbao.client import _number  # pyright: ignore[reportPrivateUsage]
from indus.gateway.core.prompts import parse_choice_reply
from indus.gateway.relay.descriptor import (
    DEFAULT_MAX_LEN,
    _max_len,  # pyright: ignore[reportPrivateUsage]
)
from indus.platform import locate as locate_mod
from indus.platform.shapes import Effort
from indus.providers.transports.chat_completions import (
    _status_from,  # pyright: ignore[reportPrivateUsage]
)
from indus.web.routes import system
from tests.security.conftest import fake_key

ODD = ["²", "①", "²²²", "--5", "7" * 5000]

# -- SEC-6 -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("odd", ODD)
def test_sec6_chat_and_platform_numbers_are_refusals_not_crashes(odd: str) -> None:
    """B-HOOK-2 (SEC-6): a clarify button payload, a typed choice, a chat id, a platform number,
    an upstream status and a relay descriptor field: each answers "not a number" (before the
    sweep each raised)."""
    button = clarify_callback("q1", 1)
    assert parse_clarify_callback(button[:-1] + odd) is None
    assert parse_choice_reply(odd, ["red", "blue"], multi=False) == [odd]
    assert parse_choice_reply(f"1, {odd}", ["red", "blue"], multi=True) == [f"1, {odd}"]
    assert _chat(odd) == odd
    assert _number(odd) is None
    assert _status_from(odd) is None
    assert _max_len(odd) == DEFAULT_MAX_LEN


def test_sec6_plain_numbers_still_read() -> None:
    """B-HOOK-2 (SEC-6): the strict parse still reads every plain number those spots expect."""
    assert parse_clarify_callback(clarify_callback("q1", 2)) == ("q1", 2)
    assert parse_choice_reply("2", ["red", "blue"], multi=False) == ["blue"]
    assert _chat("-1001234567890") == -1001234567890
    assert _number(" 42 ") == 42
    assert _status_from("429") == 429
    assert _max_len("900") == 900


# -- SEC-8 -----------------------------------------------------------------------------------------


def _leaky(scope: Scope) -> Scope:
    view = scope.env.with_overlay(
        {"OPENAI_API_KEY": fake_key(), "INDUS_YOLO_MODE": "1", "PATH": "/usr/bin"}
    )
    return scope.with_env(view)


class _Spawns:
    def __init__(self) -> None:
        self.envs: list[dict[str, str] | None] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert "env" in kwargs, f"{argv[:3]} inherits the process environment"
        self.envs.append(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, stdout="0\n", stderr="")


def test_sec8_dashboard_git_calls_get_a_child_env(
    scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-ENV-3 (SEC-8): the dashboard's git fetch and rev-list run with the scope's child env."""
    spawns = _Spawns()
    monkeypatch.setattr(system.subprocess, "run", spawns)
    assert system.git_behind(tmp_path, env=system.git_env(_leaky(scope))) == 0
    assert len(spawns.envs) == 2
    for env in spawns.envs:
        assert env is not None
        assert "OPENAI_API_KEY" not in env and "INDUS_YOLO_MODE" not in env
        assert env.get("PATH") == "/usr/bin"


def test_sec8_doctor_version_probe_gets_a_child_env(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-ENV-3 (SEC-8): doctor's `--version` probes of git and rg run with the child env."""
    spawns = _Spawns()
    monkeypatch.setattr(doctor_checks.subprocess, "run", spawns)
    monkeypatch.setattr(doctor_checks, "which", lambda name: f"/usr/bin/{name}")
    found = doctor_checks.check_external(DoctorContext(_leaky(scope)))
    assert [f.severity for f in found] == ["ok", "ok"]
    assert len(spawns.envs) == 2
    for env in spawns.envs:
        assert env is not None and "OPENAI_API_KEY" not in env


def test_sec8_locate_probe_passes_only_the_probe_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-ENV-3 (SEC-8): a diagnostic `locate.probe` passes only `PROBE_ENV_NAMES`."""
    tool = tmp_path / "tool"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    monkeypatch.setenv("OPENAI_API_KEY", fake_key())
    spawns = _Spawns()
    monkeypatch.setattr(locate_mod.subprocess, "run", spawns)
    res = locate_mod.locate(str(tool), known=(), env={})
    locate_mod.probe(res, effort=Effort.DIAGNOSTIC)
    assert len(spawns.envs) == 1
    env = spawns.envs[0]
    assert env is not None and "OPENAI_API_KEY" not in env
    assert set(env) <= set(locate_mod.PROBE_ENV_NAMES)
