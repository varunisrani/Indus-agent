"""Demo 3 (P15-36): Telegram, Discord and Slack in one gateway daemon, over the wire fakes.

The four M5 acceptance checks, in CI with fakes (the owner runs ``live_demo3.py`` with real bots):

1. the same agent answers on all three platforms;
2. a dangerous command is approved with a Telegram button (a stranger's press changes nothing);
3. the daemon is SIGKILLed between the ledger's ``attempting`` and the platform's acknowledgement,
   restarted, and the chat shows one labelled recovered reply (never regenerated);
4. a cron job "every 30m" delivers to a Telegram chat at the right fake-clock tick.

Plus, on each platform: the chunker (a reply over the platform's cap arrives in pieces within the
cap), the busy guards (``/status`` and ``/stop`` answer inline while a turn runs) and default-deny
(a stranger's message never reaches the agent).

Every daemon here runs the real adapters (``tests/integration/gateway/three_platforms.py``) with
every default part, on a fake clock, against the scripted fake model.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from indus.cron.runner import AgentRunRequest, AgentRunResult
from indus.gateway import cron_host as gateway_cron_host
from indus.gateway.adapters.base import code_points, utf16_units
from indus.gateway.delivery.ledger import DUPLICATE_PREFIX
from indus.service.cron_host import CronHost
from indus.state.gateway_runtime import EXIT_OK
from tests.e2e.gateway.demo3_child import ANSWER, ledger_states
from tests.fakes.clock import FakeClock
from tests.fakes.model import Finish, Stall, Text, Turn, text_turn, tool_batch
from tests.fakes.platform.core import FakeChatPlatform
from tests.integration.gateway.three_platforms import (
    CAPS,
    PLATFORMS,
    START,
    TELEGRAM_HOME,
    USERS,
    Brain,
    Wires,
    drive,
    start_daemon,
    stop_daemon,
    three_daemon,
    three_scope,
)

pytestmark = [
    pytest.mark.skipif(os.name != "posix", reason="POSIX sockets and signals"),
    pytest.mark.slow,
]

ROOT = Path(__file__).resolve().parents[3]
LEN = {"telegram": utf16_units, "discord": code_points, "slack": code_points}
STRANGERS = {"telegram": "666", "discord": "667", "slack": "U666"}


def plain(text: str) -> str:
    """A sent text without Telegram's MarkdownV2 escapes."""
    return text.replace("\\", "")


def texts(wires: Wires, name: str, chat: str | None = None) -> list[str]:
    platform: FakeChatPlatform = wires.platform(name)
    return [plain(t) for t in platform.texts(chat or USERS[name])]


def stalled(text: str) -> Turn:
    return Turn((Text((text,)), Stall(10_000.0), Finish("stop")))


# -- 1. one agent on three platforms ---------------------------------------------------------------


def test_the_same_agent_answers_on_telegram_discord_and_slack(tmp_path: Path) -> None:
    clock = FakeClock(START)
    wires = Wires.on(clock)
    brain = Brain(tuple(text_turn("Hello from your Indus agent") for _ in PLATFORMS))
    daemon = three_daemon(three_scope(tmp_path, clock), wires, brain, tmp_path)

    async def main() -> None:
        task = await start_daemon(daemon, clock, wires)
        try:
            assert {n: daemon.platforms[n].state for n in PLATFORMS} == dict.fromkeys(
                PLATFORMS, "connected"
            )
            for name in PLATFORMS:
                wires.platform(name).dm(USERS[name], f"hello from {name}")
            await drive(
                clock,
                wires,
                lambda: all("Hello from your Indus agent" in texts(wires, n) for n in PLATFORMS),
                what="an answer on every platform",
            )
        finally:
            assert await stop_daemon(daemon, task, clock, wires) == EXIT_OK

    asyncio.run(main())
    assert brain.calls == 3  # one agent, one model, three conversations
    for name in PLATFORMS:
        assert texts(wires, name) == ["Hello from your Indus agent"]


