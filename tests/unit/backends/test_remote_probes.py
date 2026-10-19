"""Probe table of the cloud rows and the flapping guard (P21-12, R02.13, R02.29)."""

from __future__ import annotations

import importlib.metadata
import logging
import stat
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from indus.backends import remote_probes
from indus.backends.registry import default_entries
from indus.backends.remote_probes import (
    CONFIRM_GAP_S,
    FLAP_GRACE_S,
    GUARDED_BACKENDS,
    UNSCOPED_REASON,
    GuardedProbe,
    guard_entry,
)
from indus.foundation.errors import ScopeUnresolved
from indus.foundation.scope import Scope
from tests.fakes.clock import FakeClock
from tests.fakes.terminal import terminal_scope


@pytest.fixture(autouse=True)
def fresh_guards() -> Iterator[None]:
    remote_probes.reset_guards()
    yield
    remote_probes.reset_guards()


def _installed(_: str) -> object:
    return object()


def _absent(name: str) -> object:
    raise importlib.metadata.PackageNotFoundError(name)


class Script:
    """A check that answers from a list (None = ok), then repeats the last answer."""

    def __init__(self, *answers: str | None | BaseException) -> None:
        self.answers = list(answers)
        self.calls = 0

    def __call__(self, scope: Scope) -> str | None:
        self.calls += 1
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, tzinfo=UTC))


# -- the probe table ------------------------------------------------------------------------------


def _with_path(scope: Scope, path: Path) -> Scope:
    return scope.with_env(scope.env.with_overlay({"PATH": str(path)}))


def _fake_cli(folder: Path, name: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    tool = folder / name
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)


CREDENTIALS = {
    "modal": {"MODAL_TOKEN_ID": "i" + "1", "MODAL_TOKEN_SECRET": "s" + "1"},
    "daytona": {"DAYTONA_API_KEY": "k" + "1"},
    "vercel_sandbox": {
        "VERCEL_TOKEN": "t" + "1",
        "VERCEL_PROJECT_ID": "p" + "1",
        "VERCEL_TEAM_ID": "m" + "1",
    },
}


@pytest.mark.parametrize(
    ("backend", "extra", "secrets", "expect"),
    [
        ("modal", False, "all", "`modal` extra"),
        ("modal", True, "none", "MODAL_TOKEN_ID"),
        ("modal", True, "all", None),
        ("daytona", False, "all", "`daytona` extra"),
        ("daytona", True, "none", "DAYTONA_API_KEY"),
        ("daytona", True, "all", None),
        ("vercel_sandbox", False, "all", "`vercel` extra"),
        ("vercel_sandbox", True, "none", "VERCEL_TOKEN"),
        ("vercel_sandbox", True, "all", None),
    ],
)
def test_sdk_probe_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    extra: bool,
    secrets: str,
    expect: str | None,
) -> None:
    monkeypatch.setattr(importlib.metadata, "distribution", _installed if extra else _absent)
    for name in ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "DAYTONA_API_KEY", "VERCEL_OIDC_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    for name in ("VERCEL_TOKEN", "VERCEL_PROJECT_ID", "VERCEL_TEAM_ID"):
        monkeypatch.delenv(name, raising=False)
    scope = terminal_scope(
        tmp_path,
        {"terminal": {"backend": backend}},
        secrets=CREDENTIALS[backend] if secrets == "all" else {},
    )
    rows = {entry.name: entry for entry in default_entries()}
    probe = rows[backend].probe
    assert probe is not None
    answer = probe(scope)
    if expect is None:
        assert answer is None
    else:
        assert expect in (answer or "")


@pytest.mark.parametrize(
    ("installed", "expect"),
    [("apptainer", None), ("singularity", None), (None, "Apptainer or Singularity")],
)
def test_singularity_probe_table(tmp_path: Path, installed: str | None, expect: str | None) -> None:
    folder = tmp_path / "bin"
    folder.mkdir()
    if installed:
        _fake_cli(folder, installed)
    scope = _with_path(terminal_scope(tmp_path, {"terminal": {"backend": "singularity"}}), folder)
    probe = {entry.name: entry for entry in default_entries()}["singularity"].probe
    assert probe is not None
    answer = probe(scope)
    if expect is None:
        assert answer is None
    else:
        assert expect in (answer or "")


def test_the_registry_lists_seven_rows_and_guards_the_four_cloud_probes() -> None:
    rows = default_entries()
    assert [entry.name for entry in rows] == [
        "local",
        "docker",
        "ssh",
        "singularity",
        "modal",
        "daytona",
        "vercel_sandbox",
    ]
    guarded = {entry.name for entry in rows if isinstance(entry.probe, GuardedProbe)}
    assert guarded == set(GUARDED_BACKENDS)
    again = {entry.name: entry.probe for entry in default_entries()}
    first = {entry.name: entry.probe for entry in rows}
    for name in GUARDED_BACKENDS:  # one guard per row for the process, never a guard of a guard
        assert again[name] is first[name]
        probe = again[name]
        assert isinstance(probe, GuardedProbe) and not isinstance(probe.check, GuardedProbe)


def test_rows_outside_the_cloud_set_are_left_alone() -> None:
    rows = {entry.name: entry for entry in default_entries()}
    assert guard_entry(rows["docker"]) is rows["docker"]
    assert guard_entry(rows["local"]) is rows["local"]


