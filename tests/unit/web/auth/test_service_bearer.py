"""P18-04: service bearer routes (exact opt-in, fail closed, weak secrets disable the route)."""

from __future__ import annotations

import math
import secrets
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter

from indus.service.gateway_control import ControlReply
from indus.web.auth.service_bearer import (
    DRAIN_ROUTE,
    DRAIN_SECRET_ENV,
    MIN_SECRET_BITS,
    SharedSecretProvider,
    secret_bits,
)
from tests.unit.web.conftest import Builder, Built, basic_config


def _other_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/other")
    async def other() -> dict[str, bool]:
        return {"ok": True}

    return router


def _fake_daemon(built: Built) -> list[tuple[str, dict[str, Any]]]:
    """Answer the real drain route's control call (P18-15) and record every verb sent: a refused
    request must never reach the daemon."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def query(_home: Path, verb: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        calls.append((verb, dict(params or {})))
        return ControlReply(ok=True, result={"draining": True, "active_turns": 0})

    built.state.extras["daemon.query"] = query
    return calls


def test_secret_strength() -> None:
    assert secret_bits("") == 0
    assert secret_bits("ab" * 32) == 256  # 64 hex characters
    assert secret_bits("a" * 63) < MIN_SECRET_BITS  # 252 bits of hex
    assert math.isclose(secret_bits("Z" * 43), 43 * 6)
    assert secret_bits("é" * 100) == 0  # outside every alphabet: refused


def test_a_strong_secret_opens_only_its_route(build: Builder) -> None:
    secret = secrets.token_hex(32)
    built = build(env={DRAIN_SECRET_ENV: secret}, routers=[_other_router()])
    calls = _fake_daemon(built)
    ok = built.client.post(DRAIN_ROUTE, headers={"Authorization": f"Bearer {secret}"})
    assert ok.status_code == 200 and ok.json()["draining"] is True
    assert calls == [("drain", {"on": True})]
    assert built.client.post(DRAIN_ROUTE).status_code == 401
    wrong = built.client.post(DRAIN_ROUTE, headers={"Authorization": "Bearer " + "f" * 64})
    assert wrong.status_code == 401
    # the session token is not a service token, and the service token opens nothing else
    assert built.client.post(DRAIN_ROUTE, headers=built.auth()).status_code == 401
    other = built.client.get("/api/other", headers={"Authorization": f"Bearer {secret}"})
    assert other.status_code == 401
    assert len(calls) == 1  # no refused request reached the daemon


def test_the_route_ignores_cookies(build: Builder) -> None:
    secret = secrets.token_hex(32)
    built = build(
        host="0.0.0.0",
        config=basic_config(),
        env={DRAIN_SECRET_ENV: secret},
        routers=[_other_router()],
    )
    calls = _fake_daemon(built)
    built.client.post(
        "/auth/password-login", json={"username": "admin", "password": "correct horse"}
    )
    assert built.client.get("/api/auth/me").status_code == 200
    assert built.client.post(DRAIN_ROUTE).status_code == 401
    assert calls == []
    ok = built.client.post(DRAIN_ROUTE, headers={"Authorization": f"Bearer {secret}"})
    assert ok.status_code == 200
    assert calls == [("drain", {"on": True})]


def test_a_255_bit_class_secret_disables_the_route(build: Builder) -> None:
    weak = secrets.token_hex(32)[:63]  # 252 bits: just under the bar
    assert secret_bits(weak) < MIN_SECRET_BITS <= secret_bits(weak + "a")
    built = build(env={DRAIN_SECRET_ENV: weak}, routers=[_other_router()])
    refused = built.client.post(DRAIN_ROUTE, headers={"Authorization": f"Bearer {weak}"})
    assert refused.status_code == 404
    assert "drain" in built.state.auth.registry.reasons
    with pytest.raises(ValueError):
        SharedSecretProvider(weak, frozenset({DRAIN_ROUTE}))


def test_no_secret_leaves_the_route_to_the_normal_gate(build: Builder) -> None:
    built = build(routers=[_other_router()])
    assert built.client.post(DRAIN_ROUTE).status_code == 401
