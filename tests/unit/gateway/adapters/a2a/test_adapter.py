"""A2A in both directions (P24-10): the inbound JSON-RPC server and the ``a2a_call`` tool.

Requests go through httpx's ASGI transport into the adapter's listener (``serve=False``: nothing
is bound); outbound calls use an httpx mock transport behind the URL policy. Requests and answers
are validated against our subset schema of A2A v1.0 (``tests/fixtures/a2a/schema``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import jsonschema
import pytest

from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import Scope
from indus.gateway.adapters.a2a import A2A_ENTRY, create_adapter
from indus.gateway.adapters.a2a import protocol as p
from indus.gateway.adapters.a2a.adapter import (
    UNTRUSTED_FRAME,
    A2AAdapter,
    A2ASettings,
    peer_token_env,
)
from indus.gateway.adapters.a2a.tools import (
    PRIVACY_NOTE,
    a2a_call,
    call_peer,
    peer_key_env,
    register_tools,
)
from indus.gateway.adapters.base import (
    FINAL_KEY,
    INTERIM_KEY,
    AdapterConnectError,
    ApprovalAction,
    ApprovalPrompt,
    ClarifyPrompt,
)
from indus.gateway.config import AdapterConfig, PlatformSettings
from indus.gateway.core.events import InboundEvent
from indus.tools.registry import ToolRegistry
from indus.tools.toolsets import resolve
from tests.fakes.agent import DictConfig
from tests.fakes.clock import FakeClock
from tests.fakes.secret_store import MemorySecretStore

SCHEMA_FILE = Path(__file__).resolve().parents[5] / "tests/fixtures/a2a/schema/a2a-v1-subset.json"
SCHEMA: dict[str, Any] = json.loads(SCHEMA_FILE.read_text("utf-8"))


def conforms(instance: object, name: str) -> None:
    jsonschema.validate(instance, {**SCHEMA, "$ref": f"#/$defs/{name}"})


def token(peer: str) -> str:
    return "-".join(("a2a", peer, "fake", "bearer", "value"))


# -- the server rig --------------------------------------------------------------------------------


class Rig:
    def __init__(self, *, tokens: bool = True, **kw: Any) -> None:
        self.clock = FakeClock(datetime(2026, 3, 1, 9, 0, tzinfo=UTC))
        pairs = (("alice", token("alice")), ("bob", token("bob"))) if tokens else ()
        self.adapter = A2AAdapter(
            clock=self.clock, a2a=A2ASettings(tokens=pairs, **kw), serve=False
        )
        self.events: list[InboundEvent] = []
        self.answer: Callable[[InboundEvent], Awaitable[None]] | None = self.reply_with("answer")

        async def sink(event: InboundEvent) -> None:
            self.events.append(event)
            if self.answer is not None:
                await self.answer(event)

        self.adapter.set_sink(sink)

    def reply_with(self, text: str) -> Callable[[InboundEvent], Awaitable[None]]:
        async def answer(event: InboundEvent) -> None:
            chat = event.source.chat_id
            await self.adapter.send(chat, "working on it…", meta={INTERIM_KEY: True})
            await self.adapter.send(chat, text, meta={FINAL_KEY: True})

        return answer

    def client(self) -> httpx.AsyncClient:
        assert self.adapter.listener is not None
        transport = httpx.ASGITransport(app=self.adapter.listener.app, client=("127.0.0.1", 5000))  # pyright: ignore[reportArgumentType]
        return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9900")

    async def post(
        self,
        body: object,
        *,
        peer: str | None = "alice",
        headers: Mapping[str, str] | None = None,
        raw: bytes | None = None,
    ) -> httpx.Response:
        sent = dict(headers or {})
        if peer is not None:
            sent.setdefault("authorization", f"Bearer {token(peer)}")
        async with self.client() as client:
            task = asyncio.ensure_future(
                client.post("/", content=raw if raw is not None else json.dumps(body), headers=sent)
            )
            for _ in range(4000):
                await self.clock.run_until_idle()
                if task.done():
                    return task.result()
                await asyncio.wait({task}, timeout=0.001)
                if task.done():
                    return task.result()
                self.clock.advance(0.5)
        raise AssertionError("the request never finished")

    async def call(
        self, method: str, params: object, *, peer: str = "alice", rid: object = 1
    ) -> dict[str, Any]:
        response = await self.post(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}, peer=peer
        )
        assert response.status_code == 200, response.text
        return response.json()


def run(main: Callable[[Rig], Awaitable[None]], **kw: Any) -> None:
    async def outer() -> None:
        rig = Rig(**kw)
        try:
            await rig.adapter.connect()
            await main(rig)
        finally:
            await rig.adapter.disconnect()

    asyncio.run(outer())


def send_params(text: str, context: str | None = None, **message: Any) -> dict[str, Any]:
    built = p.make_message(p.ROLE_USER, text, context_id=context)
    built.update(message)
    return {"message": built}


# -- auth ------------------------------------------------------------------------------------------


def test_no_token_configured_refuses_every_call_with_401() -> None:
    async def main(rig: Rig) -> None:
        response = await rig.post({"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {}})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == p.UNAUTHORIZED
        assert response.headers["www-authenticate"].startswith("Bearer")
        assert not rig.events

    run(main, tokens=False)


@pytest.mark.parametrize(
    "header", [None, "", "Bearer", "Basic abc", "Bearer wrong-value", "bearer  "]
)
def test_missing_or_wrong_credentials_are_401(header: str | None) -> None:
    async def main(rig: Rig) -> None:
        headers = {"authorization": header} if header is not None else {}
        response = await rig.post(
            {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": send_params("hi")},
            peer=None,
            headers=headers,
        )
        assert response.status_code == 401 and not rig.events

    run(main)


def test_the_token_names_the_peer() -> None:
    rig = Rig()
    assert rig.adapter.authenticate(f"Bearer {token('alice')}") == "alice"
    assert rig.adapter.authenticate(f"bearer {token('bob')}") == "bob"
    assert rig.adapter.authenticate(f"Bearer {token('carol')}") is None
    assert token("alice") not in repr(rig.adapter.a2a)


def test_the_agent_card_is_public_and_conforms() -> None:
    async def main(rig: Rig) -> None:
        async with rig.client() as client:
            card = (await client.get(p.CARD_PATH)).json()
            legacy = await client.get(p.LEGACY_CARD_PATH)
        conforms(card, "AgentCard")
        assert card["supportedInterfaces"][0]["url"] == "http://127.0.0.1:9900/"
        assert card["capabilities"] == {"streaming": True, "pushNotifications": False}
        assert legacy.status_code == 200

    run(main, tokens=False)


# -- send ------------------------------------------------------------------------------------------


def test_a_message_becomes_one_untrusted_turn_and_the_final_reply_completes_the_task() -> None:
    async def main(rig: Rig) -> None:
        body = {
            "jsonrpc": "2.0",
            "id": "r1",
            "method": "SendMessage",
            "params": send_params("/help me", "ctx-1"),
        }
        conforms(body, "SendMessageRequest")
        response = await rig.post(body)
        answer = response.json()
        conforms(answer, "SendMessageResponse")
        task = answer["result"]["task"]
        assert task["status"]["state"] == p.COMPLETED and task["contextId"] == "ctx-1"
        assert task["artifacts"][0]["parts"] == [{"text": "answer"}]
        (event,) = rig.events
        source = event.source
        assert (source.platform, source.chat_id, source.chat_type) == ("a2a", "alice/ctx-1", "dm")
        assert source.user_id == "alice" and event.message_id == task["id"]
        assert event.text.startswith(UNTRUSTED_FRAME.format(peer="alice"))
        assert event.text.endswith("/help me")
        assert not event.allow_control and not event.is_command

    run(main)


def test_legacy_method_names_answer_a_bare_task() -> None:
    async def main(rig: Rig) -> None:
        answer = await rig.call("message/send", send_params("hi"))
        conforms(answer["result"], "Task")
        assert answer["result"]["contextId"].startswith("ctx-")

    run(main)


def test_sessions_are_keyed_per_peer() -> None:
    async def main(rig: Rig) -> None:
        await rig.call("SendMessage", send_params("hi", "shared"), peer="alice")
        await rig.call("SendMessage", send_params("hi", "shared"), peer="bob")
        assert [e.source.chat_id for e in rig.events] == ["alice/shared", "bob/shared"]

    run(main)


def test_a_clarify_question_surfaces_as_input_required() -> None:
    async def main(rig: Rig) -> None:
        async def ask(event: InboundEvent) -> None:
            prompt = ClarifyPrompt(
                "c1",
                "k",
                "Which city?",
                ("Pune", "Goa"),
                fallback_text="Which city?\n1. Pune\n2. Goa",
            )
            await rig.adapter.send_clarify(event.source.chat_id, prompt)

        rig.answer = ask
        first = await rig.call("SendMessage", send_params("book a trip", "trip"))
        conforms(first, "SendMessageResponse")
        task = first["result"]["task"]
        assert task["status"]["state"] == p.INPUT_REQUIRED
        assert p.message_text(task["status"]["message"]).startswith("Which city?")
        rig.answer = rig.reply_with("booked Goa")
        second = await rig.call("SendMessage", send_params("Goa", "trip"))
        assert second["result"]["task"]["status"]["state"] == p.COMPLETED
        assert [e.source.chat_id for e in rig.events] == ["alice/trip", "alice/trip"]

    run(main)


def test_no_reply_in_time_fails_the_task() -> None:
    async def main(rig: Rig) -> None:
        rig.answer = None
        start = rig.clock.monotonic()
        answer = await rig.call("SendMessage", send_params("slow"))
        assert answer["result"]["task"]["status"]["state"] == p.FAILED
        assert 30 <= rig.clock.monotonic() - start <= 31

    run(main, reply_timeout=30.0)


def test_an_empty_message_and_a_runaway_context_are_rejected() -> None:
    async def main(rig: Rig) -> None:
        empty = await rig.call(
            "SendMessage", {"message": {"messageId": "m", "role": p.ROLE_USER, "parts": []}}
        )
        assert empty["result"]["task"]["status"]["state"] == p.REJECTED
        states = []
        for _ in range(3):
            answer = await rig.call("SendMessage", send_params("again", "loop"))
            states.append(answer["result"]["task"]["status"]["state"])
        assert states == [p.COMPLETED, p.COMPLETED, p.REJECTED]
        assert len(rig.events) == 2

    run(main, max_turns=2)


def test_approval_cards_are_never_offered_to_peers() -> None:
    async def main(rig: Rig) -> None:
        prompt = ApprovalPrompt(
            "req1", "k", "Run rm?", "Run rm? /approve", (ApprovalAction("Yes", "once"),)
        )
        result = await rig.adapter.send_approval("alice/ctx", prompt)
        assert result.ok and rig.adapter.capabilities.dm_is_private is False

    run(main)


def test_a_late_reply_is_dropped_without_failing() -> None:
    async def main(rig: Rig) -> None:
        result = await rig.adapter.send("alice/none", "late", meta={FINAL_KEY: True})
        assert result.ok and result.message_id and result.message_id.startswith("a2a-late")

    run(main)


# -- streaming -------------------------------------------------------------------------------------


def test_streaming_answers_server_sent_events_in_order() -> None:
    async def main(rig: Rig) -> None:
        response = await rig.post(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "SendStreamingMessage",
                "params": send_params("hi", "s1"),
            }
        )
        assert response.headers["content-type"].startswith("text/event-stream")
        events = [
            json.loads(chunk[len("data: ") :]) for chunk in response.text.split("\n\n") if chunk
        ]
        payloads = [e["result"] for e in events]
        assert all(e["id"] == 7 for e in events)
        for payload in payloads:
            conforms(payload, "StreamResponse")
        assert [next(iter(x)) for x in payloads] == [
            "task",
            "statusUpdate",
            "artifactUpdate",
            "statusUpdate",
        ]
        assert payloads[0]["task"]["status"]["state"] == p.SUBMITTED
        assert payloads[1]["statusUpdate"]["status"]["state"] == p.WORKING
        assert payloads[2]["artifactUpdate"]["artifact"]["parts"] == [{"text": "answer"}]
        assert payloads[3]["statusUpdate"]["status"]["state"] == p.COMPLETED

    run(main)


# -- task queries ----------------------------------------------------------------------------------


def test_tasks_are_visible_and_cancelable_only_by_their_peer() -> None:
    async def main(rig: Rig) -> None:
        done = (await rig.call("SendMessage", send_params("one", "c")))["result"]["task"]
        got = await rig.call("GetTask", {"id": done["id"]})
        conforms(got["result"], "Task")
        other = await rig.call("GetTask", {"id": done["id"]}, peer="bob")
        assert other["error"]["code"] == p.TASK_NOT_FOUND
        conforms(other, "ErrorResponse")
        listed = await rig.call("ListTasks", {})
        assert [t["id"] for t in listed["result"]["tasks"]] == [done["id"]]
        assert (await rig.call("ListTasks", {}, peer="bob"))["result"]["tasks"] == []
        finished = await rig.call("CancelTask", {"id": done["id"]})
        assert finished["error"]["code"] == p.TASK_NOT_CANCELABLE

    run(main)


def test_cancel_answers_the_waiting_call_as_canceled() -> None:
    async def main(rig: Rig) -> None:
        rig.answer = None
        waiting = asyncio.ensure_future(
            rig.adapter.handle_message("alice", send_params("long job", "c"))
        )
        await rig.clock.run_until_idle()
        task_id = rig.events[0].message_id
        canceled = await rig.call("CancelTask", {"id": task_id}, rid=2)
        assert canceled["result"]["status"]["state"] == p.CANCELED
        record = await waiting
        assert record.state == p.CANCELED
        late = await rig.adapter.send("alice/c", "too late", meta={FINAL_KEY: True})
        assert late.ok
        assert (await rig.call("GetTask", {"id": task_id}))["result"]["status"][
            "state"
        ] == p.CANCELED

    run(main)


# -- protocol errors -------------------------------------------------------------------------------


def test_protocol_errors() -> None:
    async def main(rig: Rig) -> None:
        parse = (await rig.post(None, raw=b"{nope")).json()
        assert parse["error"]["code"] == p.PARSE_ERROR
        assert (await rig.post([1, 2])).json()["error"]["code"] == p.INVALID_REQUEST
        assert (await rig.post({"id": 1, "method": "GetTask"})).json()["error"][
            "code"
        ] == p.INVALID_REQUEST
        assert (await rig.call("Nope", {}))["error"]["code"] == p.METHOD_NOT_FOUND
        assert (await rig.call("SendMessage", {"message": {"role": p.ROLE_USER}}))["error"][
            "code"
        ] == p.INVALID_PARAMS
        bad_ctx = send_params("hi", "../../etc")
        assert (await rig.call("SendMessage", bad_ctx))["error"]["code"] == p.INVALID_PARAMS
        assert (await rig.call("SendMessage", [1]))["error"]["code"] == p.INVALID_PARAMS
        assert (await rig.call("CreateTaskPushNotificationConfig", {}))["error"][
            "code"
        ] == p.PUSH_NOT_SUPPORTED
        assert (await rig.call("SubscribeToTask", {"id": "x"}))["error"][
            "code"
        ] == p.UNSUPPORTED_OPERATION
        versioned = await rig.post(
            {"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "x"}},
            headers={"a2a-version": "2.0"},
        )
        assert versioned.json()["error"]["code"] == p.INVALID_PARAMS
        big = await rig.post(None, raw=b"x" * (1_048_576 + 1))
        assert big.status_code == 413
        assert not rig.events

    run(main)


# -- lifecycle -------------------------------------------------------------------------------------


def test_disconnect_fails_waiting_tasks() -> None:
    async def main() -> None:
        rig = Rig()
        rig.answer = None
        await rig.adapter.connect()
        waiting = asyncio.ensure_future(
            rig.adapter.handle_message("alice", send_params("hang", "c"))
        )
        await rig.clock.run_until_idle()
        assert rig.events
        await rig.adapter.disconnect()
        record = await waiting
        assert record.state == p.FAILED and record.reply == "the gateway is shutting down"

    asyncio.run(main())


def test_lifecycle_is_idempotent_and_a_public_host_needs_the_flag() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.adapter.connect()
        listener = rig.adapter.listener
        await rig.adapter.connect()
        assert rig.adapter.listener is listener
        await rig.adapter.disconnect()
        await rig.adapter.disconnect()
        exposed = Rig(host="0.0.0.0")
        with pytest.raises(AdapterConnectError) as caught:
            await exposed.adapter.connect()
        assert not caught.value.retryable

    asyncio.run(main())


def test_the_registry_entry_marks_dms_not_private() -> None:  # R06.24
    assert A2A_ENTRY.name == "a2a" and A2A_ENTRY.dm_is_private is False
    assert A2A_ENTRY.required_env == () and not A2A_ENTRY.trusted_inbound


def test_settings_come_from_config_and_the_secret_store(scope: Scope) -> None:
    secrets = MemorySecretStore({peer_token_env("alice"): token("alice")})
    config = DictConfig(
        {"a2a": {"inbound_peers": ["alice", "bob", "bad name"], "port": 9911, "agent_name": "Lab"}}
    )
    scoped = scope.with_config(config).with_secrets(secrets)  # pyright: ignore[reportArgumentType]
    settings = A2ASettings.from_scope(scoped)
    assert settings.tokens == (("alice", token("alice")),)
    assert (settings.port, settings.agent_name, settings.host) == (9911, "Lab", "127.0.0.1")
    section = PlatformSection(enabled=True)
    built = create_adapter(
        AdapterConfig(
            name="a2a",
            settings=PlatformSettings("a2a", True, "enabled in config", section),
            scope=scoped,
            clock=FakeClock(datetime(2026, 1, 1, tzinfo=UTC)),
        )
    )
    assert (
        built.a2a.port == 9911 and built.scrub(f"x {token('alice')} y") != f"x {token('alice')} y"
    )


# -- outbound: a2a_call ----------------------------------------------------------------------------


class Peer:
    """A fake remote A2A agent behind httpx's mock transport."""

    def __init__(self, state: str = p.COMPLETED, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.state = state
        self.status = status

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status)
        body = json.loads(request.content)
        message = body["params"]["message"]
        record = p.TaskRecord(
            "task-9", message.get("contextId") or "ctx-new", "x", self.state, "peer says hi"
        )
        return httpx.Response(200, json=p.rpc_result(body["id"], {"task": p.task_view(record)}))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


