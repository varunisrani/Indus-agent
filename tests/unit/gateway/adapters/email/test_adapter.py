"""The email adapter over in-process IMAP/SMTP fakes (P16-09)."""

from __future__ import annotations

import asyncio
import imaplib
import logging
import smtplib
import threading
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from email import message_from_bytes
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import Scope, make_scope
from indus.gateway.adapters.base import AdapterConnectError
from indus.gateway.adapters.email import create_adapter
from indus.gateway.adapters.email.adapter import (
    HEADER_LIMIT,
    MAX_BODY,
    EmailAdapter,
    EmailSettings,
)
from indus.gateway.config import AdapterConfig, PlatformSettings
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind
from indus.gateway.core.send import ErrorKind
from indus.gateway.intake.media import MediaRejected
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FakeChatPlatform
from tests.fakes.platform.email_wire import (
    AUTHSERV_ID,
    BOT_ADDRESS,
    MAX_LEN,
    EmailWire,
    FakeImap,
    Mailbox,
)

# Built at runtime: never a credential-shaped literal in the source (builder rules).
PASSWORD = "".join(["unit", "-", "mail", "-", "secret", "-", "42"])
ANA = "ana@example.org"


@pytest.fixture(autouse=True)
def _fresh_snapshots() -> Iterator[None]:
    from indus.gateway.adapters.email import adapter as module

    module._SNAPSHOTS.clear()  # pyright: ignore[reportPrivateUsage]
    yield
    module._SNAPSHOTS.clear()  # pyright: ignore[reportPrivateUsage]


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))


async def inline(fn: Any, *args: Any) -> Any:
    return fn(*args)


def mail(
    sender: str = ANA,
    body: str = "hello",
    *,
    subject: str | None = None,
    message_id: str = "<m1@example.org>",
    auth: str | None = None,
    extra: dict[str, str] | None = None,
    html: str | None = None,
) -> bytes:
    message = EmailMessage()
    message["From"] = f"Ana <{sender}>"
    message["To"] = BOT_ADDRESS
    if subject is not None:
        message["Subject"] = subject
    message["Message-ID"] = message_id
    domain = sender.rpartition("@")[2]
    message["Authentication-Results"] = (
        auth if auth is not None else f"{AUTHSERV_ID}; dmarc=pass header.from={domain}"
    )
    for name, value in (extra or {}).items():
        message[name] = value
    if html is not None:
        message.set_content(html, subtype="html")
    else:
        message.set_content(body)
    return message.as_bytes()


class Rig:
    def __init__(
        self,
        *,
        runner: Any = inline,
        env: dict[str, str] | None = None,
        approved: set[str] | None = None,
        send_only: bool = False,
        **settings: Any,
    ) -> None:
        self.clock = _clock()
        platform = FakeChatPlatform(
            "email", clock=self.clock, max_len=MAX_LEN, bot_user_id=BOT_ADDRESS
        )
        self.wire = EmailWire(platform, password=PASSWORD)
        settings.setdefault("authserv_id", AUTHSERV_ID)
        settings.setdefault("allow_from", frozenset({ANA}))
        settings.setdefault("imap_host", "imap.indus.test")
        self.settings = EmailSettings(
            address=BOT_ADDRESS,
            password=PASSWORD,
            smtp_host="smtp.indus.test",
            **settings,
        )
        self.approved = approved if approved is not None else set()
        self.send_only = send_only
        self.adapter = self.build(runner=runner, env=env)
        self.events: list[InboundEvent] = []

        async def sink(event: InboundEvent) -> None:
            self.events.append(event)

        self.adapter.set_sink(sink)

    def build(self, *, runner: Any, env: dict[str, str] | None) -> EmailAdapter:
        return EmailAdapter(
            clock=self.clock,
            mail=self.settings,
            env=env or {},
            is_approved=lambda sender: sender in self.approved,
            imap_factory=self.wire.open_imap,
            smtp_factory=self.wire.open_smtp,
            runner=runner,
            send_only=self.send_only,
        )

    @property
    def box(self) -> Mailbox:
        return self.wire.mailbox

    async def poll(self) -> None:
        await self.clock.run_until_idle()  # let the inbox loop park on its timer first
        await self.clock.advance_and_settle(61.0)


