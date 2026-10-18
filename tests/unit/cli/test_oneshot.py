"""P06-12: exit-code tables, the usage report, -z checks and the hard exit."""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.cli import app as app_mod
from indus.cli import entry, oneshot
from indus.cli.args import CliState, GlobalArgs, HostOverrides
from indus.cli.oneshot import RunOutcome, finite_exit_code, oneshot_exit_code
from indus.foundation import layout
from indus.foundation import log as logmod
from indus.foundation.scope import Scope
from indus.protocol.events import ExitReason
from tests.fakes.agent import any_window
from tests.fakes.model import FakeModelServer, Finish, Text, Turn, fake_route, script_of, text_turn
from tests.fakes.model import Usage as UsageStep


@pytest.fixture(autouse=True)
def keep_cwd() -> Iterator[None]:
    here = os.getcwd()
    yield
    os.chdir(here)


def outcome(reason: ExitReason | None, text: str | None = None, **kw: object) -> RunOutcome:
    return RunOutcome(started=reason is not None, exit_reason=reason, final_text=text, **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("case", "z_code", "q_code"),
    [
        (outcome(ExitReason.COMPLETED, "hi"), 0, 0),
        (outcome(ExitReason.COMPLETED, "  "), 1, 0),
        (outcome(ExitReason.COMPLETED, None), 1, 0),
        (outcome(ExitReason.PROVIDER_FAILED, "explained", failure="http"), 2, 1),
        (outcome(ExitReason.MAX_ITERATIONS, "half"), 2, 1),
        (outcome(ExitReason.BUDGET_EXHAUSTED), 2, 1),
        (outcome(ExitReason.GUARDRAIL_HALT, "stopped"), 2, 1),
        (outcome(ExitReason.INTERRUPTED_BY_USER, "par", interrupted=True), 130, 130),
        (outcome(None), 1, 1),
    ],
)
def test_oneshot_code_table(case: RunOutcome, z_code: int, q_code: int) -> None:
    assert oneshot_exit_code(case) == z_code
    assert finite_exit_code(case) == q_code


def test_usage_report_shape() -> None:
    done = outcome(ExitReason.MAX_ITERATIONS, "half", api_calls=3, session_id="s", model="m")
    report = oneshot.usage_report(done)
    assert report["estimated_cost_usd"] is None and report["cost_status"] == "unknown"
    assert report["input_tokens"] is None and report["tokens_status"] == "unknown"
    assert report["api_calls"] == 3 and report["session_id"] == "s" and report["model"] == "m"
    assert report["completed"] is False and report["partial"] is True
    assert report["failed"] is True and report["interrupted"] is False
    assert report["exit_reason"] == "max_iterations"
    assert "failure" not in report
    assert oneshot.usage_report(RunOutcome(), "no route")["failure"] == "no route"


