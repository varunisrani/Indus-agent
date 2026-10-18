"""P07-20: `indus logs` — filters, traceback continuation, --since on the fake clock, follow."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from indus.cli.commands import logs as logs_mod
from indus.cli.commands.logs import Filters, follow
from indus.foundation.scope import Scope
from tests.unit.cli.commands._helpers_d import run

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "logs" / "agent.log"


@pytest.fixture
def agent_log(scope: Scope) -> Path:
    target = scope.home / "logs" / "agent.log"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURE, target)
    return target


def _body(out: str) -> list[str]:
    return out.splitlines()[1:]  # drop the header line


def test_default_tail(scope: Scope, agent_log: Path) -> None:
    result = run(scope, "logs", "-n", "2")
    assert result.code == 0, result.err
    assert result.out.splitlines()[0].startswith(f"--- {agent_log} (last 2)")
    assert _body(result.out) == [
        "KeyError: 'missing'",
        "2026-01-01 08:55:00 INFO indus.cli.chat [20260101_090000_def456]: chat ended",
    ]


def test_level_keeps_tracebacks_with_their_record(scope: Scope, agent_log: Path) -> None:
    body = _body(run(scope, "logs", "--level", "error").out)
    assert len(body) == 8
    assert body[0].endswith("turn failed") and body[3] == "ValueError: the model went away"
    assert body[4].endswith("job failed") and body[7] == "KeyError: 'missing'"
    assert not any("stray line" in line for line in body)


def test_session_filter_carries_to_continuation_lines(scope: Scope, agent_log: Path) -> None:
    body = _body(run(scope, "logs", "--session", "abc123").out)
    assert [line.split()[2] for line in body[:3]] == ["DEBUG", "WARNING", "ERROR"]
    assert body[-1] == "ValueError: the model went away"
    assert len(body) == 6


@pytest.mark.parametrize(
    ("component", "loggers"),
    [
        ("tools", ["indus.tools.builtin.terminal"]),
        ("agent", ["indus.providers.http", "indus.core.loop"]),
        ("cli", ["indus.cli.chat", "indus.cli.chat"]),
        ("gateway", ["indus.gateway.daemon"]),
        ("cron", ["indus.cron.ticker"]),
    ],
)
def test_component(scope: Scope, agent_log: Path, component: str, loggers: list[str]) -> None:
    body = _body(run(scope, "logs", "--component", component).out)
    stamped = [line.split()[3].rstrip(":") for line in body if line[:4].isdigit()]
    assert stamped == loggers


def test_since_uses_the_scope_clock(scope: Scope, agent_log: Path) -> None:
    # The fake clock reads 2026-01-01 09:00 UTC.
    body = _body(run(scope, "logs", "--since", "30m").out)
    assert body[0].startswith("2026-01-01 08:30:00 ERROR")
    assert body[-1].endswith("chat ended") and len(body) == 10
    assert _body(run(scope, "logs", "--since", "1m").out) == []


def test_filters_and_together(scope: Scope, agent_log: Path) -> None:
    body = _body(run(scope, "logs", "--level", "ERROR", "--component", "cron").out)
    assert body[0].endswith("job failed") and len(body) == 4
    none = _body(run(scope, "logs", "--level", "ERROR", "--session", "def456").out)
    assert none == []


def test_stray_lines_before_the_first_stamp(scope: Scope, agent_log: Path) -> None:
    plain = _body(run(scope, "logs", "-n", "100").out)
    assert plain[0].endswith("before the last restart")
    with_session = _body(run(scope, "logs", "--session", "restart").out)
    assert with_session == ["  a stray line left over from before the last restart"]


def test_list_and_missing(scope: Scope, agent_log: Path) -> None:
    (agent_log.parent / "agent.log.1").write_text("old\n")
    listed = run(scope, "logs", "list")
    assert listed.code == 0
    assert "agent.log " in listed.out and "agent.log.1" in listed.out and "764 B" in listed.out
    missing = run(scope, "logs", "errors")
    assert missing.code == 1 and "No errors log yet" in missing.err


@pytest.mark.parametrize(
    "argv",
    [["--since", "yesterday"], ["--level", "LOUD"], ["--component", "web"], ["nonsense"]],
)
def test_usage_errors(scope: Scope, agent_log: Path, argv: list[str]) -> None:
    assert run(scope, "logs", *argv).code == 2


def test_follow_survives_rotation_and_truncation(tmp_path: Path) -> None:
    path = tmp_path / "agent.log"
    path.write_text("2026-01-01 08:00:00 INFO indus.cli.x: before follow\n")
    printed: list[str] = []
    steps = iter(
        [
            lambda: path.open("a").write("2026-01-01 08:01:00 INFO indus.cli.x: appended\n"),
            lambda: (
                path.rename(tmp_path / "agent.log.1"),
                path.write_text("2026-01-01 08:02:00 INFO indus.core.y: after rotation\n"),
            ),
            lambda: path.open("a").write("partial line without an end"),
            lambda: path.open("a").write(" now ended\n"),
            lambda: path.write_text(""),
            lambda: path.write_text("2026-01-01 08:03:00 ERROR indus.cli.x: after truncate\n"),
        ]
    )

    def pause(seconds: float) -> None:
        assert seconds == logs_mod.POLL_SECONDS
        step = next(steps, None)
        if step is None:
            raise KeyboardInterrupt
        step()

    with pytest.raises(KeyboardInterrupt):
        follow(path, Filters(), printed.append, pause)
    assert printed == [
        "2026-01-01 08:01:00 INFO indus.cli.x: appended\n",
        "2026-01-01 08:02:00 INFO indus.core.y: after rotation\n",
        "partial line without an end now ended\n",
        "2026-01-01 08:03:00 ERROR indus.cli.x: after truncate\n",
    ]


def test_follow_applies_filters(tmp_path: Path) -> None:
    path = tmp_path / "agent.log"
    path.write_text("")
    printed: list[str] = []
    steps = iter(
        [
            lambda: path.open("a").write(
                "2026-01-01 08:01:00 INFO indus.cli.x: quiet\n"
                "2026-01-01 08:02:00 ERROR indus.cli.x: loud\n"
                "  frame of the loud one\n"
            )
        ]
    )

    def pause(_seconds: float) -> None:
        step = next(steps, None)
        if step is None:
            raise KeyboardInterrupt
        step()

    with pytest.raises(KeyboardInterrupt):
        follow(path, Filters(min_level=40), printed.append, pause)
    assert printed == [
        "2026-01-01 08:02:00 ERROR indus.cli.x: loud\n",
        "  frame of the loud one\n",
    ]


def test_follow_command_stops_on_ctrl_c(
    scope: Scope, agent_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(_clock: object) -> object:
        def pause(_seconds: float) -> None:
            raise KeyboardInterrupt

        return pause

    monkeypatch.setattr(logs_mod, "blocking_sleep", interrupted)
    result = run(scope, "logs", "-f", "-n", "1")
    assert result.code == 0
    assert "following" in result.out and result.out.rstrip().endswith("--- stopped ---")