def run(main: Callable[[Rig], Awaitable[None]], **kw: Any) -> None:
    async def outer() -> None:
        rig = Rig(**kw)
        try:
            await main(rig)
        finally:
            await rig.adapter.disconnect()

    asyncio.run(outer())


def kinds(box: Mailbox, uid: int) -> list[str]:
    return [kind for fetched, kind in box.fetches if fetched == uid]


# -- the mailbox -----------------------------------------------------------------------------------


def test_mail_already_in_the_inbox_at_first_start_is_never_answered() -> None:
    async def main(rig: Rig) -> None:
        old = rig.box.deliver(mail(body="old"))
        await rig.adapter.connect()
        new = rig.box.deliver(mail(body="new", message_id="<m2@example.org>"))
        await rig.poll()
        assert [e.text for e in rig.events] == ["new"]
        assert kinds(rig.box, old) == [] and kinds(rig.box, new) == ["headers", "body"]

    run(main)


def test_a_reconnect_keeps_the_seen_list_and_reads_outage_mail() -> None:
    async def main(rig: Rig) -> None:
        rig.box.deliver(mail(body="before start"))
        await rig.adapter.connect()
        await rig.adapter.disconnect()
        rig.box.deliver(mail(body="during the outage", message_id="<m2@example.org>"))
        await rig.adapter.connect(is_reconnect=True)
        await rig.poll()
        assert [e.text for e in rig.events] == ["during the outage"]

    run(main)


def test_a_fresh_adapter_built_for_a_reconnect_reads_outage_mail() -> None:
    async def main(rig: Rig) -> None:
        rig.box.deliver(mail(body="before start"))
        await rig.adapter.connect()
        await rig.poll()
        await rig.adapter.disconnect()
        rig.box.deliver(mail(body="during the outage", message_id="<m2@example.org>"))
        rig.adapter = rig.build(runner=inline, env=None)  # the daemon builds a new one
        events: list[InboundEvent] = []

        async def sink(event: InboundEvent) -> None:
            events.append(event)

        rig.adapter.set_sink(sink)
        await rig.adapter.connect(is_reconnect=True)
        await rig.poll()
        assert [e.text for e in events] == ["during the outage"]

    run(main, imap_host="imap.reconnect.test")


def test_a_renumbered_mailbox_clears_the_seen_list() -> None:
    async def main(rig: Rig) -> None:
        rig.box.deliver(mail(body="old"), seen=True)
        await rig.adapter.connect()
        await rig.adapter.disconnect()
        rig.box.uidvalidity += 1
        rig.box.messages.clear()
        rig.box.next_uid = 1
        rig.box.deliver(mail(body="renumbered new"))
        await rig.adapter.connect(is_reconnect=True)
        await rig.poll()
        assert [e.text for e in rig.events] == ["renumbered new"]

    run(main)


def test_mail_fetched_before_a_mid_batch_failure_is_still_delivered() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        rig.box.deliver(mail(body="first"))
        broken = rig.box.deliver(mail(body="second", message_id="<m2@example.org>"))
        third = rig.box.deliver(mail(body="third", message_id="<m3@example.org>"))
        session = rig.wire.imap_sessions[-1]
        real_uid = session.uid

        def flaky(command: str, *args: Any) -> tuple[str, list[Any]]:
            if command == "FETCH" and int(args[0]) == broken:
                raise imaplib.IMAP4.abort("connection reset by peer")
            return real_uid(command, *args)

        session.uid = flaky  # type: ignore[method-assign]
        await rig.poll()
        assert [e.text for e in rig.events] == ["first"]
        assert r"\Seen" not in rig.box.messages[third].flags  # left for the next check
        await rig.clock.advance_and_settle(61.0)
        await rig.poll()
        assert [e.text for e in rig.events] == ["first", "second", "third"]

    run(main)


