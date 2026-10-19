"""P09-16: the review's outcome lines, the done event and the completion log line."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from indus.core.events import EventSink
from indus.core.transcript import TranscriptRow
from indus.foundation.scope import Scope
from indus.learning.review.counters import ReviewRequest
from indus.learning.review.fork import ReviewRunner
from indus.learning.review.prompts import NOTHING, build_review_prompt
from indus.learning.review.summary import (
    PREVIEW_CHARS,
    emit_done,
    log_completion,
    mode_from,
    summary_lines,
)
from indus.protocol.events import EventBase, LearningReviewDone
from tests.unit.learning._helpers_a import with_config
from tests.unit.learning.review.helpers import ScriptedCaller, calls, make_parent, text


class _Rows:
    """Builds an assistant call row plus its result rows."""

    def __init__(self) -> None:
        self.rows: list[TranscriptRow] = []
        self.n = 0

    def add(self, name: str, args: dict[str, Any], result: dict[str, Any]) -> _Rows:
        self.n += 1
        ident = f"c{self.n}"
        call = {
            "id": ident,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }
        self.rows.append(TranscriptRow(role="assistant", content=None, tool_calls=[call]))
        self.rows.append(TranscriptRow(role="tool", tool_call_id=ident, content=json.dumps(result)))
        return self


def _memory_add(content: str = "Builds use make.") -> tuple[dict[str, Any], dict[str, Any]]:
    args = {"action": "add", "target": "memory", "content": content}
    return args, {"success": True, "target": "memory", "message": "Saved."}


def _skill_patch(
    name: str = "deploy", new: str = "Run the tests first."
) -> tuple[dict[str, Any], dict[str, Any]]:
    args = {"action": "patch", "name": name, "old_string": "x", "new_string": new}
    return args, {"success": True, "action": "patch", "name": name, "message": "patched"}


def test_add_and_patch_in_on_mode_are_two_generic_lines() -> None:
    rows = _Rows().add("memory", *_memory_add()).add("skill_manage", *_skill_patch()).rows
    assert summary_lines(rows, "on") == ["Memory updated", "Skill deploy improved"]


def test_on_mode_folds_repeats_per_store_and_skill() -> None:
    rows = (
        _Rows()
        .add("memory", *_memory_add("one"))
        .add("memory", *_memory_add("two"))
        .add("memory", {"action": "add", "target": "user", "content": "Likes tables."},
             {"success": True, "target": "user", "message": "Saved."})
        .add("skill_manage", {"action": "create", "name": "deploy", "content": "---"},
             {"success": True, "action": "create", "name": "deploy"})
        .add("skill_manage", *_skill_patch())
        .rows
    )  # fmt: skip
    assert summary_lines(rows, "on") == [
        "Memory updated",
        "User profile updated",
        "Skill deploy created",
    ]


def test_staged_failed_and_duplicate_writes_are_not_reported() -> None:
    staged_args, _ = _skill_patch()
    rows = (
        _Rows()
        .add("skill_manage", staged_args, {"success": True, "staged": True, "pending_id": "abc"})
        .add("memory", {"action": "remove", "target": "memory", "old_text": "x"},
             {"success": True, "staged": True, "pending_id": "def"})
        .add("memory", *_memory_add())
        .rows
    )  # fmt: skip
    rows[-1].content = json.dumps({"success": True, "message": "Already saved; not added again."})
    rows += (
        _Rows()
        .add("skill_manage", {"action": "create", "name": "x"}, {"success": False, "error": "bad"})
        .add(
            "skill_manage",
            {"operations": [{"action": "create", "name": "a"}, {"action": "patch", "name": "b"}]},
            {"success": True, "operations_applied": False,
             "results": [{"success": True, "action": "create", "name": "a"}]},
        )
        .rows
    )  # fmt: skip
    assert summary_lines(rows, "on") == []


def test_batch_results_name_each_applied_skill() -> None:
    rows = (
        _Rows()
        .add(
            "skill_manage",
            {"operations": [
                {"action": "create", "name": "release-notes", "content": "---"},
                {"action": "write_file", "name": "deploy", "file_path": "references/env.md",
                 "file_content": "Staging needs the VPN."},
            ]},
            {"success": True, "operations_applied": True,
             "results": [{"success": True, "action": "create", "name": "release-notes"},
                         {"success": True, "action": "write_file", "name": "deploy"}]},
        )
        .rows
    )  # fmt: skip
    assert summary_lines(rows, "on") == ["Skill release-notes created", "Skill deploy improved"]
    verbose = summary_lines(rows, "verbose")
    assert verbose[1] == "Skill deploy improved: references/env.md: Staging needs the VPN."


def test_verbose_adds_a_short_preview() -> None:
    long = "Use the staging cluster. " * 30
    rows = (
        _Rows().add("memory", *_memory_add(long)).add("skill_manage", *_skill_patch(new=long)).rows
    )
    lines = summary_lines(rows, "verbose")
    assert lines[0].startswith("Memory updated: Use the staging cluster.")
    assert lines[1].startswith("Skill deploy improved: Use the staging cluster.")
    for line in lines:
        preview = line.split(": ", 1)[1]
        assert len(preview) <= PREVIEW_CHARS and preview.endswith("…")


def test_calls_from_the_replayed_history_are_ignored() -> None:
    rows = _Rows().add("memory", *_memory_add()).rows
    orphan = TranscriptRow(
        role="tool", tool_call_id="from-history", content=json.dumps({"success": True})
    )
    assert summary_lines([orphan], "on") == []
    assert summary_lines(
        [*rows, TranscriptRow(role="tool", tool_call_id="c1", content="not json")], "on"
    ) == ["Memory updated"]


def test_off_mode_has_no_lines_and_no_event(scope: Scope) -> None:
    rows = _Rows().add("memory", *_memory_add()).rows
    assert summary_lines(rows, "off") == []
    seen: list[EventBase] = []
    sink = EventSink("s1", listeners=(seen.append,))
    assert emit_done(sink, {"memory"}, ["x"], "off") is None
    assert seen == []
    event = emit_done(sink, {"skills", "memory"}, ["Memory updated"], "on")
    assert isinstance(event, LearningReviewDone)
    assert seen == [event]
    assert event.type == "learning.review.done"
    assert event.kinds == ["memory", "skills"] and event.summary_lines == ["Memory updated"]


def test_mode_from_config(scope: Scope) -> None:
    assert mode_from(scope) == "on"
    assert mode_from(with_config(scope, {"display": {"learning_notices": "verbose"}})) == "verbose"
    assert mode_from(with_config(scope, {"display": {"learning_notices": False}})) == "off"
    assert mode_from(with_config(scope, {"display": {"learning_notices": "loud"}})) == "on"


def test_completion_log_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="indus.learning.review.summary")
    log_completion(
        "s1",
        kinds={"memory"},
        lines=["Memory updated"],
        requests=3,
        input_tokens=900,
        output_tokens=30,
        cache_read_tokens=800,
        stop="done",
        routed=False,
    )
    assert "review finished: session=s1 kinds=memory requests=3 in=900 out=30" in caplog.text
    assert "changed=memory" in caplog.text


def test_a_finished_review_emits_its_lines_on_the_parent_stream(scope: Scope) -> None:
    async def body() -> None:
        parent = make_parent(scope, ScriptedCaller([text("hi")]))
        await parent.turn("I like short answers")
        store = parent.runtime.store
        assert store is not None
        add = {"action": "add", "target": "user", "content": "Prefers short answers."}
        fork = ScriptedCaller([calls(("memory", add)), text("Saved one fact.")])
        runner = ReviewRunner(parent.review_parent(caller=fork))
        request = ReviewRequest(frozenset({"memory"}), tuple(await store.load("s1")), "s1")
        outcome = await runner.run(request)
        assert outcome.summary_lines == ("User profile updated",)
        done = [e for e in parent.events if isinstance(e, LearningReviewDone)]
        assert len(done) == 1 and done[0].summary_lines == ["User profile updated"]

    asyncio.run(body())


# -- prompts --------------------------------------------------------------------------------------


def test_prompt_variants() -> None:
    memory = build_review_prompt({"memory"}, allowed_tools={"memory", "read_file"})
    assert "`user`" in memory and "skill_manage" not in memory
    assert memory.endswith("may call only these tools: memory, read_file. Any other tool is "
                           "refused, so do not try one.")  # fmt: skip
    skills = build_review_prompt({"skills"})
    assert "Read before you write" in skills and "`user` (the memory tool" not in skills
    both = build_review_prompt({"memory", "skills"}, focus="  the deploy steps ")
    assert both.index("Memory.") < both.index("Skills.")
    assert both.rstrip().endswith("Put it ahead of everything above:\nthe deploy steps")
    assert NOTHING in both
    assert "Never delete" in both
