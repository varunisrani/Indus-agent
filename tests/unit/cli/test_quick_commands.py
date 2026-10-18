"""Quick commands from ``quick_commands`` (P06-11, R04.28)."""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

from indus import __version__
from indus.cli import quick_commands
from indus.cli.slash.dispatch import dispatch
from indus.foundation.scope import Scope
from tests.unit.cli.slash._fakes import make_ctx, with_config
from tests.unit.cli.test_bang import AdvancingClock

pytestmark = pytest.mark.skipif(os.name != "posix", reason="runs POSIX shell commands")

FAKE_TOKEN = "ghp_" + "Rw4k" * 9  # built at runtime: no credential-shaped literal
FAKE_PROVIDER_KEY = "sk-" + "Hj2" * 12


def configured(scope: Scope, entries: str) -> Scope:
    return with_config(scope, "quick_commands:\n" + entries)


def test_names_and_lookup(scope: Scope) -> None:
    scope = configured(scope, "  GS: {type: exec, command: git status}\n  bad: 3\n")
    assert quick_commands.names(scope) == ["gs"]
    assert quick_commands.lookup(scope, "/Gs") == {"type": "exec", "command": "git status"}
    assert quick_commands.lookup(scope, "bad") is None
    assert quick_commands.names(with_config(scope, "quick_commands: nope\n")) == []


def test_exec_output_redacted(scope: Scope) -> None:
    scope = configured(scope, f"  leak: {{type: exec, command: 'echo token={FAKE_TOKEN} done'}}\n")
    ctx = make_ctx(scope)
    dispatch("/leak", ctx)
    assert FAKE_TOKEN not in ctx.text
    assert "done" in ctx.text


def test_exec_env_has_no_provider_key(scope: Scope) -> None:
    scope = configured(scope, "  vars: {type: exec, command: env}\n")
    env = scope.env.with_overlay({"OPENAI_API_KEY": FAKE_PROVIDER_KEY, "PLAIN_SETTING": "kept"})
    ctx = make_ctx(replace(scope, env=env))
    dispatch("/vars", ctx)
    assert "PLAIN_SETTING=kept" in ctx.text
    assert "OPENAI_API_KEY" not in ctx.text
    assert FAKE_PROVIDER_KEY not in ctx.text


def test_exec_messages(scope: Scope) -> None:
    scope = configured(
        scope,
        "  quiet: {type: exec, command: 'true'}\n"
        "  fails: {type: exec, command: 'echo nope; exit 4'}\n"
        "  silentfail: {type: exec, command: 'exit 2'}\n",
    )
    ctx = make_ctx(scope)
    dispatch("/quiet", ctx)
    assert ctx.lines == ["/quiet printed nothing."]
    ctx.lines.clear()
    dispatch("/fails", ctx)
    assert ctx.lines == ["nope", "(exit 4)"]
    ctx.lines.clear()
    dispatch("/silentfail", ctx)
    assert ctx.lines == ["/silentfail printed nothing. (exit 2)"]


def test_exec_timeout(scope: Scope) -> None:
    scope = configured(scope, "  slow: {type: exec, command: 'echo begun; sleep 30'}\n")
    ctx = make_ctx(scope, clock=AdvancingClock(20.0))
    dispatch("/slow", ctx)
    assert ctx.lines == ["begun", "/slow timed out after 30s."]


def test_alias_redispatches(scope: Scope) -> None:
    scope = configured(
        scope,
        "  ver: {type: alias, target: /version}\n"
        "  name: {type: alias, target: title}\n"
        "  loop1: {type: alias, target: loop2}\n"
        "  loop2: {type: alias, target: loop1}\n",
    )
    ctx = make_ctx(scope)
    dispatch("/ver", ctx)
    assert ctx.lines == [f"Indus Agent {__version__}"]
    ctx.lines.clear()
    dispatch("/loop1", ctx)
    assert ctx.lines == ["Quick command /loop1 points round in a circle; stopped."]


def test_bad_entries(scope: Scope) -> None:
    scope = configured(
        scope,
        "  empty: {type: exec}\n  odd: {type: script, command: ls}\n  nowhere: {type: alias}\n",
    )
    ctx = make_ctx(scope)
    dispatch("/empty", ctx)
    dispatch("/odd", ctx)
    dispatch("/nowhere", ctx)
    assert ctx.lines == [
        "Quick command /empty has no command to run.",
        "Quick command /odd has an unknown type 'script' (use exec or alias).",
        "Quick command /nowhere has no target.",
    ]