def test_the_global_allow_all_switch_counts_only_without_env_lists() -> None:
    open_rig = Rig(env={"INDUS_GATEWAY_ALLOW_ALL_USERS": "true"}, allow_from=frozenset())
    assert open_rig.adapter.allows_dm("anyone@example.net")
    listed = Rig(
        env={"INDUS_GATEWAY_ALLOW_ALL_USERS": "true", "INDUS_GATEWAY_ALLOWED_USERS": ANA},
        allow_from=frozenset(),
    )
    assert listed.adapter.allows_dm(ANA) and not listed.adapter.allows_dm("anyone@example.net")
    platform_wide = Rig(env={"INDUS_EMAIL_ALLOW_ALL_USERS": "1"}, allow_from=frozenset())
    assert platform_wide.adapter.allows_dm("anyone@example.net")


def test_a_failed_check_drops_the_session_and_logs_in_again() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        session = rig.wire.imap_sessions[-1]
        session.closed = True  # the server hung up
        await rig.poll()
        assert rig.box.sessions == 1
        rig.box.deliver(mail(body="after the hiccup"))
        await rig.clock.advance_and_settle(61.0)  # back-off wait (one poll interval)
        await rig.poll()
        assert rig.box.sessions == 2
        assert [e.text for e in rig.events] == ["after the hiccup"]

    run(main)


# -- screening -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"Auto-Submitted": "auto-replied"},
        {"Precedence": "bulk"},
        {"Precedence": "list"},
        {"List-Id": "<news.example.org>"},
    ],
)
def test_automated_mail_is_dropped_unread_and_marked_read(extra: dict[str, str]) -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        uid = rig.box.deliver(mail(extra=extra))
        await rig.poll()
        assert rig.events == []
        assert kinds(rig.box, uid) == ["headers"]
        assert r"\Seen" in rig.box.messages[uid].flags
        await rig.poll()
        assert kinds(rig.box, uid) == ["headers"]  # never looked at again

    run(main)


def test_noreply_senders_and_our_own_address_are_dropped() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        bounce = rig.box.deliver(mail("mailer-daemon@example.org"))
        own = rig.box.deliver(mail(BOT_ADDRESS))
        await rig.poll()
        assert rig.events == []
        assert kinds(rig.box, bounce) == ["headers"] and kinds(rig.box, own) == ["headers"]

    run(main, allow_from=frozenset({"*"}))


def test_unknown_senders_are_ignored_by_default_without_a_body_fetch() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        stranger = rig.box.deliver(mail("stranger@example.net", "let me in"))
        await rig.poll()
        assert rig.events == [] and kinds(rig.box, stranger) == ["headers"]
        assert rig.adapter.access_policy("dm") == "allowlist"
        assert not rig.adapter.allows_dm("stranger@example.net")
        assert rig.adapter.allows_dm(ANA)

    run(main)


def test_unknown_senders_reach_the_gateway_when_pairing_is_opted_in() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        rig.box.deliver(mail("stranger@example.net", "let me in"))
        await rig.poll()
        assert [e.user_id for e in rig.events] == ["stranger@example.net"]
        assert rig.adapter.access_policy("dm") == "pairing"

    run(main, unknown_senders="pair")


def test_senders_known_through_env_lists_allow_all_or_pairing() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        rig.box.deliver(mail("env@example.net", "from the env list"))
        rig.box.deliver(mail("paired@example.net", "paired", message_id="<p@example.net>"))
        rig.box.deliver(mail("local@example.net", "local part only", message_id="<l@x.net>"))
        await rig.poll()
        assert sorted(e.user_id or "" for e in rig.events) == [
            "env@example.net",
            "paired@example.net",
        ]

    env = {"INDUS_EMAIL_ALLOWED_USERS": "Env@Example.net, local"}
    run(main, env=env, approved={"paired@example.net"}, allow_from=frozenset())


