"""Timeout policy (P03-13): stale and read timeouts per call shape."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from indus.foundation.scope import EnvView, Scope
from indus.providers.timeouts import CallTimeouts, TimeoutPolicy, is_local_endpoint

CLOUD = "https://api.example.com/v1"
LOCAL = "http://127.0.0.1:8080/v1"


@dataclass(frozen=True)
class Route:
    base_url: str


# (id, policy kwargs, base url, est tokens, streaming, reasoning, expected stale)
TABLE: list[tuple[str, dict[str, Any], str, int, bool, bool, float | None]] = [
    ("cloud-10k", {}, CLOUD, 10_000, True, False, 180.0),
    ("cloud-50k-edge", {}, CLOUD, 50_000, True, False, 180.0),
    ("cloud-60k", {}, CLOUD, 60_000, True, False, 240.0),
    ("cloud-100k-edge", {}, CLOUD, 100_000, True, False, 240.0),
    ("cloud-120k", {}, CLOUD, 120_000, True, False, 300.0),
    ("reasoning-10k", {}, CLOUD, 10_000, True, True, 300.0),
    ("reasoning-120k", {}, CLOUD, 120_000, True, True, 300.0),
    ("local-stream", {}, LOCAL, 10_000, True, False, 900.0),
    ("local-stream-huge", {}, LOCAL, 500_000, True, True, 900.0),
    ("local-stream-configured", {"local_stream_stale": 1200.0}, LOCAL, 1_000, True, False, 1200.0),
    ("local-private-lan", {}, "http://192.168.1.20:11434/v1", 1_000, True, False, 900.0),
    ("nonstream-10k", {}, CLOUD, 10_000, False, False, 90.0),
    ("nonstream-60k", {}, CLOUD, 60_000, False, False, 150.0),
    ("nonstream-120k", {}, CLOUD, 120_000, False, False, 240.0),
    ("nonstream-local-unset", {}, LOCAL, 120_000, False, False, None),
    ("nonstream-local-explicit", {"explicit_call_stale": 45.0}, LOCAL, 1, False, False, 45.0),
    (
        "explicit-60-on-120k-reasoning",
        {"explicit_stream_stale": 60.0},
        CLOUD,
        120_000,
        True,
        True,
        60.0,
    ),
    ("explicit-2000-kept", {"explicit_stream_stale": 2000.0}, CLOUD, 10_000, True, False, 2000.0),
    ("explicit-local-shorter", {"explicit_stream_stale": 30.0}, LOCAL, 10_000, True, False, 30.0),
    ("explicit-zero-disables", {"explicit_stream_stale": 0.0}, CLOUD, 10_000, True, False, None),
    (
        "explicit-call-not-floored",
        {"explicit_call_stale": 20.0},
        CLOUD,
        120_000,
        False,
        False,
        20.0,
    ),
    (
        "budget-100-left-caps-implicit",
        {"run_budget_remaining": 100.0},
        CLOUD,
        10_000,
        True,
        False,
        60.0,
    ),
    ("budget-500-left", {"run_budget_remaining": 500.0}, CLOUD, 120_000, True, False, 250.0),
    ("budget-plenty", {"run_budget_remaining": 10_000.0}, CLOUD, 120_000, True, False, 300.0),
    ("budget-spent", {"run_budget_remaining": -5.0}, CLOUD, 10_000, False, False, 60.0),
    (
        "budget-explicit-untouched",
        {"run_budget_remaining": 100.0, "explicit_stream_stale": 2000.0},
        CLOUD,
        10_000,
        True,
        False,
        2000.0,
    ),
    ("budget-local", {"run_budget_remaining": 100.0}, LOCAL, 10_000, True, False, 60.0),
]


@pytest.mark.parametrize(
    ("kwargs", "url", "tokens", "streaming", "reasoning", "expected"),
    [row[1:] for row in TABLE],
    ids=[row[0] for row in TABLE],
)
def test_table(
    kwargs: dict[str, Any],
    url: str,
    tokens: int,
    streaming: bool,
    reasoning: bool,
    expected: float | None,
) -> None:
    result = TimeoutPolicy(**kwargs).for_call(
        Route(url), tokens, streaming=streaming, reasoning=reasoning
    )
    assert result.stale == expected
    assert result.explicit == any(k.startswith("explicit") for k in kwargs)


def test_table_has_at_least_twenty_rows() -> None:
    assert len(TABLE) >= 20


def test_read_timeout_raised_never_lowered() -> None:
    policy = TimeoutPolicy()
    assert policy.for_call(Route(CLOUD), 1, streaming=True).read == 180.0
    assert policy.for_call(Route(LOCAL), 1, streaming=True).read == 900.0
    assert policy.for_call(Route(CLOUD), 1, streaming=False).read == 120.0  # 90 < 120
    assert policy.for_call(Route(LOCAL), 1, streaming=False).read == 120.0  # disabled stale
    short = TimeoutPolicy(explicit_stream_stale=30.0)
    assert short.for_call(Route(CLOUD), 1, streaming=True).read == 120.0


def test_local_flag_reported() -> None:
    result: CallTimeouts = TimeoutPolicy().for_call(Route(LOCAL), 1, streaming=True)
    assert result.local and not result.explicit


@pytest.mark.parametrize(
    ("url", "local"),
    [
        ("http://localhost:1234/v1", True),
        ("http://127.0.0.1:8080/v1", True),
        ("http://[::1]:8080/v1", True),
        ("http://10.0.0.5/v1", True),
        ("http://172.16.3.4:8000/v1", True),
        ("http://100.80.1.2:8000/v1", True),
        ("http://[fd00::1]:8000/v1", True),
        ("http://gpu-box.local:8000/v1", True),
        ("https://api.example.com/v1", False),
        ("https://8.8.8.8/v1", False),
        ("https://localhost.example.com/v1", False),
        ("", False),
        ("not a url", False),
    ],
)
def test_is_local_endpoint(url: str, local: bool) -> None:
    assert is_local_endpoint(url) is local


class _Config:
    def __init__(self, values: dict[str, object]) -> None:
        self.values = values

    def get(self, path: str, /) -> object:
        return self.values.get(path)


def _with(scope: Scope, env: dict[str, str], config: dict[str, object]) -> Scope:
    return scope.with_env(EnvView(env)).with_config(_Config(config))


def test_from_scope_defaults(scope: Scope) -> None:
    policy = TimeoutPolicy.from_scope(_with(scope, {}, {}))
    assert policy == TimeoutPolicy()


def test_from_scope_env_beats_config(scope: Scope) -> None:
    policy = TimeoutPolicy.from_scope(
        _with(
            scope,
            {"INDUS_STREAM_STALE_TIMEOUT": "75", "INDUS_LOCAL_STREAM_STALE_TIMEOUT": "1500"},
            {
                "agent.stream_stale_timeout": 400,
                "agent.api_call_stale_timeout": 33,
                "agent.local_stream_stale_timeout": 1000,
            },
        ),
        run_budget_remaining=500.0,
    )
    assert policy.explicit_stream_stale == 75.0
    assert policy.explicit_call_stale == 33.0
    assert policy.local_stream_stale == 1500.0
    assert policy.run_budget_remaining == 500.0


def test_from_scope_reads_local_default_from_real_config(scope: Scope) -> None:
    from indus.foundation.config import load

    loaded = scope.with_config(load(scope))
    assert TimeoutPolicy.from_scope(loaded).local_stream_stale == 900.0


@pytest.mark.parametrize("bad", ["abc", "-3", "nan", " "])
def test_bad_env_values_are_ignored(scope: Scope, bad: str) -> None:
    policy = TimeoutPolicy.from_scope(_with(scope, {"INDUS_STREAM_STALE_TIMEOUT": bad}, {}))
    assert policy.explicit_stream_stale is None


def test_breaker_threshold_from_env(scope: Scope) -> None:
    assert TimeoutPolicy.from_scope(_with(scope, {}, {})).breaker_threshold == 5
    off = TimeoutPolicy.from_scope(_with(scope, {"INDUS_STREAM_STALE_GIVEUP": "0"}, {}))
    assert off.breaker_threshold == 0
    assert off.for_call(Route(CLOUD), 1, streaming=True).breaker_threshold == 0
    bad = TimeoutPolicy.from_scope(_with(scope, {"INDUS_STREAM_STALE_GIVEUP": "x"}, {}))
    assert bad.breaker_threshold == 5