def peer_scope(scope: Scope, *, trusted: bool, url: str = "http://10.0.0.5:9900/") -> Scope:
    config = DictConfig({"a2a": {"peers": {"lab": {"url": url, "trusted": trusted}}}})
    key = MemorySecretStore({peer_key_env("lab"): token("lab")})
    return scope.with_config(config).with_secrets(key)  # pyright: ignore[reportArgumentType]


def test_outbound_to_a_private_address_is_refused_unless_trusted(scope: Scope) -> None:
    peer = Peer()
    refused = asyncio.run(
        call_peer(
            peer_scope(scope, trusted=False), "lab", "hello", inner_transport=peer.transport()
        )
    )
    assert "refused by the URL policy" in refused and not peer.requests
    allowed = asyncio.run(
        call_peer(peer_scope(scope, trusted=True), "lab", "hello", inner_transport=peer.transport())
    )
    assert allowed.startswith("[lab · context ctx-new · completed]\npeer says hi")
    assert len(peer.requests) == 1


def test_trust_never_opens_cloud_metadata(scope: Scope) -> None:
    peer = Peer()
    scoped = peer_scope(scope, trusted=True, url="http://169.254.169.254/latest")
    answer = asyncio.run(call_peer(scoped, "lab", "hi", inner_transport=peer.transport()))
    assert "refused" in answer and not peer.requests


