"""P18-13: the Logs and Analytics REST routes."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest

from indus.foundation.log import logs_dir
from indus.foundation.scope import Scope
from indus.state import registry, sessions, usage
from indus.state.usage import UsageDelta
from indus.web.routes import analytics, logs
from tests.contract.web.rest_app import RestApp, build_rest_app, route_table
from tests.fakes.clock import FakeClock

SECRET = "kv-" + "".join(["p4Wd"] * 8)
"""A fake credential built at runtime (no vendor shape)."""
DAY = 86400.0


@pytest.fixture
def rest(scope: Scope) -> Iterator[RestApp]:
    built = build_rest_app(scope)
    yield built
    built.close()


def _code(answer: Any) -> str:
    return answer.json()["detail"]["code"]


# -- logs ------------------------------------------------------------------------------------------

AGENT_LOG = [
    "2026-01-01 08:00:00 DEBUG indus.core.agent: thinking",
    "2026-01-01 08:00:01 INFO indus.tools.terminal: ran ls",
    "2026-01-01 08:00:02 WARNING indus.gateway.daemon [s1:t1]: slow platform",
    "2026-01-01 08:00:03 ERROR indus.core.agent: model call failed",
    "Traceback (most recent call last):",
    '  File "x.py", line 1, in <module>',
    "2026-01-01 08:00:04 INFO indus.cron.ticker: tick",
    f"2026-01-01 08:00:05 INFO indus.providers.http: Authorization: Bearer {SECRET}",
    f"2026-01-01 08:00:06 INFO indus.core.agent: OPENAI_API_KEY={SECRET}",
    "2026-01-01 08:00:07 INFO indusx.other: not ours",
]


def _write_log(scope: Scope, name: str, lines: list[str]) -> None:
    folder = logs_dir(scope.home)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_log_lines_with_a_key_are_redacted(rest: RestApp, scope: Scope) -> None:
    _write_log(scope, "agent.log", AGENT_LOG)
    answer = rest.client.get("/api/logs")
    assert answer.status_code == 200
    body = answer.json()
    assert body["file"] == "agent" and len(body["lines"]) == len(AGENT_LOG)
    assert SECRET not in answer.text
    assert body["lines"][0] == AGENT_LOG[0]


def test_level_component_and_tail_filters(rest: RestApp, scope: Scope) -> None:
    _write_log(scope, "agent.log", AGENT_LOG)

    def lines(**params: Any) -> list[str]:
        return rest.client.get("/api/logs", params=params).json()["lines"]

    errors = lines(level="ERROR")
    assert errors == AGENT_LOG[3:6]  # the traceback travels with its record
    assert lines(level="WARNING") == AGENT_LOG[2:6]
    assert lines(level="ALL") == lines()
    agent = lines(component="agent")
    assert AGENT_LOG[0] in agent and AGENT_LOG[4] in agent and AGENT_LOG[1] not in agent
    assert all("indusx" not in line for line in agent)
    assert lines(component="cron") == [AGENT_LOG[6]]
    assert lines(component="gateway", level="INFO") == [AGENT_LOG[2]]
    assert lines(component="all", level="all") == lines()

    many = [f"2026-01-01 09:00:00 INFO indus.core.agent: line {i}" for i in range(600)]
    _write_log(scope, "agent.log", many)
    assert lines(lines=50) == many[-50:]
    assert len(lines()) == 100 and len(lines(lines=500)) == 500


def test_other_files_and_a_missing_file(rest: RestApp, scope: Scope) -> None:
    assert rest.client.get("/api/logs", params={"file": "errors"}).json() == {
        "file": "errors",
        "lines": [],
    }
    _write_log(scope, "gateway.log", ["2026-01-01 08:00:00 INFO indus.gateway.x: up"])
    gateway = rest.client.get("/api/logs", params={"file": "gateway"}).json()
    assert gateway["lines"] == ["2026-01-01 08:00:00 INFO indus.gateway.x: up"]


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"file": "secrets"}, "invalid_file"),
        ({"file": "../config"}, "invalid_file"),
        ({"lines": 42}, "invalid_lines"),
        ({"level": "LOUD"}, "invalid_level"),
        ({"component": "kernel"}, "invalid_component"),
    ],
)
def test_bad_parameters_are_400(rest: RestApp, params: dict[str, Any], code: str) -> None:
    answer = rest.client.get("/api/logs", params=params)
    assert answer.status_code == 400 and _code(answer) == code


def test_logs_need_the_session_token(rest: RestApp) -> None:
    assert rest.anonymous().get("/api/logs").status_code == 401


# -- analytics -------------------------------------------------------------------------------------


def _usage_store(scope: Scope, clock: FakeClock) -> float:
    """Sessions over three days plus one outside a 7-day window; returns today's midnight."""
    now = clock.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    store = registry.open_unshared(scope)
    try:
        rows = [
            ("today-a", midnight + 3600, 100, 50, 0.25, None),
            ("today-b", midnight + 7200, 10, 5, 0.05, 0.04),
            ("yesterday", midnight - DAY + 60, 1000, 400, 1.0, None),
            ("last-week", midnight - 6 * DAY + 60, 7, 3, None, None),
            ("too-old", midnight - 7 * DAY - 60, 5000, 5000, 9.0, None),
        ]
        for sid, started, tokens_in, tokens_out, estimate, actual in rows:
            sessions.create(store, sid, "cli", started_at=started)

            def stamp(
                conn: sqlite3.Connection,
                sid: str = sid,
                values: tuple[Any, ...] = (tokens_in, tokens_out, estimate, actual),
            ) -> None:
                conn.execute(
                    "UPDATE sessions SET input_tokens = ?, output_tokens = ?, "
                    "estimated_cost_usd = ?, actual_cost_usd = ? WHERE id = ?",
                    (*values, sid),
                )

            store.write(stamp)
        side = UsageDelta(input_tokens=20, output_tokens=2, estimated_cost_usd=0.01, model="m")
        assert usage.record(store, "yesterday", side, task="title")
    finally:
        store.close()
    return midnight


