"""``shell.exec`` (P12-31): guards (R04.27), injected-clock timeout, redaction before the cut."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

from indus.foundation.scope import Scope
from indus.protocol.errors import SHELL_REFUSED, SHELL_TIMEOUT, TEXT_EMPTY
from indus.tui_bridge.methods_shell import STDERR_KEEP, STDOUT_KEEP
from tests.contract.tui_bridge._harness import open_rig
from tests.fakes.agent import eventually
from tests.fakes.clock import FakeClock


def test_dangerous_and_empty_commands_are_refused(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            hard = await rig.err("shell.exec", command="rm -rf /")
            assert hard["code"] == SHELL_REFUSED.code and "never allowed" in hard["message"]
            risky = await rig.err("shell.exec", command=f"rm -rf {tmp_path.parent}/elsewhere")
            assert risky["code"] == SHELL_REFUSED.code
            empty = await rig.err("shell.exec", command="   ")
            assert empty["code"] == TEXT_EMPTY.code and empty["data"]["path"] == "command"

    asyncio.run(main())


def test_command_runs_with_both_streams(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            ran = await rig.ok("shell.exec", command="echo out; echo err >&2; exit 3")
            assert ran == {"stdout": "out\n", "stderr": "err\n", "code": 3}

    asyncio.run(main())


class CountingClock:
    """The test clock, counting how often the shell runner reads it."""

    def __init__(self, inner: FakeClock) -> None:
        self.inner = inner
        self.reads = 0

    def monotonic(self) -> float:
        self.reads += 1
        return self.inner.monotonic()

    def now(self) -> datetime:
        return self.inner.now()

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)


def test_sleep_times_out_at_thirty_seconds_on_the_bridge_clock(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            assert rig.bridge.shell_timeout == 30.0
            clock = CountingClock(fake_clock)
            rig.bridge.clock = clock  # pyright: ignore[reportAttributeAccessIssue]
            call = asyncio.ensure_future(rig.call("shell.exec", command="sleep 40"))
            await eventually(lambda: clock.reads >= 2)  # started, and polling
            fake_clock.advance(29.0)
            seen = clock.reads
            await eventually(lambda: clock.reads >= seen + 2)  # polled again at 29 s
            assert not call.done()
            fake_clock.advance(1.0)
            await eventually(call.done)
            answer = call.result()
            assert answer["error"]["code"] == SHELL_TIMEOUT.code
            assert answer["error"]["message"] == "stopped after 30 s"

    asyncio.run(main())


def test_long_output_keeps_the_redacted_tail(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    script = tmp_path / "emit.py"
    script.write_text(
        "import sys\n"
        f"sys.stdout.write('a' * 9000 + ' {token} ' + 'b' * 900)\n"
        f"sys.stderr.write('e' * 5000 + ' {token}')\n",
        encoding="utf-8",
    )

    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            ran = await rig.ok("shell.exec", command=f"{sys.executable} {script}")
            assert ran["code"] == 0
            assert len(ran["stdout"]) == STDOUT_KEEP and ran["stdout"].endswith("b" * 900)
            assert token not in ran["stdout"] and "ghp_" in ran["stdout"]
            assert len(ran["stderr"]) <= STDERR_KEEP and token not in ran["stderr"]

    asyncio.run(main())
