"""The adapter drives the real Node bridge (P16-02) over loopback, with the bridge's fake WhatsApp
provider (``apps/whatsapp-bridge/src/testing/fakeSocket.ts``): the field names on both sides of the
HTTP API, the spawn environment, the files and the stop are the real ones. Skipped without Node."""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import stat

import pytest

from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from indus.gateway.adapters.whatsapp import default_bridge_script
from indus.gateway.adapters.whatsapp.adapter import WhatsAppAdapter, http_probe
from indus.gateway.adapters.whatsapp.sidecar import BridgeError, SidecarOwner
from indus.gateway.core.events import InboundEvent, MessageKind
from indus.safety.child_env import child_env

NODE = shutil.which("node")
SCRIPT = default_bridge_script()

pytestmark = pytest.mark.skipif(
    NODE is None or SCRIPT is None, reason="needs Node.js and the bridge sources"
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
            return handle.read().rsplit(")", 1)[-1].split()[0] == "Z"
    except OSError:
        return False


def test_the_adapter_runs_the_real_bridge(scope: Scope) -> None:
    from indus.gateway.extras import attach_fetcher
    from indus.gateway.intake.media import MediaIntake

    assert SCRIPT is not None
    provider = SCRIPT.parent / "testing" / "fakeSocket.ts"
    clock = SystemClock()
    owner = SidecarOwner(
        home=scope.home,
        root=scope.root,
        profile_id=scope.profile_id,
        clock=clock,
        probe=http_probe(),
        node=NODE,
        script=SCRIPT,
        env_for=lambda extra: child_env(scope, variant="minimal", extra=extra),
        explicit_port=_free_port(),
        provider=str(provider),
    )
    adapter = WhatsAppAdapter(clock=clock, launcher=owner, auth_dir=owner.auth_dir)
    events: list[InboundEvent] = []

    async def sink(event: InboundEvent) -> None:
        events.append(event)

    adapter.set_sink(sink)

    async def main() -> None:
        await asyncio.wait_for(adapter.connect(), 20)
        pid = owner.pid
        assert pid is not None
        try:
            for _ in range(200):
                if events:
                    break
                await clock.sleep(0.05)
            assert [e.message_id for e in events] == ["HELLO"]
            hello = events[0]
            assert hello.user_id == "15550000200"
            assert hello.source.chat_id == "15550000200@s.whatsapp.net"
            assert hello.kind is MessageKind.DOCUMENT
            intake = MediaIntake(scope)
            undo = attach_fetcher(intake, adapter)
            assert undo is not None
            text = await intake.enrich(hello)
            undo()
            assert "fake notes" in text and text.endswith("hello from the fake")
            sent = await adapter.send("15550000200", "**hi** there", reply_to="HELLO")
            assert sent.ok and sent.message_id == "OUT1"
            await adapter.typing_start("15550000200")
            for path in (owner.token_file, owner.pidfile, owner.log_file):
                assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert stat.S_IMODE(owner.auth_dir.stat().st_mode) == 0o700
            token = owner.token_file.read_text().strip()
            assert token not in owner.log_file.read_text()
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                assert token.encode() not in handle.read()
        finally:
            await adapter.disconnect()
        for _ in range(100):
            if _gone(pid):
                break
            await clock.sleep(0.05)
        assert _gone(pid)
        assert not owner.pidfile.exists() and not owner.token_file.exists()

    asyncio.run(main())


def test_the_real_bridge_without_a_provider_fails_closed(scope: Scope) -> None:
    clock = SystemClock()
    owner = SidecarOwner(
        home=scope.home,
        root=scope.root,
        profile_id=scope.profile_id,
        clock=clock,
        probe=http_probe(),
        node=NODE,
        script=SCRIPT,
        env_for=lambda extra: child_env(scope, variant="minimal", extra=extra),
        explicit_port=_free_port(),
    )

    async def main() -> None:
        with pytest.raises(BridgeError) as raised:
            await asyncio.wait_for(owner.ensure(), 20)
        assert raised.value.code == "whatsapp_provider_missing"
        assert not raised.value.retryable
        assert '"no_provider"' in owner.log_file.read_text(encoding="utf-8")
        assert owner.pid is None and not owner.pidfile.exists()

    asyncio.run(main())
