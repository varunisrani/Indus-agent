"""The callback listener kit: settings, the listener, signatures, replay, outbound results
(P16-01), and the sample callback adapter through the shared contract suite."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import socket
from collections.abc import Awaitable, Callable, Iterator, MutableMapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from indus.foundation.clock import SystemClock
from indus.gateway.adapters import _http
from indus.gateway.adapters._http import (
    DEFAULT_HOST,
    CallbackListener,
    CallbackRequest,
    ListenerError,
    ListenerSettings,
    ReplayWindow,
    Reply,
    SeenIds,
    constant_time_equal,
    failure_from_exception,
    failure_from_status,
    hmac_sha1_url_signature,
    hmac_sha256_base64,
    hmac_sha256_hex,
    retry_after_seconds,
    sha1_sorted_signature,
    verify_hmac_sha256_base64,
    verify_hmac_sha256_hex,
    verify_sha1_sorted,
    verify_url_signature,
)
from indus.gateway.adapters.base import AdapterConnectError
from indus.gateway.core.events import InboundEvent
from indus.gateway.core.send import ErrorKind
from tests.contract.gateway.conftest import CLOCK_START, SocketGuard, run_check, settle
from tests.contract.gateway.test_adapter_contract import CHECKS, check_no_real_network
from tests.fakes.clock import FakeClock
from tests.unit.gateway.adapters.http_sample import (
    SIGNING_SECRET,
    HookAdapter,
    build,
    hook_case,
    make_env,
    sign,
)

SECRET = "".join(["shh", "-", "k" * 12])


def run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # pyright: ignore[reportArgumentType]


def clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))


def settings(**kw: Any) -> ListenerSettings:
    values: dict[str, Any] = {"port": 0, "max_body_bytes": 1024, "path": "/in"}
    values.update(kw)
    return ListenerSettings(**values)


def asgi_client(listener: CallbackListener) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=listener.app, client=("198.51.100.4", 5555))  # pyright: ignore[reportArgumentType]
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8090")


# -- settings -----------------------------------------------------------------------------------


def test_settings_default_to_loopback_and_the_adapters_values() -> None:
    made = ListenerSettings.from_extra({}, port=8090, max_body_bytes=3 * 1024 * 1024, path="/wa")
    assert made == ListenerSettings(port=8090, max_body_bytes=3 * 1024 * 1024, path="/wa")
    assert made.host == DEFAULT_HOST and not made.public and made.public_url is None


def test_settings_read_the_platform_keys() -> None:
    extra = {
        "webhook_host": "0.0.0.0",
        "webhook_port": "9001",
        "webhook_path": "/cb",
        "webhook_public": "true",
        "webhook_public_url": "https://example.org/cb",
        "webhook_max_body_bytes": 2048,
    }
    made = ListenerSettings.from_extra(extra, port=8090, max_body_bytes=1024)
    assert (made.host, made.port, made.path, made.public) == ("0.0.0.0", 9001, "/cb", True)
    assert made.public_url == "https://example.org/cb" and made.max_body_bytes == 2048


@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        ({"webhook_host": "0.0.0.0"}, "webhook_public: true"),
        ({"webhook_host": "192.0.2.10", "webhook_public": "no"}, "not a loopback"),
        ({"webhook_port": "eighty"}, "whole number"),
        ({"webhook_port": True}, "whole number"),
        ({"webhook_port": 70000}, "not a port"),
        ({"webhook_path": "hook"}, "must start with /"),
        ({"webhook_path": "/health"}, "not be /health"),
        ({"webhook_max_body_bytes": 0}, "between 1"),
        ({"webhook_max_body_bytes": 64 * 1024 * 1024}, "between 1"),
        ({"webhook_public_url": "ftp://x"}, "http(s) URL"),
    ],
)
def test_bad_settings_are_refused(extra: dict[str, Any], needle: str) -> None:
    with pytest.raises(ListenerError, match=needle.replace("(", r"\(").replace(")", r"\)")):
        ListenerSettings.from_extra(extra, port=8090, max_body_bytes=1024)


def test_a_public_bind_refused_at_start_is_a_fatal_connect_error() -> None:
    async def main() -> None:
        listener = CallbackListener(
            settings(host="0.0.0.0"), name="demo", clock=clock(), serve=False
        )
        with pytest.raises(AdapterConnectError) as raised:
            await listener.start()
        assert not raised.value.retryable and raised.value.code == "listener_config"
        assert not listener.accepting

    run(main())


# -- the listener, in-process -------------------------------------------------------------------


def echo_listener(**kw: Any) -> tuple[CallbackListener, list[CallbackRequest]]:
    seen: list[CallbackRequest] = []

    async def handler(request: CallbackRequest) -> Reply:
        seen.append(request)
        return Reply.json({"got": len(request.body)})

    listener = CallbackListener(settings(**kw), name="demo", clock=clock(), serve=False)
    listener.route("/in", handler)
    return listener, seen


def test_routes_answer_only_while_started() -> None:
    async def main() -> None:
        listener, seen = echo_listener()
        async with asgi_client(listener) as client:
            before = await client.post("/in", content=b"x")
            assert before.status_code == 503 and before.headers["retry-after"] == "5"
            assert (await client.get("/health")).json() == {
                "status": "stopping",
                "platform": "demo",
            }
            await listener.start()
            ok = await client.post("/in", content=b"hello")
            assert ok.status_code == 200 and ok.json() == {"got": 5}
            assert ok.headers["x-content-type-options"] == "nosniff"
            health = await client.get("/health")
            assert health.status_code == 200 and health.json()["status"] == "ok"
            assert (await client.head("/health")).status_code == 200
            assert (await client.post("/health")).status_code == 405
            assert (await client.post("/other")).status_code == 404
            wrong = await client.get("/in")
            assert wrong.status_code == 405 and wrong.headers["allow"] == "POST"
            await listener.stop()
            assert (await client.post("/in", content=b"x")).status_code == 503
        assert len(seen) == 1

    run(main())


def test_the_request_carries_what_handlers_need() -> None:
    async def main() -> None:
        listener, seen = echo_listener()
        await listener.start()
        async with asgi_client(listener) as client:
            await client.post(
                "/in?b=2&a=1&a=3",
                content=b"To=%2B1555&Body=hi+there",
                headers={"X-Thing": "one", "Content-Type": "application/x-www-form-urlencoded"},
            )
        request = seen[0]
        assert request.method == "POST" and request.path == "/in"
        assert request.query_string == "b=2&a=1&a=3"
        assert request.query_params() == {"b": "2", "a": "1"}
        assert request.header("X-THING") == "one" and request.client == "198.51.100.4"
        assert request.form() == {"To": "+1555", "Body": "hi there"}
        assert request.received_at == CLOCK_START
        assert (
            request.signed_url("https://example.org/sms") == "https://example.org/sms?b=2&a=1&a=3"
        )
        assert request.signed_url() == "http://127.0.0.1:8090/in?b=2&a=1&a=3"
        with pytest.raises(_http.BadPayload):
            request.json()

    run(main())


def test_a_handler_error_is_a_bare_500_and_is_logged_scrubbed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def main() -> None:
        async def broken(request: CallbackRequest) -> Reply:
            raise RuntimeError(f"vendor said no to {SECRET}")

        listener = CallbackListener(
            settings(),
            name="demo",
            clock=clock(),
            serve=False,
            scrub=lambda text: text.replace(SECRET, "***"),
        )
        listener.route("/in", broken)
        await listener.start()
        async with asgi_client(listener) as client:
            answer = await client.post("/in", content=b"{}")
        assert answer.status_code == 500 and answer.json() == {"error": "internal error"}
        assert SECRET not in answer.text

    run(main())
    assert "vendor said no to ***" in caplog.text and SECRET not in caplog.text


def test_a_declared_oversized_body_is_refused_before_any_byte_is_read() -> None:
    async def main() -> None:
        listener, seen = echo_listener(max_body_bytes=10)
        await listener.start()
        async with asgi_client(listener) as client:
            big = await client.post("/in", content=b"x" * 11)
            assert big.status_code == 413
            assert (await client.post("/in", content=b"x" * 10)).status_code == 200
        assert len(seen) == 1

    run(main())


type Message = MutableMapping[str, Any]


async def raw_call(
    listener: CallbackListener, chunks: list[bytes], headers: list[tuple[bytes, bytes]]
) -> tuple[int, int]:
    """Call the app with a body streamed in ``chunks``; (status, chunks the app pulled)."""
    pulled = 0
    status: list[int] = []

    async def receive() -> Message:
        nonlocal pulled
        if pulled < len(chunks):
            pulled += 1
            return {
                "type": "http.request",
                "body": chunks[pulled - 1],
                "more_body": pulled < len(chunks),
            }
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            status.append(message["status"])

    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": "/in",
        "query_string": b"",
        "headers": headers,
        "client": ("198.51.100.4", 1),
    }
    await listener.app(scope, receive, send)
    return status[0], pulled


def test_a_streamed_body_is_cut_at_the_cap_and_the_rest_is_never_read() -> None:
    async def main() -> None:
        listener, seen = echo_listener(max_body_bytes=10)
        await listener.start()
        status, pulled = await raw_call(listener, [b"abcd"] * 100, [])
        assert status == 413
        assert pulled == 3  # 12 bytes are over the 10-byte cap; 97 chunks never pulled
        assert not seen
        status, pulled = await raw_call(listener, [b"x"], [(b"content-length", b"99")])
        assert (status, pulled) == (413, 0)
        status, pulled = await raw_call(listener, [b"x"], [(b"content-length", b"ten")])
        assert (status, pulled) == (400, 0)

    run(main())


def test_route_rules() -> None:
    listener, _ = echo_listener()

    async def handler(request: CallbackRequest) -> Reply:
        return Reply.empty()

    with pytest.raises(ValueError, match="already registered"):
        listener.route("/in", handler)
    with pytest.raises(ValueError, match="/health"):
        listener.route("/health", handler)
    with pytest.raises(ValueError, match="start with /"):
        listener.route("in", handler)
    with pytest.raises(ValueError, match="at least one method"):
        listener.route("/x", handler, methods=())


def test_stop_drains_then_cancels_a_stuck_handler_and_background_work() -> None:
    async def main() -> None:
        release = asyncio.Event()
        cancelled: list[str] = []

        async def slow(request: CallbackRequest) -> Reply:
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.append("handler")
                raise
            return Reply.empty()

        async def forever() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append("background")
                raise

        listener = CallbackListener(settings(), name="demo", clock=clock(), serve=False)
        listener.route("/in", slow)
        await listener.start()
        listener.background(forever())
        async with asgi_client(listener) as client:
            pending = asyncio.ensure_future(client.post("/in", content=b"x"))
            await asyncio.wait_for(_until(lambda: bool(listener._handlers)), 5)  # pyright: ignore[reportPrivateUsage]
            loop = asyncio.get_running_loop()
            started = loop.time()
            await listener.stop(budget=0.4)
            assert loop.time() - started < 1.0
            answer = await asyncio.wait_for(pending, 5)
        assert answer.status_code == 503
        assert sorted(cancelled) == ["background", "handler"]
        await listener.stop()  # a second stop does nothing

    run(main())


async def _until(check: Callable[[], bool]) -> None:
    while not check():
        idle = asyncio.get_running_loop().create_future()
        await asyncio.wait({idle}, timeout=0.002)
        idle.cancel()


# -- the listener on a real loopback socket -----------------------------------------------------


def test_the_listener_serves_on_loopback_and_stops_within_the_budget() -> None:
    async def main() -> None:
        gate = asyncio.Event()

        async def handler(request: CallbackRequest) -> Reply:
            if request.body == b"wait":
                await gate.wait()
            return Reply.text("ok")

        listener = CallbackListener(settings(), name="demo", clock=SystemClock())
        listener.route("/in", handler)
        await listener.start()
        address = listener.address
        assert address is not None and address[0] == "127.0.0.1" and address[1] > 0
        base = f"http://127.0.0.1:{address[1]}"
        async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=10) as client:
            assert (await client.get("/health")).json()["status"] == "ok"
            assert (await client.post("/in", content=b"hi")).text == "ok"
            assert (await client.post("/in", content=b"x" * 2000)).status_code == 413
            stuck = asyncio.ensure_future(client.post("/in", content=b"wait"))
            await asyncio.wait_for(_until(lambda: bool(listener._handlers)), 5)  # pyright: ignore[reportPrivateUsage]
            loop = asyncio.get_running_loop()
            started = loop.time()
            await listener.stop(budget=1.0)
            assert loop.time() - started < 1.5
            answer = await asyncio.wait_for(stuck, 5)
            assert answer.status_code == 503
        assert listener.address is None
        with pytest.raises(OSError), socket.create_connection(address, timeout=1):
            pass

    run(main())


def test_a_busy_port_is_a_retryable_connect_error() -> None:
    async def main() -> None:
        first = CallbackListener(settings(), name="one", clock=SystemClock())
        await first.start()
        try:
            address = first.address
            assert address is not None
            second = CallbackListener(
                settings(port=address[1]), name="two", clock=FakeClock(CLOCK_START)
            )
            task = asyncio.ensure_future(second.start())
            with pytest.raises(AdapterConnectError) as raised:
                await settle_fake(second.clock, task)
            assert raised.value.retryable and raised.value.code == "listener_bind"
        finally:
            await first.stop()

    run(main())


async def settle_fake(fake: Any, task: asyncio.Future[Any]) -> Any:
    for _ in range(50):
        await fake.run_until_idle()
        if task.done():
            return task.result()
        fake.advance(1.0)
    raise AssertionError("never finished")


# -- signatures ---------------------------------------------------------------------------------


BODY = b'{"object":"whatsapp_business_account"}'


def test_hmac_sha256_known_answers() -> None:
    expected = hmac.new(SECRET.encode(), BODY, hashlib.sha256)
    assert hmac_sha256_hex(SECRET, BODY) == expected.hexdigest()
    assert hmac_sha256_base64(SECRET, BODY) == base64.b64encode(expected.digest()).decode()
    # RFC 4231 test case 2.
    rfc = hmac_sha256_hex(b"Jefe", b"what do ya want for nothing?")
    assert rfc == "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"


def test_hex_signatures_verify_with_prefix_and_either_case() -> None:
    good = "sha256=" + hmac_sha256_hex(SECRET, BODY)
    assert verify_hmac_sha256_hex(SECRET, BODY, good, prefix="sha256=")
    assert verify_hmac_sha256_hex(SECRET, BODY, good.upper(), prefix="sha256=")
    assert verify_hmac_sha256_hex(SECRET, BODY, f"  {good} ", prefix="sha256=")
    assert not verify_hmac_sha256_hex(SECRET, BODY, good[:-1] + "0", prefix="sha256=")
    assert not verify_hmac_sha256_hex(SECRET, BODY + b" ", good, prefix="sha256=")
    assert not verify_hmac_sha256_hex(SECRET, BODY, good.removeprefix("sha256="), prefix="sha256=")
    assert not verify_hmac_sha256_hex(SECRET, BODY, None, prefix="sha256=")
    assert not verify_hmac_sha256_hex(SECRET, BODY, "sha256=ünïcode", prefix="sha256=")
    assert not verify_hmac_sha256_hex(SECRET, BODY, "")


def test_base64_signatures_verify_exactly() -> None:
    good = hmac_sha256_base64(SECRET, BODY)
    assert verify_hmac_sha256_base64(SECRET, BODY, good)
    assert not verify_hmac_sha256_base64(SECRET, BODY, good.lower())
    assert not verify_hmac_sha256_base64(SECRET, BODY, None)
    assert not verify_hmac_sha256_base64("other", BODY, good)


def test_sha1_sorted_signature() -> None:
    parts = ("tok", "1700000000", "nonce42", "ENCRYPTED")
    expected = hashlib.sha1("".join(sorted(parts)).encode()).hexdigest()
    assert sha1_sorted_signature(*parts) == expected
    assert sha1_sorted_signature(*reversed(parts)) == expected
    assert verify_sha1_sorted(expected.upper(), *parts)
    assert not verify_sha1_sorted(expected, "tok", "1700000000", "nonce43", "ENCRYPTED")
    assert not verify_sha1_sorted(None, *parts)


def test_url_signature_covers_the_url_and_sorted_params() -> None:
    url = "https://example.org/sms?profile=work"
    params = {"To": "+15550001", "From": "+15550002", "Body": "hi"}
    signed = url + "Body" + "hi" + "From" + "+15550002" + "To" + "+15550001"
    expected = base64.b64encode(
        hmac.new(SECRET.encode(), signed.encode(), hashlib.sha1).digest()
    ).decode()
    assert hmac_sha1_url_signature(SECRET, url, params) == expected
    assert hmac_sha1_url_signature(SECRET, url, list(params.items())[::-1]) == expected
    assert verify_url_signature(SECRET, url, params, expected)
    assert not verify_url_signature(SECRET, url.split("?")[0], params, expected)
    assert not verify_url_signature(SECRET, url, {**params, "Body": "hj"}, expected)


def test_every_check_compares_in_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(_http, "_compare", spy)
    assert not verify_hmac_sha256_hex(SECRET, BODY, "sha256=" + "0" * 64, prefix="sha256=")
    assert not verify_hmac_sha256_hex(SECRET, BODY, None, prefix="sha256=")
    assert not verify_hmac_sha256_base64(SECRET, BODY, "AAAA")
    assert not verify_sha1_sorted("00", "a", "b")
    assert not verify_url_signature(SECRET, "https://x", {}, "AAAA")
    assert constant_time_equal("same", "same")
    assert len(calls) == 6


# -- replay -------------------------------------------------------------------------------------


def test_replay_window_refuses_stale_and_repeated_callbacks() -> None:
    fake = clock()
    window = ReplayWindow(fake, 300.0)
    now = fake.now().timestamp()
    assert window.accept(now, "a")
    assert not window.accept(now, "a")  # repeat
    assert not window.accept(now - 301, "b")  # stale
    assert window.accept(now - 299, "b")  # a stale attempt did not record the key
    assert window.fresh(str(int(now + 300)))
    assert window.fresh((now - 10) * 1000, unit="ms")
    assert not window.fresh(None) and not window.fresh("soon") and not window.fresh("nan")
    fake.advance(601)
    assert window.accept(fake.now().timestamp(), "a")  # remembered for the window both ways


def test_seen_ids_expire_and_stay_bounded() -> None:
    fake = clock()
    seen = SeenIds(fake, ttl_s=60.0, max_entries=3)
    assert all(seen.first_time(k) for k in ("1", "2", "3"))
    assert not seen.first_time("2")
    assert seen.first_time("4") and "1" not in seen and len(seen) == 3
    fake.advance(61)
    assert len(seen) == 0 and seen.first_time("2")
    seen.forget("2")
    assert seen.first_time("2")
    with pytest.raises(ValueError):
        SeenIds(fake, ttl_s=0)


# -- outbound results (R06.4) -------------------------------------------------------------------


REQUEST = httpx.Request("POST", "https://api.example.test/send")


@pytest.mark.parametrize(
    ("exc", "kind", "retryable", "maybe"),
    [
        (httpx.ConnectTimeout("timed out", request=REQUEST), ErrorKind.TRANSIENT, True, False),
        (httpx.ConnectError("refused", request=REQUEST), ErrorKind.TRANSIENT, True, False),
        (httpx.PoolTimeout("timed out", request=REQUEST), ErrorKind.TRANSIENT, True, False),
        (httpx.ReadTimeout("timed out", request=REQUEST), ErrorKind.UNKNOWN, False, True),
        (httpx.WriteTimeout("timed out", request=REQUEST), ErrorKind.UNKNOWN, False, True),
        (httpx.ReadError("reset", request=REQUEST), ErrorKind.UNKNOWN, False, True),
        (httpx.RemoteProtocolError("closed", request=REQUEST), ErrorKind.UNKNOWN, False, True),
    ],
)
def test_httpx_failures_are_classified_by_class(
    exc: Exception, kind: ErrorKind, retryable: bool, maybe: bool
) -> None:
    result = failure_from_exception(exc)
    assert not result.ok and result.kind is kind
    assert result.retryable is retryable and result.maybe_delivered is maybe


def test_a_read_timeout_is_attempted_once_and_a_connect_failure_retried() -> None:
    """R06.4 through the real retry policy."""
    from indus.gateway.core.send import RetryPolicy, SendResult, send_with_retry

    async def main() -> tuple[int, int]:
        counts = {"read": 0, "connect": 0}

        def attempt_with(name: str, exc: Exception) -> Callable[[str, bool], Awaitable[SendResult]]:
            async def attempt(body: str, plain: bool) -> SendResult:
                counts[name] += 1
                return failure_from_exception(exc)

            return attempt

        fake = clock()
        for name, exc in (
            ("read", httpx.ReadTimeout("t", request=REQUEST)),
            ("connect", httpx.ConnectTimeout("timed out", request=REQUEST)),
        ):
            task = asyncio.ensure_future(
                send_with_retry(
                    attempt_with(name, exc),
                    "hi",
                    clock=fake,
                    policy=RetryPolicy(),
                    rng=lambda: 0.0,
                    notice=None,
                )
            )
            await settle_fake(fake, task)
        return counts["read"], counts["connect"]

    assert run(main()) == (1, 3)


NOW = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


def test_retry_after_reads_seconds_and_dates() -> None:
    assert retry_after_seconds("7", NOW) == 7.0
    later = (NOW + timedelta(seconds=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert retry_after_seconds(later, NOW) == 30.0
    assert retry_after_seconds("-3", NOW) == 0.0
    assert retry_after_seconds(None, NOW) is None
    assert retry_after_seconds("whenever", NOW) is None


def test_vendor_statuses_are_classified() -> None:
    limited = failure_from_status(429, "slow", {"Retry-After": "7"}, now=NOW)
    assert limited.kind is ErrorKind.RATE_LIMITED and limited.retry_after == 7 and limited.retryable
    gone = failure_from_status(404, '{"error":"chat not found"}', now=NOW)
    assert gone.kind is ErrorKind.NOT_FOUND and gone.chat_unreachable
    read = failure_from_status(404, "nothing", now=NOW, reading="chat not found")
    assert read.kind is ErrorKind.NOT_FOUND and "(chat not found)" in (read.error or "")
    assert failure_from_status(404, "nothing", now=NOW).kind is ErrorKind.UNKNOWN
    blocked = failure_from_status(403, "nope", now=NOW)
    assert blocked.kind is ErrorKind.FORBIDDEN and blocked.chat_unreachable
    auth = failure_from_status(401, "Forbidden: invalid token", now=NOW)
    assert auth.kind is ErrorKind.UNKNOWN and not auth.chat_unreachable and not auth.retryable
    assert failure_from_status(413, "", now=NOW).kind is ErrorKind.TOO_LONG
    assert failure_from_status(400, "can't parse entities", now=NOW).kind is ErrorKind.BAD_FORMAT
    down = failure_from_status(503, "", now=NOW)
    assert down.kind is ErrorKind.TRANSIENT and down.retryable
    paused = failure_from_status(503, "", {"retry-after": "12"}, now=NOW)
    assert paused.kind is ErrorKind.RATE_LIMITED and paused.retry_after == 12
    for status in (502, 504):
        unsure = failure_from_status(status, "upstream", now=NOW)
        assert unsure.maybe_delivered and not unsure.retryable
    plain = failure_from_status(500, "boom", now=NOW)
    assert plain.kind is ErrorKind.UNKNOWN and not plain.maybe_delivered
    long = failure_from_status(400, "x" * 5000, now=NOW)
    assert len(long.error or "") < 400


# -- the sample callback adapter ----------------------------------------------------------------


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[SocketGuard]:
    guard = SocketGuard()

    def refuse(*args: object, **kwargs: object) -> Any:
        raise guard.refuse(args[-1] if args else None)

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield guard


@pytest.mark.parametrize("rule", sorted(CHECKS))
def test_the_sample_callback_adapter_passes_the_contract(
    rule: str, no_network: SocketGuard
) -> None:
    run_check(hook_case(), CHECKS[rule])
    assert not no_network.attempts


def test_the_sample_callback_adapter_uses_no_real_network(no_network: SocketGuard) -> None:
    check_no_real_network(hook_case(), no_network)


async def _connected() -> tuple[Any, HookAdapter, list[InboundEvent]]:
    env = make_env(FakeClock(CLOCK_START))
    adapter = build(env)
    events: list[InboundEvent] = []

    async def sink(event: InboundEvent) -> None:
        events.append(event)

    adapter.set_sink(sink)
    await settle(env, adapter.connect())
    return env, adapter, events


def test_callback_signature_replay_and_payload_checks(no_network: SocketGuard) -> None:
    async def main() -> None:
        env, adapter, events = await _connected()
        wire = env.wire
        item = env.platform.dm("200", "hello")
        good = wire.render(env.platform.drain_inbound()[0])
        assert good is not None

        forged = dict(good.headers, **{"X-Hook-Signature": "sha256=" + "0" * 64})
        assert (await wire.deliver(type(good)(good.path, good.body, forged))).status_code == 401
        other_key = sign("not-the-secret", good.headers["X-Hook-Timestamp"], good.body)
        swapped = dict(good.headers, **{"X-Hook-Signature": other_key})
        assert (await wire.deliver(type(good)(good.path, good.body, swapped))).status_code == 401

        old = str(int(env.clock.now().timestamp()) - 600)
        stale = wire.signed(good.body, "d-old", timestamp=old)
        assert (await wire.deliver(stale)).status_code == 401

        first = await wire.deliver(good)
        again = await wire.deliver(good)
        assert first.json() == {"status": "ok"} and again.json() == {"status": "duplicate"}
        bad = wire.signed(b"[1, 2]", "d-bad")
        assert (await wire.deliver(bad)).status_code == 400
        await adapter.listener.idle()
        assert [e.text for e in events] == [item.text]
        assert adapter.signing_secret == SIGNING_SECRET
        await adapter.disconnect()
        late = await wire.deliver(wire.signed(good.body, "d-late"))
        assert late.status_code == 503

    run(main())


def test_sample_failures_keep_credentials_out_of_errors(no_network: SocketGuard) -> None:
    async def main() -> None:
        env, adapter, _ = await _connected()
        env.platform.remove_chat("300")
        result = await settle(env, adapter.send("300", "hi"))
        assert result.kind is ErrorKind.NOT_FOUND and result.chat_unreachable
        assert adapter.token not in (result.error or "") and "Bearer" not in (result.error or "")
        await adapter.disconnect()
        assert json.loads(env.wire.calls_to("POST", "/v1/logout")[0].body or b"null") is None

    run(main())
