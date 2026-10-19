"""P15-32: webhook route rules — filters, templates, coalesce keys, rate window, delivery ids."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from indus.gateway.http.webhook_rules import (
    IDEMPOTENCY_TTL_S,
    CoalesceSpec,
    DeliveryMemory,
    RateWindow,
    RouteError,
    check_route,
    coalesce_key,
    filter_matches,
    filters_match,
    load_route,
    render,
)
from tests.fakes.clock import FakeClock

PAYLOADS = Path(__file__).parents[3] / "fixtures" / "gateway" / "webhook_payloads"
PR = json.loads((PAYLOADS / "github_pull_request.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ({"field": "action", "equals": "opened"}, True),
        ({"field": "action", "equals": "closed"}, False),
        ({"field": "pull_request.number", "equals": 42}, True),
        ({"field": "pull_request.draft", "equals": False}, True),
        ({"field": "pull_request.title", "contains": "nightly"}, True),
        ({"field": "pull_request.labels.1.name", "equals": "needs-review"}, True),
        ({"field": "pull_request.labels.9.name", "exists": False}, True),
        ({"field": "repository.private", "exists": True}, True),
        ({"field": "nope", "contains": "x"}, False),
        ({"field": "event", "equals": "pull_request"}, True),
        (
            {
                "all": [
                    {"field": "action", "equals": "opened"},
                    {"field": "pull_request.draft", "equals": True},
                ]
            },
            False,
        ),
        (
            {
                "any": [
                    {"field": "action", "equals": "closed"},
                    {"field": "pull_request.base.ref", "equals": "main"},
                ]
            },
            True,
        ),
        ({"not": {"field": "pull_request.draft", "equals": True}}, True),
        ({"field": "action"}, False),  # no operator
        ("not a mapping", False),
    ],
)
def test_filters(spec: object, expected: bool) -> None:
    assert filter_matches(spec, PR, "pull_request") is expected


def test_contains_checks_list_members() -> None:
    payload = {"tags": ["disk", "ci"]}
    assert filter_matches({"field": "tags", "contains": "ci"}, payload, "x")
    assert not filter_matches({"field": "tags", "contains": "c"}, payload, "x")
    assert filters_match([], payload, "x")


def test_templates_read_dot_paths_and_never_fail() -> None:
    text = render(
        "{pull_request.title} #{number} by {pull_request.user.login} "
        "[{pull_request.labels.0.name}] draft={pull_request.draft} {missing.key}|{event}",
        PR,
        "pull_request",
        "gh",
    )
    assert text == (
        "Speed up the nightly import #42 by river-otter [performance] draft=false |pull_request"
    )
    assert json.loads(render("{payload}", PR, "e", "r")) == PR
    assert render("{pull_request.base}", PR, "e", "r") == '{"ref": "main"}'
    default = render("", {"a": 1}, "push", "gh")
    assert "Webhook event 'push' on route 'gh'" in default and '"a": 1' in default
    assert render("{x}", {"x": "y" * 5000}, "e", "r").endswith("…")


def test_coalesce_keys() -> None:
    assert coalesce_key(CoalesceSpec(key="pull_request.number"), PR, "e") == "42"
    spec = CoalesceSpec(key="{repository.full_name}#{number}")
    assert coalesce_key(spec, PR, "e") == "example-org/data-tools#42"
    assert coalesce_key(CoalesceSpec(key="{repository.nope}#{number}"), PR, "e") is None
    assert coalesce_key(CoalesceSpec(key="no.such"), PR, "e") is None


def test_route_checks() -> None:
    ok = load_route("gh", {"secret": "INDUS_WEBHOOK_GH"})
    check_route("gh", ok, "0.0.0.0")
    with pytest.raises(RouteError, match="names"):
        check_route("Bad Name", ok, "127.0.0.1")
    insecure = load_route("t", {"insecure": True})
    check_route("t", insecure, "127.0.0.1")
    check_route("t", insecure, "::1")
    with pytest.raises(RouteError, match="loopback"):
        check_route("t", insecure, "0.0.0.0")
    with pytest.raises(RouteError, match="no secret"):
        check_route("t", load_route("t", {"secret": "  "}), "127.0.0.1")
    with pytest.raises(RouteError, match="coalesce"):
        check_route(
            "t",
            load_route(
                "t",
                {"secret": "S", "deliver_only": True, "deliver": "local", "coalesce": {"key": "a"}},
            ),
            "127.0.0.1",
        )
    with pytest.raises(RouteError, match="auth"):
        load_route("t", {"secret": "S", "auth": "svix"})
    with pytest.raises(RouteError, match="window_seconds"):
        load_route("t", {"secret": "S", "coalesce": {"key": "a", "window_seconds": 0}})


def test_rate_window_slides(fake_clock: FakeClock) -> None:
    window = RateWindow(fake_clock, lambda: 3)
    assert [window.allow("a") for _ in range(4)] == [True, True, True, False]
    assert window.allow("b")  # per route
    fake_clock.advance(59.0)
    assert not window.allow("a")
    fake_clock.advance(1.0)
    assert window.allow("a")


def test_delivery_memory_remembers_an_hour(fake_clock: FakeClock) -> None:
    memory = DeliveryMemory(fake_clock, cap=3)
    assert memory.first_time("a", "1") and not memory.first_time("a", "1")
    assert memory.first_time("b", "1")  # per route
    fake_clock.advance(IDEMPOTENCY_TTL_S)
    assert memory.first_time("a", "1")
    memory.forget("a", "1")
    assert memory.first_time("a", "1")
    for n in range(10):
        memory.first_time("c", str(n))
    assert len(memory.seen) <= 3