# -- 2. approval by a Telegram button --------------------------------------------------------------


def test_a_dangerous_command_is_approved_with_a_telegram_button(tmp_path: Path) -> None:
    clock = FakeClock(START)
    wires = Wires.on(clock)
    brain = Brain((tool_batch(("ask", {})), text_turn("Cleaned up the build folder.")))
    daemon = three_daemon(three_scope(tmp_path, clock), wires, brain, tmp_path)
    chat = wires.telegram.platform
    user = USERS["telegram"]
    cards: list[str] = []

    async def main() -> None:
        task = await start_daemon(daemon, clock, wires)
        try:
            chat.dm(user, "please clean the build folder")
            await drive(
                clock, wires, lambda: any(m.buttons for m in chat.messages(user)), what="a card"
            )
            (card,) = [m for m in chat.messages(user) if m.buttons]
            cards.append(card.message_id)
            assert "rm -r build" in plain(card.text)
            once = next(b for b in card.buttons if b.label == "Run once")
            # a stranger's press is answered (no spinner) but changes nothing
            stranger = chat.button_press(user, "666", once.data, message_id=card.message_id)
            await drive(
                clock,
                wires,
                lambda: any(a[0] == stranger.button_id for a in chat.answered),
                what="the stranger's press answered",
            )
            await drive(clock, wires, lambda: True, what="settle", step=1.0)
            assert brain.answers == [] and chat.message(card.message_id).buttons  # type: ignore[union-attr]
            # the requester's press approves
            chat.button_press(user, user, once.data, message_id=card.message_id)
            await drive(
                clock,
                wires,
                lambda: "Cleaned up the build folder." in texts(wires, "telegram"),
                what="the approved turn's answer",
            )
        finally:
            assert await stop_daemon(daemon, task, clock, wires) == EXIT_OK

    asyncio.run(main())
    assert brain.answers == ["once"]
    edited = chat.message(cards[0])  # the card lost its buttons once answered
    assert edited is not None and edited.buttons == ()
    assert brain.calls == 2


# -- 3. SIGKILL between attempting and acknowledgement -------------------------------------------


def _child(root: Path, mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tests.e2e.gateway.demo3_child", str(root), mode],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        check=False,
    )


def test_a_kill_mid_send_gives_one_labelled_recovered_reply_after_restart(tmp_path: Path) -> None:
    killed = _child(tmp_path, "crash")
    assert killed.returncode == -signal.SIGKILL, killed.stdout + killed.stderr
    assert "finished without dying" not in killed.stdout
    assert ledger_states(tmp_path) == ["attempting"]

    again = _child(tmp_path, "recover")
    assert again.returncode == 0, again.stdout + again.stderr
    report = json.loads(again.stdout.strip().splitlines()[-1])
    assert report["exit"] == EXIT_OK
    recovered = [plain(t) for t in report["texts"]]
    assert len(recovered) == 1  # one reply in the chat, labelled
    assert recovered[0].startswith(DUPLICATE_PREFIX) and ANSWER in recovered[0]
    assert report["model_calls"] == 0  # delivered from the ledger, never regenerated
    assert report["states"] == ["delivered"]


# -- 4. cron "every 30m" to a Telegram chat --------------------------------------------------------