def test_outbound_carries_the_privacy_note_masks_secrets_and_conforms(scope: Scope) -> None:
    peer = Peer()
    scoped = peer_scope(scope, trusted=True)
    leaked = "my key is " + "sk-" + "proj" + "A" * 40
    answer = asyncio.run(
        call_peer(scoped, "lab", leaked, context_id="ctx-7", inner_transport=peer.transport())
    )
    request = peer.requests[0]
    body = json.loads(request.content)
    conforms(body, "SendMessageRequest")
    text = body["params"]["message"]["parts"][0]["text"]
    assert text.startswith(PRIVACY_NOTE) and "A" * 40 not in text
    assert body["params"]["message"]["contextId"] == "ctx-7"
    assert request.headers["authorization"] == f"Bearer {token('lab')}"
    assert request.headers["a2a-version"] == "1.0"
    assert token("lab") not in answer


def test_input_required_tells_the_model_how_to_answer(scope: Scope) -> None:
    peer = Peer(state=p.INPUT_REQUIRED)
    answer = asyncio.run(
        call_peer(
            peer_scope(scope, trusted=True),
            "lab",
            "book",
            context_id="trip",
            inner_transport=peer.transport(),
        )
    )
    assert "input-required" in answer and "context_id 'trip'" in answer


@pytest.mark.parametrize(
    ("status", "word"), [(401, "credentials"), (429, "rate limiting"), (502, "HTTP 502")]
)
def test_outbound_http_failures_are_plain_errors(scope: Scope, status: int, word: str) -> None:
    peer = Peer(status=status)
    answer = asyncio.run(
        call_peer(peer_scope(scope, trusted=True), "lab", "hi", inner_transport=peer.transport())
    )
    assert word in answer and '"error"' in answer and token("lab") not in answer


