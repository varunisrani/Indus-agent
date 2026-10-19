"""P22-08 end to end: ``indus research batch`` in a real process with a spawn pool of 4 workers
against a loopback fake model (ACC-O1-1, ACC-O1-2, ACC-O1-3; the M8 demo item).

100 prompts: most answer with reasoning, every tenth from 3 runs one ``read_file`` round, four
answer without reasoning (tombstones) and one calls a tool that does not exist (left out at the
merge as corrupt). The first run is frozen after exactly 37 conversations (the fake stalls every
later one), its whole process group is killed with SIGKILL once those 37 rows are durable, and a
``--resume`` run must hold exactly the 63 remaining conversations, none of them repeated.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from indus.research.batch_io import batch_files, read_batch_file
from tests.fakes.research.routed_model import RoutedFakeModel, serving_routed

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(sys.platform == "win32", reason="process groups and SIGKILL are POSIX"),
]

PROMPTS = 100
NO_REASONING = {7, 32, 57, 82}
GHOST = 50
KILL_AFTER = 37
RUN_TIMEOUT_S = 180.0


def prompt_text(index: int) -> str:
    if index in NO_REASONING:
        return f"Prompt {index}: answer without thinking."
    if index == GHOST:
        return f"Prompt {index}: teleport me."
    if index % 10 == 3:
        return f"Prompt {index}: read a file."
    return f"Prompt {index}: say something."


def reply(prompt: str, step: int) -> Mapping[str, Any]:
    if "without thinking" in prompt:
        return {"steps": [{"text": "Plain answer."}]}
    if "teleport" in prompt and step == 0:
        call = {"name": "teleport_user", "arguments": {"to": "mars"}, "id": "call_tp"}
        return {"steps": [{"reasoning": "Use the teleporter."}, {"tool_call": call}]}
    if "read a file" in prompt and step == 0:
        call = {"name": "read_file", "arguments": {"path": "absent-research.txt"}, "id": "call_rf"}
        return {"steps": [{"reasoning": "Read it first."}, {"tool_call": call}]}
    return {"steps": [{"reasoning": f"Thinking about step {step}."}, {"text": "Done."}]}


@pytest.fixture
def work(tmp_path: Path, isolated_home: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    (path / "prompts.jsonl").write_text(
        "".join(json.dumps({"prompt": prompt_text(i)}) + "\n" for i in range(PROMPTS)),
        encoding="utf-8",
    )
    return path


def configure(home: Path, model: RoutedFakeModel) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n"
        "  default: fake-model\n"
        "  provider: custom\n"
        f"  base_url: http://127.0.0.1:{model.port}/v1\n",
        encoding="utf-8",
    )


def command(*extra: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "indus",
        "research",
        "batch",
        "prompts.jsonl",
        "--run-name",
        "demo",
        "--batch-size",
        "5",
        "--workers",
        "4",
        "--distribution",
        "terminal_only",
        *extra,
    ]


def env_for(home: Path, work: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("INDUS_")}
    env.update({"INDUS_HOME": str(home), "HOME": str(work), "NO_COLOR": "1"})
    return env


def durable_rows(run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in batch_files(run_dir):
        rows.extend(read_batch_file(path, lambda _t: None))
    return rows


PAUSE = threading.Event()  # never set: ``PAUSE.wait(s)`` is a plain timed wait


def wait_for(check: Any, what: str, limit: float = RUN_TIMEOUT_S) -> None:
    end = time.monotonic() + limit
    while not check():
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for {what}")
        PAUSE.wait(0.05)


def test_killed_run_resumes_without_paying_twice(isolated_home: Path, work: Path) -> None:
    run_dir = work / "research-runs" / "demo"

    # -- run 1: frozen after 37 conversations, then the whole process group is killed --------
    with serving_routed(reply, start_limit=KILL_AFTER) as model:
        configure(isolated_home, model)
        first = subprocess.Popen(
            command(),
            cwd=work,
            env=env_for(isolated_home, work),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            wait_for(lambda: len(durable_rows(run_dir)) >= KILL_AFTER, "37 durable rows")
            wait_for(lambda: first.poll() is not None or len(model.starts) >= KILL_AFTER, "starts")
            PAUSE.wait(0.5)  # nothing else may land: every new conversation is stalled
            os.killpg(first.pid, signal.SIGKILL)
        finally:
            if first.poll() is None:
                with_kill = first.wait(30)
                assert with_kill is not None
            output = first.stdout.read().decode() if first.stdout else ""
        assert first.returncode == -signal.SIGKILL, output
        assert len(model.starts) == KILL_AFTER
    rows = durable_rows(run_dir)
    assert len(rows) == KILL_AFTER, output
    done_before = {int(r["prompt_index"]) for r in rows}
    assert not (run_dir / "trajectories.jsonl").exists()

    # -- run 2: --resume pays only for the rest -----------------------------------------------
    with serving_routed(reply) as model:
        configure(isolated_home, model)
        second = subprocess.run(
            command("--resume"),
            cwd=work,
            env=env_for(isolated_home, work),
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_S,
            check=False,
        )
        assert second.returncode == 0, second.stdout + second.stderr
        started = model.starts
    assert len(started) == PROMPTS - KILL_AFTER  # exactly 63 more conversations
    assert len(set(started)) == len(started)
    assert not {prompt_text(i) for i in done_before} & set(started)

    # -- the merged result and the run's records ----------------------------------------------
    merged = [
        json.loads(line)
        for line in (run_dir / "trajectories.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    expected = set(range(PROMPTS)) - NO_REASONING - {GHOST}
    assert sorted(r["prompt_index"] for r in merged) == sorted(expected)
    tombstones = [r for r in durable_rows(run_dir) if r.get("discarded")]
    assert sorted(r["prompt_index"] for r in tombstones) == sorted(NO_REASONING)
    sample = next(r for r in merged if r["prompt_index"] == 13)
    assert [t["from"] for t in sample["conversations"]] == ["system", "human", "gpt", "tool", "gpt"]
    assert sample["tool_stats"]["read_file"]["failure"] == 1
    assert sample["metadata"]["model"] == "fake-model"
    checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["completed"] == list(range(PROMPTS))
    stats = json.loads((run_dir / "statistics.json").read_text(encoding="utf-8"))
    files = len(batch_files(run_dir))  # batch numbers restart at 0 on each run
    assert stats["merged"] == {"kept": 95, "tombstones": 4, "dropped": 1, "files": files}
    assert "effective config:" in second.stdout
    assert "fake-model via custom" in second.stdout
    assert "reasoning effort:" in second.stdout and "provider default" in second.stdout

    # -- ACC-O1-3: an edited dataset under the same run name is refused -----------------------
    edited = work / "prompts.jsonl"
    lines = edited.read_text(encoding="utf-8").splitlines()
    lines[10] = json.dumps({"prompt": "Prompt 10: something else."})
    edited.write_text("\n".join(lines) + "\n", encoding="utf-8")
    refused = subprocess.run(
        command(),
        cwd=work,
        env=env_for(isolated_home, work),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert refused.returncode == 2, refused.stdout + refused.stderr
    assert "dataset changed" in refused.stderr
