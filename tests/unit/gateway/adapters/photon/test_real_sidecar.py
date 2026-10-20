"""The adapter drives the real Node sidecar (P16-25) over loopback, with the sidecar's fake Spectrum
provider (``apps/photon-sidecar/src/testing/fakeLink.ts``): the field names on both sides of the
HTTP API, the event stream, the spawn environment, the files and the stop are the real ones.
Skipped without Node."""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import stat

import pytest

from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from indus.gateway.adapters.photon import default_sidecar_script
from indus.gateway.adapters.photon.adapter import PhotonAdapter
from indus.gateway.adapters.photon.sidecar import PhotonSidecar, SidecarError, http_probe
from indus.gateway.core.events import InboundEvent, MessageKind
from indus.safety.child_env import child_env

NODE = shutil.which("node")
SCRIPT = default_sidecar_script()
# Built at run time: never a credential-shaped literal in the tree.
SECRET = "-".join(("real", "sidecar", "project", "secret"))

pytestmark = pytest.mark.skipif(
    NODE is None or SCRIPT is None, reason="needs Node.js and the sidecar sources"
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


def _owner(scope: Scope, **kw: object) -> PhotonSidecar:
    assert SCRIPT is not None
    options: dict[str, object] = {
        "provider": str(SCRIPT.parent / "testing" / "fakeLink.ts"),
        "project_id": "project-1",
        "project_secret": SECRET,
        **kw,
    }
    return PhotonSidecar(
        home=scope.home,
        profile_id=scope.profile_id,
        clock=SystemClock(),
        probe=http_probe(),
        node=NODE,
        script=SCRIPT,
        env_for=lambda extra: child_env(scope, variant="minimal", extra=extra),
        port=_free_port(),
        **options,  # pyright: ignore[reportArgumentType]
    )


def test_the_adapter_runs_the_real_sidecar(scope: Scope) -> None:
    from indus.gateway.extras import attach_fetcher
    from indus.gateway.intake.media import MediaIntake

    clock = SystemClock()
    owner = _owner(scope)
    adapter = PhotonAdapter(clock=clock, launcher=owner, secrets=(SECRET,))
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
            assert hello.user_id == "+15550000200"
            assert hello.source.chat_id == "any;-;+15550000200"
            assert hello.kind is MessageKind.DOCUMENT
            intake = MediaIntake(scope)
            undo = attach_fetcher(intake, adapter)
            assert undo is not None
            text = await intake.enrich(hello)
            undo()
            assert "fake notes" in text and text.endswith("hello from the fake")
            sent = await adapter.send("+15550000200", "**hi** there")
            assert sent.ok and sent.message_id == "OUT1"
            await adapter.typing_start("+15550000200")
            for path in (owner.token_file, owner.pidfile, owner.log_file):
                assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert stat.S_IMODE(owner.state.stat().st_mode) == 0o700
            token = owner.token_file.read_text().strip()
            logged = owner.log_file.read_text()
            assert token not in logged and SECRET not in logged
            assert '"listening"' in logged
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                argv = handle.read()
            assert token.encode() not in argv and SECRET.encode() not in argv
        finally:
            await adapter.disconnect()
        for _ in range(100):
            if _gone(pid):
                break
            await clock.sleep(0.05)
        assert _gone(pid)
        assert not owner.pidfile.exists() and not owner.token_file.exists()

    asyncio.run(main())


def test_the_real_sidecar_with_an_unloadable_provider_fails_closed(scope: Scope) -> None:
    owner = _owner(scope, provider=str(scope.home / "missing-provider.mjs"))

    async def main() -> None:
        with pytest.raises(SidecarError) as raised:
            await asyncio.wait_for(owner.ensure(), 20)
        assert raised.value.code == "photon_provider_failed"
        assert not raised.value.retryable
        assert '"provider"' in owner.log_file.read_text(encoding="utf-8")
        assert owner.pid is None and not owner.pidfile.exists()

    asyncio.run(main())
