"""The Photon adapter over the fake sidecar (P16-25)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import Scope
from indus.gateway.adapters.base import AdapterConnectError
from indus.gateway.adapters.photon import create_adapter, default_sidecar_script
from indus.gateway.adapters.photon.adapter import (
    DEDUPE_MAX,
    DEDUPE_TTL_S,
    MAX_LEN,
    MEDIA_REF,
    PLACEHOLDER,
    PhotonAdapter,
    PhotonOptions,
    ndjson_lines,
)
from indus.gateway.adapters.photon.sidecar import (
    DEFAULT_PORT,
    PhotonSidecar,
    SidecarEndpoint,
    http_probe,
    state_dir,
)
from indus.gateway.config import AdapterConfig, PlatformSettings
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind
from indus.gateway.core.send import ErrorKind
from indus.gateway.intake.media import MediaRejected
from tests.fakes.clock import FakeClock
from tests.fakes.platform.photon_wire import FakeLauncher, FakeSidecar, FakeSpawner

ANA = "+15550000200"
ANA_SPACE = f"any;-;{ANA}"
GROUP = "any;+;chat300"
# Built at run time: never a credential-shaped literal in the tree.
SECRET = "-".join(("project", "secret", "for", "tests"))


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))


class Rig:
    def __init__(self, *, options: PhotonOptions | None = None, **kw: Any) -> None:
        self.clock = _clock()
        self.sidecar = FakeSidecar()
        self.sidecar.platform.clock = self.clock
        self.launcher = kw.pop("launcher", FakeLauncher)(self.sidecar)
        self.events: list[InboundEvent] = []
        self.fatal: list[tuple[str, str]] = []
        self.adapter = PhotonAdapter(
            clock=self.clock,
            launcher=self.launcher,
            options=options or PhotonOptions(),
            transport=self.sidecar.transport,
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
        await self.settle(self.adapter.connect())

    def ids(self) -> list[str | None]:
        return [e.message_id for e in self.events]


def run(main: Callable[[Rig], Awaitable[None]], **kw: Any) -> None:
    async def outer() -> None:
        rig = Rig(**kw)
        try:
            await main(rig)
        finally:
            await rig.adapter.disconnect()

    asyncio.run(outer())


# -- the at-least-once stream ----------------------------------------------------------------------


def test_a_replayed_event_is_ignored() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        rig.sidecar.push_event(id="E1", text="first")
        rig.sidecar.push_event(id="E2", text="second")
        await rig.step()
        rig.sidecar.replay()  # the provider redelivers both
        rig.sidecar.push_event(id="E1", text="first")  # and the stream repeats one more time
        await rig.step()
        assert rig.ids() == ["E1", "E2"]
        assert rig.sidecar.streams >= 2

    run(main)


def test_dedupe_keeps_4000_ids_for_48_hours() -> None:
    assert (DEDUPE_MAX, DEDUPE_TTL_S) == (4000, 48 * 3600)

    async def main(rig: Rig) -> None:
        assert rig.adapter.seen.max_entries == 4000
        await rig.connect()
        rig.sidecar.push_event(id="OLD", text="first")
        await rig.step()
        rig.clock.advance(47 * 3600)
        rig.sidecar.push_event(id="OLD", text="again")
        await rig.step()
        assert [e.text for e in rig.events] == ["first"]
        rig.clock.advance(2 * 3600)
        rig.sidecar.push_event(id="OLD", text="much later")
        await rig.step()
        assert [e.text for e in rig.events] == ["first", "much later"]

    run(main)


def test_a_new_sidecar_run_restarts_the_cursor_and_lines_split_on_lf_only() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        rig.sidecar.push_event(id="A", text="one two")
        await rig.step()
        rig.sidecar.restart()  # another run: new epoch, seq starts over
        rig.sidecar.push_event(id="B", text="after")
        await rig.step(times=5)
        assert rig.ids() == ["A", "B"]
        assert rig.events[0].text == "one two"

    run(main)


def test_ndjson_lines_split_on_lf_only_and_drop_an_oversized_line() -> None:
    async def chunks(*parts: bytes):  # noqa: ANN202
        for part in parts:
            yield part

    async def main() -> list[str]:
        big = b"x" * (1024 * 1024 + 10)
        found: list[str] = []
        async for line in ndjson_lines(
            chunks(b'{"a":"x\xe2\x80', b'\xa8y"}\n\n', big, b"\n", b"z")
        ):
            found.append(line)
        return found

    assert asyncio.run(main()) == ['{"a":"x y"}', "", "z"]


def test_a_dropped_stream_is_reopened_after_1_s_doubling(caplog: pytest.LogCaptureFixture) -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        await rig.step()
        rig.sidecar.alive = False  # every call fails, but the process is not reported gone
        rig.launcher.exited = lambda: False  # type: ignore[method-assign]
        start = len(rig.sidecar.paths("/events"))
        with caplog.at_level(logging.WARNING, logger="indus.gateway.adapters.photon"):
            await rig.step(0.5, times=16)
        times = [c.at for c in rig.sidecar.paths("/events")[start:]]
        gaps = [round(b - a) for a, b in zip(times, times[1:], strict=False)]
        assert gaps[:2] == [2, 4]
        # Only the reopen warnings: the connect line is INFO, captured too when an earlier test in
        # the same process left the ``indus`` loggers at INFO.
        reopens = [r for r in caplog.records if r.levelno >= logging.WARNING]
        waits = [r.getMessage().rsplit("in ", 1)[-1] for r in reopens]
        assert waits[:4] == ["1 s", "2 s", "4 s", "8 s"]

    run(main)


# -- placeholders and previews ---------------------------------------------------------------------


def test_a_placeholder_then_its_attachment_within_15_s_is_one_event() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        rig.sidecar.push_event(id="P1", text=f"look at this {PLACEHOLDER}")
        await rig.step()
        assert rig.events == []
        photo = rig.sidecar.media_item(
            b"\xff\xd8\xff", kind="image", mime="image/jpeg", name="a.jpg"
        )
        rig.clock.advance(5)
        rig.sidecar.push_event(id="A1", text="", media=[photo])
        await rig.step()
        assert rig.ids() == ["A1"]
        event = rig.events[0]
        assert event.text == "look at this"
        assert event.kind is MessageKind.PHOTO and len(event.media) == 1
        assert event.metadata["placeholder_id"] == "P1"
        rig.clock.advance(20)
        await rig.step()
        assert rig.ids() == ["A1"]

    run(main)


def test_a_placeholder_without_its_attachment_gives_its_words_after_15_s(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        rig.sidecar.push_event(id="P1", text=f"{PLACEHOLDER} for you")
        rig.sidecar.push_event(
            id="P2", text=PLACEHOLDER, space={"id": GROUP, "type": "group", "name": "G"}
        )
        await rig.step()
        await rig.step(times=10)
        assert rig.ids() == []
        with caplog.at_level(logging.WARNING, logger="indus.gateway.adapters.photon"):
            await rig.step(1.0, times=6)
        assert [(e.message_id, e.text) for e in rig.events] == [("P1", "for you")]
        assert "placeholder came without its attachment" in caplog.text

    run(main)


def test_preview_art_is_dropped_for_30_s_after_our_own_link() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        sent = await rig.settle(rig.adapter.send(ANA, "https://example.org/page"))
        assert sent.ok
        assert [c.body for c in rig.sidecar.paths("/send-link")] == [
            {"space": ANA, "url": "https://example.org/page"}
        ]
        art = rig.sidecar.media_item(
            b"png", kind="image", mime="image/png", name="X.pluginPayloadAttachment", preview=True
        )
        rig.sidecar.push_event(id="ART1", text="", media=[art], space={"id": ANA, "type": "dm"})
        await rig.step()
        assert rig.ids() == []
        rig.clock.advance(31)
        art2 = dict(art, ref=rig.sidecar.media_item(b"png")["ref"])
        rig.sidecar.push_event(id="ART2", text="", media=[art2], space={"id": ANA, "type": "dm"})
        await rig.step()
        assert rig.ids() == ["ART2"]

    run(main)


def test_preview_art_after_their_link_is_dropped_but_real_photos_are_not() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        rig.sidecar.push_event(
            id="L1",
            text="",
            link={"url": "https://example.org/x", "title": "Example", "summary": "A page"},
        )
        art = rig.sidecar.media_item(
            b"png", kind="image", name="Y.pluginPayloadAttachment", preview=True
        )
        photo = rig.sidecar.media_item(b"jpg", kind="image", name="IMG_1.jpg")
        rig.sidecar.push_event(id="ART", text="", media=[art])
        rig.sidecar.push_event(id="PHOTO", text="", media=[photo])
        await rig.step()
        assert rig.ids() == ["L1", "PHOTO"]
        assert rig.events[0].text == "Example\nA page\nhttps://example.org/x"

    run(main)


# -- the sidecar's token, restarts, health ---------------------------------------------------------


def test_the_sidecar_token_is_required() -> None:
    async def main(rig: Rig) -> None:
        launcher = rig.launcher
        assert isinstance(launcher, FakeLauncher)
        foreign = SidecarEndpoint("http://127.0.0.1:8789", 8789, "tok-" + "0" * 40)
        launcher.endpoint = lambda: foreign  # type: ignore[method-assign]  # not our sidecar
        with pytest.raises(AdapterConnectError) as raised:
            await rig.connect()
        assert raised.value.code == "photon_sidecar_port_taken"
        assert not raised.value.retryable
        assert all(c.path == "/health" for c in rig.sidecar.calls), (
            "nothing but the health check was tried"
        )

    run(main)


def test_a_token_refused_mid_run_is_fatal() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        await rig.step()
        rig.sidecar.token = "tok-" + "1" * 40
        await rig.step(times=4)
        assert rig.fatal and rig.fatal[0][0] == "photon_sidecar_port_taken"

    run(main)


class SlowLauncher(FakeLauncher):
    """Records when each restart happened; the sidecar it brings back dies again at once."""

    def __init__(self, sidecar: FakeSidecar) -> None:
        super().__init__(sidecar)
        self.at: list[float] = []

    async def restart(self) -> SidecarEndpoint:
        self.at.append(self.sidecar.platform.clock.monotonic())
        endpoint = await super().restart()
        self.sidecar.alive = False
        return endpoint


def test_a_dead_sidecar_is_restarted_after_2_s_doubling() -> None:
    async def main(rig: Rig) -> None:
        launcher = rig.launcher
        assert isinstance(launcher, SlowLauncher)
        await rig.connect()
        await rig.step()
        died = rig.clock.monotonic()
        rig.sidecar.alive = False
        await rig.step(0.5, times=60)
        assert len(launcher.at) >= 3
        assert round(launcher.at[0] - died) in (2, 3)
        gaps = [round(b - a) for a, b in zip(launcher.at, launcher.at[1:], strict=False)]
        assert gaps[:2] == [4, 8]
        assert rig.fatal == []

    run(main, launcher=SlowLauncher)


def test_a_degraded_upstream_link_restarts_the_sidecar() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        launcher = rig.launcher
        assert isinstance(launcher, FakeLauncher)
        rig.sidecar.status = "degraded"
        await rig.step(10.0, times=4)
        assert launcher.restarts == 0  # once is not enough
        await rig.step(10.0, times=6)
        assert launcher.restarts == 1
        rig.sidecar.push_event(id="AFTER", text="back")
        await rig.step(1.0, times=6)
        assert "AFTER" in rig.ids()

    run(main)


class FreshLauncher(FakeLauncher):
    """A restart brings up a sidecar that answers again."""

    async def restart(self) -> SidecarEndpoint:
        self.sidecar.hang_health = False
        return await super().restart()


def test_a_sidecar_that_stops_answering_its_health_check_is_restarted() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        launcher = rig.launcher
        assert isinstance(launcher, FakeLauncher)
        rig.sidecar.hang_health = True
        await rig.step(10.0, times=8)  # two missed checks: not yet
        assert launcher.restarts == 0
        await rig.step(10.0, times=4)
        await rig.step(1.0, times=6)
        assert launcher.restarts == 1
        rig.sidecar.push_event(id="AFTER", text="back")
        await rig.step(1.0, times=6)
        assert rig.ids() == ["AFTER"] and rig.fatal == []

    run(main, launcher=FreshLauncher)


def test_refused_credentials_are_fatal_at_connect_and_while_running() -> None:
    async def main(rig: Rig) -> None:
        rig.sidecar.status = "auth_failed"
        with pytest.raises(AdapterConnectError) as raised:
            await rig.connect()
        assert (raised.value.code, raised.value.retryable) == ("photon_auth", False)
        rig.sidecar.status = "open"
        await rig.connect()
        rig.sidecar.status = "auth_failed"
        await rig.step(10.0, times=4)
        assert [f[0] for f in rig.fatal] == ["photon_auth"]

    run(main)


def test_a_sidecar_still_connecting_is_accepted_after_30_s() -> None:
    async def main(rig: Rig) -> None:
        rig.sidecar.status = "connecting"
        await rig.connect()
        assert rig.adapter.connected
        result = await rig.settle(rig.adapter.send(ANA, "hi"))
        assert not result.ok and result.kind is ErrorKind.TRANSIENT

    run(main)


# -- send-only (indus send) ------------------------------------------------------------------------


def test_send_only_uses_the_running_sidecar_and_never_streams() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        result = await rig.settle(rig.adapter.send(ANA, "from cron"))
        assert result.ok
        launcher = rig.launcher
        assert isinstance(launcher, FakeLauncher)
        assert (launcher.attaches, launcher.ensures) == (1, 0)
        await rig.step(5.0)
        assert rig.sidecar.paths("/events") == []
        await rig.adapter.disconnect()
        assert launcher.stops == 0

    run(main, send_only=True)


def test_send_only_without_a_running_sidecar_fails_and_spawns_nothing(tmp_path: Path) -> None:
    clock = _clock()
    sidecar = FakeSidecar()
    sidecar.platform.clock = clock
    sidecar.alive = False
    spawner = FakeSpawner(sidecar)
    script = tmp_path / "main.ts"
    script.write_text("")
    owner = PhotonSidecar(
        home=tmp_path,
        profile_id="default",
        clock=clock,
        probe=http_probe(sidecar.transport),
        node="/usr/bin/node",
        script=script,
        env_for=dict,
        spawner=spawner,
        start_time=spawner.start_time,
        alive_check=spawner.alive,
        kill=spawner.kill,
    )
    adapter = PhotonAdapter(clock=clock, launcher=owner, send_only=True)

    async def main() -> None:
        with pytest.raises(AdapterConnectError) as raised:
            await adapter.connect()
        assert raised.value.code == "photon_sidecar_not_running"
        assert spawner.argvs == []

    asyncio.run(main())


def test_create_adapter_reads_the_credentials_and_builds_a_minimal_child_env(
    scope: Scope,
) -> None:
    scope = scope.with_env(
        scope.env.with_overlay(
            {"OPENAI_API_KEY": "x" * 12, "PATH": "/bin", "PHOTON_PROJECT_ID": "project-env"}
        )
    )
    secrets: Any = scope.secrets
    secrets.set("PHOTON_PROJECT_SECRET", SECRET)
    section = PlatformSection.model_validate(
        {"enabled": True, "provider": "/opt/provider/index.mjs", "reactions": True}
    )
    settings = PlatformSettings("photon", True, "enabled in config", section)
    config = AdapterConfig("photon", settings, scope, scope.clock, send_only=True)
    adapter = create_adapter(config)
    assert isinstance(adapter, PhotonAdapter)
    assert adapter.send_only and adapter.options.reactions
    owner = adapter.launcher
    assert isinstance(owner, PhotonSidecar)
    assert (owner.port, owner.provider, owner.project_id) == (
        DEFAULT_PORT,
        "/opt/provider/index.mjs",
        "project-env",
    )
    assert owner.pidfile.parent == state_dir(scope.home, scope.profile_id)
    env = owner.env_for({"INDUS_PHOTON_TOKEN": "t" * 40})
    assert env["INDUS_PHOTON_TOKEN"] == "t" * 40
    assert env["PATH"] == "/bin"
    assert "OPENAI_API_KEY" not in env and "INDUS_HOME" not in env
    assert "PHOTON_PROJECT_SECRET" not in env  # only the spawn adds it
    scrubbed = adapter.scrub(f"failed with {SECRET} and project-env")
    assert SECRET not in scrubbed and "project-env" not in scrubbed
    assert SECRET not in repr(adapter) and SECRET not in repr(owner)
    script = default_sidecar_script()
    assert script is not None and script.name == "main.ts"


# -- inbound ---------------------------------------------------------------------------------------


def test_group_messages_need_the_wake_word_when_asked() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        group = {"id": GROUP, "type": "group", "name": "Family"}
        rig.sidecar.push_event(id="G0", text="just chatting", space=group)
        rig.sidecar.push_event(id="G1", text="/status", space=group)
        rig.sidecar.push_event(id="G2", text="Indus, what time is it?", space=group)
        rig.sidecar.push_event(id="G3", text="hey @indus agent: help", space=group)
        rig.sidecar.push_event(id="D1", text="no wake word in a DM")
        await rig.step()
        assert rig.ids() == ["G1", "G2", "G3", "D1"]
        texts = {e.message_id: e.text for e in rig.events}
        assert texts["G2"] == "what time is it?"
        assert texts["G3"] == "hey @indus agent: help"
        g2 = rig.events[1]
        assert (g2.source.chat_type, g2.source.chat_id, g2.source.chat_name) == (
            "group",
            GROUP,
            "Family",
        )

    run(main, options=PhotonOptions(require_mention=True))


def test_tapbacks_reach_the_agent_only_when_asked_and_only_on_ours() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        sent = await rig.settle(rig.adapter.send(ANA, "here you go"))
        assert sent.message_id is not None
        rig.sidecar.push_event(
            id="T1",
            kind="reaction",
            text="",
            reaction={"target": sent.message_id, "emoji": "👍", "targetFromUs": False},
        )
        rig.sidecar.push_event(
            id="T2",
            kind="reaction",
            text="",
            reaction={"target": "THEIRS", "emoji": "😂", "targetFromUs": False},
        )
        await rig.step()
        assert rig.ids() == ["T1"]
        tap = rig.events[0]
        assert tap.metadata["reaction"] == "👍"
        assert tap.reply_to_is_own and tap.reply_to_id == sent.message_id
        assert tap.parsed_command() is None

    run(main, options=PhotonOptions(reactions=True))


def test_tapbacks_are_dropped_by_default() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        rig.sidecar.push_event(
            id="T1",
            kind="reaction",
            reaction={"target": "OUT", "emoji": "👍", "targetFromUs": True},
        )
        await rig.step()
        assert rig.events == []

    run(main)


def test_replies_media_and_read_receipts() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        sent = await rig.settle(rig.adapter.send(ANA, "earlier"))
        voice = rig.sidecar.media_item(b"caff", kind="voice", mime="audio/x-caf", name="Audio.caf")
        rig.sidecar.push_event(
            id="R1",
            text="",
            media=[voice],
            reply={"id": sent.message_id, "text": "", "fromUs": False},
        )
        await rig.step()
        event = rig.events[0]
        assert event.kind is MessageKind.VOICE
        assert event.reply_to_id == sent.message_id and event.reply_to_is_own
        assert event.timestamp == datetime.fromtimestamp(1_767_258_000, tz=UTC)
        assert event.media[0].ref.startswith(MEDIA_REF)
        chunks = [c async for c in rig.adapter.fetch_media(event.media[0])]
        assert b"".join(chunks) == b"caff"
        with pytest.raises(MediaRejected):
            [c async for c in rig.adapter.fetch_media(Attachment(ref="https://example.org/x"))]
        with pytest.raises(MediaRejected):
            [c async for c in rig.adapter.fetch_media(Attachment(ref=MEDIA_REF + "0" * 24))]
        assert [c.body for c in rig.sidecar.paths("/read")] == [{"space": ANA_SPACE, "id": "R1"}]

    run(main)


def test_a_reply_to_our_message_quotes_what_we_sent() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        sent = await rig.settle(rig.adapter.send(ANA, "the answer is 42"))
        rig.sidecar.push_event(id="Q1", text="why?", reply={"id": sent.message_id, "text": ""})
        await rig.step()
        event = rig.events[0]
        assert event.reply_to_is_own and event.reply_to_text == "the answer is 42"

    run(main)


def test_an_attachment_too_large_to_fetch_is_named_in_the_text() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        big = rig.sidecar.media_item(b"x", kind="video", name="movie.mov", size=200 * 1024 * 1024)
        rig.sidecar.push_event(id="BIG", text="", media=[big])
        await rig.step()
        event = rig.events[0]
        assert event.media == () and event.text == "[an attachment too large to fetch: movie.mov]"

    run(main)


def test_a_direct_message_without_a_sender_is_from_its_space() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        rig.sidecar.push_event(id="N1", sender={"id": "", "name": ""})
        rig.sidecar.push_event(
            id="N2", sender={"id": ""}, space={"id": GROUP, "type": "group", "name": ""}
        )
        await rig.step()
        assert rig.ids() == ["N1"] and rig.events[0].user_id == ANA_SPACE

    run(main)


def test_read_receipts_can_be_turned_off() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        rig.sidecar.push_event(id="M1")
        await rig.step()
        assert rig.ids() == ["M1"] and rig.sidecar.paths("/read") == []

    run(main, options=PhotonOptions(read_receipts=False))


def test_the_fetcher_is_registered_with_the_intake() -> None:
    registered: list[str] = []

    class Seat:
        def register_fetcher(self, platform: str, fetcher: Any) -> Callable[[], None]:
            registered.append(platform)
            return lambda: None

    rig = Rig()
    undo = rig.adapter.register_fetchers(Seat())
    assert undo is not None and registered == ["photon"]


# -- outbound --------------------------------------------------------------------------------------


def test_markdown_goes_as_markdown_and_a_piece_with_a_url_as_plain_text() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        await rig.settle(rig.adapter.send(ANA, "**bold** reply"))
        await rig.settle(rig.adapter.send(ANA, "see **this**: https://example.org/a"))
        await rig.settle(rig.adapter.send(ANA, "*as is*", meta={"plain": True}))
        bodies = [c.body for c in rig.sidecar.paths("/send")]
        assert bodies[0] == {"space": ANA, "text": "**bold** reply", "format": "markdown"}
        assert bodies[1]["format"] == "plain" and "**" not in bodies[1]["text"]
        assert "https://example.org/a" in bodies[1]["text"]
        assert bodies[2] == {"space": ANA, "text": "*as is*", "format": "plain"}

    run(main)


def test_markdown_can_be_turned_off() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        await rig.settle(rig.adapter.send(ANA, "**bold**"))
        await rig.settle(rig.adapter.send(ANA, "https://example.org"))
        bodies = [c.body for c in rig.sidecar.paths("/send")]
        assert bodies[0] == {"space": ANA, "text": "bold", "format": "plain"}
        assert rig.sidecar.paths("/send-link") == []

    run(main, options=PhotonOptions(markdown=False))


def test_a_refused_link_send_falls_back_to_plain_text_but_not_a_maybe_sent_or_refused_target() -> (
    None
):
    async def main(rig: Rig) -> None:
        await rig.connect()
        rig.sidecar.refusals.append((400, "bad_request"))
        result = await rig.settle(rig.adapter.send(ANA, "https://example.org"))
        assert result.ok
        assert [c.body["format"] for c in rig.sidecar.paths("/send")] == ["plain"]
        rig.sidecar.refusals.append((502, "failed"))  # may be out already: never sent twice
        maybe = await rig.settle(rig.adapter.send(ANA, "https://example.org/c"))
        assert not maybe.ok and maybe.maybe_delivered
        assert len(rig.sidecar.paths("/send")) == 1
        rig.sidecar.refusals.append((403, "target_not_allowed"))
        refused = await rig.settle(rig.adapter.send("+15550000999", "https://example.org/b"))
        assert not refused.ok and refused.kind is ErrorKind.FORBIDDEN
        assert rig.adapter.is_final_failure(refused)
        assert "cannot start a conversation" in (refused.error or "")
        assert len(rig.sidecar.paths("/send")) == 1
        rig.sidecar.refusals.append((403, "target_not_allowed"))
        retried = await rig.settle(
            rig.adapter.send_with_retry("+15550000999", "hello", rng=lambda: 0.0, notice=None)
        )
        assert not retried.ok
        assert len([c for c in rig.sidecar.calls if c.path == "/send"]) == 2

    run(main)


def test_long_text_is_split_at_8000_code_points() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        result = await rig.settle(rig.adapter.send(ANA, "word " * 1601))
        assert result.ok and len(result.ids) == 2
        assert all(len(c.body["text"]) <= MAX_LEN for c in rig.sidecar.paths("/send"))

    run(main)


def test_files_go_by_path_with_the_caption_after_them(tmp_path: Path) -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        note = tmp_path / "note.ogg"
        note.write_bytes(b"OggS")
        doc = tmp_path / "report.pdf"
        doc.write_bytes(b"%PDF")
        voice = await rig.settle(rig.adapter.send_voice(ANA, str(note)))
        assert voice.ok
        sent = await rig.settle(rig.adapter.send_document(ANA, str(doc), caption="the **report**"))
        assert sent.ok
        files = [c.body for c in rig.sidecar.paths("/send-file")]
        assert files[0] == {
            "space": ANA,
            "path": str(note.resolve()),
            "voice": True,
            "mime": "audio/ogg",
        }
        assert files[1]["name"] == "report.pdf" and files[1]["voice"] is False
        assert files[1]["mime"] == "application/pdf"
        assert [c.body["text"] for c in rig.sidecar.paths("/send")] == ["the **report**"]

    run(main)


def test_typing_and_tapbacks() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        await rig.settle(rig.adapter.typing_start(ANA))
        await rig.settle(rig.adapter.typing_stop(ANA))
        assert await rig.settle(rig.adapter.react(ANA, "IN1", "👀"))
        assert await rig.settle(rig.adapter.unreact(ANA, "IN1"))
        assert [c.body for c in rig.sidecar.paths("/typing")] == [
            {"space": ANA, "on": True},
            {"space": ANA, "on": False},
        ]
        assert [c.path for c in rig.sidecar.calls if c.path in ("/react", "/unreact")] == [
            "/react",
            "/unreact",
        ]

    run(main)


def test_a_timeout_inside_the_sidecar_is_maybe_delivered_and_never_resent() -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        rig.sidecar.refusals.append((504, "timeout"))
        result = await rig.settle(rig.adapter.send(ANA, "hello"))
        assert result.maybe_delivered and not result.retryable

    run(main)


def test_numbers_and_secrets_are_masked(caplog: pytest.LogCaptureFixture) -> None:
    async def main(rig: Rig) -> None:
        await rig.connect()
        rig.sidecar.refusals.append((403, "forbidden"))
        result = await rig.settle(rig.adapter.send(ANA, "x"))
        text = rig.adapter.scrub(f"send to {ANA} failed with {SECRET} and {rig.sidecar.token}")
        assert "15550000200" not in text
        assert SECRET not in text and rig.sidecar.token not in text
        assert SECRET not in (result.error or "")
        with caplog.at_level(logging.DEBUG, logger="indus.gateway.adapters.photon"):
            rig.sidecar.push_event(id="LOG1", text="a private message")
            await rig.step()
        assert "a private message" not in caplog.text
        assert "15550000200" not in caplog.text

    run(main, secrets=(SECRET,))


def test_the_stream_request_carries_the_token_in_a_header_only() -> None:
    async def main(rig: Rig) -> None:
        seen: list[httpx.Request] = []
        inner = rig.sidecar.handle

        async def spy(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return await inner(request)

        rig.sidecar.transport = httpx.MockTransport(spy)
        rig.adapter = PhotonAdapter(
            clock=rig.clock, launcher=rig.launcher, transport=rig.sidecar.transport
        )
        await rig.connect()
        await rig.step()
        assert seen
        for request in seen:
            assert rig.sidecar.token not in str(request.url)
            assert request.headers["authorization"] == f"Bearer {rig.sidecar.token}"

    run(main)
