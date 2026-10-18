"""P06-12: ``indus -z`` and finite ``chat -q`` as real processes against the fake model.

Each run is ``python -m tests.integration.cli.fake_runner …`` (the real entry point with the fake
model selected by ``INDUS_TEST_MODEL_SCRIPT``), so the hard exit, the stream silencing and the exit
codes are the ones a script would see.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
TIMEOUT = 90


def write_script(folder: Path, name: str, turns: list[list[dict[str, Any]]]) -> Path:
    path = folder / f"{name}.json"
    path.write_text(json.dumps({"turns": [{"steps": steps} for steps in turns]}), encoding="utf-8")
    return path


def base_env(home: Path, script: Path | str, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("INDUS_")}
    env.update(
        {
            "INDUS_HOME": str(home),
            "INDUS_TEST_MODEL_SCRIPT": str(script),
            "PYTHONPATH": str(ROOT),
        }
    )
    env.update(extra)
    return env


def indus(
    tmp_path: Path, script: Path | str, *args: str, stdin: str = "", **extra: str
) -> subprocess.CompletedProcess[str]:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return subprocess.run(
        [sys.executable, "-m", "tests.integration.cli.fake_runner", *args],
        capture_output=True,
        text=True,
        input=stdin,
        cwd=work,
        env=base_env(tmp_path / "home", script, **extra),
        timeout=TIMEOUT,
    )


def test_completed_prints_only_text(tmp_path: Path) -> None:
    out = indus(tmp_path, "hello", "-z", "say hello")
    assert out.returncode == 0, out.stderr
    assert out.stdout == "hello\n"
    assert out.stderr == ""


def test_empty_answers_are_a_failure_not_success(tmp_path: Path) -> None:
    empty = [{"usage": {"prompt": 10, "completion": 0}}, {"finish": "stop"}]
    script = write_script(tmp_path, "silent", [empty] * 8)
    out = indus(tmp_path, script, "-z", "say nothing")
    assert out.returncode == 2


def test_failed_run_exits_2_and_writes_the_usage_file(tmp_path: Path) -> None:
    script = write_script(
        tmp_path, "refused", [[{"fail": {"status": 400, "body": '{"error": "bad request"}'}}]]
    )
    usage = tmp_path / "usage" / "report.json"
    out = indus(tmp_path, script, "-z", "anything", "--usage-file", str(usage))
    assert out.returncode == 2, out.stderr
    assert out.stderr.startswith("indus -z: ")
    report = json.loads(usage.read_text(encoding="utf-8"))
    assert report["failed"] is True and report["completed"] is False
    assert report["cost_status"] == "unknown" and report["estimated_cost_usd"] is None
    assert report["exit_reason"] == "provider_failed"


def test_text_then_failure_is_partial_exit_2(tmp_path: Path) -> None:
    script = write_script(
        tmp_path,
        "cut",
        [[{"text": ["half an ans"]}, {"mid_stream_error": {"error": {"message": "boom"}}}]] * 4,
    )
    out = indus(tmp_path, script, "-z", "go", "--usage-file", str(tmp_path / "u.json"))
    assert out.returncode == 2, (out.stdout, out.stderr)
    assert json.loads((tmp_path / "u.json").read_text(encoding="utf-8"))["completed"] is False
    # stdout carries only a model reply; the failure notice goes to stderr with the diagnostics
    assert out.stdout == ""
    assert out.stderr.strip()


def test_usage_errors_exit_2(tmp_path: Path) -> None:
    out = indus(tmp_path, "hello", "-z", "x", "--provider", "openai")
    assert out.returncode == 2
    assert "--provider needs --model" in out.stderr
    assert out.stdout == ""
    assert indus(tmp_path, "hello", "-z", "x", "--reasoning", "loud").returncode == 2
    assert indus(tmp_path, "hello", "--bogus-flag").returncode == 2


def test_unwritable_usage_file_keeps_the_code(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")
    out = indus(tmp_path, "hello", "-z", "hi", "--usage-file", str(blocker / "under" / "u.json"))
    assert out.returncode == 0 and out.stdout == "hello\n"


def test_interrupted_exits_130(tmp_path: Path) -> None:
    script = write_script(tmp_path, "slow", [[{"text": ["thinking"]}, {"stall": 120}]])
    marker = tmp_path / "streaming"
    work = tmp_path / "work"
    work.mkdir()
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.integration.cli.fake_runner", "-z", "think long"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=work,
        env=base_env(tmp_path / "home", script, INDUS_TEST_MARKER=str(marker)),
    )
    try:
        waiter = threading.Event()
        for _ in range(int(TIMEOUT / 0.05)):
            if marker.exists() or proc.poll() is not None:
                break
            waiter.wait(0.05)
        assert marker.exists(), "the model never started answering"
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=TIMEOUT)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 130, err
    assert out in ("", "thinking\n")  # at most the reply text sealed at the interrupt


def test_hardline_refused_and_dangerous_auto_approved_under_z(tmp_path: Path) -> None:
    work = tmp_path / "work"
    (work / "victim").mkdir(parents=True)
    script = write_script(
        tmp_path,
        "commands",
        [
            [
                {
                    "tool_call": {
                        "name": "terminal",
                        "arguments": {"command": "touch HARD_RAN; grep 'unterminated"},
                        "id": "call_hard",
                    }
                }
            ],
            [
                {
                    "tool_call": {
                        "name": "terminal",
                        "arguments": {"command": "find victim -delete"},
                        "id": "call_risky",
                    }
                }
            ],
            [{"text": "done"}],
        ],
    )
    requests = tmp_path / "requests.jsonl"
    out = indus(tmp_path, script, "-z", "clean up", INDUS_TEST_REQUESTS=str(requests))
    assert out.returncode == 0, out.stderr
    assert out.stdout == "done\n"
    bodies = [json.loads(line) for line in requests.read_text(encoding="utf-8").splitlines()]
    hard_result = next(
        m
        for m in bodies[1]["messages"]
        if m.get("role") == "tool" and m["tool_call_id"] == "call_hard"
    )
    assert "BLOCKED" in json.dumps(hard_result)
    assert not (work / "HARD_RAN").exists()
    assert not (work / "victim").exists()


def test_finite_chat_query_denies_risky_commands(tmp_path: Path) -> None:
    work = tmp_path / "work"
    (work / "victim").mkdir(parents=True)
    script = write_script(
        tmp_path,
        "risky",
        [
            [
                {
                    "tool_call": {
                        "name": "terminal",
                        "arguments": {"command": "find victim -delete"},
                        "id": "call_risky",
                    }
                }
            ],
            [{"text": "could not"}],
        ],
    )
    out = indus(tmp_path, script, "chat", "-q", "clean up")
    assert out.returncode == 0, out.stderr
    assert out.stdout == "could not\n"
    assert out.stderr.startswith("session_id: ")
    assert (work / "victim").exists()


def test_finite_chat_codes(tmp_path: Path) -> None:
    script = write_script(tmp_path, "refused", [[{"fail": {"status": 400, "body": "{}"}}]])
    out = indus(tmp_path, script, "chat", "-Q", "-q", "x")
    assert out.returncode == 1
    out = indus(tmp_path, "hello", "chat", "--query-file", "-", stdin="from stdin")
    assert out.returncode == 0 and out.stdout == "hello\n"


def test_resume_chain_with_z(tmp_path: Path) -> None:
    script = write_script(tmp_path, "two", [[{"text": "one"}], [{"text": "two"}]])
    first = indus(tmp_path, script, "-z", "first")
    assert first.returncode == 0
    requests = tmp_path / "r.jsonl"
    second = indus(
        tmp_path,
        write_script(tmp_path, "again", [[{"text": "again"}]]),
        "--resume",
        "latest",
        "-z",
        "second",
        INDUS_TEST_REQUESTS=str(requests),
    )
    assert second.returncode == 0 and second.stdout == "again\n"
    sent = json.loads(requests.read_text(encoding="utf-8").splitlines()[0])["messages"]
    assert [m["content"] for m in sent if m["role"] == "user"] == ["first", "second"]


@pytest.mark.parametrize("flag", ["-c", "--continue"])
def test_continue_miss_exits_1(tmp_path: Path, flag: str) -> None:
    out = indus(tmp_path, "hello", flag, "nothing", "here", "-z", "x")
    assert out.returncode == 1
    assert "No session found matching 'nothing here'." in out.stderr