def test_usage_file_written_and_failure_ignored(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "u.json"
    assert oneshot.write_usage_file(str(target), outcome(ExitReason.COMPLETED, "x"))
    assert json.loads(target.read_text(encoding="utf-8"))["completed"] is True
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")
    assert not oneshot.write_usage_file(str(blocker / "u.json"), RunOutcome())
    assert not oneshot.write_usage_file(None, RunOutcome())


def state_for(scope: Scope, server: FakeModelServer, **values: object) -> CliState:
    state = CliState(
        scope=scope,
        overrides=HostOverrides(
            route=fake_route(server),
            client_factory=lambda route: server.client(),
            list_models=any_window,
        ),
    )
    state.globals = GlobalArgs(**values)  # type: ignore[arg-type]
    return state


def test_provider_without_model_is_a_usage_error(
    scope: Scope, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    usage = tmp_path / "u.json"
    server = FakeModelServer(script_of(text_turn("x")))
    state = state_for(scope, server, oneshot="hi", provider="openai", usage_file=str(usage))
    assert oneshot.run_from_cli(state) == 2
    assert "--provider needs --model" in capsys.readouterr().err
    assert state.hard_exit is True
    assert json.loads(usage.read_text(encoding="utf-8"))["failed"] is True
    with_env = scope.with_env(scope.env.with_overlay({"INDUS_INFERENCE_MODEL": "fake-model-1"}))
    os.chdir(tmp_path)
    state = state_for(with_env, server, oneshot="hi", provider="openai")
    assert oneshot.run_from_cli(state) == 0


def test_empty_prompt_and_bad_toolsets_are_usage_errors(scope: Scope) -> None:
    server = FakeModelServer(script_of(text_turn("x")))
    assert oneshot.run_from_cli(state_for(scope, server, oneshot="   ")) == 2
    assert oneshot.run_from_cli(state_for(scope, server, oneshot="x", toolsets="nope,zilch")) == 2


def test_toolsets_all_and_unknown_entries(
    scope: Scope, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    os.chdir(tmp_path)
    server = FakeModelServer(script_of(text_turn("ok"), text_turn("ok")))
    assert oneshot.run_from_cli(state_for(scope, server, oneshot="x", toolsets="web,nope")) == 0
    assert "ignoring unknown toolsets: nope" in capsys.readouterr().err
    names = {t["function"]["name"] for t in server.model_requests[0].body.get("tools", [])}
    assert "terminal" not in names
    assert oneshot.run_from_cli(state_for(scope, server, oneshot="x", toolsets="*")) == 0
    names = {t["function"]["name"] for t in server.model_requests[1].body.get("tools", [])}
    assert "terminal" in names


def test_stdout_holds_only_the_reply(
    scope: Scope, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    os.chdir(tmp_path)
    server = FakeModelServer(script_of(text_turn("just ", "this")))
    assert oneshot.run_from_cli(state_for(scope, server, oneshot="hi")) == 0
    out, err = capsys.readouterr()
    assert out == "just this\n" and err == ""


def test_usage_file_reads_the_session_ledger(scope: Scope, tmp_path: Path) -> None:
    """P08-08: ``-z --usage-file`` carries the ledger's tokens; a custom endpoint has no list
    price, so the cost stays unknown (null) rather than $0."""
    os.chdir(tmp_path)
    turn = Turn((Text(("hello",)), UsageStep(120, 7, 20), Finish("stop")))
    server = FakeModelServer(script_of(turn))
    usage = tmp_path / "u.json"
    assert oneshot.run_from_cli(state_for(scope, server, oneshot="hi", usage_file=str(usage))) == 0
    report = json.loads(usage.read_text(encoding="utf-8"))
    assert report["api_calls"] == 1 and report["tokens_status"] == "reported"
    assert (report["input_tokens"], report["cache_read_tokens"]) == (100, 20)
    assert report["output_tokens"] == 7 and report["total_tokens"] == 127
    assert report["cost_status"] == "unknown" and report["estimated_cost_usd"] is None


def test_usage_report_with_a_priced_ledger() -> None:
    ledger = {"api_calls": 2, "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0,
              "cache_write_tokens": 0, "reasoning_tokens": 0, "total_tokens": 15,
              "cost_usd": 0.0042, "cost_status": "estimated"}  # fmt: skip
    report = oneshot.usage_report(outcome(ExitReason.COMPLETED, "x", usage=ledger))
    assert report["estimated_cost_usd"] == 0.0042 and report["cost_status"] == "estimated"
    assert report["cost_source"] == "price_table" and report["total_tokens"] == 15


def test_lone_surrogates_are_replaced() -> None:
    assert oneshot._scrub("a\ud800b") == "a?b"  # pyright: ignore[reportPrivateUsage]


# -- hard exit -------------------------------------------------------------------------------------


@pytest.fixture
def clean_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers = list(root.handlers)
    factory = logging.getLogRecordFactory()
    layout.reset_memo()
    yield
    logmod.shutdown()
    root.handlers[:] = handlers
    logging.setLogRecordFactory(factory)
    layout.reset_memo()


def test_hard_exit_runs_after_cleanups(
    monkeypatch: pytest.MonkeyPatch, clean_logging: None
) -> None:
    order: list[str] = []

    def dispatch(args: list[str], state: CliState) -> int:
        order.append("run")
        state.hard_exit = True
        return 3

    def fake_exit(code: int) -> None:
        order.append(f"exit {code}")

    monkeypatch.setattr(app_mod, "dispatch", dispatch)
    monkeypatch.setattr(logmod, "shutdown", lambda: order.append("logging down"))
    monkeypatch.setattr(entry.os, "_exit", fake_exit)
    monkeypatch.setattr(sys, "argv", ["indus", "-z", "x"])
    saved = dict(os.environ)
    try:
        assert entry.main() == 3
    finally:
        os.environ.clear()
        os.environ.update(saved)
    assert order == ["run", "logging down", "exit 3"]


def test_no_hard_exit_in_tests(monkeypatch: pytest.MonkeyPatch, clean_logging: None) -> None:
    def dispatch(args: list[str], state: CliState) -> int:
        state.hard_exit = True
        return 0

    monkeypatch.setattr(app_mod, "dispatch", dispatch)
    monkeypatch.setattr(entry.os, "_exit", lambda code: pytest.fail("hard exit in a test call"))
    assert entry.main(["-z", "x"], env={"INDUS_HOME": os.environ["INDUS_HOME"]}) == 0