def test_usage_sums_match_the_store(rest: RestApp, scope: Scope, fake_clock: FakeClock) -> None:
    fake_clock.advance(12 * 3600)
    midnight = _usage_store(scope, fake_clock)
    week = rest.client.get("/api/analytics/usage", params={"days": 7}).json()
    today = fake_clock.now().date()
    days = {row["day"]: row for row in week["daily"]}
    assert sorted(days) == [
        (today - timedelta(days=6)).isoformat(),
        (today - timedelta(days=1)).isoformat(),
        today.isoformat(),
    ]
    assert days[today.isoformat()] == {
        "day": today.isoformat(),
        "input_tokens": 110,
        "output_tokens": 55,
        "cost": 0.29,  # the billed 0.04 replaces the 0.05 estimate
        "sessions": 2,
    }
    yesterday = days[(today - timedelta(days=1)).isoformat()]
    assert yesterday["input_tokens"] == 1020 and yesterday["output_tokens"] == 402
    assert yesterday["cost"] == pytest.approx(1.01) and yesterday["sessions"] == 1
    assert week["totals"] == {
        "input_tokens": 110 + 1020 + 7,
        "output_tokens": 55 + 402 + 3,
        "cost": pytest.approx(1.30),
        "sessions": 4,
    }
    assert week["days"] == 7
    month = rest.client.get("/api/analytics/usage").json()
    assert month["days"] == 30 and month["totals"]["sessions"] == 5
    assert midnight > 0


def test_usage_without_a_store_is_empty_and_creates_nothing(rest: RestApp, scope: Scope) -> None:
    before = sorted(p.name for p in scope.home.iterdir())
    body = rest.client.get("/api/analytics/usage", params={"days": 90}).json()
    assert body == {
        "days": 90,
        "daily": [],
        "totals": {"input_tokens": 0, "output_tokens": 0, "cost": 0, "sessions": 0},
    }
    assert sorted(p.name for p in scope.home.iterdir()) == before


@pytest.mark.parametrize("days", [0, -1, 366, "many"])
def test_usage_days_out_of_range_is_422(rest: RestApp, days: Any) -> None:
    assert rest.client.get("/api/analytics/usage", params={"days": days}).status_code == 422


def test_route_lists_match_the_app(rest: RestApp) -> None:
    assert route_table(rest.app, ("/api/logs",)) == set(logs.ROUTE_LIST)
    assert route_table(rest.app, ("/api/analytics",)) == set(analytics.ROUTE_LIST)
