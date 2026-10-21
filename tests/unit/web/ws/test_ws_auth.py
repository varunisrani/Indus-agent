"""P18-06: the one WebSocket auth helper (R09.2): check order, codes, tickets, clamp, coverage."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import APIRouter
from starlette.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from indus.foundation.scope import Scope
from indus.service.ws_auth import (
    CLOSE_BAD_TICKET,
    CLOSE_DISABLED,
    CLOSE_FORBIDDEN,
    CLOSE_PEER,
    DASHBOARD_PURPOSES,
    DASHBOARD_SUBPROTOCOL,
    MAX_CLOSE_REASON,
    WEB_PTY,
    WEB_PUB,
    WEB_RPC,
    DashboardAccepted,
    InternalCredential,
    TicketBook,
    UpgradeRefused,
    authenticate_dashboard_upgrade,
    clamp_close_reason,
    dashboard_offer,
)
from indus.web.ws import WS_PURPOSE_ATTR, WsGrant, guarded_ws, websocket_routes
from tests.unit.web.conftest import Builder, Built, basic_config


class Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


def _scope(
    *offered: str, peer: str = "127.0.0.1", query: str = "", base: bool = True
) -> dict[str, Any]:
    """An upgrade scope offering ``offered`` (``"kind=value"`` each) as credential
    subprotocols, next to ``indus-dashboard-v1`` unless ``base`` is False."""
    protocols = [DASHBOARD_SUBPROTOCOL] if base else []
    for item in offered:
        kind, _, value = item.partition("=")
        protocols.append(dashboard_offer(kind, value)[1])  # pyright: ignore[reportArgumentType]
    return {
        "type": "websocket",
        "query_string": query.encode(),
        "headers": [(b"host", b"127.0.0.1:9119")],
        "client": (peer, 5000),
        "subprotocols": protocols,
    }


def _decide(
    scope: dict[str, Any],
    *,
    book: TicketBook,
    gated: bool = False,
    enabled: bool = True,
    problem: str | None = None,
    loopback_peer: bool = True,
    internal: InternalCredential | None = None,
    allow_internal: bool = False,
    purpose: str = WEB_PTY,
    profile_id: str | None = None,
) -> DashboardAccepted | UpgradeRefused:
    return authenticate_dashboard_upgrade(
        scope,
        purpose,
        enabled=enabled,
        gated=gated,
        book=book,
        session_token=lambda value: value == "the-token",
        host_origin_problem=problem,
        require_loopback_peer=loopback_peer,
        internal=internal,
        allow_internal=allow_internal,
        profile_id=profile_id,
    )


def _book(clock: Callable[[], float] | None = None) -> TicketBook:
    return TicketBook(profile_id="p" * 16, purposes=DASHBOARD_PURPOSES, monotonic=clock or Clock())


def _code(verdict: DashboardAccepted | UpgradeRefused) -> int:
    return verdict.close_code if isinstance(verdict, UpgradeRefused) else 0


def test_checks_run_in_order() -> None:
    book = _book()
    everything_wrong = _scope("token=nope", peer="10.0.0.9")
    assert _code(_decide(everything_wrong, book=book, enabled=False, problem="x")) == CLOSE_DISABLED
    assert _code(_decide(everything_wrong, book=book, problem="x")) == CLOSE_BAD_TICKET
    good_token_bad_host = _scope("token=the-token", peer="10.0.0.9")
    assert _code(_decide(good_token_bad_host, book=book, problem="host")) == CLOSE_FORBIDDEN
    assert _code(_decide(good_token_bad_host, book=book)) == CLOSE_PEER
    accepted = _decide(_scope("token=the-token"), book=book)
    assert isinstance(accepted, DashboardAccepted) and accepted.credential == "token"


def test_tickets_are_single_use_and_expire() -> None:
    clock = Clock()
    book = _book(clock)
    first = book.mint(WEB_PTY).ticket
    assert isinstance(_decide(_scope(f"ticket={first}"), book=book, gated=True), DashboardAccepted)
    assert _code(_decide(_scope(f"ticket={first}"), book=book, gated=True)) == CLOSE_BAD_TICKET
    late = book.mint(WEB_PTY).ticket
    clock.value += 30.001
    refused = _decide(_scope(f"ticket={late}"), book=book, gated=True)
    assert isinstance(refused, UpgradeRefused) and refused.reason == "ticket expired"


def test_wrong_purpose_and_profile_are_refused() -> None:
    book = _book()
    ticket = book.mint(WEB_RPC).ticket
    assert _code(_decide(_scope(f"ticket={ticket}"), book=book, gated=True)) == CLOSE_BAD_TICKET
    # the failed redemption spent it, even for its own purpose
    assert (
        _code(_decide(_scope(f"ticket={ticket}"), book=book, gated=True, purpose=WEB_RPC))
        == CLOSE_BAD_TICKET
    )
    other = book.mint(WEB_PTY, profile_id="q" * 16).ticket
    wrong = _decide(_scope(f"ticket={other}"), book=book, gated=True, profile_id="p" * 16)
    assert isinstance(wrong, UpgradeRefused) and wrong.reason == "ticket wrong_profile"
    bound = book.mint(WEB_PTY, profile_id="q" * 16).ticket
    accepted = _decide(_scope(f"ticket={bound}"), book=book, gated=True)
    assert isinstance(accepted, DashboardAccepted) and accepted.profile_id == "q" * 16


def test_mode_decides_the_credential() -> None:
    book = _book()
    in_gated = _decide(_scope("token=the-token"), book=book, gated=True)
    assert _code(in_gated) == CLOSE_BAD_TICKET
    ticket = book.mint(WEB_PTY).ticket
    in_loopback = _decide(_scope(f"ticket={ticket}"), book=book)
    # token mode takes a minted ticket too (the desktop's per-dial ticket, D-16.691), once
    assert isinstance(in_loopback, DashboardAccepted) and in_loopback.credential == "ticket"
    assert _code(_decide(_scope(f"ticket={ticket}"), book=book)) == CLOSE_BAD_TICKET
    both = _decide(_scope("token=the-token", f"ticket={book.mint(WEB_PTY).ticket}"), book=book)
    assert _code(both) == CLOSE_BAD_TICKET
    assert _code(_decide(_scope(), book=book)) == CLOSE_BAD_TICKET


def test_internal_credential_only_where_allowed() -> None:
    book = _book()
    internal = InternalCredential()
    query = f"internal={internal.value}"
    assert _code(_decide(_scope(query), book=book, internal=internal)) == CLOSE_BAD_TICKET
    accepted = _decide(
        _scope(query),
        book=book,
        internal=internal,
        allow_internal=True,
        purpose=WEB_PUB,
        gated=True,
    )
    assert isinstance(accepted, DashboardAccepted) and accepted.credential == "internal"
    wrong = _decide(
        _scope("internal=guess"), book=book, internal=internal, allow_internal=True, purpose=WEB_PUB
    )
    assert _code(wrong) == CLOSE_BAD_TICKET
    assert internal.value not in repr(internal)


def test_credentials_in_the_url_are_refused_and_spent() -> None:
    book = _book()
    ticket = book.mint(WEB_PTY).ticket
    for query in ("token=the-token", f"ticket={ticket}", "internal=x", "TOKEN=the-token"):
        refused = _decide(_scope(query=query), book=book, gated="ticket" in query)
        assert isinstance(refused, UpgradeRefused) and refused.close_code == CLOSE_BAD_TICKET
        assert refused.reason == "credentials are not accepted in the URL"
    # the ticket that rode in the URL was spent: offered properly afterwards it is refused
    later = _decide(_scope(f"ticket={ticket}"), book=book, gated=True)
    assert isinstance(later, UpgradeRefused) and later.reason == "ticket used"
    # a good subprotocol credential does not rescue a URL credential
    both = _decide(_scope("token=the-token", query="token=the-token"), book=book)
    assert _code(both) == CLOSE_BAD_TICKET


def test_the_protocol_name_must_be_offered_and_kinds_known() -> None:
    book = _book()
    bare = _decide(_scope("token=the-token", base=False), book=book)
    assert isinstance(bare, UpgradeRefused) and DASHBOARD_SUBPROTOCOL in bare.reason
    odd = {**_scope(), "subprotocols": [DASHBOARD_SUBPROTOCOL, "indus-dashboard-cookie.x"]}
    assert _code(_decide(odd, book=book)) == CLOSE_BAD_TICKET
    header_only = {**_scope(), "subprotocols": None}
    header_only["headers"] = [
        (b"host", b"127.0.0.1:9119"),
        (b"sec-websocket-protocol", ", ".join(dashboard_offer("token", "the-token")).encode()),
    ]
    accepted = _decide(header_only, book=book)
    assert isinstance(accepted, DashboardAccepted) and accepted.credential == "token"


def test_reasons_are_clamped_to_123_bytes() -> None:
    long = "origin not accepted: http://" + "é" * 200
    clamped = clamp_close_reason(long)
    assert len(clamped.encode("utf-8")) <= MAX_CLOSE_REASON
    assert clamped.endswith("...")
    assert clamp_close_reason("short") == "short"
    refused = _decide(_scope("token=the-token"), book=_book(), problem=long)
    assert isinstance(refused, UpgradeRefused)
    assert len(refused.reason.encode("utf-8")) <= MAX_CLOSE_REASON


# -- through the app ------------------------------------------------------------------------------


@pytest.fixture
def ws_app(build: Builder) -> Iterator[Callable[..., Built]]:
    seen: list[WsGrant] = []

    async def handler(websocket: WebSocket, grant: WsGrant) -> None:
        seen.append(grant)
        await websocket.send_json({"purpose": grant.purpose, "credential": grant.credential})
        await websocket.close()

    def make(**kwargs: Any) -> Built:
        router = APIRouter()
        seen.clear()
        router.add_api_websocket_route("/api/test-pty", guarded_ws(WEB_PTY)(handler))
        router.add_api_websocket_route(
            "/api/test-pub", guarded_ws(WEB_PUB, allow_internal=True)(handler)
        )
        built = build(routers=[router], **kwargs)
        built.app.state.test_grants = seen
        return built

    yield make


WS = "ws://127.0.0.1:9119"


def _offer(kind: str, value: str) -> list[str]:
    return dashboard_offer(kind, value)  # pyright: ignore[reportArgumentType]


def _close_code(
    client: TestClient, url: str, protocols: list[str] | None = None, **headers: str
) -> int:
    with client.websocket_connect(WS + url, subprotocols=protocols, headers=headers) as socket:
        try:
            socket.receive_json()
        except WebSocketDisconnect as closed:
            return closed.code
        return 0


def test_loopback_token_through_the_app(ws_app: Callable[..., Built]) -> None:
    built = ws_app()
    with built.client.websocket_connect(
        f"{WS}/api/test-pty", subprotocols=_offer("token", built.token)
    ) as socket:
        assert socket.accepted_subprotocol == DASHBOARD_SUBPROTOCOL  # the name only, no secret
        assert socket.receive_json() == {"purpose": WEB_PTY, "credential": "token"}
    assert _close_code(built.client, "/api/test-pty") == CLOSE_BAD_TICKET
    assert _close_code(built.client, "/api/test-pty", _offer("token", "wrong")) == CLOSE_BAD_TICKET
    # the token in the URL is refused even when it is right
    assert _close_code(built.client, f"/api/test-pty?token={built.token}") == CLOSE_BAD_TICKET


def test_a_token_mode_ticket_opens_one_socket_through_the_app(
    ws_app: Callable[..., Built],
) -> None:
    """D-16.691: the desktop mints a ticket over HTTP with the token, then dials with the ticket."""
    built = ws_app()
    pty = "/api/test-pty"
    answer = built.client.post(
        "/api/auth/ws-ticket", json={"purpose": WEB_PTY}, headers=built.auth()
    )
    ticket = answer.json()["ticket"]
    with built.client.websocket_connect(WS + pty, subprotocols=_offer("ticket", ticket)) as socket:
        assert socket.accepted_subprotocol == DASHBOARD_SUBPROTOCOL
        assert socket.receive_json() == {"purpose": WEB_PTY, "credential": "ticket"}
    assert _close_code(built.client, pty, _offer("ticket", ticket)) == CLOSE_BAD_TICKET  # spent


def test_a_refusal_still_echoes_the_protocol(ws_app: Callable[..., Built]) -> None:
    built = ws_app()
    with built.client.websocket_connect(
        f"{WS}/api/test-pty", subprotocols=_offer("token", "wrong")
    ) as socket:
        assert socket.accepted_subprotocol == DASHBOARD_SUBPROTOCOL  # a browser sees the code
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert closed.value.code == CLOSE_BAD_TICKET


def test_foreign_origin_is_4403_with_a_short_reason(ws_app: Callable[..., Built]) -> None:
    built = ws_app()
    origin = "http://evil" + ".example" * 30
    with (
        built.client.websocket_connect(
            f"{WS}/api/test-pty",
            subprotocols=_offer("token", built.token),
            headers={"origin": origin},
        ) as socket,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        socket.receive_json()
    assert closed.value.code == CLOSE_FORBIDDEN
    reason = closed.value.reason or ""
    assert reason.startswith("origin not accepted") and reason.endswith("...")
    assert len(reason.encode("utf-8")) <= MAX_CLOSE_REASON
    # the same token from our own origin is fine
    with built.client.websocket_connect(
        f"{WS}/api/test-pty",
        subprotocols=_offer("token", built.token),
        headers={"origin": "http://127.0.0.1:9119"},
    ) as socket:
        assert socket.receive_json()["credential"] == "token"


def test_non_loopback_peer_is_4408_on_a_loopback_bind(ws_app: Callable[..., Built]) -> None:
    built = ws_app(peer="192.168.1.30")
    code = _close_code(built.client, "/api/test-pty", _offer("token", built.token))
    assert code == CLOSE_PEER


def test_disabled_feature_is_4404(ws_app: Callable[..., Built]) -> None:
    built = ws_app(embedded_chat=False)
    code = _close_code(built.client, "/api/test-pty", _offer("token", built.token))
    assert code == CLOSE_DISABLED


def test_gated_tickets_through_the_app(ws_app: Callable[..., Built]) -> None:
    clock = Clock()
    built = ws_app(host="0.0.0.0", config=basic_config(), monotonic=clock)
    pty = "/api/test-pty"
    assert _close_code(built.client, pty, _offer("token", built.token)) == CLOSE_BAD_TICKET
    built.client.post(
        "/auth/password-login", json={"username": "admin", "password": "correct horse"}
    )

    def mint(purpose: str) -> str:
        return built.client.post("/api/auth/ws-ticket", json={"purpose": purpose}).json()["ticket"]

    ticket = mint(WEB_PTY)
    with built.client.websocket_connect(WS + pty, subprotocols=_offer("ticket", ticket)) as socket:
        assert socket.receive_json()["credential"] == "ticket"
    assert _close_code(built.client, pty, _offer("ticket", ticket)) == CLOSE_BAD_TICKET
    late = mint(WEB_PTY)
    clock.value += 30.001
    assert _close_code(built.client, pty, _offer("ticket", late)) == CLOSE_BAD_TICKET
    rpc = mint(WEB_RPC)
    assert _close_code(built.client, pty, _offer("ticket", rpc)) == CLOSE_BAD_TICKET
    in_url = mint(WEB_PTY)
    assert _close_code(built.client, f"{pty}?ticket={in_url}") == CLOSE_BAD_TICKET
    assert _close_code(built.client, pty, _offer("ticket", in_url)) == CLOSE_BAD_TICKET  # spent
    internal = built.state.internal.value
    with built.client.websocket_connect(
        f"{WS}/api/test-pub", subprotocols=_offer("internal", internal)
    ) as socket:
        assert socket.receive_json()["credential"] == "internal"
    assert _close_code(built.client, pty, _offer("internal", internal)) == CLOSE_BAD_TICKET


def test_an_unknown_profile_reaches_the_route_without_a_profile(
    ws_app: Callable[..., Built], scope: Scope
) -> None:
    built = ws_app()
    with built.client.websocket_connect(
        f"{WS}/api/test-pty?profile=ghost", subprotocols=_offer("token", built.token)
    ) as socket:
        assert socket.receive_json()["credential"] == "token"
    with built.client.websocket_connect(
        f"{WS}/api/test-pty", subprotocols=_offer("token", built.token)
    ) as socket:
        socket.receive_json()
    unknown, own = built.app.state.test_grants[-2:]
    assert unknown.profile is None  # the route answers "no such profile" in its own terms
    assert own.profile is not None and own.profile.home == scope.home


def test_refusals_never_log_or_audit_the_credential(
    ws_app: Callable[..., Built], caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock()
    built = ws_app(host="0.0.0.0", config=basic_config(), monotonic=clock)
    built.client.post(
        "/auth/password-login", json={"username": "admin", "password": "correct horse"}
    )
    ticket = built.client.post("/api/auth/ws-ticket", json={"purpose": WEB_RPC}).json()["ticket"]
    caplog.set_level("DEBUG")
    secrets_offered = [built.token, ticket, built.state.internal.value]
    _close_code(built.client, "/api/test-pty", _offer("token", built.token))
    _close_code(built.client, "/api/test-pty", _offer("ticket", ticket))
    _close_code(built.client, f"/api/test-pty?internal={built.state.internal.value}")
    audit_text = built.state.auth.audit.path.read_text(encoding="utf-8")
    assert audit_text.count("\n") >= 3  # each refusal was audited
    for value in secrets_offered:
        assert value not in caplog.text
        assert value not in audit_text


def test_every_websocket_route_goes_through_the_helper(ws_app: Callable[..., Built]) -> None:
    built = ws_app()
    routes = websocket_routes(built.app)
    paths = {path for path, _ in routes}
    assert paths >= {"/api/test-pty", "/api/test-pub", "/api/pty", "/api/pub", "/api/events"}
    assert "/api/ws" in paths
    for path, endpoint in routes:
        purpose = getattr(endpoint, WS_PURPOSE_ATTR, None)
        assert purpose in DASHBOARD_PURPOSES, path
        if not built.state.feature_enabled(purpose):
            # a socket whose feature is off (the admin console by default, P24-36) answers 4404
            assert _close_code(built.client, path) == CLOSE_DISABLED, path
            continue
        assert _close_code(built.client, path) == CLOSE_BAD_TICKET, path
        in_url = f"{path}?token={built.token}"
        assert _close_code(built.client, in_url, [DASHBOARD_SUBPROTOCOL]) == CLOSE_BAD_TICKET, path
