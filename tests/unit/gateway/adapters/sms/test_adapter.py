"""P16-10: the SMS adapter over the fake Twilio API and signed webhooks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import replace
from typing import Any

import httpx
import pytest

from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import Scope
from indus.gateway.adapters import _http
from indus.gateway.adapters import sms as sms_mod
from indus.gateway.adapters.base import AdapterConnectError
from indus.gateway.adapters.sms import create_adapter
from indus.gateway.adapters.sms.adapter import (
    EMPTY_TWIML,
    MAX_BODY_BYTES,
    TWILIO_ROUTES,
    SmsAdapter,
    url_variants,
)
from indus.gateway.config import AdapterConfig, PlatformSettings
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind
from indus.gateway.core.send import ErrorKind
from indus.gateway.delivery import send_command
from indus.gateway.outbound.sender import FinalReply, OutboundSender
from indus.gateway.registry import PlatformEntry, PlatformRegistry
from tests.contract.gateway.conftest import CLOCK_START
from tests.contract.gateway.sms_case import build, make_env
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FailureKind
from tests.fakes.platform.http_wire import Callback
from tests.fakes.platform.sms_wire import (
    ACCOUNT_SID,
    AUTH_TOKEN,
    PHONE_NUMBER,
    PUBLIC_URL,
    TwilioWire,
    message_fields,
)

ANA = "+15550000200"
BEN = "+15550000300"
MESSAGES = f"/2010-04-01/Accounts/{ACCOUNT_SID}/Messages.json"


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


class Rig:
    """The adapter over the fake Twilio API, its events collected."""

    def __init__(self, *, send_only: bool = False, **twilio: str) -> None:
        self.clock = FakeClock(CLOCK_START)
        env = make_env(self.clock)
        self.platform = env.platform
        self.wire: TwilioWire = env.wire
        self.adapter = build(env, send_only=send_only, **twilio)
        self.events: list[InboundEvent] = []

        async def sink(event: InboundEvent) -> None:
            self.events.append(event)

        self.adapter.set_sink(sink)

    async def deliver(self, callback: Callback) -> httpx.Response:
        answer = await self.wire.deliver(callback)
        await self.adapter.listener.idle()
        return answer

    async def text(self, sender: str, body: str, **extra: Any) -> httpx.Response:
        return await self.deliver(
            self.wire.callback(message_fields(self.wire, sender, body, **extra))
        )


# -- signatures ----------------------------------------------------------------------------------


def test_the_signature_covers_a_url_with_a_query_string() -> None:
    rig = Rig()
    rig.wire.query = "tenant=work&lang=en"

    async def main() -> list[int]:
        await rig.adapter.connect()
        good = await rig.text(ANA, "signed with the query", sid="SM1")
        fields = message_fields(rig.wire, ANA, "signed without it", sid="SM2")
        without = rig.wire.callback(fields, signature=rig.wire.sign(fields, url=PUBLIC_URL))
        bad = await rig.deliver(without)
        await rig.adapter.disconnect()
        return [good.status_code, bad.status_code]

    assert run(main()) == [200, 403]
    assert [e.text for e in rig.events] == ["signed with the query"]


def test_a_signature_with_or_without_the_default_port_is_accepted() -> None:
    rig = Rig()
    with_port = Rig(public_url="https://sms.indus.test:443/webhooks/twilio")

    async def main(r: Rig, url: str) -> int:
        await r.adapter.connect()
        fields = message_fields(r.wire, ANA, "ported", sid="SM5")
        answer = await r.deliver(r.wire.callback(fields, url=url))
        await r.adapter.disconnect()
        return answer.status_code

    assert run(main(rig, "https://sms.indus.test:443/webhooks/twilio")) == 200
    assert run(main(with_port, PUBLIC_URL)) == 200
    assert url_variants("https://h.test/p?q=1") == [
        "https://h.test/p?q=1",
        "https://h.test:443/p?q=1",
    ]
    assert url_variants("http://h.test:80/p") == ["http://h.test:80/p", "http://h.test/p"]
    assert url_variants("https://h.test:8443/p") == ["https://h.test:8443/p"]


def test_a_configured_url_with_its_own_query_is_not_doubled() -> None:
    rig = Rig(public_url=PUBLIC_URL + "?tenant=work")
    rig.wire.query = "tenant=work"

    async def main() -> int:
        await rig.adapter.connect()
        answer = await rig.text(ANA, "hello")
        await rig.adapter.disconnect()
        return answer.status_code

    assert run(main()) == 200 and len(rig.events) == 1


def test_forged_or_missing_signatures_are_403_and_compared_in_constant_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig()
    compared: list[bytes] = []
    real = _http._compare  # pyright: ignore[reportPrivateUsage]

    def spy(offered: bytes, expected: bytes) -> bool:
        compared.append(offered)
        return real(offered, expected)

    monkeypatch.setattr(_http, "_compare", spy)
    fields = message_fields(rig.wire, ANA, "forged")

    async def main() -> list[httpx.Response]:
        await rig.adapter.connect()
        answers = [
            await rig.deliver(rig.wire.callback(fields, signature="bm90IGl0")),
            await rig.deliver(rig.wire.callback(fields, signature="")),
            await rig.deliver(
                rig.wire.callback([*fields, ("Extra", "added")], signature=rig.wire.sign(fields))
            ),
        ]
        await rig.adapter.disconnect()
        return answers

    answers = run(main())
    assert [a.status_code for a in answers] == [403, 403, 403]
    assert answers[0].text == EMPTY_TWIML
    assert rig.events == [] and b"bm90IGl0" in compared


def test_an_oversized_body_is_refused() -> None:
    rig = Rig()

    async def main() -> int:
        await rig.adapter.connect()
        body = [("Body", "x" * MAX_BODY_BYTES)]
        answer = await rig.deliver(rig.wire.callback(body))
        await rig.adapter.disconnect()
        return answer.status_code

    assert run(main()) == 413


# -- inbound -------------------------------------------------------------------------------------


def test_keywords_echoes_foreign_numbers_and_repeats_start_no_turn() -> None:
    rig = Rig()

    async def main() -> list[int]:
        await rig.adapter.connect()
        codes = [
            (await rig.text(ANA, "STOP", sid="SM10")).status_code,
            (await rig.text(ANA, " help ", sid="SM11")).status_code,
            (await rig.text(ANA, "please stop now", sid="SM12", OptOutType="STOP")).status_code,
            (await rig.text(PHONE_NUMBER, "our own echo", sid="SM13")).status_code,
            (await rig.text(ANA, "to another number", sid="SM14", to="+15550001111")).status_code,
            (await rig.text(ANA, "", sid="SM15")).status_code,
            (await rig.text(ANA, "stop the build please", sid="SM16")).status_code,
            (await rig.text(ANA, "stop the build please", sid="SM16")).status_code,
        ]
        await rig.adapter.disconnect()
        return codes

    assert run(main()) == [200] * 8
    assert [e.text for e in rig.events] == ["stop the build please"]
    event = rig.events[0]
    assert event.source.chat_type == "dm" and event.source.chat_id == ANA
    assert event.user_id == ANA and event.source.user_id_alt == ANA[1:]
    assert event.message_id == "SM16"


def test_mms_media_become_attachments_and_the_fetcher_stays_on_twilio() -> None:
    rig = Rig()
    rig.wire.media["pic.jpg"] = (b"jpeg", "image/jpeg")
    rig.wire.media["clip.mp4"] = (b"mp4", "video/mp4")
    pic = rig.wire.media_url("MM1", "pic.jpg")
    clip = rig.wire.media_url("MM1", "clip.mp4")
    fields = message_fields(
        rig.wire,
        ANA,
        "two files",
        sid="MM1",
        NumMedia=2,
        MediaUrl0=pic,
        MediaContentType0="image/jpeg",
        MediaUrl1=clip,
        MediaContentType1="video/mp4",
    )
    registered: list[tuple[str, Callable[..., Any]]] = []

    class Seat:
        def register_fetcher(self, platform: str, fetcher: Any) -> Callable[[], None]:
            registered.append((platform, fetcher))
            return lambda: None

    async def fetch(ref: str) -> bytes:
        return b"".join([c async for c in rig.adapter.fetch_media(Attachment(ref=ref))])

    async def main() -> None:
        await rig.adapter.connect()
        await rig.deliver(rig.wire.callback(fields))
        assert await fetch(pic) == b"jpeg"
        rig.wire.redirect_media = True
        assert await fetch(clip) == b"mp4"
        for foreign in (
            "https://evil.test/2010-04-01/Accounts/x/Messages/MM1/Media/pic.jpg",
            pic.replace(ACCOUNT_SID, "AC" + "1" * 32),
            pic.replace("https://", "http://"),
        ):
            with pytest.raises(ValueError):
                await fetch(foreign)
        await rig.adapter.disconnect()

    assert rig.adapter.register_fetchers(Seat()) is not None
    run(main())
    (event,) = rig.events
    assert event.kind is MessageKind.PHOTO and event.text == "two files"
    assert event.media == (
        Attachment(ref=pic, mime="image/jpeg"),
        Attachment(ref=clip, mime="video/mp4"),
    )
    assert registered == [("sms", rig.adapter.fetch_media)]
    cdn = [c for c in rig.wire.calls if c.path == "/clip.mp4"]
    assert cdn and "authorization" not in cdn[0].headers  # the redirect dropped the credentials


# -- sending -------------------------------------------------------------------------------------


def test_1601_characters_become_two_messages() -> None:
    rig = Rig()
    text = "x" * 1601

    async def main() -> Any:
        await rig.adapter.connect()
        result = await rig.adapter.send(ANA, text)
        await rig.adapter.disconnect()
        return result

    result = run(main())
    assert result.ok and len(result.ids) == 2
    posts = rig.wire.calls_to("POST", MESSAGES)
    assert len(posts) == 2
    bodies = rig.platform.texts(ANA)
    assert all(len(b) <= 1600 for b in bodies)
    assert "".join(b.rsplit(" ", 1)[0] for b in bodies) == text
    assert bodies[0].endswith("(1/2)") and bodies[1].endswith("(2/2)")


def test_markdown_is_removed_by_the_plain_formatter(scope: Scope) -> None:
    rig = Rig()

    async def main() -> Any:
        await rig.adapter.connect()
        sender = OutboundSender(rig.adapter, scope=scope)
        text = "## Status\n\n**Build** passed, see [the log](https://ci.test/1) and `make test`."
        report = await sender.deliver(ANA, FinalReply(text))
        await rig.adapter.disconnect()
        return report

    assert run(main()).outcome == "sent"
    (sent,) = rig.platform.texts(ANA)
    assert sent == "Status\n\nBuild passed, see the log (https://ci.test/1) and make test."


def test_an_unsubscribed_number_is_forbidden_final_and_tried_once() -> None:
    rig = Rig()
    rig.wire.unsubscribed.add(BEN)

    async def main() -> Any:
        await rig.adapter.connect()
        result = await rig.adapter.send_with_retry(BEN, "reminder", rng=lambda: 0.0)
        await rig.adapter.disconnect()
        return result

    result = run(main())
    assert result.kind is ErrorKind.FORBIDDEN and result.chat_unreachable
    assert "STOP" in (result.error or "") and rig.adapter.is_final_failure(result)
    assert len(rig.wire.calls_to("POST", MESSAGES)) == 1
    assert BEN not in (result.error or "")


def test_twilio_codes_become_typed_results() -> None:
    rig = Rig()
    fatal: list[tuple[str, str]] = []
    rig.adapter.set_fatal_listener(lambda code, message: fatal.append((code, message)))

    async def main() -> list[Any]:
        await rig.adapter.connect()
        rig.platform.fail(FailureKind.CHAT_NOT_FOUND, op="send")
        invalid = await rig.adapter.send(BEN, "hi")
        rig.platform.fail(FailureKind.RATE_LIMIT, op="send", retry_after=3)
        limited = await rig.adapter.send(BEN, "hi")
        rig.wire.auth_token = "rotated-" + "q" * 16
        refused = await rig.adapter.send_with_retry(BEN, "hi", rng=lambda: 0.0)
        await rig.adapter.disconnect()
        return [invalid, limited, refused]

    invalid, limited, refused = run(main())
    assert invalid.kind is ErrorKind.NOT_FOUND and invalid.chat_unreachable
    assert limited.kind is ErrorKind.RATE_LIMITED and limited.retry_after == 3
    assert rig.adapter.is_final_failure(refused) and fatal[0][0] == "sms_auth"
    assert AUTH_TOKEN not in (refused.error or "") and AUTH_TOKEN not in fatal[0][1]


def test_a_failure_after_the_first_piece_resumes_with_the_rest_only() -> None:
    rig = Rig()
    text = " ".join(f"w{n:04d}" for n in range(500))

    async def main() -> Any:
        await rig.adapter.connect()
        rig.platform.fail(FailureKind.CONNECT_TIMEOUT, op="send", after=1)
        result = await rig.adapter.send(ANA, text)
        await rig.adapter.disconnect()
        return result

    result = run(main())
    assert not result.ok and result.partial and result.retryable
    assert result.remainder is not None and text.startswith(
        text[: len(text) - len(result.remainder)]
    )
    assert len(result.ids) == 1


# -- connecting ----------------------------------------------------------------------------------


def test_connect_refusals_are_fatal_with_a_reason() -> None:
    def refused(rig: Rig) -> AdapterConnectError:
        async def main() -> AdapterConnectError:
            try:
                await rig.adapter.connect()
            except AdapterConnectError as exc:
                return exc
            raise AssertionError("connect should have failed")

        return run(main())

    no_url = Rig(public_url="")
    exc = refused(no_url)
    assert exc.code == "sms_public_url_missing" and not exc.retryable and no_url.wire.calls == []
    bad = refused(Rig(auth_token="w" * 32))
    assert bad.code == "sms_auth" and not bad.retryable
    other = refused(Rig(account_sid="AC" + "2" * 32))
    assert other.code in ("sms_auth", "sms_config") and not other.retryable
    rig = Rig()
    rig.platform.fail_connect(FailureKind.CONNECT_TIMEOUT)
    flaky = refused(rig)
    assert flaky.retryable and rig.wire.closed == 1


def test_send_only_opens_no_listener_and_needs_no_public_url() -> None:
    rig = Rig(send_only=True, public_url="")

    async def main() -> Any:
        await rig.adapter.connect()
        rig.platform.dm(ANA, "for the gateway")
        result = await rig.adapter.send(ANA, "one-shot")
        accepting = rig.adapter.listener.accepting
        await rig.adapter.disconnect()
        return result, accepting

    result, accepting = run(main())
    assert result.ok and not accepting
    assert rig.platform.texts(ANA) == ["one-shot"] and rig.events == []
    assert rig.platform.next_inbound() is not None


def platform_settings(**extra: Any) -> PlatformSettings:
    return PlatformSettings(
        "sms",
        True,
        "credentials",
        PlatformSection.model_validate({"extra": extra}),
        credentials={"TWILIO_ACCOUNT_SID": ACCOUNT_SID, "TWILIO_AUTH_TOKEN": AUTH_TOKEN},
    )


def test_create_adapter_reads_credentials_and_binds_loopback(
    scope: Scope, fake_clock: FakeClock
) -> None:
    secrets: Any = scope.secrets
    secrets.set("TWILIO_PHONE_NUMBER", PHONE_NUMBER)
    adapter = create_adapter(
        AdapterConfig("sms", platform_settings(webhook_public_url=PUBLIC_URL), scope, fake_clock)
    )
    assert adapter.twilio.phone_number == PHONE_NUMBER and adapter.twilio.public_url == PUBLIC_URL
    listen = adapter.listener.settings
    assert (listen.host, listen.port, listen.path) == ("127.0.0.1", 8080, "/webhooks/twilio")
    assert listen.max_body_bytes == 64 * 1024
    for shown in (repr(adapter), repr(adapter.twilio)):
        assert AUTH_TOKEN not in shown and ACCOUNT_SID not in shown
    with pytest.raises(ValueError, match="TWILIO_ACCOUNT_SID"):
        create_adapter(
            AdapterConfig("sms", replace(platform_settings(), credentials={}), scope, fake_clock)
        )
    secrets.set("TWILIO_PHONE_NUMBER", "555-0100")
    with pytest.raises(ValueError, match="E.164"):
        create_adapter(AdapterConfig("sms", platform_settings(), scope, fake_clock))


def test_indus_send_builds_a_send_only_adapter(
    scope: Scope, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(fake_clock)
    wire: TwilioWire = env.wire
    secrets: Any = scope.secrets
    secrets.set("TWILIO_PHONE_NUMBER", PHONE_NUMBER)
    monkeypatch.setattr(sms_mod, "CLIENT_FACTORY", lambda twilio, proxy: wire.client())
    registry = PlatformRegistry(builtins=())
    registry.register(
        PlatformEntry("sms", "SMS (Twilio)", factory=create_adapter, check_fn=lambda: True)
    )
    adapter = send_command.build_adapter(scope, "sms", platform_settings(), registry)
    assert isinstance(adapter, SmsAdapter) and adapter.send_only

    async def main() -> Any:
        await adapter.connect()
        result = await adapter.send(ANA, "deploy done")
        await adapter.disconnect()
        return result

    assert run(main()).ok
    assert env.platform.texts(ANA) == ["deploy done"] and not adapter.listener.accepting


def test_every_twilio_route_has_a_fake() -> None:
    rig = Rig()
    rig.wire.media["ME1"] = (b"x", "image/png")
    values = {"AccountSid": ACCOUNT_SID, "MessageSid": "MM1", "MediaSid": "ME1"}

    async def main() -> None:
        client = rig.wire.client()
        for method, template in TWILIO_ROUTES:
            answer = await client.request(
                method,
                template.format(**values),
                auth=(ACCOUNT_SID, AUTH_TOKEN),
                data={"To": ANA, "From": PHONE_NUMBER, "Body": "x"} if method == "POST" else None,
            )
            assert answer.status_code < 400, (method, template, answer.text)
        await client.aclose()

    run(main())


def test_numbers_and_credentials_are_masked_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    rig = Rig()
    caplog.set_level(logging.DEBUG)

    async def main() -> Any:
        await rig.adapter.connect()
        await rig.text(BEN, "hi", to="+15550001111")
        rig.wire.unsubscribed.add(BEN)
        result = await rig.adapter.send(BEN, "hello")
        await rig.adapter.disconnect()
        return result

    result = run(main())
    assert not result.ok
    for secret in (BEN, BEN[1:], AUTH_TOKEN, PHONE_NUMBER):
        assert secret not in caplog.text and secret not in (result.error or "")
    assert "0300" in caplog.text  # the masked form keeps the last digits