def test_a_forged_from_is_dropped() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        no_header = rig.box.deliver(mail(auth=""))
        wrong_server = rig.box.deliver(
            mail(auth="mx.attacker.example; dmarc=pass header.from=example.org")
        )
        failed = rig.box.deliver(mail(auth=f"{AUTHSERV_ID}; dmarc=fail header.from=example.org"))
        await rig.poll()
        assert rig.events == []
        for uid in (no_header, wrong_server, failed):
            assert kinds(rig.box, uid) == ["headers"]

    run(main)


def test_without_a_pin_nothing_is_trusted_and_the_fix_is_logged_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        rig.box.deliver(mail())
        await rig.poll()
        assert rig.events == []
        await rig.adapter.disconnect()
        await rig.adapter.connect(is_reconnect=True)

    with caplog.at_level(logging.WARNING):
        run(main, authserv_id="")
    assert caplog.text.count("no authserv-id is pinned") == 1


def test_the_authentication_check_can_be_switched_off() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        rig.box.deliver(mail(auth=""))
        await rig.poll()
        assert [e.text for e in rig.events] == ["hello"]

    run(main, require_authenticated_sender=False)


def test_header_and_from_size_caps() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        huge = rig.box.deliver(mail(extra={"List-Unsubscribe": "x" * (HEADER_LIMIT + 10)}))
        long_from = EmailMessage()
        long_from["From"] = "A" * 2100 + f" <{ANA}>"
        long_from["Message-ID"] = "<lf@example.org>"
        long_from.set_content("x")
        long_uid = rig.box.deliver(long_from.as_bytes())
        twice = rig.box.deliver(
            b"From: " + ANA.encode() + b"\r\nFrom: boss@example.org\r\n" + b"\r\nbody\r\n"
        )
        await rig.poll()
        assert rig.events == []
        for uid in (huge, long_uid, twice):
            assert kinds(rig.box, uid) == ["headers"]

    run(main, require_authenticated_sender=False)


# -- what becomes an event -------------------------------------------------------------------------


def test_subject_html_and_attachments() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        rig.box.deliver(mail(subject="Deploy plan", body="ship it"))
        rig.box.deliver(mail(subject="Re: Deploy plan", body="done", message_id="<m2@e.org>"))
        rig.box.deliver(
            mail(html="<p>Hi <b>there</b></p><script>x()</script>", message_id="<m3@e.org>")
        )
        with_file = EmailMessage()
        with_file["From"] = ANA
        with_file["Message-ID"] = "<m4@e.org>"
        with_file.set_content("photo attached")
        with_file.add_attachment(
            b"\xff\xd8\xff\xe0jpeg", maintype="image", subtype="jpeg", filename="cat.jpg"
        )
        rig.box.deliver(with_file.as_bytes())
        await rig.poll()
        texts = [e.text for e in rig.events]
        assert texts == ["[Subject: Deploy plan]\n\nship it", "done", "Hi there", "photo attached"]
        photo = rig.events[3]
        assert photo.kind is MessageKind.PHOTO and len(photo.media) == 1
        assert photo.media[0].mime == "image/jpeg"
        data = [chunk async for chunk in rig.adapter.fetch_attachment(photo.media[0])]
        assert data == [b"\xff\xd8\xff\xe0jpeg"]
        with pytest.raises(MediaRejected):
            async for _ in rig.adapter.fetch_attachment(Attachment(ref="email-attachment:nope")):
                pass
        source = rig.events[0].source
        assert (source.chat_id, source.chat_type, source.user_name) == (ANA, "dm", "Ana")

    run(main, require_authenticated_sender=False)


def test_skip_attachments() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        message = EmailMessage()
        message["From"] = ANA
        message["Message-ID"] = "<s@e.org>"
        message.set_content("see file")
        message.add_attachment(b"x", maintype="application", subtype="pdf", filename="a.pdf")
        rig.box.deliver(message.as_bytes())
        await rig.poll()
        assert rig.events[0].media == () and rig.events[0].kind is MessageKind.TEXT

    run(main, require_authenticated_sender=False, skip_attachments=True)


