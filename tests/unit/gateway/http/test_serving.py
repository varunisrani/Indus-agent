"""P15-30..32: binding with retries and the real uvicorn server on an ephemeral loopback port
(``start_http_surfaces`` inside the daemon: ``tests/integration/gateway/test_http_part.py``)."""

from __future__ import annotations

import asyncio
import errno
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest

from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from indus.gateway.http.api_server import ApiServer, NoApiKey
from indus.gateway.http.serving import BIND_ATTEMPTS, bind_listener
from indus.state.api_keys import ApiKeyRing
from tests.contract.gateway._http_kit import build_api, until
from tests.fakes.clock import FakeClock
from tests.fakes.model import Finish, Stall, Text, Turn, text_turn
from tests.fakes.secret_store import MemorySecretStore


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_bind_retries_a_busy_port_on_the_clock(fake_clock: FakeClock) -> None:
    calls: list[tuple[str, int]] = []

    def opener(host: str, port: int) -> socket.socket:
        calls.append((host, port))
        if len(calls) < 3:
            raise OSError(errno.EADDRINUSE, "busy")
        return socket.socket()

    async def main() -> None:
        task = asyncio.ensure_future(bind_listener("127.0.0.1", 8642, fake_clock, opener=opener))
        for _ in range(10):
            await fake_clock.advance_and_settle(1.0)
            if task.done():
                break
        sock = await task
        sock.close()

    run(main())
    assert len(calls) == 3


def test_bind_gives_up_after_five_attempts_and_other_errors_at_once(
    fake_clock: FakeClock,
) -> None:
    calls: list[int] = []

    def busy(host: str, port: int) -> socket.socket:
        calls.append(port)
        raise OSError(errno.EADDRINUSE, "busy")

    def denied(host: str, port: int) -> socket.socket:
        calls.append(port)
        raise OSError(errno.EACCES, "denied")

    async def main() -> None:
        task = asyncio.ensure_future(bind_listener("127.0.0.1", 1, fake_clock, opener=busy))
        for _ in range(10):
            await fake_clock.advance_and_settle(1.0)
        with pytest.raises(OSError, match="busy"):
            await task
        assert len(calls) == BIND_ATTEMPTS
        calls.clear()
        with pytest.raises(OSError, match="denied"):
            await bind_listener("127.0.0.1", 1, fake_clock, opener=denied)
        assert len(calls) == 1

    run(main())


def test_the_api_server_serves_on_an_ephemeral_loopback_port(scope: Scope, tmp_path: Path) -> None:
    clock = SystemClock()

    async def main() -> None:
        kit = await build_api(scope, FakeClock(scope.clock.now()), tmp_path, text_turn("hi"))
        kit.ctx.clock = clock
        kit.knobs["port"] = 0
        beats: list[dict[str, Any]] = []
        server = ApiServer(kit.ctx, on_status=beats.append)
        await server.start()
        try:
            address = server.address
            assert address is not None and address[0] == "127.0.0.1" and address[1] > 0
            base = f"http://127.0.0.1:{address[1]}"
            async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=10) as client:
                assert (await client.get("/health")).status_code == 401
                auth = {"Authorization": f"Bearer {kit.key}"}
                answer = await client.get("/health", headers=auth)
                assert answer.status_code == 200 and answer.json()["status"] == "ok"
                assert answer.headers["x-frame-options"] == "DENY"
            assert beats and beats[0]["listening"] == f"127.0.0.1:{address[1]}"
        finally:
            await server.aclose()
            await kit.service.aclose()
            kit.store.close()
        with pytest.raises(OSError), socket.create_connection(address, timeout=1):
            pass

    run(main())


def test_the_api_server_will_not_start_without_a_key(scope: Scope, tmp_path: Path) -> None:
    async def main() -> None:
        kit = await build_api(scope, FakeClock(scope.clock.now()), tmp_path)
        kit.ctx.keys = ApiKeyRing(scope, MemorySecretStore())
        server = ApiServer(kit.ctx)
        with pytest.raises(NoApiKey, match="api-key create"):
            await server.start()
        await kit.close()

    run(main())


def test_a_streaming_client_that_hangs_up_interrupts_its_turn(scope: Scope, tmp_path: Path) -> None:
    async def main() -> None:
        stalled = Turn((Text(("first words",)), Stall(1000.0), Finish("stop")))
        kit = await build_api(scope, FakeClock(scope.clock.now()), tmp_path, stalled)
        kit.knobs["port"] = 0
        server = ApiServer(kit.ctx)
        await server.start()
        try:
            address = server.address
            assert address is not None
            body = {"messages": [{"role": "user", "content": "go"}], "stream": True}
            headers = {"Authorization": f"Bearer {kit.key}"}
            async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
                url = f"http://127.0.0.1:{address[1]}/v1/chat/completions"
                async with client.stream("POST", url, json=body, headers=headers) as answer:
                    async for line in answer.aiter_lines():
                        if "first words" in line:
                            break
            await until(lambda: kit.ctx.gate.active == 0 and kit.desk.active == 0)
        finally:
            await server.aclose()
            await kit.service.aclose()
            kit.store.close()

    run(main())