def test_a_cron_job_every_30m_reaches_the_telegram_chat_on_its_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(START)
    wires = Wires.on(clock)
    brain = Brain(())
    scope = three_scope(tmp_path, clock)
    runs: list[str] = []

    async def agent(request: AgentRunRequest) -> AgentRunResult:
        runs.append(request.job.id)
        return AgentRunResult(final_text="Nightly build 812 is green.", completed=True, api_calls=1)

    monkeypatch.setattr(
        gateway_cron_host, "CronHost", lambda s, **kw: CronHost(s, agent=agent, **kw)
    )
    job = CronHost(scope, agent=agent).create(
        {"schedule": "every 30m", "prompt": "check the nightly build", "deliver": "telegram"}
    )
    daemon = three_daemon(scope, wires, brain, tmp_path)
    home = wires.telegram.platform

    def delivered() -> list[str]:
        return [plain(t) for t in home.texts(TELEGRAM_HOME) if "build 812" in plain(t)]

    async def main() -> None:
        task = await start_daemon(daemon, clock, wires)
        try:
            # 29 minutes in: not yet due
            await drive(
                clock,
                wires,
                lambda: clock.now() >= START.replace(minute=29),
                what="29 minutes",
                step=10.0,
            )
            assert delivered() == [] and runs == []
            # the tick after the 30-minute mark runs it once and delivers it to the home chat
            await drive(clock, wires, lambda: bool(delivered()), what="the cron delivery", step=5.0)
            assert clock.now() <= START.replace(minute=32)
            assert runs == [job.id]
            (text,) = delivered()
            assert job.id in text  # cron's wrapper names the job
            # nothing more until the next half hour
            await drive(
                clock,
                wires,
                lambda: clock.now() >= START.replace(minute=55),
                what="55 minutes",
                step=10.0,
            )
            assert len(delivered()) == 1
        finally:
            assert await stop_daemon(daemon, task, clock, wires) == EXIT_OK

    asyncio.run(main())
    assert brain.calls == 0  # cron used its own runner, not the chat agent


# -- chunker, busy guards and default-deny on each platform ----------------------------------------

LONG = "\n".join(f"line {n:04d} of the long answer with some words" for n in range(1000))


@pytest.mark.parametrize("name", PLATFORMS)
def test_chunker_busy_guards_and_default_deny(tmp_path: Path, name: str) -> None:
    clock = FakeClock(START)
    wires = Wires.on(clock)
    brain = Brain((text_turn(LONG), stalled("working on it")))
    daemon = three_daemon(three_scope(tmp_path, clock), wires, brain, tmp_path)
    platform = wires.platform(name)
    user = USERS[name]
    cap, measure = CAPS[name], LEN[name]

    async def main() -> None:
        task = await start_daemon(daemon, clock, wires)
        try:
            # chunker: a reply over the cap arrives in pieces, each within the cap, nothing lost
            platform.dm(user, "write me the long answer")
            await drive(
                clock,
                wires,
                lambda: any("line 0999" in t for t in texts(wires, name)),
                what="the last piece",
                step=1.0,
            )
            pieces = platform.texts(user)
            assert len(pieces) >= 2 and all(measure(p) <= cap for p in pieces)
            joined = "\n".join(plain(p) for p in pieces)
            assert all(f"line {n:04d}" in joined for n in range(1000))

            # busy guards: /status and /stop answer inline while a turn runs
            before = brain.calls
            platform.dm(user, "start the long job")
            await drive(clock, wires, lambda: brain.calls > before, what="the busy turn")
            platform.dm(user, "/status")
            await drive(
                clock,
                wires,
                lambda: any("Conversation:" in t for t in texts(wires, name)),
                what="/status while busy",
            )
            platform.dm(user, "/stop")
            await drive(
                clock,
                wires,
                lambda: any(t.startswith("Stopped") for t in texts(wires, name)),
                what="/stop while busy",
            )

            # default-deny: a stranger never reaches the agent
            calls = brain.calls
            platform.dm(STRANGERS[name], "hello, let me in")
            await drive(clock, wires, lambda: True, what="settle", step=1.0)
            for _ in range(10):
                await drive(clock, wires, lambda: True, what="settle", step=1.0)
                clock.advance(1.0)
            assert brain.calls == calls
            assert not any(
                "working on it" in t or "line 0" in t for t in platform.texts(STRANGERS[name])
            )
        finally:
            assert await stop_daemon(daemon, task, clock, wires) == EXIT_OK

    asyncio.run(main())