def test_a_poison_message_does_not_stop_the_batch() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        rig.box.deliver(
            b"From: "
            + ANA.encode()
            + b"\r\nSubject: =?bogus-8?q?caf=E9?=\r\nContent-Type: text/plain; charset=x-junk"
            + b"\r\n\r\ncaf\xe9\r\n"
        )
        rig.box.deliver(mail(body="second", message_id="<m2@e.org>"))
        await rig.poll()
        assert len(rig.events) == 2 and rig.events[1].text == "second"

    run(main, require_authenticated_sender=False)


# -- replies ---------------------------------------------------------------------------------------


def test_a_reply_is_threaded_under_the_message_it_answers() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        raw = mail(
            subject="Quarterly numbers",
            message_id="<q1@example.org>",
            extra={"References": "<root@example.org>"},
        )
        rig.box.deliver(raw)
        await rig.poll()
        event = rig.events[0]
        result = await rig.adapter.send(ANA, "Here they are.", reply_to=event.message_id)
        assert result.ok and result.message_id is not None
        sent = rig.wire.smtp.sent[-1]
        assert sent["In-Reply-To"] == "<q1@example.org>"
        assert sent["References"] == "<root@example.org> <q1@example.org>"
        assert sent["Subject"] == "Re: Quarterly numbers"
        assert sent["Message-ID"] == result.message_id
        assert result.message_id.startswith("<indus-") and result.message_id.endswith(
            "@indus.test>"
        )
        assert sent["Auto-Submitted"] == "auto-replied"
        assert sent["To"] == ANA and sent["From"] == BOT_ADDRESS
        assert rig.wire.platform.message(result.message_id) is not None
        # Without reply_to, the last message from that sender is the parent.
        await rig.adapter.send(ANA, "One more thing.")
        assert rig.wire.smtp.sent[-1]["In-Reply-To"] == "<q1@example.org>"
        # A sender we know nothing about gets a fresh subject.
        await rig.adapter.send("bob@example.org", "Hello Bob")
        fresh = rig.wire.smtp.sent[-1]
        assert fresh["Subject"] == "Re: Indus Agent" and fresh["In-Reply-To"] is None

    run(main)


def test_long_bodies_and_bad_recipients_are_refused_before_smtp() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        too_long = await rig.adapter.send(ANA, "x" * (MAX_BODY + 1))
        assert too_long.kind is ErrorKind.TOO_LONG
        for bad in ("Ana <ana@example.org>", "a@x.org, b@y.org", "ana@example.org\r\nBcc: v@x"):
            result = await rig.adapter.send(bad, "hi")
            assert result.kind is ErrorKind.NOT_FOUND
        assert rig.wire.smtp.sent == []
        upper = await rig.adapter.send("Ana@Example.org", "case does not matter")
        assert upper.ok and rig.wire.smtp.sent[-1]["To"] == ANA

    run(main)


def test_files_go_out_as_attachments(tmp_path: Path) -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        report = tmp_path / "report.pdf"
        report.write_bytes(b"%PDF-1.7 tiny")
        result = await rig.adapter.send_document(ANA, str(report), caption="the report")
        assert result.ok
        sent = rig.wire.smtp.sent[-1]
        parts = [p for p in sent.walk() if p.get_filename()]
        assert [(p.get_filename(), p.get_content_type()) for p in parts] == [
            ("report.pdf", "application/pdf")
        ]
        assert parts[0].get_payload(decode=True) == b"%PDF-1.7 tiny"
        missing = await rig.adapter.send_image(ANA, str(tmp_path / "nope.png"))
        assert not missing.ok

    run(main)


def _refused(code: int, text: bytes) -> smtplib.SMTPRecipientsRefused:
    return smtplib.SMTPRecipientsRefused({ANA: (code, text)})


