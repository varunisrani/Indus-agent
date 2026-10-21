"""P24-47: the register route and the controller WebSocket on an API-style app (bearer gate in
front), driven with Starlette's test client from a loopback address."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from indus.gateway.browser_broker import broker as bb
from indus.gateway.browser_broker import routes
from indus.gateway.browser_broker.artifacts import ArtifactStore
from indus.gateway.browser_broker.broker import BrowserBroker, Identity
from indus.gateway.http.serving import BearerGate
from indus.service.ws_auth import TicketBook
from tests.fakes.clock import FakeClock

PROFILE = "q" * 16
KEY_ID = "key-7"
BODY = {"controller_id": "ext-1", "browser_profile": "default", "session": "sess-1", "protocol": 1}


def api_key() -> str:
    return "test-" + "k" * 20  # built at runtime; not a real key shape


@dataclass
class Rig:
    client: TestClient
    broker: BrowserBroker
    clock: FakeClock

    @property
    def me(self) -> Identity:
        return Identity("api", KEY_ID, "default", "ext-1", "sess-1", PROFILE)

    def register(self, body: dict[str, Any] | None = None, *, key: str | None = None) -> Any:
        headers = {"Authorization": f"Bearer {key or api_key()}"}
        return self.client.post(routes.REGISTER_PATH, json=body or BODY, headers=headers)

    def ticket(self) -> str:
        answer = self.register()
        assert answer.status_code == 200, answer.text
        return answer.json()["ticket"]

    def connect(self, ticket: str | None = None, *, base: bool = True, query: str = "") -> Any:
        offered = [bb.SUBPROTOCOL] if base else []
        if ticket is not None:
            offered.append(bb.TICKET_PREFIX + ticket)
        return self.client.websocket_connect(routes.WS_PATH + query, subprotocols=offered)


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
    book = TicketBook(profile_id=PROFILE, monotonic=clock.monotonic)
    store = ArtifactStore(tmp_path / "art", monotonic=clock.monotonic)
    broker = BrowserBroker(book, clock=clock, artifacts=store)
    app = FastAPI()
    routes.mount_routes(app, broker, lambda: ())
    gated = BearerGate(app, lambda token: KEY_ID if token == api_key() else None)
    with TestClient(gated, client=("127.0.0.1", 50000)) as client:
        yield Rig(client, broker, clock)


def hello(ws: Any, capabilities: list[str] | None = None) -> dict[str, Any]:
    caps = capabilities if capabilities is not None else ["navigate", "screenshot", "evaluate"]
    ws.send_json({"method": bb.FRAME_HELLO, "params": {"protocol": 1, "capabilities": caps}})
    return ws.receive_json()


def test_register_needs_the_key_and_a_good_body(rig: Rig) -> None:
    assert rig.register(key="wrong").status_code == 401
    answer = rig.register()
    assert answer.status_code == 200 and answer.headers["cache-control"] == "no-store"
    data = answer.json()
    assert data["subprotocol"] == bb.SUBPROTOCOL and data["ws_path"] == routes.WS_PATH
    assert data["expires_in"] == 30.0 and len(data["ticket"]) == 43
    for bad in (
        {**BODY, "protocol": True},
        {**BODY, "protocol": 2},
        {**BODY, "session": "../../x"},
        {k: v for k, v in BODY.items() if k != "controller_id"},
    ):
        assert rig.register(bad).status_code == 400


def test_register_refuses_a_peer_that_is_not_loopback(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
    broker = BrowserBroker(TicketBook(profile_id=PROFILE), clock=clock)
    app = FastAPI()
    routes.mount_routes(app, broker, lambda: ())
    gated = BearerGate(app, lambda token: KEY_ID)
    with TestClient(gated, client=("192.0.2.4", 50000)) as client:
        answer = client.post(routes.REGISTER_PATH, json=BODY, headers={"Authorization": "Bearer x"})
    assert answer.status_code == 403


def test_hello_ready_and_one_command(rig: Rig) -> None:
    with rig.connect(rig.ticket()) as ws:
        assert ws.accepted_subprotocol == bb.SUBPROTOCOL
        ready = hello(ws)
        assert ready["method"] == "browser.controller.ready"
        assert ready["params"] == {"protocol": 1, "capabilities": ["navigate", "screenshot"]}
        assert rig.client.portal is not None
        future = rig.client.portal.start_task_soon(
            rig.broker.call, rig.me, "navigate", {"url": "https://example.org"}
        )
        command = ws.receive_json()
        assert command["method"] == bb.FRAME_COMMAND
        params = command["params"]
        assert params["capability"] == "navigate"
        reply = {"command_id": params["command_id"], "ok": True, "result": {"title": "Example"}}
        ws.send_json({"method": "browser.controller.result", "params": reply})
        ws.send_json({"method": "browser.controller.result", "params": {**reply, "result": "late"}})
        assert future.result(timeout=10) == {"title": "Example"}


def test_ticket_is_single_use_over_the_socket(rig: Rig) -> None:
    ticket = rig.ticket()
    with rig.connect(ticket) as ws:
        hello(ws)
    with rig.connect(ticket) as ws:
        closed = ws.receive()
        assert closed["type"] == "websocket.close" and closed["code"] == 4401


def test_expired_ticket_and_query_ticket_close_4401(rig: Rig) -> None:
    old = rig.ticket()
    rig.clock.advance(31.0)
    with rig.connect(old) as ws:
        assert ws.receive()["code"] == 4401
    fresh = rig.ticket()
    with rig.connect(None, query=f"?ticket={fresh}") as ws:
        assert ws.receive()["code"] == 4401
    with rig.connect(fresh) as ws:  # it was spent by the query attempt
        assert ws.receive()["code"] == 4401


@pytest.mark.parametrize(
    ("frame", "code"),
    [
        ({"method": bb.FRAME_HELLO, "params": {"protocol": 2, "capabilities": []}}, 4400),
        ({"method": bb.FRAME_HELLO, "params": {"protocol": True, "capabilities": []}}, 4400),
        ({"method": "browser.controller.result", "params": {}}, 4400),
    ],
)
def test_bad_hello_closes_4400(rig: Rig, frame: dict[str, Any], code: int) -> None:
    with rig.connect(rig.ticket()) as ws:
        ws.send_json(frame)
        assert ws.receive()["code"] == code


def test_unknown_frames_and_stray_bytes_close_1003(rig: Rig) -> None:
    with rig.connect(rig.ticket()) as ws:
        hello(ws)
        ws.send_text("not json")
        assert ws.receive()["code"] == 1003
    with rig.connect(rig.ticket()) as ws:
        hello(ws)
        ws.send_bytes(b"\x00\x01")
        assert ws.receive()["code"] == 1003


def test_oversized_text_frame_closes_1009(rig: Rig) -> None:
    with rig.connect(rig.ticket()) as ws:
        hello(ws)
        ws.send_text("x" * (routes.MAX_FRAME_BYTES + 1))
        assert ws.receive()["code"] == 1009


def test_drop_marks_offline_and_detach_frame_detaches(rig: Rig) -> None:
    with rig.connect(rig.ticket()) as ws:
        hello(ws)
        assert rig.broker.is_connected(rig.me)
    assert not rig.broker.is_connected(rig.me)
    assert rig.broker.lane(session="sess-1", principal=KEY_ID, transport_family="api") == rig.me
    with rig.connect(rig.ticket()) as ws:
        hello(ws)
        ws.send_json({"method": "browser.controller.detach", "params": {}})
        assert ws.receive()["code"] == 1000
    assert rig.broker.lane(session="sess-1", principal=KEY_ID, transport_family="api") is None


def test_artifact_put_then_fetch_once(rig: Rig) -> None:
    data = b"\x89PNG\r\n\x1a\n" + b"p" * 2048
    with rig.connect(rig.ticket()) as ws:
        hello(ws)
        ws.send_json({"method": "browser.controller.artifact.put", "params": {"mime": "image/png"}})
        ws.send_bytes(data)
        stored = ws.receive_json()
        assert stored["method"] == "browser.controller.artifact.stored"
        receipt = stored["params"]
        assert receipt["sha256"] == hashlib.sha256(data).hexdigest() and receipt["size"] == len(
            data
        )
        ws.send_json({"method": "browser.controller.artifact.put", "params": {"mime": "text/html"}})
        ws.send_bytes(b"<script>")
        assert ws.receive_json()["params"] == {"reason": "mime"}
        fetch = {"method": "browser.controller.artifact.fetch",
                 "params": {"artifact_id": receipt["artifact_id"]}}  # fmt: skip
        ws.send_json(fetch)
        header = ws.receive_json()
        assert header["method"] == "browser.controller.artifact.data"
        assert ws.receive_bytes() == data
        ws.send_json(fetch)
        assert ws.receive_json()["params"] == {"reason": "unknown"}


def test_ws_settings_leave_room_for_one_artefact() -> None:
    settings = routes.ws_settings()
    assert (
        settings["ws_max_size"] > 10 * 1024 * 1024 and settings["ws_per_message_deflate"] is False
    )


class _Keys:
    def verify(self, token: str) -> str | None:
        return KEY_ID if token == api_key() else None


def test_the_api_app_mounts_the_routes_only_with_a_broker(tmp_path: Path, scope: Any) -> None:
    from indus.gateway.http.api_server import ApiContext, ApiSettings, build_api_app

    clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
    broker = BrowserBroker(TicketBook(profile_id=PROFILE, monotonic=clock.monotonic), clock=clock)

    def app_for(with_broker: bool) -> Any:
        ctx = ApiContext(
            scope=scope,
            desk=None,  # type: ignore[arg-type]
            keys=_Keys(),  # type: ignore[arg-type]
            settings=ApiSettings,
            clock=clock,
            browser_broker=broker if with_broker else None,
        )
        return build_api_app(ctx)

    headers = {"Authorization": f"Bearer {api_key()}"}
    with TestClient(app_for(False), client=("127.0.0.1", 1)) as client:
        assert client.post(routes.REGISTER_PATH, json=BODY, headers=headers).status_code == 404
    with TestClient(app_for(True), client=("127.0.0.1", 1)) as client:
        assert client.post(routes.REGISTER_PATH, json=BODY).status_code == 401
        answer = client.post(routes.REGISTER_PATH, json=BODY, headers=headers)
        assert answer.status_code == 200
        with client.websocket_connect(
            routes.WS_PATH,
            subprotocols=[bb.SUBPROTOCOL, bb.TICKET_PREFIX + answer.json()["ticket"]],
        ) as ws:
            assert hello(ws, ["tabs"])["params"]["capabilities"] == ["tabs"]
