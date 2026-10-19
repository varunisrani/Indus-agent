"""P15-13: chat slash commands (tables, behaviour, tiers, quick commands, R04.28)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.foundation.yamlio import dump_roundtrip, load_safe, to_roundtrip
from indus.gateway.core.admission import ModelChoice
from indus.gateway.core.commands import (
    ANYTIME,
    BUSY_REJECT,
    DENIED,
    IDLE_ONLY,
    MID_RUN,
    UNKNOWN,
    Rewrite,
    run_exec_quick,
)
from indus.gateway.core.events import InboundEvent, Source
from indus.service.commands.registry import REGISTRY
from indus.state import gateway_routes as routes
from tests.fakes.clock import FakeClock
from tests.fakes.model import Fail, Finish, Stall, Text, Turn, text_turn
from tests.unit.gateway.core._kit import ALLOWED, Kit, build_kit, dm, model_inputs


def run(coro: Any) -> None:
    asyncio.run(coro)


def stalled(text: str, seconds: float = 1000.0) -> Turn:
    return Turn((Text((text,)), Stall(seconds), Finish("stop")))


def write_config(scope: Scope, data: Mapping[str, Any]) -> None:
    (scope.home / "config.yaml").write_text(dump_roundtrip(to_roundtrip(dict(data))))


def group(text: str, user: str = ALLOWED, chat: str = "g1", **kw: Any) -> InboundEvent:
    return InboundEvent(
        source=Source(platform="telegram", chat_id=chat, chat_type="group", user_id=user),
        text=text,
        message_id="g",
        **kw,
    )


async def idle_reply(kit: Kit, text: str, user: str = ALLOWED) -> Any:
    event = dm(text, user=user)
    return await kit.pipeline.commands.dispatch_idle(event, kit.key(user))


# -- the tables -----------------------------------------------------------------------------------


def test_tables_cover_the_gateway_registry_and_hold_no_overlap() -> None:
    assert not set(ANYTIME) & set(IDLE_ONLY) - {"stop", "queue", "steer", "goal", "loop"}
    gateway = {cmd.name for cmd in REGISTRY.commands("gateway")}
    assert set(ANYTIME) <= gateway and set(IDLE_ONLY) <= gateway and set(MID_RUN) <= gateway
    for name in ("status", "approve", "deny", "help", "commands", "yolo", "verbose", "usage",
                 "bg", "btw", "platform", "version"):  # fmt: skip
        assert name in ANYTIME, name
    for name in ("new", "resume", "sessions", "title", "model", "sethome", "whoami", "retry",
                 "undo", "compress", "approvals", "personality", "skills", "memory",
                 "reload-mcp"):  # fmt: skip
        assert name in IDLE_ONLY, name
    assert set(MID_RUN) == {"stop", "new", "queue", "steer", "goal", "loop"}
    for name in ANYTIME:
        cmd = REGISTRY.resolve(name)
        assert cmd is not None and cmd.busy_policy != "reject", name


def _names() -> list[str]:
    return sorted({*ANYTIME, *IDLE_ONLY})


def test_every_command_answers_while_idle(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(scope, fake_clock, tmp_path, text_turn("hello"))
        try:
            await kit.pipeline.handle(dm("hello"))
            await kit.until(lambda: kit.wire() == ["hello"])
            for name in _names():
                if name in ("bg", "new"):
                    continue  # covered on their own (they start sessions)
                answer = await idle_reply(kit, f"/{name}")
                assert answer is None or isinstance(answer, str | Rewrite), name
                if isinstance(answer, str):
                    assert not answer.startswith(f"/{name} failed"), (name, answer)
                    assert answer != UNKNOWN.format(name=name), name
        finally:
            await kit.close()

    run(main())


def test_every_command_while_busy_runs_its_variant_or_the_fixed_reply(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(scope, fake_clock, tmp_path, stalled("busy"))
        try:
            kit.pipeline.spawn(kit.pipeline.handle(dm("start")))
            await kit.until(lambda: kit.server.requests)
            for name in _names():
                if name in ("stop", "new", "bg"):
                    continue  # they end the turn / start sessions: tested on their own
                answer = await kit.pipeline.commands.dispatch_busy(dm(f"/{name}"), kit.key())
                rejected = answer == BUSY_REJECT.format(name=name)
                allowed = name in MID_RUN or name in ANYTIME
                assert rejected is not allowed, (name, answer)
            assert kit.pipeline.busy(kit.key())
        finally:
            await kit.close()

    run(main())


# -- resolution -----------------------------------------------------------------------------------


def test_unknown_commands_get_a_hint_and_paths_reach_the_agent(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(scope, fake_clock, tmp_path, text_turn("it is a hosts file"))
        try:
            assert await idle_reply(kit, "/foo") == UNKNOWN.format(name="foo")
            assert await idle_reply(kit, "/start") is None  # Telegram's bot-start ping
            assert (await idle_reply(kit, "/vers")).startswith("Indus Agent")  # unique prefix
            ambiguous = await idle_reply(kit, "/re")
            assert "could mean" in ambiguous and "/reset" in ambiguous
            assert (await idle_reply(kit, "/quit")) == "/quit only works in the terminal app."
            await kit.pipeline.handle(dm("/etc/hosts what is this"))
            await kit.until(lambda: kit.wire())
            assert model_inputs(kit.server) == ["/etc/hosts what is this"]
        finally:
            await kit.close()

    run(main())


def test_admin_tiers_gate_commands_but_never_plain_chat(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        write_config(scope, {"quick_commands": {"hi": {"type": "exec", "command": "echo hi"}}})
        kit = await build_kit(
            scope,
            fake_clock,
            tmp_path,
            text_turn("chat works"),
            extra={"allow_admin_from": ["999"], "user_allowed_commands": ["status"]},
        )
        try:
            assert await idle_reply(kit, "/model x") == DENIED.format(name="model")
            assert await idle_reply(kit, "/hi") == DENIED.format(name="hi")
            assert (await idle_reply(kit, "/status")).startswith("No conversation yet")
            listed = await idle_reply(kit, "/help")
            assert "/status" in listed and "/whoami" in listed and "/model" not in listed
            assert "user (limited commands)" in await idle_reply(kit, "/whoami")
            await kit.pipeline.handle(dm("just chatting"))
            await kit.until(lambda: kit.wire() == ["chat works"])
        finally:
            await kit.close()

    run(main())


# -- sessions --------------------------------------------------------------------------------------


def test_new_rotates_the_route_to_a_fresh_session(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(scope, fake_clock, tmp_path, text_turn("one"), text_turn("two"))
        try:
            await kit.pipeline.handle(dm("first"))
            await kit.until(lambda: kit.wire() == ["one"])
            before = routes.get(kit.store, kit.key())
            assert before is not None
            await kit.pipeline.handle(dm("/reset", message_id="r"))
            after = routes.get(kit.store, kit.key())
            assert after is not None and after.session_id != before.session_id
            assert after.prev_session_id == before.session_id
            assert before.session_id not in kit.service.attached()
            await kit.pipeline.handle(dm("second", message_id="s"))
            await kit.until(lambda: "two" in kit.wire())
            assert kit.pipeline.cached_session(kit.key()) == after.session_id
        finally:
            await kit.close()

    run(main())


def test_new_and_undo_ask_first_when_confirmation_is_on(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(
            scope, fake_clock, tmp_path, text_turn("one"), settings={"destructive_confirm": True}
        )
        try:
            await kit.pipeline.handle(dm("first"))
            await kit.until(lambda: kit.wire() == ["one"])
            first = kit.pipeline.cached_session(kit.key())
            await kit.pipeline.handle(dm("/new", message_id="n1"))
            assert "Reply 1 to go ahead" in kit.wire()[-1]
            await kit.pipeline.handle(dm("3", message_id="n2"))
            assert kit.wire()[-1] == "Cancelled."
            assert kit.pipeline.cached_session(kit.key()) == first
            await kit.pipeline.handle(dm("/new", message_id="n3"))
            await kit.pipeline.handle(dm("1", message_id="n4"))
            assert kit.wire()[-1].startswith("Started a new conversation")
            assert kit.pipeline.cached_session(kit.key()) != first
            await kit.pipeline.handle(dm("/undo", message_id="u1"))
            await kit.pipeline.handle(dm("2", message_id="u2"))  # go ahead, stop asking
            assert "destructive_slash_confirm is now false" in kit.wire()[-1]
            assert "destructive_slash_confirm: false" in (scope.home / "config.yaml").read_text()
        finally:
            await kit.close()

    run(main())


def test_title_sessions_and_resume(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    async def main() -> None:
        kit = await build_kit(scope, fake_clock, tmp_path, text_turn("one"), text_turn("two"))
        try:
            await kit.pipeline.handle(dm("first"))
            await kit.until(lambda: kit.wire() == ["one"])
            assert await idle_reply(kit, "/title Trip plans") == "Title set: Trip plans"
            assert await idle_reply(kit, "/title") == "Title: Trip plans"
            first = kit.pipeline.cached_session(kit.key())
            await idle_reply(kit, "/new")
            listing = await idle_reply(kit, "/sessions")
            assert "Trip plans" in listing and "(current)" in listing
            assert (await idle_reply(kit, "/resume trip")) == "Resumed 'Trip plans'."
            assert kit.pipeline.cached_session(kit.key()) == first
            entry = routes.get(kit.store, kit.key())
            assert entry is not None and entry.session_id == first
            assert "No conversation of this chat matches" in await idle_reply(kit, "/resume zzz")
        finally:
            await kit.close()

    run(main())


def test_cross_origin_session_listing_is_admin_only(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(
            scope,
            fake_clock,
            tmp_path,
            text_turn("for 300"),
            text_turn("for 200"),
            env={"INDUS_TELEGRAM_ALLOWED_USERS": "200,300"},
            extra={"allow_admin_from": ["300"], "user_allowed_commands": ["sessions"]},
        )
        try:
            await kit.pipeline.handle(dm("secret project", user="300"))
            await kit.until(lambda: kit.wire("300"))
            other = kit.pipeline.cached_session(kit.key("300"))
            assert other is not None
            await idle_reply(kit, "/title Secret plan", user="300")
            refused = await idle_reply(kit, "/sessions all")
            assert refused.startswith("Only an admin can list other chats")
            assert "Secret plan" not in refused
            admin = await idle_reply(kit, "/sessions all", user="300")
            assert "Secret plan" in admin
        finally:
            await kit.close()

    run(main())


def test_undo_and_retry_take_back_the_last_message(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(
            scope, fake_clock, tmp_path, text_turn("one"), text_turn("again"), text_turn("two")
        )
        try:
            await kit.pipeline.handle(dm("what is 2+2"))
            await kit.until(lambda: kit.wire() == ["one"])
            await kit.pipeline.handle(dm("/retry", message_id="r"))
            await kit.until(lambda: "again" in kit.wire())
            assert model_inputs(kit.server)[-1] == "what is 2+2"
            assert (await idle_reply(kit, "/undo")).startswith("Took back")
        finally:
            await kit.close()

    run(main())


# -- /model ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", ["success", "failure", "interrupt"])
def test_model_once_is_reverted_on_every_exit_path(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path, outcome: str
) -> None:
    async def main() -> None:
        turn = {
            "success": text_turn("done"),
            "failure": Turn((Fail(400, "no"),)),
            "interrupt": stalled("long"),
        }[outcome]
        kit = await build_kit(scope, fake_clock, tmp_path, turn, text_turn("after"))
        try:
            answer = await idle_reply(kit, "/model fast-model --once")
            assert answer.startswith("The next reply uses fast-model")
            kit.pipeline.spawn(kit.pipeline.handle(dm("go")))
            await kit.until(lambda: kit.server.requests)
            if outcome == "interrupt":
                await kit.pipeline.accept(dm("/stop", message_id="s"))
            await kit.until(lambda: not kit.pipeline.busy(kit.key()))
            await kit.pipeline.idle()
            sid = kit.pipeline.cached_session(kit.key())
            assert kit.switcher.applied == [(sid, "fast-model"), (sid, None)]
            assert kit.key() not in kit.pipeline.once_next
        finally:
            await kit.close()

    run(main())


def test_model_precedence_session_over_channel_and_persisted_in_the_route(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(
            scope,
            fake_clock,
            tmp_path,
            text_turn("a"),
            text_turn("b"),
            channel_model=lambda _p, _chat: ModelChoice("channel-model", source="channel"),
        )
        try:
            assert (await idle_reply(kit, "/model")) == "Model: channel-model (this channel)"
            await kit.pipeline.handle(dm("hi"))
            await kit.until(lambda: kit.wire() == ["a"])
            sid = kit.pipeline.cached_session(kit.key())
            assert kit.switcher.applied == [(sid, "channel-model")]
            assert await idle_reply(kit, "/model acme:session-model") == (
                "This chat now uses acme:session-model."
            )
            entry = routes.get(kit.store, kit.key())
            assert entry is not None
            assert entry.model_override == {"model": "session-model", "provider": "acme"}
            assert kit.switcher.applied[-1] == (sid, "session-model")
            kit.switcher.refuse.add("nope")
            assert (await idle_reply(kit, "/model nope")).startswith("Can't use nope")
            assert (await idle_reply(kit, "/model x --global --once")) == (
                "Pick one of --global and --once."
            )
        finally:
            await kit.close()

    run(main())


# -- chat-only commands ----------------------------------------------------------------------------


def test_sethome_writes_the_home_channel(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(scope, fake_clock, tmp_path)
        try:
            event = group("/sethome", chat="g7")
            answer = await kit.pipeline.commands.dispatch_idle(event, "k")
            assert isinstance(answer, str)
            assert answer.startswith("This chat is now the telegram home channel")
            saved = load_safe((scope.home / "config.yaml").read_text())
            assert saved["platforms"]["telegram"]["home_channel"]["chat_id"] == "g7"
        finally:
            await kit.close()

    run(main())


def test_bg_posts_a_prefixed_result_with_its_task_id(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(scope, fake_clock, tmp_path, text_turn("background result"))
        try:
            await kit.pipeline.handle(dm("/bg summarise the logs"))
            started = kit.wire()[0]
            assert started.startswith("Started background task bg_")
            task_id = started.split()[3].rstrip(":")
            await kit.until(lambda: len(kit.wire()) == 2)
            assert kit.wire()[1] == f"[{task_id}] Done:\nbackground result"
            assert kit.pipeline.cached_session(kit.key()) is None  # the chat's own is untouched
        finally:
            await kit.close()

    run(main())


def test_platform_pause_drops_inbound_until_resumed(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(scope, fake_clock, tmp_path, text_turn("back"))
        try:
            assert (await idle_reply(kit, "/platform")) == "Connected: telegram"
            assert (await idle_reply(kit, "/platform pause telegram")).startswith("Paused")
            await kit.pipeline.accept(dm("anyone?"))
            await kit.settle(10)
            assert kit.server.requests == []
            assert (await idle_reply(kit, "/platform resume telegram")) == "Resumed telegram."
            await kit.pipeline.accept(dm("hello again"))
            await kit.until(lambda: "back" in kit.wire())
        finally:
            await kit.close()

    run(main())


def test_idle_queue_and_steer_send_their_text_as_a_turn(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(scope, fake_clock, tmp_path, text_turn("q"), text_turn("s"))
        try:
            await kit.pipeline.handle(dm("/queue first thing"))
            await kit.until(lambda: kit.wire() == ["q"])
            await kit.pipeline.handle(dm("/steer /etc/passwd please", message_id="2"))
            await kit.until(lambda: kit.wire() == ["q", "s"])
            assert model_inputs(kit.server) == ["first thing", "/etc/passwd please"]
            assert await idle_reply(kit, "/queue") == "Usage: /queue <text>"
        finally:
            await kit.close()

    run(main())


# -- extensions: hooks, skills, quick commands -----------------------------------------------------


def test_command_hooks_can_deny_answer_or_rewrite(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        calls: list[str] = []

        async def hooks(name: str, context: Mapping[str, Any]) -> list[Any]:
            calls.append(name)
            if name == "command:usage":
                return [{"decision": "deny", "message": "no usage for you"}]
            if name == "command:help":
                return [{"decision": "rewrite", "command_name": "version"}]
            return []

        kit = await build_kit(scope, fake_clock, tmp_path)
        kit.pipeline.commands.command_hooks = hooks
        try:
            assert await idle_reply(kit, "/usage") == "no usage for you"
            assert (await idle_reply(kit, "/help")).startswith("Indus Agent")
            assert calls == ["command:usage", "command:help", "command:version"]
        finally:
            await kit.close()

    run(main())


@dataclass
class FakeExpansion:
    text: str
    loaded: tuple[str, ...] = ("deploy",)


class FakeSkillTable:
    def names(self) -> dict[str, str]:
        return {"deploy": "skill: ship it"}

    def run(self, text: str, **_kw: Any) -> FakeExpansion:
        return FakeExpansion(f"Use the deploy skill. Task: {text.removeprefix('/deploy ')}")


def test_a_skill_command_becomes_a_skill_turn(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(scope, fake_clock, tmp_path, text_turn("deployed"))
        kit.pipeline.commands.skills = lambda _scope, _platform, _sid: FakeSkillTable()
        try:
            await kit.pipeline.handle(dm("/deploy the web app"))
            await kit.until(lambda: kit.wire() == ["deployed"])
            assert model_inputs(kit.server) == ["Use the deploy skill. Task: the web app"]
        finally:
            await kit.close()

    run(main())


def test_quick_exec_has_no_provider_keys_and_redacted_output(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        secret = "sk-" + "proj-" + "A1b2C3d4E5f6G7h8I9j0" * 2
        scoped = scope.with_env(scope.env.with_overlay({"OPENAI_API_KEY": secret}))
        env_dump = await run_exec_quick(scoped, "env", clock=fake_clock)
        assert "OPENAI_API_KEY" not in env_dump and secret not in env_dump
        echoed = await run_exec_quick(scoped, f"echo token {secret}", clock=fake_clock)
        assert secret not in echoed and echoed.startswith("token")
        write_config(
            scope,
            {
                "quick_commands": {
                    "hello": {"type": "exec", "command": "echo hello from quick"},
                    "v": {"type": "alias", "target": "version"},
                }
            },
        )
        kit = await build_kit(scope, fake_clock, tmp_path)
        try:
            assert await idle_reply(kit, "/hello") == "hello from quick"
            assert (await idle_reply(kit, "/v")).startswith("Indus Agent")
        finally:
            await kit.close()

    run(main())


def test_a_quick_exec_command_is_stopped_after_its_limit(
    fake_clock: FakeClock, scope: Scope
) -> None:
    async def main() -> None:
        task = asyncio.ensure_future(run_exec_quick(scope, "sleep 30", clock=fake_clock))
        while fake_clock.pending == 0:
            await asyncio.wait({task}, timeout=0.01)
        fake_clock.advance(30.0)
        answer = await task
        assert answer == "The command ran longer than 30 s and was stopped."

    run(main())