@pytest.mark.parametrize(
    ("error", "stage", "check"),
    [
        (
            smtplib.SMTPAuthenticationError(535, b"5.7.8 bad"),
            "connect",
            lambda r: r.extra.get("auth") is True,
        ),
        (
            smtplib.SMTPDataError(421, b"4.7.0 try again in 30 seconds"),
            "send",
            lambda r: r.kind is ErrorKind.RATE_LIMITED and r.retry_after == 30,
        ),
        (
            smtplib.SMTPDataError(451, b"4.3.0 local error"),
            "send",
            lambda r: r.kind is ErrorKind.TRANSIENT and r.retryable,
        ),
        (
            _refused(550, b"5.1.1 unknown user"),
            "send",
            lambda r: r.kind is ErrorKind.NOT_FOUND and r.chat_unreachable,
        ),
        (_refused(550, b"5.7.1 relaying denied"), "send", lambda r: r.kind is ErrorKind.FORBIDDEN),
        (
            smtplib.SMTPDataError(552, b"5.3.4 too big"),
            "send",
            lambda r: r.kind is ErrorKind.TOO_LONG,
        ),
        (TimeoutError("timed out"), "send", lambda r: r.maybe_delivered and not r.retryable),
        (smtplib.SMTPServerDisconnected("closed"), "send", lambda r: r.maybe_delivered),
        (OSError("unreachable"), "connect", lambda r: r.kind is ErrorKind.TRANSIENT),
        (ConnectionRefusedError(111, "refused"), "send", lambda r: r.kind is ErrorKind.TRANSIENT),
    ],
)
def test_smtp_failures_are_classified(
    error: BaseException, stage: str, check: Callable[[Any], bool]
) -> None:
    rig = Rig()
    result = rig.adapter._smtp_failure(error, stage)  # pyright: ignore[reportPrivateUsage]
    assert not result.ok and check(result), result
    assert rig.adapter.is_final_failure(result) is (result.extra.get("auth") is True)


def test_send_with_retry_stops_at_a_refused_login() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        rig.wire.smtp.password = "changed"
        result = await rig.adapter.send_with_retry(ANA, "hi", rng=lambda: 0.0)
        assert not result.ok and result.extra.get("auth") is True

    run(main)


# -- lifecycle, secrets, config --------------------------------------------------------------------


def test_send_only_open_never_touches_the_mailbox() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        assert rig.wire.imap_sessions == [] and rig.wire.smtp.logins == 1
        assert (await rig.adapter.send(ANA, "from a script")).ok
        await rig.clock.advance_and_settle(300.0)
        assert rig.wire.imap_sessions == []

    run(main, send_only=True)


