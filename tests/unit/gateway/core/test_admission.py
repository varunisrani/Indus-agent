"""P15-11: the admission pipeline and the session claim."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.gateway.core.admission import (
    ClaimTable,
    GatewayIdentity,
    bind_identity,
    current_identity,
    identity_env,
)
from indus.gateway.core.bot_loop import CONCURRENCY_REFUSAL, ConcurrencyCap
from indus.gateway.core.events import InboundEvent
from indus.state import gateway_routes as routes
from tests.fakes.clock import FakeClock, checkpoint
from tests.fakes.model import Fail, Finish, Stall, Text, Turn, text_turn
from tests.unit.gateway.core._kit import ALLOWED, build_kit, dm, model_inputs


def stalled(text: str, seconds: float = 1000.0) -> Turn:
    return Turn((Text((text,)), Stall(seconds), Finish("stop")))


def run(coro: Any) -> None:
    asyncio.run(coro)


# -- end to end through the fake platform ---------------------------------------------------------


def test_an_authorized_message_becomes_a_turn_and_a_reply_on_the_wire(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(scope, fake_clock, tmp_path, text_turn("Hello ", "there"))
        try:
            inbound = kit.fake.dm(ALLOWED, "hi bot")
            await kit.until(lambda: kit.wire())
            assert kit.wire() == ["Hello there"]
            assert model_inputs(kit.server) == ["hi bot"]
            sent = kit.fake.ops_of("send")[0]
            assert sent.reply_to == inbound.message_id
            key = kit.pipeline.session_key(dm("x").source)
            await kit.until(lambda: not kit.pipeline.busy(key))
            entry = routes.get(kit.store, key)
            assert entry is not None and entry.active_turn_token is None
            assert entry.origin["user_id"] == ALLOWED
        finally:
            await kit.close()

    run(main())


def test_an_unauthorized_dm_gets_a_pairing_code_and_never_reaches_the_model(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(
            scope,
            fake_clock,
            tmp_path,
            text_turn("never"),
            env={"INDUS_TELEGRAM_ALLOWED_USERS": ""},
        )
        try:
            kit.fake.dm("999", "let me in")
            await kit.until(lambda: kit.wire("999"))
            assert "pairing approve telegram" in kit.wire("999")[0]
            kit.fake.dm("999", "hello?")
            kit.fake.dm("999", "anyone?")
            await kit.settle()
            assert kit.server.requests == []
            # One code per request window, then one "try later", then silence (P15-09).
            assert len(kit.wire("999")) == 2 and "try again later" in kit.wire("999")[1]
        finally:
            await kit.close()

    run(main())


def test_an_allowlist_makes_strangers_silent_and_they_never_reach_the_model(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(scope, fake_clock, tmp_path, text_turn("never"))
        try:
            kit.fake.dm("999", "hello")
            kit.fake.group("g1", "999", "hey @bot", mention=True)
            await kit.settle()
            assert kit.server.requests == []
            assert kit.fake.ops_of("send") == []
        finally:
            await kit.close()

    run(main())


def test_a_group_message_is_answered_only_when_it_mentions_the_bot(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(scope, fake_clock, tmp_path, text_turn("yes?"))
        try:
            kit.fake.group("g1", ALLOWED, "chatting among ourselves")
            await kit.settle()
            assert kit.server.requests == []
            kit.fake.group("g1", ALLOWED, "@bot what time is it", mention=True)
            await kit.until(lambda: kit.wire("g1"))
            assert kit.wire("g1") == ["yes?"]
        finally:
            await kit.close()

    run(main())


def test_a_failed_turn_gets_a_short_notice(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(scope, fake_clock, tmp_path, Turn((Fail(400, "bad request"),)))
        try:
            await kit.pipeline.handle(dm("hello"))
            await kit.until(lambda: kit.wire())
            assert kit.wire()[0].startswith("Sorry, I couldn't finish that reply")
            assert kit.pipeline.claims.current(kit.key()) is None
        finally:
            await kit.close()

    run(main())


# -- the claim (R06.2) -----------------------------------------------------------------------------


def test_claims_are_synchronous_and_released_by_generation(fake_clock: FakeClock) -> None:
    table = ClaimTable(fake_clock)
    first = table.claim("k")
    assert first is not None and table.claim("k") is None
    assert table.drop("k") is first  # /new or /stop takes it away
    successor = table.claim("k")
    assert successor is not None and successor.generation > first.generation
    assert table.release(first) is False  # the displaced turn's release is a no-op
    assert table.current("k") is successor
    assert table.release(successor) is True and table.current("k") is None


def test_leaked_and_stale_claims_are_evicted_before_the_busy_check(
    fake_clock: FakeClock,
) -> None:
    async def main() -> None:
        table = ClaimTable(fake_clock)
        leaked = table.claim("a")
        assert leaked is not None
        done = asyncio.ensure_future(checkpoint())
        await done
        leaked.task = done
        assert table.evict_stale("a", stale_after=1800, running=lambda c: True) is leaked
        old = table.claim("b")
        assert old is not None
        fake_clock.advance(1800)
        assert table.evict_stale("b", stale_after=1800, running=lambda c: True) is None
        assert table.evict_stale("b", stale_after=1800, running=lambda c: False) is old
        assert table.current("b") is None

    run(main())


def test_two_events_for_one_key_with_no_await_between_start_one_turn(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(
            scope,
            fake_clock,
            tmp_path,
            text_turn("first answer"),
            text_turn("second answer"),
            settings={"busy_mode": "queue", "busy_ack": False},
        )
        try:
            created: list[str] = []
            real_create = kit.service.create

            async def counting_create(params: Any = None) -> Any:
                result = await real_create(params)
                created.append(result.session_id)
                return result

            kit.service.create = counting_create  # type: ignore[method-assign]
            one = asyncio.ensure_future(kit.pipeline.handle(dm("one", message_id="1")))
            two = asyncio.ensure_future(kit.pipeline.handle(dm("two", message_id="2")))
            await asyncio.gather(one, two)
            await kit.until(lambda: len(kit.wire()) == 2)
            assert len(created) == 1  # one session, one agent
            assert model_inputs(kit.server) == ["one", "two"]
            assert kit.wire() == ["first answer", "second answer"]
        finally:
            await kit.close()

    run(main())


def test_a_displaced_turn_cannot_release_its_successors_claim(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(
            scope, fake_clock, tmp_path, stalled("old"), stalled("new"), text_turn("unused")
        )
        try:
            kit.pipeline.spawn(kit.pipeline.handle(dm("long job", message_id="1")))
            await kit.until(lambda: len(kit.server.requests) == 1)
            first = kit.pipeline.claims.current(kit.key())
            assert first is not None
            await kit.pipeline.accept(dm("/new", message_id="2"))  # drops the old claim
            kit.pipeline.spawn(kit.pipeline.handle(dm("next job", message_id="3")))
            await kit.until(lambda: len(kit.server.requests) == 2)
            successor = kit.pipeline.claims.current(kit.key())
            assert successor is not None and successor is not first
            await kit.settle()  # the old turn has ended by now and run its release
            assert kit.pipeline.claims.current(kit.key()) is successor
        finally:
            await kit.close()

    run(main())


def test_the_concurrency_cap_refuses_a_new_session(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(
            scope,
            fake_clock,
            tmp_path,
            stalled("busy"),
            env={"INDUS_TELEGRAM_ALLOWED_USERS": "200,201"},
            cap=ConcurrencyCap(lambda: 1),
        )
        try:
            kit.pipeline.spawn(kit.pipeline.handle(dm("work", user="200")))
            await kit.until(lambda: kit.server.requests)
            await kit.pipeline.handle(dm("me too", user="201"))
            assert kit.wire("201") == [CONCURRENCY_REFUSAL]
        finally:
            await kit.close()

    run(main())


# -- identity context (R06.21, R01.16) -------------------------------------------------------------


def test_identity_env_names_only_the_bound_chat() -> None:
    assert identity_env(None) == {}
    identity = GatewayIdentity("telegram", "42", "7", "", "@default:telegram:dm:42", "s1")
    env = identity_env(identity)
    assert env == {
        "INDUS_GATEWAY_PLATFORM": "telegram",
        "INDUS_GATEWAY_CHAT_ID": "42",
        "INDUS_GATEWAY_USER_ID": "7",
        "INDUS_GATEWAY_SESSION_KEY": "@default:telegram:dm:42",
        "INDUS_SESSION_ID": "s1",
    }


def test_a_subprocess_sees_only_its_own_chat_under_50_interleaved_chats(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        seen: dict[str, str] = {}

        async def enrich(event: InboundEvent) -> str:
            for _ in range(3):  # interleave with the other chats
                await checkpoint()
            env = {**identity_env(), "PATH": os.environ.get("PATH", "")}
            proc = await asyncio.create_subprocess_exec(
                "env", stdout=asyncio.subprocess.PIPE, env=env
            )
            out, _ = await proc.communicate()
            lines = dict(line.split("=", 1) for line in out.decode().splitlines() if "=" in line)
            seen[str(event.source.chat_id)] = lines.get("INDUS_GATEWAY_CHAT_ID", "")
            return ""  # no model turn needed

        kit = await build_kit(
            scope,
            fake_clock,
            tmp_path,
            env={"INDUS_TELEGRAM_ALLOW_ALL_USERS": "1"},
            enrich=enrich,
        )
        try:
            # A leaked identity from "chat B" in the task that starts the handlers.
            bind_identity(GatewayIdentity("telegram", "chat-B", "b", "", "leak"))
            for n in range(50):
                await kit.pipeline.accept(dm(f"hi {n}", user=str(1000 + n), message_id=str(n)))
            await kit.until(lambda: len(seen) == 50, limit=60.0)
            assert seen == {str(1000 + n): str(1000 + n) for n in range(50)}
            assert current_identity() is not None  # the test's own context is untouched
        finally:
            bind_identity(None)
            await kit.close()

    run(main())


# -- gates ----------------------------------------------------------------------------------------


def test_the_pause_gate_passes_status_but_blocks_new_turns(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(
            scope, fake_clock, tmp_path, text_turn("never"), paused=lambda: "Paused by the owner."
        )
        try:
            await kit.pipeline.handle(dm("do something"))
            await kit.pipeline.handle(dm("/status", message_id="2"))
            await kit.until(lambda: len(kit.wire()) == 2)
            assert kit.wire()[0] == "Paused by the owner."
            assert "No conversation yet" in kit.wire()[1]
            assert kit.server.requests == []
        finally:
            await kit.close()

    run(main())


def test_events_during_startup_restore_are_replayed_in_order(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(
            scope,
            fake_clock,
            tmp_path,
            text_turn("a1"),
            text_turn("a2"),
            settings={"busy_mode": "queue", "busy_ack": False},
        )
        try:
            kit.pipeline.begin_restore()
            await kit.pipeline.accept(dm("first", message_id="1"))
            await kit.pipeline.accept(dm("second", message_id="2"))
            await kit.settle(5)
            assert kit.server.requests == []
            assert await kit.pipeline.finish_restore() == 2
            await kit.until(lambda: len(kit.wire()) == 2)
            assert model_inputs(kit.server) == ["first", "second"]
        finally:
            await kit.close()

    run(main())


class Hooks:
    def __init__(self, answer: dict[str, Any] | None) -> None:
        self.answer = answer
        self.admitted: list[str] = []

    async def pre_gateway_dispatch(self, **payload: Any) -> dict[str, Any] | None:
        return self.answer

    async def observe(self, name: str, **payload: Any) -> None:
        if name == "post_gateway_admission":
            self.admitted.append(payload["event"].text)


@pytest.mark.parametrize(
    ("answer", "seen"),
    [
        (None, ["original"]),
        ({"action": "rewrite", "text": "rewritten"}, ["rewritten"]),
        ({"action": "skip", "reason": "spam"}, []),
    ],
)
def test_the_pre_dispatch_hook_may_rewrite_or_drop_and_admission_fires_once(
    scope: Scope,
    fake_clock: FakeClock,
    tmp_path: Path,
    answer: dict[str, Any] | None,
    seen: list[str],
) -> None:
    async def main() -> None:
        hooks = Hooks(answer)
        kit = await build_kit(scope, fake_clock, tmp_path, text_turn("ok"), hooks=hooks)
        try:
            await kit.pipeline.handle(dm("original"))
            await kit.pipeline.idle()
            await kit.settle(5)
            assert model_inputs(kit.server) == seen
            assert hooks.admitted == seen
        finally:
            await kit.close()

    run(main())


def test_a_hook_that_raises_fails_open(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    class Broken(Hooks):
        async def pre_gateway_dispatch(self, **payload: Any) -> dict[str, Any] | None:
            raise RuntimeError("plugin bug")

        async def observe(self, name: str, **payload: Any) -> None:
            raise RuntimeError("plugin bug")

    async def main() -> None:
        kit = await build_kit(
            scope, fake_clock, tmp_path, text_turn("still here"), hooks=Broken(None)
        )
        try:
            await kit.pipeline.handle(dm("hello"))
            await kit.until(lambda: kit.wire())
            assert kit.wire() == ["still here"]
        finally:
            await kit.close()

    run(main())
