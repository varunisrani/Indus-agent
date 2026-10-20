"""The WeCom callback adapter and its message crypto (P16-18): known-answer vectors made with
openssl from the vendor's description (``tests/fixtures/gateway/crypto_vectors``), the signature
checked in constant time before any decryption, XXE refused, and the corp API."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import re
import shutil
import subprocess
from collections.abc import Awaitable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import make_scope
from indus.gateway.adapters import _http
from indus.gateway.adapters._http import ListenerSettings
from indus.gateway.adapters.base import AdapterConnectError
from indus.gateway.adapters.wecom import create_callback_adapter
from indus.gateway.adapters.wecom.callback import WeComCallbackAdapter, utf8_bytes
from indus.gateway.adapters.wecom.crypto import (
    CallbackCrypto,
    CryptoError,
    UnsafeXml,
    decode_aes_key,
    parse_xml,
    unpad,
)
from indus.gateway.config import AdapterConfig, PlatformSettings
from indus.gateway.core.events import InboundEvent, MessageKind
from indus.gateway.core.send import ErrorKind
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FailureKind, FakeChatPlatform
from tests.fakes.platform.http_wire import Callback
from tests.fakes.platform.wecom_wire import AGENT_ID, CORP_ID, WeComCallbackWire

VECTORS_DIR = Path(__file__).parents[4] / "fixtures" / "gateway" / "crypto_vectors"
VECTORS: dict[str, Any] = json.loads((VECTORS_DIR / "wecom_callback.json").read_text())
HAS_CRYPTO = importlib.util.find_spec("cryptography") is not None
needs_crypto = pytest.mark.skipif(not HAS_CRYPTO, reason="needs cryptography (wecom extra)")
ANA = "ana"


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))


def run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # pyright: ignore[reportArgumentType]


def _vector_crypto() -> CallbackCrypto:
    return CallbackCrypto(VECTORS["token"], VECTORS["encoding_aes_key"], VECTORS["receive_id"])


# -- known answers (made with openssl, not our code) -----------------------------------------------


def test_signatures_match_the_vectors() -> None:
    crypto = _vector_crypto()
    for name in ("echostr", "message", "wrong_receive_id", "bad_padding"):
        entry = VECTORS[name]
        expected = crypto.signature(VECTORS["timestamp"], VECTORS["nonce"], entry["encrypted"])
        assert expected == entry["signature"], name
        assert crypto.verify(
            entry["signature"], VECTORS["timestamp"], VECTORS["nonce"], entry["encrypted"]
        )
        assert crypto.verify(
            entry["signature"].upper(), VECTORS["timestamp"], VECTORS["nonce"], entry["encrypted"]
        )
    assert decode_aes_key(VECTORS["encoding_aes_key"]).hex() == VECTORS["aes_key_hex"]


@needs_crypto
def test_decryption_matches_the_vectors() -> None:
    crypto = _vector_crypto()
    for name in ("echostr", "message"):
        entry = VECTORS[name]
        plain = crypto.open(
            entry["signature"], VECTORS["timestamp"], VECTORS["nonce"], entry["encrypted"]
        )
        assert plain.decode() == entry["plain"]
    with pytest.raises(CryptoError, match="receive id"):
        entry = VECTORS["wrong_receive_id"]
        crypto.open(entry["signature"], VECTORS["timestamp"], VECTORS["nonce"], entry["encrypted"])
    with pytest.raises(CryptoError, match="padding"):
        entry = VECTORS["bad_padding"]
        crypto.open(entry["signature"], VECTORS["timestamp"], VECTORS["nonce"], entry["encrypted"])


@needs_crypto
def test_encryption_matches_openssl_given_the_same_random_prefix() -> None:
    crypto = _vector_crypto()
    made = crypto.encrypt(VECTORS["message"]["plain"].encode(), prefix=bytes(range(16)))
    assert made == VECTORS["message"]["encrypted"]


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl command")
def test_the_vectors_file_is_what_the_script_makes() -> None:
    made = subprocess.run(
        ["sh", str(VECTORS_DIR / "make_wecom_vectors.sh")],
        capture_output=True,
        check=True,
        text=True,
        timeout=60,
    ).stdout
    assert json.loads(made) == VECTORS


def test_bad_inputs_fail_closed() -> None:
    crypto = _vector_crypto()
    timestamp, nonce = VECTORS["timestamp"], VECTORS["nonce"]
    with pytest.raises(CryptoError, match="bad signature"):
        crypto.open("0" * 40, timestamp, nonce, VECTORS["message"]["encrypted"])
    assert not crypto.verify(None, timestamp, nonce, "x")
    assert not crypto.verify("é", timestamp, nonce, "x")
    with pytest.raises(CryptoError):
        crypto.decrypt("not base64!!")
    with pytest.raises(CryptoError):
        crypto.decrypt("AAAA")  # not a whole AES block
    for bad in ("short", "a" * 44, "a" * 42 + "!"):
        with pytest.raises(CryptoError):
            decode_aes_key(bad)
    with pytest.raises(CryptoError):
        unpad(b"abc" + bytes([33]))
    with pytest.raises(CryptoError):
        unpad(b"ab" + bytes([1, 2]))
    with pytest.raises(CryptoError):
        unpad(b"")
    assert unpad(b"ab" + bytes([2, 2])) == b"ab"


# -- XML -------------------------------------------------------------------------------------------

XXE = (
    '<?xml version="1.0"?><!DOCTYPE xml [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
    "<xml><Encrypt>&x;</Encrypt></xml>"
)
LAUGHS = (
    '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
    '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
    "<xml><Encrypt>&lol2;</Encrypt></xml>"
)


def test_the_parser_refuses_dtds_entities_and_references() -> None:
    for payload in (XXE, LAUGHS, "<xml><A>&undefined;</A></xml>", "<xml><A>", "", "<a/><b/>"):
        with pytest.raises(UnsafeXml):
            parse_xml(payload)
    with pytest.raises(UnsafeXml):
        parse_xml("<xml>" + "<A>1</A>" * 300 + "</xml>")
    assert parse_xml("<xml><A><![CDATA[<b>&amp;</b>]]></A><A>2</A><B>x &amp; y</B></xml>") == {
        "A": "<b>&amp;</b>",
        "B": "x & y",
    }


# -- the adapter over the callback wire ------------------------------------------------------------


class Rig:
    def __init__(self, **kw: Any) -> None:
        self.clock = _clock()
        self.platform = FakeChatPlatform(
            "wecom_callback", clock=self.clock, max_len=2048, len_fn=utf8_bytes
        )
        self.wire = WeComCallbackWire(self.platform)
        self.adapter = WeComCallbackAdapter(
            clock=self.clock,
            corp_id=CORP_ID,
            corp_secret=self.wire.corp_secret,
            agent_id=AGENT_ID,
            token=self.wire.token,
            encoding_aes_key=self.wire.aes_key,
            listener=ListenerSettings(port=0, max_body_bytes=65_536, path="/wecom/callback"),
            transport=self.wire.transport(),
            api_base=self.wire.base_url,
            serve=False,
            **kw,
        )
        self.wire.attach(self.adapter.listener.app)
        self.events: list[InboundEvent] = []
        self.adapter.set_sink(self.sink)

    async def sink(self, event: InboundEvent) -> None:
        self.events.append(event)

    async def connect(self) -> None:
        await self.adapter.connect()

    async def deliver(self, callback: Callback) -> httpx.Response:
        response = await self.wire.deliver(callback)
        await self.adapter.listener.idle()
        return response

    def inner(self, **fields: str) -> str:
        base = {
            "ToUserName": CORP_ID,
            "FromUserName": ANA,
            "CreateTime": str(int(self.clock.now().timestamp())),
            "MsgType": "text",
            "Content": "hi",
            "MsgId": "100",
            "AgentID": AGENT_ID,
        }
        base.update(fields)
        return "<xml>" + "".join(f"<{k}><![CDATA[{v}]]></{k}>" for k, v in base.items()) + "</xml>"


@needs_crypto
def test_url_verification_echoes_the_decrypted_echostr() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        crypto = rig.wire.crypto()
        echo = crypto.encrypt(b"4471859337394731221")
        stamp = str(int(rig.clock.now().timestamp()))
        query = httpx.QueryParams(
            {
                "msg_signature": crypto.signature(stamp, "n1", echo),
                "timestamp": stamp,
                "nonce": "n1",
                "echostr": echo,
            }
        )
        ok = await rig.deliver(Callback("/wecom/callback", b"", {}, "GET", str(query)))
        assert ok.status_code == 200 and ok.text == "4471859337394731221"
        forged = httpx.QueryParams({**dict(query), "msg_signature": "0" * 40})
        bad = await rig.deliver(Callback("/wecom/callback", b"", {}, "GET", str(forged)))
        assert bad.status_code == 401 and "4471859337394731221" not in bad.text
        await rig.adapter.disconnect()

    run(main())


@needs_crypto
def test_xxe_payloads_are_refused_before_any_event() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        outer = Callback(
            "/wecom/callback",
            XXE.encode(),
            {"Content-Type": "text/xml"},
            "POST",
            "msg_signature=x&timestamp=1&nonce=n",
        )
        assert (await rig.deliver(outer)).status_code == 400
        laughs = Callback("/wecom/callback", LAUGHS.encode(), {}, "POST", "")
        assert (await rig.deliver(laughs)).status_code == 400
        # A correctly signed and encrypted message whose inner XML is hostile.
        inner_xxe = rig.wire.signed(XXE.replace("<Encrypt>&x;</Encrypt>", "<Content>&x;</Content>"))
        assert (await rig.deliver(inner_xxe)).status_code == 400
        assert rig.events == []
        await rig.adapter.disconnect()

    run(main())


@needs_crypto
def test_the_signature_is_checked_in_constant_time_before_decrypting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compared: list[tuple[bytes, bytes]] = []
    real_compare = _http._compare  # pyright: ignore[reportPrivateUsage]

    def spy(a: bytes, b: bytes) -> bool:
        compared.append((a, b))
        return real_compare(a, b)

    decrypted: list[str] = []
    real_decrypt = CallbackCrypto.decrypt

    def decrypt_spy(self: CallbackCrypto, encrypted: str) -> bytes:
        decrypted.append(encrypted)
        return real_decrypt(self, encrypted)

    monkeypatch.setattr(_http, "_compare", spy)
    monkeypatch.setattr(CallbackCrypto, "decrypt", decrypt_spy)

    async def main() -> None:
        rig = Rig()
        await rig.connect()
        good = rig.wire.signed(rig.inner())
        forged = Callback(
            good.path,
            good.body,
            good.headers,
            good.method,
            good.query.replace("msg_signature=", "msg_signature=0"),
        )
        response = await rig.deliver(forged)
        assert response.status_code == 401
        assert compared and decrypted == []  # compared, never decrypted
        assert (await rig.deliver(good)).status_code == 200
        assert len(decrypted) == 1 and [e.text for e in rig.events] == ["hi"]
        await rig.adapter.disconnect()

    run(main())


@needs_crypto
def test_stale_timestamps_wrong_corps_and_repeats() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        old = str(int(rig.clock.now().timestamp()) - 301)
        stale = rig.wire.signed(rig.inner(MsgId="1"), timestamp=old)
        assert (await rig.deliver(stale)).status_code == 401
        other = CallbackCrypto(rig.wire.token, rig.wire.aes_key, "wwsomebodyelse")
        encrypted = other.encrypt(rig.inner(MsgId="2").encode())
        stamp = str(int(rig.clock.now().timestamp()))
        outer = f"<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>".encode()
        query = httpx.QueryParams(
            {
                "msg_signature": other.signature(stamp, "n", encrypted),
                "timestamp": stamp,
                "nonce": "n",
            }
        )
        wrong = await rig.deliver(Callback("/wecom/callback", outer, {}, "POST", str(query)))
        assert wrong.status_code == 400
        first = await rig.deliver(rig.wire.signed(rig.inner(MsgId="3", Content="once")))
        again = await rig.deliver(rig.wire.signed(rig.inner(MsgId="3", Content="once")))
        assert first.text == again.text == "success"
        assert [e.text for e in rig.events] == ["once"]
        await rig.adapter.disconnect()

    run(main())


@needs_crypto
def test_message_types_and_lifecycle_events() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        for fields in (
            {"MsgType": "event", "Event": "enter_agent", "MsgId": ""},
            {
                "MsgType": "location",
                "Label": "Office",
                "Location_X": "31.2",
                "Location_Y": "121.5",
                "MsgId": "4",
            },
            {"MsgType": "link", "Title": "Docs", "Url": "https://example.org", "MsgId": "5"},
            {"MsgType": "voice", "MediaId": "mid-v", "Format": "amr", "MsgId": "6"},
        ):
            assert (await rig.deliver(rig.wire.signed(rig.inner(**fields)))).text == "success"
        got = [(e.kind, e.text, [m.ref for m in e.media]) for e in rig.events]
        assert got == [
            (MessageKind.LOCATION, "Office (31.2, 121.5)", []),
            (MessageKind.TEXT, "Docs https://example.org", []),
            (MessageKind.VOICE, "", ["wecom-media://mid-v"]),
        ]
        rig.wire.media["mid-v"] = b"amr bytes"
        data = b"".join([c async for c in rig.adapter.fetch_media(rig.events[-1].media[0])])
        assert data == b"amr bytes"
        await rig.adapter.disconnect()

    run(main())


@needs_crypto
def test_oversized_bodies_are_refused_unread() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        body = b"<xml>" + b"x" * 70_000 + b"</xml>"
        response = await rig.deliver(Callback("/wecom/callback", body, {}, "POST", ""))
        assert response.status_code == 413
        await rig.adapter.disconnect()

    run(main())


# -- outbound --------------------------------------------------------------------------------------


@needs_crypto
def test_replies_are_split_by_bytes_and_tokens_are_refreshed_once() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        text = "字" * 1500  # 4,500 bytes
        result = await rig.adapter.send(ANA, text)
        assert result.ok and len(result.ids) == 3
        texts = rig.platform.texts(ANA)
        assert all(len(t.encode()) <= 2048 for t in texts)
        assert "".join(re.sub(r"\s*\(\d+/\d+\)$", "", t) for t in texts) == text
        rig.wire.revoked.add(rig.wire.access_token)
        again = await rig.adapter.send(ANA, "after expiry")
        assert again.ok and rig.wire.token_requests == 2
        sent = rig.wire.calls_to("POST", "/cgi-bin/message/send")[-1].json()
        assert sent["agentid"] == int(AGENT_ID) and sent["msgtype"] == "text"
        await rig.adapter.disconnect()

    run(main())


@needs_crypto
def test_api_errors_are_classified() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.platform.fail(FailureKind.RATE_LIMIT, op="send", retry_after=11)
        limited = await rig.adapter.send(ANA, "x")
        assert limited.kind is ErrorKind.RATE_LIMITED and limited.retry_after == 11
        rig.platform.fail(FailureKind.CHAT_NOT_FOUND, op="send")
        gone = await rig.adapter.send(ANA, "x")
        assert gone.chat_unreachable
        await rig.adapter.disconnect()

    run(main())


@needs_crypto
def test_the_token_and_secret_never_reach_logs(caplog: pytest.LogCaptureFixture) -> None:
    seen: dict[str, str] = {}

    async def main() -> None:
        rig = Rig()
        await rig.connect()
        await rig.adapter.send(ANA, "hello")
        seen["token"] = rig.wire.access_token
        seen["secret"] = rig.wire.corp_secret
        assert any(c.params.get("corpsecret") == rig.wire.corp_secret for c in rig.wire.calls)
        rig.platform.fail(FailureKind.FORBIDDEN, op="send")
        failed = await rig.adapter.send(ANA, f"leak {rig.wire.access_token}")
        assert rig.wire.access_token not in (failed.error or "")
        await rig.adapter.disconnect()

    for name in ("httpx", "httpcore"):
        caplog.set_level(logging.DEBUG, logger=name)
    with caplog.at_level(logging.DEBUG):
        run(main())
    assert "HTTP Request" in caplog.text and "/cgi-bin/gettoken" in caplog.text
    assert seen["token"] not in caplog.text and seen["secret"] not in caplog.text


# -- connecting ------------------------------------------------------------------------------------


def test_bad_configuration_is_a_fatal_connect_error() -> None:
    async def main() -> None:
        clock = _clock()
        wire = WeComCallbackWire(FakeChatPlatform("wecom_callback", clock=clock))
        settings = ListenerSettings(port=0, max_body_bytes=65_536, path="/wecom/callback")

        def make(**kw: str) -> WeComCallbackAdapter:
            return WeComCallbackAdapter(
                clock=clock,
                corp_id=CORP_ID,
                corp_secret=kw.get("corp_secret", wire.corp_secret),
                agent_id=kw.get("agent_id", AGENT_ID),
                token=wire.token,
                encoding_aes_key=kw.get("encoding_aes_key", wire.aes_key),
                listener=settings,
                transport=wire.transport(),
                api_base=wire.base_url,
                serve=False,
            )

        for kw, needle in (
            ({"agent_id": ""}, "WECOM_CALLBACK_AGENT_ID"),
            ({"agent_id": "abc"}, "numeric agent id"),
            ({"encoding_aes_key": "short"}, "ENCODING_AES_KEY"),
        ):
            with pytest.raises(AdapterConnectError, match=needle) as raised:
                await make(**kw).connect()
            assert not raised.value.retryable
        with pytest.raises(AdapterConnectError) as refused:
            await make(corp_secret="wrong").connect()
        assert refused.value.code == "auth_failed" and "wrong" not in str(refused.value)

    run(main())


def test_create_callback_adapter_reads_secrets_and_listener_keys(tmp_path: Path) -> None:
    env = {
        "WECOM_CALLBACK_CORP_ID": CORP_ID,
        "WECOM_CALLBACK_CORP_SECRET": "c" * 30,
        "WECOM_CALLBACK_TOKEN": "t" * 12,
        "WECOM_CALLBACK_ENCODING_AES_KEY": VECTORS["encoding_aes_key"],
        "WECOM_CALLBACK_AGENT_ID": "1000009",
    }
    scope = make_scope(tmp_path / "home", env=env, clock=_clock())
    section = PlatformSection.model_validate({"extra": {"webhook_port": 9001}})
    settings = PlatformSettings("wecom_callback", True, "credentials", section)
    adapter = create_callback_adapter(AdapterConfig("wecom_callback", settings, scope, scope.clock))
    assert adapter.agent_id == "1000009" and adapter.corp_id == CORP_ID
    assert adapter.listener.settings.port == 9001
    assert adapter.listener.settings.host == "127.0.0.1"
    assert adapter.listener.settings.path == "/wecom/callback"
    assert "c" * 30 not in adapter.scrub("secret " + "c" * 30)
    public = PlatformSection.model_validate({"extra": {"webhook_host": "0.0.0.0"}})
    with pytest.raises(ValueError, match="webhook_public"):
        create_callback_adapter(
            AdapterConfig(
                "wecom_callback",
                PlatformSettings("wecom_callback", True, "credentials", public),
                scope,
                scope.clock,
            )
        )