def test_indus_send_builds_a_send_only_adapter_that_never_opens_the_mailbox(
    capsys: pytest.CaptureFixture[str], scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    from indus.gateway.delivery import send_command
    from tests.unit.cli.commands.cmdrun import run_cli

    wire = EmailWire(password=PASSWORD)
    secrets: Any = scope.secrets
    for name, value in (
        ("EMAIL_ADDRESS", BOT_ADDRESS),
        ("EMAIL_PASSWORD", PASSWORD),
        ("EMAIL_IMAP_HOST", "imap.indus.test"),
        ("EMAIL_SMTP_HOST", "smtp.indus.test"),
    ):
        secrets.set(name, value)
    built: list[EmailAdapter] = []
    real_build = send_command.build_adapter

    def build(*args: Any) -> Any:
        adapter = real_build(*args)  # the registry row and ``AdapterConfig(send_only=True)``
        assert isinstance(adapter, EmailAdapter)
        adapter._imap_factory = wire.open_imap  # pyright: ignore[reportPrivateUsage]
        adapter._smtp_factory = wire.open_smtp  # pyright: ignore[reportPrivateUsage]
        built.append(adapter)
        return adapter

    monkeypatch.setattr(send_command, "ADAPTER_BUILDER", build)
    ran = run_cli(capsys, scope, "send", f"email:{ANA}", "deploy done")
    assert ran.code == 0, ran.err
    assert built[0].send_only and not built[0].connected
    assert wire.imap_sessions == [] and wire.mailbox.sessions == 0
    assert wire.smtp.logins >= 1 and PASSWORD not in ran.out + ran.err


def test_a_login_refused_after_connect_is_fatal_and_stops_the_mailbox_loop() -> None:
    async def main(rig: Rig) -> None:
        reports: list[tuple[str, str]] = []
        rig.adapter.set_fatal_listener(lambda code, message: reports.append((code, message)))
        await rig.adapter.connect()
        rig.box.password = "rotated"
        rig.wire.imap_sessions[-1].closed = True  # the server hung up
        await rig.poll()
        assert reports == []  # a dropped session is retried
        await rig.clock.advance_and_settle(61.0)  # back-off wait, then the login is refused
        await rig.poll()
        assert [code for code, _ in reports] == ["email_auth"]
        assert PASSWORD not in reports[0][1]
        sessions = len(rig.wire.imap_sessions)
        await rig.clock.advance_and_settle(3600.0)
        assert len(rig.wire.imap_sessions) == sessions  # no retry of a refused login

    run(main)


def test_the_attachment_fetcher_feeds_the_intake(scope: Scope) -> None:
    from indus.gateway.extras import attach_fetcher
    from indus.gateway.intake.media import MediaIntake

    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        message = EmailMessage()
        message["From"] = ANA
        message["Message-ID"] = "<m9@e.org>"
        message.set_content("summarise")
        message.add_attachment(
            b"line one\nline two\n", maintype="text", subtype="plain", filename="notes.txt"
        )
        rig.box.deliver(message.as_bytes())
        await rig.poll()
        intake = MediaIntake(scope)
        undo = attach_fetcher(intake, rig.adapter)
        assert undo is not None
        text = await intake.enrich(rig.events[0])
        undo()
        assert "line one\nline two" in text and text.endswith("summarise")

    run(main, require_authenticated_sender=False)


def test_connect_errors_and_secrets(caplog: pytest.LogCaptureFixture) -> None:
    async def main(rig: Rig) -> None:
        empty = EmailAdapter(clock=rig.clock, mail=EmailSettings(address=""), runner=inline)
        with pytest.raises(AdapterConnectError) as config:
            await empty.connect()
        assert not config.value.retryable
        assert "EMAIL_ADDRESS" in str(config.value) and "EMAIL_PASSWORD" in str(config.value)
        rig.box.password = "rotated"
        with pytest.raises(AdapterConnectError) as refused:
            await rig.adapter.connect()
        assert not refused.value.retryable and refused.value.code == "auth"
        rig.box.password = PASSWORD
        rig.wire.platform.fail_connect()
        with pytest.raises(AdapterConnectError) as down:
            await rig.adapter.connect()
        assert down.value.retryable
        rig.wire.smtp.password = "rotated"
        with pytest.raises(AdapterConnectError) as smtp_refused:
            await rig.adapter.connect()
        assert not smtp_refused.value.retryable
        assert rig.wire.imap_sessions[-1].closed  # the mailbox session was closed again
        for error in (config.value, refused.value, down.value, smtp_refused.value):
            assert PASSWORD not in str(error)
        assert PASSWORD not in repr(rig.adapter) and PASSWORD not in repr(rig.settings)

    with caplog.at_level(logging.DEBUG):
        run(main)
    assert PASSWORD not in caplog.text


def test_create_adapter_reads_secrets_then_config(tmp_path: Path) -> None:
    env = {
        "EMAIL_ADDRESS": "Agent@Example.org",
        "EMAIL_PASSWORD": PASSWORD,
        "EMAIL_IMAP_HOST": "imap.example.org",
        "EMAIL_SMTP_HOST": "smtp.example.org",
        "EMAIL_SMTP_PORT": "465",
        "EMAIL_AUTHSERV_ID": "MX.Example.org",
    }
    scope = make_scope(tmp_path / "home", env=env, clock=_clock())
    section = PlatformSection.model_validate(
        {
            "extra": {
                "allow_from": ["Ana@Example.org"],
                "unauthorized_dm_behavior": "pair",
                "imap_security": "starttls",
                "poll_seconds": 30,
            }
        }
    )
    settings = PlatformSettings("email", True, "enabled in config", section, True)
    adapter = create_adapter(AdapterConfig("email", settings, scope, scope.clock))
    mail_settings = adapter.mail
    assert mail_settings.address == "agent@example.org"
    assert mail_settings.password == PASSWORD
    assert (mail_settings.imap_host, mail_settings.imap_port) == ("imap.example.org", 993)
    assert (mail_settings.smtp_port, mail_settings.smtp_security) == (465, "tls")
    assert mail_settings.imap_security == "starttls"
    assert mail_settings.authserv_id == "mx.example.org"
    assert mail_settings.allow_from == {"ana@example.org"}
    assert mail_settings.unknown_senders == "pair" and mail_settings.poll_seconds == 30
    assert PASSWORD not in repr(mail_settings) and PASSWORD not in repr(adapter)
    assert not adapter._is_approved("someone@example.org")  # pyright: ignore[reportPrivateUsage]


# -- IMAP IDLE (real worker threads) ---------------------------------------------------------------


class _Idler:
    def __init__(self, session: IdleImap, duration: float) -> None:
        self.session = session
        self.duration = duration

    def __enter__(self) -> _Idler:
        self.session.idles += 1
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def __iter__(self) -> Iterator[tuple[str, list[bytes]]]:
        wake = self.session.wake
        if wake.wait(5.0) and not self.session.closed:
            wake.clear()
            yield "EXISTS", [b"1"]
        elif self.session.closed:
            raise imaplib.IMAP4.abort("socket closed")


class IdleImap(FakeImap):
    def __init__(self, mailbox: Mailbox) -> None:
        super().__init__(mailbox)
        self.capabilities = (*mailbox.capabilities, "IDLE")
        self.wake = threading.Event()
        self.idles = 0

    def idle(self, duration: float | None = None) -> _Idler:
        return _Idler(self, duration or 0.0)

    def shutdown(self) -> None:
        super().shutdown()
        self.wake.set()


def test_idle_wakes_a_fetch_when_mail_arrives() -> None:
    async def main() -> None:
        rig = Rig()
        sessions: list[IdleImap] = []

        def open_idle(settings: Any) -> IdleImap:
            sessions.append(IdleImap(rig.box))
            return sessions[-1]

        adapter = replace_factory(rig, open_idle)
        got = asyncio.Event()

        async def sink(event: InboundEvent) -> None:
            rig.events.append(event)
            got.set()

        adapter.set_sink(sink)
        await adapter.connect()
        try:
            rig.box.deliver(mail(body="pushed"))
            sessions[-1].wake.set()
            await asyncio.wait_for(got.wait(), timeout=5.0)
            assert [e.text for e in rig.events] == ["pushed"] and sessions[-1].idles >= 1
        finally:
            await asyncio.wait_for(adapter.disconnect(), timeout=10.0)
        assert sessions[-1].closed

    asyncio.run(main())


def replace_factory(rig: Rig, factory: Callable[[Any], FakeImap]) -> EmailAdapter:
    """An adapter on the real email worker pool with another IMAP factory."""
    return EmailAdapter(
        clock=rig.clock,
        mail=replace(rig.settings),
        is_approved=lambda _sender: False,
        imap_factory=factory,
        smtp_factory=rig.wire.open_smtp,
    )


def test_the_fake_mail_parses_as_mail() -> None:
    parsed = message_from_bytes(mail(subject="x"))
    assert parsed["Authentication-Results"].startswith(AUTHSERV_ID)
