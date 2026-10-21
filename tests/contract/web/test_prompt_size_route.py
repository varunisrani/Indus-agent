"""P18-16 follow-up: ``GET /api/ops/prompt-size`` over the real dashboard app."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from indus.foundation.config import loader as config_loader
from indus.foundation.scope import Scope
from indus.state import registry, sessions
from indus.web.routes import system
from tests.contract.web.rest_app import RestApp, build_rest_app, route_table

PATH = "/api/ops/prompt-size"


@pytest.fixture
def rest(scope: Scope) -> Iterator[RestApp]:
    config_loader.reset()
    built = build_rest_app(scope)
    yield built
    built.close()
    config_loader.reset()


def _code(answer: Any) -> str:
    return answer.json()["detail"]["code"]


def test_the_route_is_listed_and_scoped(rest: RestApp) -> None:
    assert ("GET", PATH, True) in system.ROUTE_LIST
    assert ("GET", PATH, True) in route_table(rest.app, ("/api/ops",))


def test_the_profile_breakdown(rest: RestApp, scope: Scope) -> None:
    (scope.home / "PERSONA.md").write_text("You are Pat.\n", encoding="utf-8")
    answer = rest.client.get(PATH)
    assert answer.status_code == 200, answer.text
    got = answer.json()
    assert got["profile"] == scope.profile_id and got["session_id"] is None
    assert got["platform"] == "cli" and got["estimate"] == "chars/4"
    assert got["sections"]["persona"] == {"chars": len("You are Pat."), "tokens": 3}
    assert (
        got["request"]["chars"] == got["system_prompt"]["chars"] + got["sections"]["tools"]["chars"]
    )
    assert got["tool_count"] > 0
    acp = rest.client.get(PATH, params={"platform": "acp"}).json()
    assert acp["platform"] == "acp"


def test_a_stored_session(rest: RestApp, scope: Scope) -> None:
    with registry.open_unshared(scope) as handle:
        sessions.create(handle, "s-measured", "telegram", system_prompt="Stored prompt text.")
    got = rest.client.get(PATH, params={"session_id": "s-measured"}).json()
    assert got["session_id"] == "s-measured" and got["platform"] == "telegram"
    assert got["stored_prompt"] == {"chars": len("Stored prompt text."), "tokens": 5}
    missing = rest.client.get(PATH, params={"session_id": "nope"})
    assert missing.status_code == 404 and _code(missing) == "session_not_found"


def test_an_unknown_session_without_a_store(rest: RestApp) -> None:
    answer = rest.client.get(PATH, params={"session_id": "s-1"})
    assert answer.status_code == 404 and _code(answer) == "session_not_found"


@pytest.mark.parametrize("platform", ["../x", "CLI;rm", "a" * 40, "-x"])
def test_a_bad_platform_is_refused(rest: RestApp, platform: str) -> None:
    answer = rest.client.get(PATH, params={"platform": platform})
    assert answer.status_code == 400 and _code(answer) == "bad_platform"


def test_an_unknown_profile_is_404(rest: RestApp) -> None:
    assert rest.client.get(PATH, params={"profile": "ghost"}).status_code == 404


def test_without_the_session_token_it_is_refused(rest: RestApp) -> None:
    assert rest.anonymous().get(PATH).status_code == 401