# -- the flapping guard (R02.13) ------------------------------------------------------------------


def test_one_flake_after_a_success_serves_last_good_until_confirmed_30s_later(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = _clock()
    check = Script(None, "token missing", "token missing", "token missing", "token missing", None)
    guard = GuardedProbe("modal", check, clock=clock)
    scope = terminal_scope(tmp_path)
    assert guard(scope) is None  # ok
    clock.advance(5)
    with caplog.at_level(logging.WARNING, logger="indus.backends.remote_probes"):
        assert guard(scope) is None  # first failure: served
    assert "serving the last good answer" in caplog.text
    assert guard.last_reason(scope) == "token missing"
    clock.advance(CONFIRM_GAP_S - 10)
    assert guard(scope) is None  # streak only 20 s old: still served
    clock.advance(10)
    assert guard(scope) == "token missing"  # streak 30 s old: honoured
    clock.advance(1)
    assert guard(scope) == "token missing"  # stays down without a success
    clock.advance(1)
    assert guard(scope) is None  # a success brings it back
    assert guard.last_reason(scope) is None
    assert check.calls == 6


def test_a_failure_long_after_the_last_success_is_honoured_at_once(tmp_path: Path) -> None:
    clock = _clock()
    guard = GuardedProbe("daytona", Script(None, "no key"), clock=clock)
    scope = terminal_scope(tmp_path)
    assert guard(scope) is None
    clock.advance(FLAP_GRACE_S + 1)
    assert guard(scope) == "no key"


def test_never_ok_is_unavailable_from_the_first_failure(tmp_path: Path) -> None:
    guard = GuardedProbe("vercel_sandbox", Script("no token"), clock=_clock())
    assert guard(terminal_scope(tmp_path)) == "no token"


def test_the_grace_window_ends_120s_after_the_success(tmp_path: Path) -> None:
    clock = _clock()
    guard = GuardedProbe("modal", Script(None, "x"), clock=clock)
    scope = terminal_scope(tmp_path)
    assert guard(scope) is None
    clock.advance(FLAP_GRACE_S - 10)
    assert guard(scope) is None  # inside the window: served, streak starts
    clock.advance(15)
    assert guard(scope) == "x"  # streak young, but the window is over


def test_a_raising_check_is_a_failure_that_names_the_exception(tmp_path: Path) -> None:
    clock = _clock()
    guard = GuardedProbe("singularity", Script(RuntimeError("boom")), clock=clock)
    answer = guard(terminal_scope(tmp_path)) or ""
    assert "singularity check failed" in answer and "RuntimeError: boom" in answer


def test_profiles_keep_their_own_history(tmp_path: Path) -> None:
    clock = _clock()
    answers: dict[str, list[str | None]] = {"a": [None, "down"], "b": ["down"]}

    def check(scope: Scope) -> str | None:
        queue = answers[scope.home.name]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    guard = GuardedProbe("modal", check, clock=clock)
    a = terminal_scope(tmp_path, name="a")
    b = terminal_scope(tmp_path, name="b")
    assert guard(a) is None
    clock.advance(1)
    assert guard(b) == "down"  # b never succeeded: no grace borrowed from a
    assert guard(a) is None  # a's flake is served


def test_the_scope_clock_is_used_when_none_is_injected(tmp_path: Path) -> None:
    clock = _clock()
    guard = GuardedProbe("modal", Script(None, "x"))
    scope = terminal_scope(tmp_path, clock=clock)
    assert guard(scope) is None
    clock.advance(FLAP_GRACE_S + 1)
    assert guard(scope) == "x"


# -- unscoped probes (R02.29) ---------------------------------------------------------------------


def test_an_unscoped_probe_caches_nothing_and_the_first_scoped_call_checks(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    check = Script("no token")
    guard = GuardedProbe("modal", check, clock=_clock())
    with caplog.at_level(logging.DEBUG, logger="indus.backends.remote_probes"):
        assert guard(None) == UNSCOPED_REASON
    assert check.calls == 0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    scope = terminal_scope(tmp_path)
    assert guard.last_reason(scope) is None
    with caplog.at_level(logging.INFO, logger="indus.backends.remote_probes"):
        assert guard(scope) == "no token"
    assert check.calls == 1
    assert "probe runs now that a profile scope exists" in caplog.text


def test_a_check_that_loses_its_scope_is_quiet_and_not_remembered(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = _clock()
    check = Script(ScopeUnresolved("no scope"), None)
    guard = GuardedProbe("daytona", check, clock=clock)
    scope = terminal_scope(tmp_path)
    with caplog.at_level(logging.DEBUG, logger="indus.backends.remote_probes"):
        assert guard(scope) == UNSCOPED_REASON
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert guard.last_reason(scope) is None
    assert guard(scope) is None  # the next call checks for real


def test_unscoped_answer_after_a_success_does_not_end_the_grace(tmp_path: Path) -> None:
    clock = _clock()
    guard = GuardedProbe("modal", Script(None, "x"), clock=clock)
    scope = terminal_scope(tmp_path)
    assert guard(scope) is None
    assert guard(None) == UNSCOPED_REASON
    clock.advance(1)
    assert guard(scope) is None  # the flake grace still applies
