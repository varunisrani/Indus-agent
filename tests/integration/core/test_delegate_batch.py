"""M2 demo slice: a parent hands three file-reading tasks to subagents and gets three summaries
(P10-03, ACC-C36-1)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from indus.core.persist import MemoryStore
from indus.foundation.scope import Scope
from indus.protocol.events import ExitReason
from tests.fakes.agent import run_driven
from tests.fakes.clock import FakeClock
from tests.fakes.model import text_turn, tool_batch
from tests.fakes.subagents import RoutedModel, parent_agent
from tests.fakes.turn import check_alternation

FILES = {
    "alpha.txt": "Alpha holds the build settings.",
    "beta.txt": "Beta lists the release dates.",
    "gamma.txt": "Gamma is the changelog.",
}


def test_three_subagents_read_three_files(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    for name, text in FILES.items():
        (work / name).write_text(text + "\n", encoding="utf-8")
    goals = {name: f"Summarise {name}" for name in FILES}
    children = {
        goals[name]: [
            tool_batch(("read_file", {"path": str(work / name)})),
            text_turn(f"{name}: {text}"),
        ]
        for name, text in FILES.items()
    }
    tasks = [{"goal": goals[name], "context": f"The file is {work / name}."} for name in FILES]
    model = RoutedModel(
        fake_clock,
        parent=[
            tool_batch(("delegate_task", {"tasks": tasks})),
            text_turn("Three summaries are in."),
        ],
        children=children,
    )
    store = MemoryStore()

    async def main() -> Any:
        parent = await parent_agent(scope, model, store=store, cwd=work)
        result = await parent.run_turn("Summarise these three files")
        await parent.close()
        return result

    result = asyncio.run(run_driven(main(), fake_clock))
    assert result.exit_reason is ExitReason.COMPLETED
    assert result.final_text == "Three summaries are in."
    tool = next(r for r in result.rows if r.role == "tool")
    entries = json.loads(tool.content)["results"]
    assert [e["goal"] for e in entries] == [goals[name] for name in FILES]
    assert [e["status"] for e in entries] == ["completed"] * 3
    for entry, (name, text) in zip(entries, FILES.items(), strict=True):
        assert entry["summary"] == f"{name}: {text}"
    # Each child really read its file: its second request carries the file's text.
    for name, text in FILES.items():
        requests = model.child(goals[name]).model_requests
        assert len(requests) == 2
        tool_messages = [m for m in requests[1].body["messages"] if m["role"] == "tool"]
        assert text in json.dumps(tool_messages)
    children_ids = [s for s, row in store.sessions.items() if row.get("source") == "subagent"]
    assert len(children_ids) == 3
    for sid in children_ids:
        assert store.sessions[sid]["parent_session_id"] == "parent"
        check_alternation(store.rows[sid])
