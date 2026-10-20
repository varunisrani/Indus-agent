"""The WhatsApp bridge adapter over the fake bridge (P16-03)."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import stat
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import Scope
from indus.gateway.adapters.base import AdapterConnectError
from indus.gateway.adapters.whatsapp import create_adapter, default_bridge_script
from indus.gateway.adapters.whatsapp.adapter import (
    MEDIA_REF,
    WhatsAppAdapter,
    WhatsAppOptions,
    http_probe,
)
from indus.gateway.adapters.whatsapp.sidecar import SidecarOwner, state_dir
from indus.gateway.config import AdapterConfig, PlatformSettings
from indus.gateway.core.events import InboundEvent, MessageKind
from indus.gateway.core.send import ErrorKind
from indus.gateway.intake.media import MediaRejected
from tests.fakes.clock import FakeClock
from tests.fakes.platform.whatsapp_wire import (
    BOT_JID,
    BOT_LID,
    FakeBridge,
    FakeLauncher,
    FakeSpawner,
)

ANA = "15550000200@s.whatsapp.net"
ANA_LID = "77700000200@lid"
GROUP = "120363000000000300@g.us"


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))


class Rig:
    def __init__(self, *, options: WhatsAppOptions | None = None, **kw: Any) -> None:
        self.clock = _clock()
        self.bridge = FakeBridge()
        self.bridge.platform.clock = self.clock
        self.launcher = kw.pop("launcher", None) or FakeLauncher(self.bridge)
        self.events: list[InboundEvent] = []
        self.fatal: list[tuple[str, str]] = []
        self.adapter = WhatsAppAdapter(
            clock=self.clock,
            launcher=self.launcher,
            options=options or WhatsAppOptions(),
            transport=self.bridge.transport,
            **kw,
        )

        async def sink(event: InboundEvent) -> None:
            self.events.append(event)

        self.adapter.set_sink(sink)
        self.adapter.set_fatal_listener(lambda code, msg: self.fatal.append((code, msg)))

    async def step(self, seconds: float = 1.0, times: int = 3) -> None:
        for _ in range(times):
            await self.clock.advance_and_settle(seconds)

    async def settle[T](self, awaitable: Awaitable[T]) -> T:
        """Await ``awaitable`` while moving the clock past its sleeps (send pacing)."""
        task = asyncio.ensure_future(awaitable)
        for _ in range(400):
            await self.clock.run_until_idle()
            if task.done():
                return task.result()
            self.clock.advance(0.1)
        raise AssertionError("the call never finished on the fake clock")

    async def connect(self) -> None:
        task = asyncio.ensure_future(self.adapter.connect())
        for _ in range(200):
            await self.clock.run_until_idle()
            if task.done():
                break
            self.clock.advance(0.5)
        await task


def run(main: Callable[[Rig], Awaitable[None]], **kw: Any) -> None:
    async def outer() -> None:
        rig = Rig(**kw)
        try:
            await main(rig)
        finally:
            await rig.adapter.disconnect()

    asyncio.run(outer())


def group_event(text: str, **extra: Any) -> dict[str, Any]:
    return {"chat": GROUP, "sender": ANA, "group": True, "text": text, **extra}


# -- groups and policies ---------------------------------------------------------------------------


def test_a_group_message_without_a_mention_is_ignored() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        rig.bridge.push_event(**group_event("just chatting", id="G0"))
        rig.bridge.push_event(**group_event("/status", id="G1"))
        rig.bridge.push_event(**group_event("@15550000999 hi there", id="G2", mentionsMe=True))
        rig.bridge.push_event(
            **group_event("yes", id="G3", quoted={"id": "OUT1", "sender": BOT_JID, "fromMe": True})
        )
        rig.bridge.push_event(**group_event("hey indus, help", id="G4"))
        await rig.step()
        assert [e.message_id for e in rig.events] == ["G1", "G2", "G3", "G4"]
        assert rig.events[1].text == "hi there"
        assert rig.events[1].metadata.get("mentioned") is True
        assert rig.events[2].reply_to_is_own
        assert rig.events[0].source.chat_type == "group"

    run(
        main,
        options=WhatsAppOptions(
            mention_patterns=WhatsAppOptions.read(
                {"mention_patterns": [r"\bindus\b"]}
            ).mention_patterns
        ),
    )


def test_free_response_chats_and_require_mention_off_take_every_group_message() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        rig.bridge.push_event(**group_event("anything", id="F1"))
        await rig.step()
        assert [e.message_id for e in rig.events] == ["F1"]

    run(main, options=WhatsAppOptions.read({"free_response_chats": [GROUP]}))
    run(main, options=WhatsAppOptions.read({"require_mention": "false"}))


def test_disabled_and_allowlist_policies() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        rig.bridge.push_event(id="D1", chat=ANA, sender=ANA, text="hi")
        rig.bridge.push_event(**group_event("/a", id="G1"))
        rig.bridge.push_event(**group_event("/b", id="G2", sender="15550000300@s.whatsapp.net"))
        rig.bridge.push_event(
            **group_event(
                "/c", id="G3", chat="120363000000000301@g.us", sender="15550000300@s.whatsapp.net"
            )
        )
        await rig.step()
        assert [e.message_id for e in rig.events] == ["G1", "G3"]

    options = WhatsAppOptions.read(
        {
            "dm_policy": "disabled",
            "group_policy": "allowlist",
            "group_allow_from": ["+1 555 000 0200", "120363000000000301@g.us"],
        }
    )
    run(main, options=options)
    assert options.dm_policy == "disabled"


def test_the_core_reads_the_own_policy_and_allowlists_match_any_alias() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        assert rig.adapter.capabilities.owns_access_policy
        assert rig.adapter.access_policy("dm") == "allowlist"
        assert rig.adapter.access_policy("group") == "open"
        rig.bridge.push_event(id="L1", chat=ANA_LID, sender=ANA_LID, senderAlt=ANA, text="hi")
        await rig.step()
        event = rig.events[0]
        assert event.user_id == "15550000200"
        assert event.source.user_id_alt == "77700000200"
        assert event.source.chat_id == ANA_LID
        assert rig.adapter.allows_dm("77700000200")
        assert rig.adapter.allows_dm(ANA)
        assert not rig.adapter.allows_dm("15550000300")

    run(main, options=WhatsAppOptions.read({"dm_policy": "allowlist", "allow_from": "15550000200"}))


def test_lid_links_come_from_the_bridges_mapping_files(tmp_path: Path) -> None:
    auth = tmp_path / "auth"
    auth.mkdir()
    (auth / "lid-mapping-15550000200.json").write_text(json.dumps("77700000200"))

    async def main(rig: Rig) -> None:
        await rig.connect()
        rig.bridge.push_event(id="L1", chat=ANA_LID, sender="77700000200:5@lid", text="hi")
        await rig.step()
        assert rig.events[0].user_id == "15550000200"

    run(main, auth_dir=auth)


def test_broadcasts_are_dropped_and_own_self_chat_messages_are_marked() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        rig.bridge.push_event(id="S1", chat="status@broadcast", sender=ANA)
        rig.bridge.push_event(id="N1", chat="1203630000@newsletter", sender=ANA)
        rig.bridge.push_event(id="M1", chat=BOT_JID, sender=BOT_JID, fromMe=True, text="note")
        rig.bridge.push_event(id="E1", chat=ANA, sender=ANA, text="fixed", edited=True)
        rig.bridge.push_event(id="X1", chat=GROUP, sender=GROUP, group=True, text="/x")
        await rig.step()
        assert [e.message_id for e in rig.events] == ["M1", "E1"]
        assert rig.events[0].metadata.get("note_to_self") is True
        assert rig.events[1].metadata.get("edited") is True

    run(main)


# -- connect, pairing, send-only -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code"),
    [("pairing", "whatsapp_not_paired"), ("logged_out", "whatsapp_logged_out")],
)
def test_an_unpaired_or_logged_out_session_is_a_fatal_connect(status: str, code: str) -> None:
    async def main(rig: Rig) -> None:
        rig.bridge.status = status
        with pytest.raises(AdapterConnectError) as raised:
            await rig.connect()
        assert raised.value.code == code
        assert not raised.value.retryable
        assert not rig.adapter.connected
        assert isinstance(rig.launcher, FakeLauncher)
        assert rig.launcher.stops == 1

    run(main)


def test_a_bridge_still_connecting_is_waited_for() -> None:
    async def main(rig: Rig) -> None:
        rig.bridge.status = "connecting"
        task = asyncio.ensure_future(rig.adapter.connect())
        await rig.clock.advance_and_settle(2.0)
        assert not task.done()
        rig.bridge.status = "open"
        await rig.clock.advance_and_settle(0.5)
        await task
        assert rig.adapter.connected

    run(main)


def test_send_only_uses_the_running_bridge_and_never_polls() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        result = await rig.settle(rig.adapter.send(ANA, "hello"))
        assert result.ok
        await rig.step(times=5)
        assert isinstance(rig.launcher, FakeLauncher)
        assert (rig.launcher.attaches, rig.launcher.ensures) == (1, 0)
        assert rig.bridge.paths("/events") == []
        await rig.adapter.disconnect()
        assert rig.launcher.stops == 0

    run(main, send_only=True)


def test_create_adapter_builds_an_owner_with_a_minimal_child_env(scope: Scope) -> None:
    scope = scope.with_env(scope.env.with_overlay({"OPENAI_API_KEY": "x" * 12, "PATH": "/bin"}))
    section = PlatformSection.model_validate({"enabled": True, "dm_policy": "open"})
    settings = PlatformSettings("whatsapp", True, "enabled in config", section)
    config = AdapterConfig("whatsapp", settings, scope, scope.clock, send_only=True)
    adapter = create_adapter(config)
    assert isinstance(adapter, WhatsAppAdapter)
    assert adapter.send_only
    assert adapter.options.dm_policy == "open"
    owner = adapter.launcher
    assert isinstance(owner, SidecarOwner)
    assert owner.auth_dir == state_dir(scope.home, scope.profile_id) / "auth"
    env = owner.env_for({"INDUS_WA_TOKEN": "t" * 40})
    assert env["INDUS_WA_TOKEN"] == "t" * 40
    assert env["PATH"] == "/bin"
    assert "OPENAI_API_KEY" not in env
    assert "INDUS_HOME" not in env
    script = default_bridge_script()
    assert script is not None and script.name == "main.ts"


def test_send_only_without_a_running_bridge_fails_and_spawns_nothing(tmp_path: Path) -> None:
    clock = _clock()
    bridge = FakeBridge()
    bridge.platform.clock = clock
    bridge.alive = False
    spawner = FakeSpawner(bridge)
    owner = SidecarOwner(
        home=tmp_path,
        root=tmp_path,
        profile_id="default",
        clock=clock,
        probe=http_probe(bridge.transport),
        node="/usr/bin/node",
        script=Path(__file__),
        env_for=lambda extra: dict(extra),
        spawner=spawner,
        start_time=spawner.start_time,
        alive_check=spawner.alive,
        kill=spawner.kill,
    )
    adapter = WhatsAppAdapter(clock=clock, launcher=owner, send_only=True)

    async def main() -> None:
        with pytest.raises(AdapterConnectError) as raised:
            await adapter.connect()
        assert raised.value.code == "whatsapp_bridge_not_running"
        assert spawner.children == []

    asyncio.run(main())


# -- crash restarts --------------------------------------------------------------------------------


def test_a_crashed_bridge_restarts_with_a_capped_doubling_wait(tmp_path: Path) -> None:
    clock = _clock()
    bridge = FakeBridge()
    bridge.platform.clock = clock
    bridge.alive = False
    spawner = FakeSpawner(bridge)
    owner = SidecarOwner(
        home=tmp_path,
        root=tmp_path,
        profile_id="default",
        clock=clock,
        probe=http_probe(bridge.transport),
        node="/usr/bin/node",
        script=Path(__file__),
        env_for=lambda extra: dict(extra),
        spawner=spawner,
        start_time=spawner.start_time,
        alive_check=spawner.alive,
        kill=spawner.kill,
        is_free=lambda port: not bridge.alive,
    )
    adapter = WhatsAppAdapter(clock=clock, launcher=owner, transport=bridge.transport)
    fatal: list[str] = []
    adapter.set_fatal_listener(lambda code, msg: fatal.append(code))

    async def main() -> None:
        await adapter.connect()
        assert len(spawner.children) == 1
        crashed_at = clock.monotonic()
        spawner.crash_at_start = 6
        spawner.crash()
        for _ in range(400):
            await clock.advance_and_settle(0.5)
            if len(spawner.children) == 8:
                break
        starts = [crashed_at, *spawner.times[1:]]
        gaps = [round(b - a) for a, b in itertools.pairwise(starts)]
        assert gaps == [2, 4, 8, 16, 32, 60, 60]
        assert bridge.alive and adapter.restarts == 1
        # Stable for a minute: the next crash waits 2 s again.
        for _ in range(130):
            await clock.advance_and_settle(0.5)
        spawner.crash()
        crashed_at = clock.monotonic()
        for _ in range(20):
            await clock.advance_and_settle(0.5)
            if len(spawner.children) == 9:
                break
        # A 2 s wait, plus at most the 1 s the poll in flight takes to notice the crash.
        assert 2 <= round(spawner.times[-1] - crashed_at) <= 3
        bridge.push_event(id="AFTER", chat=ANA, sender=ANA, text="still here")
        received: list[InboundEvent] = []

        async def sink(event: InboundEvent) -> None:
            received.append(event)

        adapter.set_sink(sink)
        for _ in range(4):
            await clock.advance_and_settle(1.0)
        assert [e.message_id for e in received] == ["AFTER"]
        assert fatal == []
        await adapter.disconnect()
        assert spawner.children[-1].signals

    asyncio.run(main())


def test_a_refused_token_while_polling_is_fatal() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        rig.bridge.token = "other-" + "z" * 10
        await rig.step()
        assert [c for c, _ in rig.fatal] == ["whatsapp_bridge_port_taken"]

    run(main)


# -- outbound --------------------------------------------------------------------------------------


def test_sends_quote_the_first_piece_and_use_whatsapp_markup() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        result = await rig.settle(
            rig.adapter.send("+1 555 000 0200", "**bold** reply", reply_to="IN7")
        )
        assert result.ok
        body = rig.bridge.paths("/send")[0].body
        assert body == {"chat": ANA, "text": "*bold* reply", "replyTo": "IN7"}
        plain = await rig.settle(rig.adapter.send(GROUP, "**as is**", meta={"plain": True}))
        assert plain.ok
        assert rig.bridge.paths("/send")[1].body["text"] == "**as is**"

    run(main)


def test_media_goes_by_resolved_path(tmp_path: Path) -> None:
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-1.4")

    async def main(rig: Rig) -> None:
        await rig.connect()
        result = await rig.settle(
            rig.adapter.send_document(ANA, str(doc), caption="**read**", reply_to="IN1")
        )
        assert result.ok
        voice = await rig.settle(rig.adapter.send_voice(ANA, str(doc)))
        assert voice.ok
        bodies = [c.body for c in rig.bridge.paths("/send-media")]
        assert bodies[0] == {
            "chat": ANA,
            "path": str(doc.resolve()),
            "kind": "document",
            "caption": "*read*",
            "fileName": "report.pdf",
            "replyTo": "IN1",
        }
        assert bodies[1]["kind"] == "voice"

    run(main)


def test_bridge_errors_are_classified() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        rig.bridge.status = "connecting"
        result = await rig.settle(rig.adapter.send(ANA, "x"))
        assert not result.ok and result.retryable and result.kind is ErrorKind.TRANSIENT
        rig.bridge.status = "open"
        rig.adapter._bridge = None  # pyright: ignore[reportPrivateUsage]
        gone = await rig.settle(rig.adapter.send(ANA, "x"))
        assert gone.retryable

    run(main)


def test_typing_reactions_and_read_receipts() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        await rig.adapter.typing_start(ANA)
        await rig.adapter.typing_stop(ANA)
        assert [c.body["state"] for c in rig.bridge.paths("/typing")] == ["composing", "paused"]
        assert await rig.adapter.react(GROUP, "G1", "👍", participant="15550000200")
        assert rig.bridge.paths("/react")[0].body["participant"] == ANA
        rig.bridge.push_event(**group_event("/x", id="G9"))
        await rig.step()
        assert rig.bridge.paths("/read")[0].body == {"chat": GROUP, "id": "G9", "participant": ANA}

    run(main, options=WhatsAppOptions.read({"send_read_receipts": True}))


# -- media in --------------------------------------------------------------------------------------


def test_media_refs_are_fetched_lazily_and_documents_reach_the_intake(scope: Scope) -> None:
    from indus.gateway.extras import attach_fetcher
    from indus.gateway.intake.media import MediaIntake

    async def main(rig: Rig) -> None:
        await rig.connect()
        rig.bridge.media["ab" * 12] = b"line one\nline two\n"
        rig.bridge.push_event(
            id="D1",
            text="summarise",
            media=[
                {"ref": "ab" * 12, "kind": "document", "mime": "text/plain", "name": "notes.txt"}
            ],
        )
        rig.bridge.push_event(
            id="V1", text="", media=[{"ref": "cd" * 12, "kind": "voice", "mime": "audio/ogg"}]
        )
        rig.bridge.push_event(id="B1", text="", media=[{"ref": "../etc/passwd", "kind": "image"}])
        await rig.step()
        assert [e.message_id for e in rig.events] == ["D1", "V1"]
        doc, voice = rig.events
        assert doc.kind is MessageKind.DOCUMENT
        assert doc.media[0].ref == f"{MEDIA_REF}{'ab' * 12}#notes.txt"
        assert voice.kind is MessageKind.VOICE
        assert rig.bridge.paths("/media") == []
        intake = MediaIntake(scope)
        undo = attach_fetcher(intake, rig.adapter)
        assert undo is not None
        text = await intake.enrich(doc)
        undo()
        assert "line one\nline two" in text and text.endswith("summarise")
        with pytest.raises(MediaRejected):
            async for _ in rig.adapter.fetch_media(voice.media[0]):
                pass
        bad = doc.media[0].model_copy(update={"ref": "https://example.invalid/x"})
        with pytest.raises(MediaRejected):
            async for _ in rig.adapter.fetch_media(bad):
                pass

    run(main)


# -- secrets and numbers in logs -------------------------------------------------------------------


def test_numbers_and_the_token_are_masked(caplog: pytest.LogCaptureFixture) -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        text = rig.adapter.scrub(f"send to 15550000200 with {rig.bridge.token}")
        assert "15550000200" not in text and rig.bridge.token not in text
        assert "0200" in text
        assert "token" not in repr(rig.adapter)

    with caplog.at_level(logging.DEBUG, logger="indus.gateway.adapters.whatsapp"):
        run(main)
    assert "15550000200" not in caplog.text


def test_options_read_settings_with_safe_defaults() -> None:
    defaults = WhatsAppOptions.read({})
    assert (defaults.dm_policy, defaults.group_policy) == ("pairing", "open")
    assert defaults.require_mention and not defaults.self_chat and not defaults.send_read_receipts
    odd = WhatsAppOptions.read(
        {
            "dm_policy": "everyone",
            "bridge_port": "3005",
            "mention_patterns": ["(", "ok"],
            "self_chat": True,
            "provider": "/opt/wa",
        }
    )
    assert odd.dm_policy == "pairing"
    assert odd.bridge_port == 3005
    assert [p.pattern for p in odd.mention_patterns] == ["ok"]
    assert odd.self_chat and odd.provider == "/opt/wa"
    assert WhatsAppOptions.read({}, {"INDUS_WHATSAPP_PROVIDER": "/x"}).provider == "/x"


def test_state_files_are_owner_only(tmp_path: Path) -> None:
    clock = _clock()
    bridge = FakeBridge()
    bridge.platform.clock = clock
    bridge.alive = False
    spawner = FakeSpawner(bridge)
    owner = SidecarOwner(
        home=tmp_path,
        root=tmp_path,
        profile_id="default",
        clock=clock,
        probe=http_probe(bridge.transport),
        node="/usr/bin/node",
        script=Path(__file__),
        env_for=lambda extra: dict(extra),
        spawner=spawner,
        start_time=spawner.start_time,
        alive_check=spawner.alive,
        kill=spawner.kill,
        is_free=lambda port: not bridge.alive,
    )

    async def main() -> None:
        await owner.ensure()
        state = state_dir(tmp_path, "default")
        for name in ("bridge.pid", "bridge.token"):
            assert stat.S_IMODE((state / name).stat().st_mode) == 0o600
        assert spawner.children[0].env["INDUS_WA_AUTH_DIR"].startswith(str(state))
        assert spawner.children[0].env["INDUS_WA_SELF_CHAT"] == "0"
        await owner.stop()

    asyncio.run(main())
    assert BOT_LID.endswith("@lid")
