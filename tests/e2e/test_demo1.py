"""P06-14: Demo 1 end to end — the real ``indus`` in a pseudo-terminal against the fake model.

"create hello.py and run it": the reply streams, the run command opens an approval card answered
with ``1``, the command runs on the local backend, the final answer arrives, ``/quit`` leaves;
``indus -c`` shows the recap and ``/history`` lists the turn; the stored transcript alternates.
Then the M1 criteria around it: a SIGKILL right after the approval runs the command at most once
and leaves a valid transcript; Ctrl+C during a stream seals the reply; Ctrl+C during ``!command``
gives 130 and the session goes on; ``-z`` exit codes from the real entry.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest

from indus.foundation.clock import SystemClock
from tests.e2e.conftest import Demo, pexpect, write_script
from tests.fakes.turn import check_alternation

pytestmark = [
    pytest.mark.slow,
    pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning"),
]

PROMPT = "create hello.py and run it"
# A real process in a real terminal: waiting here is wall-clock time.
CLOCK = SystemClock()


def ran_count(work: Path) -> int:
    log = work / "ran.log"
    return len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0


def wait_for(path: Path, timeout: float = 30.0) -> None:
    end = CLOCK.monotonic() + timeout
    while not path.exists():
        assert CLOCK.monotonic() < end, f"{path.name} never appeared"
        CLOCK.sleep_sync(0.05)


def test_demo1_create_run_quit_then_continue(demo: Demo) -> None:
    child = demo.spawn()
    child.expect("ready")
    child.send(PROMPT + "\r")
    child.expect("I'll create hello.py first.")  # streamed before any approval
    child.expect("Approval needed")
    child.expect("chmod \\+x hello.py && ./hello.py")
    child.send("1")
    child.expect("approved")
    child.expect("Done: hello.py printed")
    child.send("/quit\r")
    assert demo.finish(child) == 0, demo.transcript
    assert ran_count(demo.work) == 1
    assert "hello from indus" in (demo.work / "hello.py").read_text(encoding="utf-8")

    (session_id,) = demo.session_ids()
    rows = demo.rows(session_id)
    check_alternation(rows)
    assert [r.role for r in rows if r.role == "user"] == ["user"]
    assert rows[-1].role == "assistant" and "Done" in str(rows[-1].content)

    again = demo.spawn("-c")
    again.expect("resumed")
    again.expect("earlier in this conversation")
    again.expect(PROMPT)
    again.send("/history\r")
    again.expect("You: " + PROMPT)
    again.expect("used terminal")
    again.send("/quit\r")
    assert demo.finish(again) == 0, demo.transcript
    assert demo.session_ids() == [session_id]
    assert ran_count(demo.work) == 1


def test_kill_after_the_approval_runs_the_command_at_most_once(demo: Demo) -> None:
    slow = write_script(
        demo.home.parent,
        "demo1_slow",
        [
            [
                {"text": ["Writing it."]},
                {
                    "tool_call": {
                        "name": "write_file",
                        "id": "call_write_1",
                        "arguments": {
                            "path": "hello.py",
                            "content": "#!/usr/bin/env python3\n"
                            "with open('ran.log', 'a') as log:\n"
                            "    log.write('ran\\n')\n"
                            "print('hello from indus')\n",
                        },
                    }
                },
            ],
            [
                {"text": ["Running it."]},
                {
                    "tool_call": {
                        "name": "terminal",
                        "id": "call_run_1",
                        "arguments": {"command": "chmod +x hello.py && ./hello.py && sleep 60"},
                    }
                },
            ],
        ],
    )
    child = demo.spawn(script=slow)
    child.expect("ready")
    child.send(PROMPT + "\r")
    child.expect("Approval needed")
    child.send("1")
    child.expect("approved")
    wait_for(demo.work / "ran.log")  # the command ran; its tool row is not written yet
    os.kill(child.pid, signal.SIGKILL)
    child.expect(pexpect.EOF)
    child.close()
    assert child.signalstatus == signal.SIGKILL

    followup = write_script(
        demo.home.parent, "followup", [[{"text": ["It ran once; the file is there."]}]]
    )
    again = demo.spawn("-c", script=followup)
    again.expect("earlier in this conversation")
    again.send("did it run?\r")
    again.expect("It ran once")
    again.send("/quit\r")
    assert demo.finish(again) == 0, demo.transcript
    assert ran_count(demo.work) == 1
    (session_id,) = demo.session_ids()
    check_alternation(demo.rows(session_id))


def test_ctrl_c_during_a_stream_seals_the_reply(demo: Demo) -> None:
    stall = write_script(
        demo.home.parent,
        "stall",
        [[{"text": ["The first line of the answer.\n", "and a cut-off sec"]}, {"stall": 120}]],
    )
    child = demo.spawn(script=stall)
    child.expect("ready")
    child.send("tell me something long\r")
    child.expect("The first line of the answer.")  # streamed while the turn still runs
    child.sendcontrol("c")
    child.expect("cut-off sec")  # the partial line is sealed and shown at once
    child.expect("\\(interrupted\\)")
    child.expect("stopping the reply")
    CLOCK.sleep_sync(0.5)
    child.send("/quit\r")
    assert demo.finish(child) == 0, demo.transcript
    (session_id,) = demo.session_ids()
    rows = demo.rows(session_id)
    assert rows[0].role == "user"
    check_alternation(rows, user_index=None)


def test_ctrl_c_during_a_bang_command_gives_130_and_the_session_goes_on(demo: Demo) -> None:
    child = demo.spawn()
    child.expect("ready")
    child.send("!sleep 30\r")
    CLOCK.sleep_sync(1.5)
    child.sendcontrol("c")
    child.expect("interrupted \\(exit 130\\)")
    child.send("!echo still-here\r")
    child.expect("still-here")
    child.send("/quit\r")
    assert demo.finish(child) == 0, demo.transcript
    assert demo.session_ids() == []  # bang commands never enter the conversation


def test_oneshot_exit_codes_from_the_real_entry(demo: Demo) -> None:
    done = demo.run("-z", "say hello", script="hello")
    assert done.returncode == 0, done.stderr
    assert done.stdout == "hello\n"
    broken = write_script(
        demo.home.parent, "refused", [[{"fail": {"status": 400, "body": '{"error": "no"}'}}]]
    )
    failed = demo.run("-z", "anything", script=broken)
    assert failed.returncode == 2 and failed.stderr.startswith("indus -z: ")
    usage = demo.run("-z", "x", "--provider", "openai", script="hello")
    assert usage.returncode == 2
