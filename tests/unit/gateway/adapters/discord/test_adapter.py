"""P15-34: the Discord adapter over the Discord wire fake (discord.py itself is not needed)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import Scope
from indus.gateway.adapters.base import (
    AdapterConnectError,
    ApprovalAction,
    ApprovalPrompt,
    ClarifyPrompt,
    approval_callback,
    clarify_callback,
)
from indus.gateway.adapters.discord import DiscordAdapter, create_adapter
from indus.gateway.adapters.discord import commands as cmds
from indus.gateway.adapters.discord.adapter import REFUSED_COMMAND, REFUSED_PRESS, ROLES_ENV
from indus.gateway.config import AdapterConfig, PlatformSettings
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind
from indus.gateway.core.send import ErrorKind
from indus.gateway.core.session_keys import build_session_key
from indus.gateway.delivery import send_command
from indus.gateway.intake.media import MediaIntake
from indus.gateway.registry import PlatformEntry, PlatformRegistry
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FailureKind, FakeChatPlatform
from tests.fakes.platform.discord_wire import (
    MAX_LEN,
    DiscordWire,
    LoginFailure,
    PrivilegedIntentsRequired,
    UISelect,
)
from tests.unit.cli.commands.cmdrun import run_cli, write_settings

START = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
GUILD = "77"
ROLE_OK = 11
ROLE_OTHER = 12


def fake_token() -> str:
    """A token-shaped value built at run time (never a literal credential)."""
    return ".".join(("MTAw" + "q" * 20, "uvwxyz", "k" * 27))


def settings(**extra: object) -> PlatformSettings:
    return PlatformSettings(
        name="discord",
        enabled=True,
        reason="credentials",
        section=PlatformSection(extra=dict(extra)),
        credentials={"DISCORD_BOT_TOKEN": fake_token()},
    )


@dataclass
class World:
    clock: FakeClock
    platform: FakeChatPlatform
    wire: DiscordWire
    adapter: DiscordAdapter
    events: list[InboundEvent] = field(default_factory=list[InboundEvent])

    async def settle(self, awaitable: Awaitable[Any], step: float = 0.5) -> Any:
        task = asyncio.ensure_future(awaitable)
        for _ in range(4000):
            await self.clock.run_until_idle()
            if task.done():
                return task.result()
            self.clock.advance(step)
        task.cancel()
        raise AssertionError("never finished on the fake clock")

    async def pump(self) -> list[InboundEvent]:
        await self.wire.dispatch_pending()
        await self.clock.run_until_idle()
        return self.events


def world(
    *,
    env: dict[str, str] | None = None,
    extra: dict[str, object] | None = None,
    send_only: bool = False,
    is_paired: Callable[[str], Awaitable[bool]] | None = None,
    extra_commands: tuple[tuple[str, str, str], ...] = (),
    sync: bool = False,
) -> World:
    clock = FakeClock(START)
    platform = FakeChatPlatform("discord", clock=clock, max_len=MAX_LEN, bot_user_id="900")
    wire = DiscordWire(platform)
    options = dict(extra or {})
    if not sync:
        options.setdefault("command_sync", "off")
    adapter = DiscordAdapter(
        clock=clock,
        settings=settings(**options),
        env=env or {},
        sdk=wire.sdk,
        send_only=send_only,
        is_paired=is_paired,
        extra_commands=extra_commands,
    )
    made = World(clock, platform, wire, adapter)

    async def sink(event: InboundEvent) -> None:
        made.events.append(event)

    adapter.set_sink(sink)
    return made


def run(fn: Callable[[World], Awaitable[None]], made: World) -> None:
    async def main() -> None:
        try:
            await fn(made)
        finally:
            await made.adapter.disconnect()

    asyncio.run(main())


def key(event: InboundEvent) -> str:
    return build_session_key(event.source)


# -- connect ------------------------------------------------------------------------------------


def test_intents_ask_for_members_only_with_a_role_allowlist() -> None:
    plain, roles = world(), world(env={ROLES_ENV: f"{ROLE_OK}"})

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())

    run(body, plain)
    run(body, roles)
    for made, members in ((plain, False), (roles, True)):
        intents = made.wire.client.intents
        assert intents.message_content and intents.dm_messages and intents.guild_messages
        assert intents.members is members
        assert made.wire.gateway_sessions == 1
        assert made.wire.client.options["max_ratelimit_timeout"] == 30.0


@pytest.mark.parametrize(
    ("error", "retryable", "code"),
    [
        (LoginFailure("Improper token has been passed."), False, "auth_failed"),
        (PrivilegedIntentsRequired("shard requires intents"), False, "intents_required"),
        (ConnectionResetError("reset by peer"), True, "connect_failed"),
    ],
)
def test_connect_errors_are_classified(error: Exception, retryable: bool, code: str) -> None:
    made = world()
    made.wire.login_error = error

    async def body(made: World) -> None:
        with pytest.raises(AdapterConnectError) as raised:
            await made.settle(made.adapter.connect())
        assert raised.value.retryable is retryable and raised.value.code == code
        assert not made.adapter.connected

    run(body, made)


def test_the_token_never_shows_in_a_connect_error() -> None:
    made = world()
    made.wire.login_error = RuntimeError(f"bad header Authorization: Bot {fake_token()}")

    async def body(made: World) -> None:
        with pytest.raises(AdapterConnectError) as raised:
            await made.settle(made.adapter.connect())
        assert fake_token() not in str(raised.value)
        assert "Authorization" in str(raised.value)

    run(body, made)


def test_no_token_is_a_fatal_configuration_problem() -> None:
    clock = FakeClock(START)
    wire = DiscordWire(FakeChatPlatform("discord", clock=clock, bot_user_id="900"))
    bare = PlatformSettings("discord", True, "credentials", PlatformSection())
    adapter = DiscordAdapter(clock=clock, settings=bare, sdk=wire.sdk)

    async def main() -> None:
        with pytest.raises(AdapterConnectError) as raised:
            await adapter.connect()
        assert raised.value.code == "missing_credentials" and not raised.value.retryable

    asyncio.run(main())
    assert wire.logins == 0


# -- inbound ------------------------------------------------------------------------------------


def test_own_system_duplicate_and_unchanged_edit_messages_are_ignored() -> None:
    made = world()

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        p = made.platform
        p.bot_message("300", "900", "my own words")  # the bot itself
        first = p.dm("200", "hello")
        await made.pump()
        message = made.wire.logs[200][-1]
        await made.adapter._on_message(message)  # the same id again
        message.type = type("T", (), {"name": "pins_add"})()
        await made.adapter._on_edit(message, message)
        p.edited(first, "hello again")
        events = await made.pump()
        assert [e.text for e in events] == ["hello", "hello again"]
        assert events[1].metadata["edited"] is True
        assert events[0].reply_expected is True and events[0].source.chat_type == "dm"

    run(body, made)


def test_attachments_are_listed_with_their_kind() -> None:
    made = world()

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        made.platform.voice("200", "200", duration=4.5)
        made.platform.photo("200", "200", caption="look")
        events = await made.pump()
        voice, photo = events
        assert voice.kind is MessageKind.VOICE and voice.metadata["duration"] == 4.5
        assert voice.media[0].ref.startswith("https://cdn.discordapp.com/attachments/")
        assert photo.kind is MessageKind.PHOTO and photo.text == "look"

    run(body, made)


def test_role_grant_on_guild_messages_only() -> None:
    made = world(env={ROLES_ENV: f"{ROLE_OK}"})

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        made.platform.add_chat("300", "group", guild_id=GUILD)
        made.wire.member_roles["200"] = (ROLE_OK,)
        made.wire.member_roles["201"] = (ROLE_OTHER,)
        made.platform.group("300", "200", "hi", mention=True)
        made.platform.group("300", "201", "hi too", mention=True)
        made.platform.dm("200", "in private")
        events = await made.pump()
        grants = {
            f"{e.source.user_id}:{e.source.chat_type}": e.source.role_authorized for e in events
        }
        assert grants == {"200:thread": True, "201:thread": False, "200:dm": False}
        assert {e.source.guild_id for e in events if e.source.chat_type != "dm"} == {GUILD}

    run(body, made)


def test_thread_spawned_by_a_mention_shares_the_session_with_follow_ups() -> None:
    made = world()

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        p = made.platform
        asked = p.group("300", "200", "<@900> please summarise the logs", mention=True)
        (first,) = await made.pump()
        assert first.source.chat_type == "thread"
        assert first.source.prospective_thread_id == asked.message_id
        assert first.source.thread_id == asked.message_id
        assert first.text == "please summarise the logs"
        assert made.wire.threads == []  # nothing is opened before we answer

        result = await made.adapter.send(
            "300", "on it", reply_to=asked.message_id, meta={"thread_id": asked.message_id}
        )
        assert result.ok
        assert made.wire.threads == [(int(asked.message_id), "please summarise the logs", 1440)]
        sent = p.ops_of("send")[-1]
        assert sent.chat_id == asked.message_id and sent.reply_to is None

        p.thread("300", asked.message_id, "201", "and the errors?")  # no mention needed now
        (_, follow) = await made.pump()
        assert follow.metadata.get("mentioned") is True
        assert key(follow) == key(first) == f"@default:discord:thread:300:{asked.message_id}"

    run(body, made)


def test_a_mention_that_is_never_answered_opens_no_thread() -> None:
    made = world()

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        made.platform.group("300", "200", "hello?", mention=True)
        await made.pump()
        assert made.wire.threads == [] and made.platform.ops_of("send") == []

    run(body, made)


@pytest.mark.parametrize(
    ("extra", "text", "reply_to", "threaded"),
    [
        ({}, "/status", None, False),  # commands are answered where they were typed
        ({"free_response_channels": "300"}, "hi", None, False),
        ({"no_thread_channels": ["300"]}, "hi", None, False),
        ({"auto_thread": False}, "hi", None, False),
        ({}, "hi", "1", False),  # a reply stays a reply
        ({}, "hi", None, True),
    ],
)
def test_when_a_mention_opens_a_thread(
    extra: dict[str, object], text: str, reply_to: str | None, threaded: bool
) -> None:
    made = world(extra=extra)

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        made.platform.group("300", "200", text, mention=True, reply_to=reply_to)
        (event,) = await made.pump()
        assert (event.source.chat_type == "thread") is threaded
        assert (event.source.prospective_thread_id is not None) is threaded

    run(body, made)


def test_failed_thread_creation_answers_in_the_channel() -> None:
    made = world()
    made.wire.thread_failures = 1

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        asked = made.platform.group("300", "200", "help", mention=True)
        (event,) = await made.pump()
        meta = {"thread_id": event.source.thread_id}
        first = await made.adapter.send("300", "answer", reply_to=asked.message_id, meta=meta)
        second = await made.adapter.send("300", "more", meta=meta)
        assert first.ok and second.ok
        sends = made.platform.ops_of("send")
        assert [(s.chat_id, s.reply_to) for s in sends] == [
            ("300", asked.message_id),
            ("300", None),
        ]
        assert made.wire.threads == []

    run(body, made)


def test_history_backfill_on_a_mention_in_a_busy_channel() -> None:
    made = world()

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        p = made.platform
        p.group("300", "201", "old message before our reply")
        await made.pump()
        await made.adapter.send("300", "our last answer")
        for n in range(25):
            p.group("300", "201", f"chatter {n:02d}", user_name="Bo")
        await made.pump()
        made.events.clear()
        p.group("300", "200", "what do you think?", mention=True)
        (event,) = await made.pump()
        lines = event.text.splitlines()
        assert lines[0] == "[Recent messages in this channel, oldest first]"
        assert lines[1:21] == [f"[Bo] chatter {n:02d}" for n in range(5, 25)]
        assert "old message" not in event.text and "our last answer" not in event.text
        assert event.text.endswith("[The message addressed to you]\nwhat do you think?")
        assert event.metadata["channel_context"] is True

    run(body, made)


def test_no_backfill_in_a_free_response_channel() -> None:
    made = world(extra={"free_response_channels": ["300"]})

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        made.platform.group("300", "201", "chatter")
        made.platform.group("300", "200", "and you?", mention=True)
        events = await made.pump()
        assert [e.text for e in events] == ["chatter", "and you?"]

    run(body, made)


def test_a_bare_mention_counts_only_with_context_around_it() -> None:
    made = world()

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        made.platform.group("300", "200", "<@900>", mention=True)
        assert await made.pump() == []
        made.platform.group("300", "201", "is the build green?")
        made.platform.group("300", "200", "<@900>", mention=True)
        events = await made.pump()
        assert events[-1].text.endswith("[They mentioned you without writing anything]")
        assert "[201] is the build green?" in events[-1].text

    run(body, made)


def test_threads_the_bot_opened_stay_addressed_after_a_restart() -> None:
    made = world()

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        asked = made.platform.group("300", "200", "start", mention=True)
        await made.pump()
        await made.adapter.send("300", "ok", meta={"thread_id": asked.message_id})
        await made.adapter.disconnect()
        fresh = DiscordAdapter(
            clock=made.clock,
            settings=settings(command_sync="off"),
            sdk=made.wire.sdk,
        )
        seen: list[InboundEvent] = []

        async def sink(event: InboundEvent) -> None:
            seen.append(event)

        fresh.set_sink(sink)
        await made.settle(fresh.connect())
        made.platform.thread("300", asked.message_id, "201", "follow-up")
        await made.wire.dispatch_pending()
        assert seen[-1].metadata.get("mentioned") is True
        await fresh.disconnect()

    run(body, made)


# -- interactions -------------------------------------------------------------------------------


def _guild_world(**kw: Any) -> World:
    made = world(env={ROLES_ENV: f"{ROLE_OK}"}, **kw)
    made.platform.add_chat("300", "group", guild_id=GUILD)
    made.wire.member_roles["200"] = (ROLE_OK,)
    made.wire.member_roles["201"] = (ROLE_OTHER,)
    return made


def test_component_press_by_a_member_without_an_allowed_role_is_refused() -> None:
    made = _guild_world()
    data = approval_callback("req1", "once")

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        made.platform.button_press("300", "201", data, chat_kind="group")
        await made.pump()
        assert made.events == []
        assert made.platform.answered[-1][1] == REFUSED_PRESS

        made.platform.button_press("300", "200", data, chat_kind="group")
        (event,) = await made.pump()
        assert event.metadata["button"] == data and event.source.role_authorized
        assert made.platform.answered[-1][1] is None  # acknowledged silently

    run(body, made)


def test_an_application_command_by_a_member_without_the_role_is_refused() -> None:
    made = _guild_world()

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        refused = await made.wire.invoke_command("300", "201", "status")
        assert made.events == [] and refused.response.sent == [(REFUSED_COMMAND, True)]
        await made.wire.invoke_command("300", "200", "status")
        (event,) = made.events
        assert event.command == "status" and event.source.role_authorized

    run(body, made)


def test_a_paired_or_listed_user_may_press_without_the_role() -> None:
    async def paired(user_id: str) -> bool:
        return user_id == "202"

    made = _guild_world(is_paired=paired)
    made.adapter._env = {**made.adapter._env, "INDUS_DISCORD_ALLOWED_USERS": "203"}
    data = approval_callback("req1", "deny")

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        for user in ("202", "203", "204"):
            made.platform.button_press("300", user, data, chat_kind="group")
        events = await made.pump()
        assert [e.source.user_id for e in events] == ["202", "203"]

    run(body, made)


def test_select_menu_values_become_the_button_data() -> None:
    made = world()

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        choices = tuple(f"option {n}" for n in range(30))
        prompt = ClarifyPrompt("q1", "key", "Which one?", choices, fallback_text="1. ...")
        result = await made.adapter.send_clarify("300", prompt)
        assert result.ok
        view = made.wire.sent_views[-1]
        assert view is not None and view.stopped
        (select,) = view.items
        assert isinstance(select, UISelect) and len(select.options) == 25
        assert select.options[3].value == clarify_callback("q1", 3)
        made.platform.button_press("300", "200", "ignored", chat_kind="group")
        item = made.platform.next_inbound()
        assert item is not None
        message = made.wire._message(item)
        from tests.fakes.platform.discord_wire import Interaction

        press = Interaction(
            "cb1",
            message.author,
            message.channel,
            {"custom_id": "x", "values": [select.options[3].value]},
        )
        await made.adapter._on_interaction(press)
        assert made.events[-1].metadata["button"] == clarify_callback("q1", 3)

    run(body, made)


def test_clarify_and_approval_buttons_fill_rows_of_five() -> None:
    made = world()

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        prompt = ClarifyPrompt("q2", "key", "Pick", tuple(f"c{n}" for n in range(12)))
        assert (await made.adapter.send_clarify("300", prompt)).ok
        view = made.wire.sent_views[-1]
        assert view is not None
        assert [b.row for b in view.items] == [n // 5 for n in range(12)]
        long = "x" * 120
        card = ApprovalPrompt(
            "r9", "key", "Run it?", "Run it? /approve", (ApprovalAction(long, "once", "primary"),)
        )
        assert (await made.adapter.send_approval("300", card)).ok
        view = made.wire.sent_views[-1]
        assert view is not None
        (button,) = view.items
        assert len(button.label) == 80 and button.style == "primary"

    run(body, made)


def test_an_application_command_becomes_command_text() -> None:
    made = world()

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        interaction = await made.wire.invoke_command("300", "200", "model", "gpt --once")
        (event,) = made.events
        assert event.text == "/model gpt --once" and event.command == "model"
        assert event.source.chat_id == "300" and event.source.chat_type == "group"
        assert interaction.response.sent == [("Running /model…", True)]

    run(body, made)


# -- outbound -----------------------------------------------------------------------------------


def test_sends_allow_no_mentions_by_default() -> None:
    plain = world()
    opted = world(extra={"allowed_mentions": {"users": True}})

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        assert (await made.adapter.send("300", "@everyone hello")).ok

    run(body, plain)
    run(body, opted)
    assert plain.wire.sent_allowed_mentions[-1] == plain.wire.sdk.AllowedMentions.none()
    sent = opted.wire.sent_allowed_mentions[-1]
    assert sent is not None and sent.users and not sent.everyone and not sent.roles


def test_a_long_rate_limit_is_a_typed_failure_without_sleeping() -> None:
    made = world()

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        made.platform.fail(FailureKind.RATE_LIMIT, op="send", retry_after=97 * 60)
        before = made.clock.monotonic()
        result = await made.adapter.send_with_retry("300", "hello", rng=lambda: 0.0)
        assert not result.ok and result.kind is ErrorKind.RATE_LIMITED
        assert result.retry_after == 97 * 60
        assert made.clock.monotonic() == before

    run(body, made)


def test_an_edit_past_the_cap_continues_in_new_messages() -> None:
    made = world()

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        sent = await made.adapter.send("300", "draft")
        assert sent.ok and sent.message_id
        words = " ".join(f"w{n:04d}" for n in range(500))  # about 2,500 characters
        result = await made.adapter.edit("300", sent.message_id, words, final=True)
        assert result.ok and len(result.ids) == 2
        texts = made.platform.texts("300")
        assert all(len(t) <= MAX_LEN for t in texts)
        assert texts[0].startswith("w0000") and texts[-1].removesuffix(" (2/2)").endswith("w0499")

    run(body, made)


def test_edits_and_deletes_inside_a_thread_find_their_message() -> None:
    made = world()

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        made.platform.thread("300", "400", "200", "hi")
        await made.pump()
        sent = await made.adapter.send("300", "draft", meta={"thread_id": "400"})
        assert sent.ok and sent.message_id
        assert (await made.adapter.edit("300", sent.message_id, "final", final=True)).ok
        assert await made.adapter.delete("300", sent.message_id)
        assert [op.kind for op in made.platform.ops_of("send", "edit", "delete")] == [
            "send",
            "edit",
            "delete",
        ]
        assert {op.chat_id for op in made.platform.ops_of("send", "edit", "delete")} == {"400"}

    run(body, made)


# -- application commands -----------------------------------------------------------------------


def _specs(count: int) -> list[cmds.CommandSpec]:
    return [cmds.CommandSpec(f"cmd{n:03d}", f"command {n}") for n in range(count)]


def test_command_specs_follow_discord_limits() -> None:
    specs = cmds.registry_commands()
    assert specs and len(specs) <= cmds.MAX_COMMANDS
    for spec in specs:
        assert len(spec.name) <= 32 and spec.name == spec.name.lower()
        assert 1 <= len(spec.description) <= 100
        for option in spec.payload().get("options", []):
            assert option["name"] == "args" and len(option["description"]) <= 100
    odd = cmds.desired_commands([("Bad Name!", "x", ""), ("ok-name", "d" * 300, "a" * 300)])
    names = [s.name for s in odd]
    assert "ok-name" in names and not any(" " in n for n in names)
    ok = next(s for s in odd if s.name == "ok-name")
    assert len(ok.description) == 100 and ok.description.endswith("…")


def test_command_sync_caps_at_100_with_one_warning(caplog: pytest.LogCaptureFixture) -> None:
    base = len(cmds.registry_commands())
    extra = tuple((f"extra{n:03d}", f"extra command {n}", "") for n in range(101 - base))
    made = world(extra_commands=extra, sync=True)
    caplog.set_level(logging.WARNING, logger="indus.gateway.adapters.discord")

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        task = made.adapter._sync_task
        assert task is not None
        await made.settle(task, step=5.0)

    run(body, made)
    assert len(made.wire.client.http.commands) == 100
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "1 command(s) left out" in warnings[0].getMessage()
    assert cmds.cap_commands(_specs(101))[-1].name == "cmd099"


def test_command_sync_paces_mutations_and_deletes_first() -> None:
    made = world(sync=True)
    http = made.wire.client.http
    wanted = {s.name: s for s in cmds.registry_commands()}
    first, second = list(wanted)[:2]
    http.commands = {
        "1": {"id": "1", "type": 1, "name": "gone", "description": "obsolete"},
        "2": {**wanted[first].payload(), "id": "2"},  # unchanged
        "3": {**wanted[second].payload(), "id": "3", "description": "old words"},
        "4": {"id": "4", "type": 2, "name": "Context menu", "description": ""},
    }

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        task = made.adapter._sync_task
        assert task is not None
        await made.settle(task, step=0.5)

    run(body, made)
    ops = [(op, name) for op, name, _ in http.mutations]
    assert ops[0] == ("delete", "gone")
    assert ("edit", second) in ops and all(name != first for _, name in ops)
    assert all(name != "Context menu" for _, name in ops)
    assert len(ops) == 1 + 1 + (len(wanted) - 2)
    times = [at for _, _, at in http.mutations]
    gaps = {round(b - a, 3) for a, b in zip(times, times[1:], strict=False)}
    assert gaps == {cmds.MUTATION_GAP_S}


def test_disconnect_cancels_a_running_sync() -> None:
    made = world(sync=True)

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        await made.clock.advance_and_settle(10.0)
        task = made.adapter._sync_task
        assert task is not None and not task.done()
        await made.adapter.disconnect()
        assert task.cancelled()
        count = len(made.wire.client.http.mutations)
        await made.clock.advance_and_settle(100.0)
        assert len(made.wire.client.http.mutations) == count < 10

    run(body, made)


# -- send-only and media ------------------------------------------------------------------------


def test_indus_send_opens_discord_send_only(
    capsys: pytest.CaptureFixture[str], scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets: Any = scope.secrets
    secrets.set("DISCORD_BOT_TOKEN", fake_token())
    write_settings(scope, "platforms:\n  discord:\n    command_sync: diff\n")
    wire = DiscordWire(FakeChatPlatform("discord", max_len=MAX_LEN, bot_user_id="900"))
    waiting = wire.platform.dm("200", "an inbound message nobody should take")
    built: list[DiscordAdapter] = []

    def factory(config: AdapterConfig) -> DiscordAdapter:
        adapter = create_adapter(config, sdk=wire.sdk)
        built.append(adapter)
        return adapter

    entry = PlatformEntry("discord", "Discord", factory, lambda: True)
    registry = PlatformRegistry([entry])
    real = send_command.build_adapter

    def build(scope: Scope, name: str, settings: PlatformSettings, *_: Any) -> Any:
        return real(scope, name, settings, registry)

    monkeypatch.setattr(send_command, "ADAPTER_BUILDER", build)
    ran = run_cli(capsys, scope, "send", "discord:300", "deploy done")
    assert ran.code == 0, ran.err
    (adapter,) = built
    assert adapter.send_only and not adapter.connected
    assert wire.platform.texts("300") == ["deploy done"]
    assert wire.logins == 1 and wire.gateway_sessions == 0
    assert wire.client.handlers == {} and wire.client.http.mutations == []
    assert wire.platform.next_inbound() == waiting  # still waiting: nothing was consumed
    assert wire.tokens == [fake_token()]


def _cdn(handler: Callable[[httpx.Request], httpx.Response]) -> DiscordAdapter:
    clock = FakeClock(START)
    return DiscordAdapter(
        clock=clock, settings=settings(), fetch_transport=httpx.MockTransport(handler)
    )


def test_the_media_fetcher_streams_discord_cdn_files_only(scope: Scope) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=b"OggS" + b"\x00" * 64)

    adapter = _cdn(handler)

    async def collect(ref: str) -> bytes:
        return b"".join([part async for part in adapter.fetch_attachment(Attachment(ref=ref))])

    url = "https://cdn.discordapp.com/attachments/1/2/voice-message.ogg"
    assert asyncio.run(collect(url)).startswith(b"OggS") and seen == [url]
    for bad in ("http://cdn.discordapp.com/a.ogg", "https://example.com/a.ogg", "/etc/passwd"):
        with pytest.raises(ValueError):
            asyncio.run(collect(bad))
    assert seen == [url]

    intake = MediaIntake(scope)
    undo = adapter.register_fetchers(intake)
    assert undo is not None
    assert intake._fetchers["discord"] == adapter.fetch_attachment
    undo()
    assert "discord" not in intake._fetchers


def test_a_cdn_error_status_fails_the_download() -> None:
    adapter = _cdn(lambda request: httpx.Response(403))

    async def collect() -> None:
        async for _ in adapter.fetch_attachment(Attachment(ref="https://cdn.discordapp.com/x")):
            pass

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(collect())


def test_the_core_never_addresses_a_discord_thread_before_the_adapter_opens_it() -> None:
    """Discord's prospective thread does not exist until the adapter opens it on the first reply:
    the core keeps sending to the channel (``reply_opens_thread`` off); a real thread is kept."""
    from indus.gateway.core import prompts as prompts_mod
    from indus.gateway.core.events import Source
    from indus.gateway.outbound.ports import typing_meta
    from indus.gateway.outbound.replies import final_reply_for

    adapter = world().adapter
    caps = adapter.capabilities
    assert not caps.reply_opens_thread
    top = Source(
        platform="discord",
        chat_id="300",
        chat_type="group",
        user_id="200",
        prospective_thread_id="555",
    )
    meta = prompts_mod._meta  # pyright: ignore[reportPrivateUsage]
    reply = final_reply_for(
        top, "hi", reply_to="555", kind="final", opens_thread=caps.reply_opens_thread
    )
    assert reply.thread_id is None
    assert meta(top, adapter) == {} and typing_meta(adapter, top) == {}
    inside = top.model_copy(update={"thread_id": "777", "chat_type": "thread"})
    threaded = final_reply_for(inside, "hi", reply_to=None, kind="final", opens_thread=False)
    assert threaded.thread_id == "777"
    assert meta(inside, adapter) == {"thread_id": "777"}
    assert typing_meta(adapter, inside) == {"thread_id": "777"}