def test_the_tool_needs_a_known_peer_and_arguments(scope: Scope) -> None:
    scoped = peer_scope(scope, trusted=True)
    assert "unknown A2A peer" in asyncio.run(call_peer(scoped, "nobody", "hi"))
    assert "required" in asyncio.run(a2a_call({"agent": "lab"}, scope=scoped))
    assert "scope" in asyncio.run(a2a_call({"agent": "lab", "message": "x"}))
    bad = asyncio.run(call_peer(scoped, "lab", "hi", context_id="../x"))
    assert "context_id" in bad


def test_the_tool_is_offered_only_with_peers_and_sits_in_indus_a2a(scope: Scope) -> None:
    registry = ToolRegistry()
    register_tools(registry)
    assert "a2a_call" in resolve("indus-a2a", registry)
    assert "terminal" in resolve("indus-a2a", registry)
    entry = registry.get("a2a_call")
    assert entry is not None and entry.probe is not None
    assert entry.probe(peer_scope(scope, trusted=False))
    assert not entry.probe(scope.with_config(DictConfig({})))  # pyright: ignore[reportArgumentType]


def test_a_peer_over_the_rate_is_answered_429() -> None:
    async def main(rig: Rig) -> None:
        for _ in range(3):
            assert (await rig.call("ListTasks", {}))["result"]["tasks"] == []
        limited = await rig.post({"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": {}})
        assert limited.status_code == 429 and limited.json()["error"]["code"] == p.RATE_LIMITED
        assert (await rig.call("ListTasks", {}, peer="bob"))["result"]["tasks"] == []
        rig.clock.advance(60)
        assert (await rig.call("ListTasks", {}))["result"]["tasks"] == []

    run(main, rate_per_minute=3)
