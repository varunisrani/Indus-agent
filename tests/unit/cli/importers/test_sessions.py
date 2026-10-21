"""P24-28: session import from Claude Code and Codex CLI files (our own sample files)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.cli.importers import sessions as importer
from indus.foundation.scope import Scope
from indus.state import messages, registry, session_meta
from indus.state.registry import StoreHandle


def _lines(path: Path, records: list[Any], *, extra: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(r) for r in records) + "\n" + extra
    path.write_text(body, encoding="utf-8")
    return path


def _claude(home: Path, name: str = "abc") -> Path:
    records: list[Any] = [
        {"type": "summary", "summary": "a chat about tests"},
        {
            "type": "user",
            "timestamp": "2026-09-01T10:00:00Z",
            "cwd": "/work/app",
            "sessionId": name,
            "message": {"role": "user", "content": "Run the tests please\nand tell me"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-09-01T10:00:05Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hidden thoughts"},
                    {"type": "text", "text": "Running them now."},
                    {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"cmd": "pytest"}},
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": [{"type": "text", "text": "3 passed"}],
                    }
                ],
            },
        },
        {"type": "assistant", "message": {"role": "assistant", "content": "All 3 pass."}},
    ]
    return _lines(home / ".claude" / "projects" / "-work-app" / f"{name}.jsonl", records)


def _codex(home: Path) -> Path:
    records: list[Any] = [
        {
            "timestamp": "2026-09-02T08:00:00Z",
            "type": "session_meta",
            "payload": {"id": "c-1", "cwd": "/work/lib", "timestamp": "2026-09-02T08:00:00Z"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "<environment_context>cwd</environment_context>"}
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "List the files"}],
            },
        },
        {"type": "response_item", "payload": {"type": "reasoning", "summary": []}},
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "arguments": json.dumps({"command": ["ls"]}),
                "call_id": "call_9",
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "call_9", "output": "a.py"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "One file: a.py"}],
            },
        },
        {"type": "event_msg", "payload": {"type": "token_count"}},
    ]
    return _lines(home / ".codex" / "sessions" / "2026" / "09" / "02" / "rollout-x.jsonl", records)


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()


def test_preview_lists_mapped_messages_for_claude_code(tmp_path: Path) -> None:
    path = _claude(tmp_path)
    mapped = importer.preview(path, "claude-code")
    rows = mapped.to_dict()["messages"]
    assert [r["role"] for r in rows] == ["user", "assistant", "assistant", "tool", "assistant"]
    assert rows[1]["content"] == "Running them now."
    assert rows[2]["tool"] == "Bash" and rows[3] == {
        "role": "tool",
        "content": "3 passed",
        "tool": "Bash",
    }
    assert "hidden thoughts" not in json.dumps(rows)  # thinking is dropped
    assert mapped.title == "Run the tests please"
    assert mapped.cwd == "/work/app" and mapped.started_at is not None


def test_preview_lists_mapped_messages_for_codex(tmp_path: Path) -> None:
    path = _codex(tmp_path)
    mapped = importer.preview(path, "codex")
    rows = mapped.messages
    assert [r["role"] for r in rows] == ["user", "assistant", "tool", "assistant"]
    assert rows[0]["content"] == "List the files"  # the injected context turn is not a person's
    call = rows[1]["tool_calls"][0]
    assert call["id"] == "call_9" and call["function"]["name"] == "shell"
    assert json.loads(call["function"]["arguments"]) == {"command": ["ls"]}
    assert rows[2] == {
        "role": "tool",
        "tool_call_id": "call_9",
        "tool_name": "shell",
        "content": "a.py",
    }
    assert mapped.cwd == "/work/lib"


def test_an_unknown_format_is_refused(tmp_path: Path) -> None:
    path = _lines(tmp_path / ".codex" / "sessions" / "rollout-z.jsonl", [{"kind": "other"}, [1, 2]])
    with pytest.raises(importer.ImportRefused) as refused:
        importer.preview(path, "codex")
    assert refused.value.code == "unknown_format"
    plain = tmp_path / "notes.jsonl"
    plain.write_text("not json at all\n", encoding="utf-8")
    with pytest.raises(importer.ImportRefused):
        importer.preview(plain, "claude-code")


def test_broken_lines_are_counted_not_fatal(tmp_path: Path) -> None:
    path = _claude(tmp_path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{broken\n")
    mapped = importer.preview(path, "claude-code")
    assert mapped.skipped >= 1 and len(mapped.messages) == 5
    assert any("not understood" in line for line in importer.preview_lines(path, "claude-code"))


def test_candidates_come_from_both_roots_newest_first(tmp_path: Path) -> None:
    older = _claude(tmp_path)
    newer = _codex(tmp_path)
    (tmp_path / ".claude" / "projects" / "stray.jsonl").write_text("{}", encoding="utf-8")
    os.utime(older, (1_000_000, 1_000_000))
    rows = importer.candidates(tmp_path)
    assert [r.path for r in rows] == [newer, older]
    assert rows[0].to_dict()["source"] == "codex" and rows[1].title == "Run the tests please"
    assert [r.kind for r in importer.candidates(tmp_path, kind="claude-code")] == ["claude-code"]


def test_only_the_tools_own_files_are_accepted(tmp_path: Path) -> None:
    good = _claude(tmp_path)
    assert importer.resolve_candidate(tmp_path, "claude-code", str(good)) == good.resolve()
    outside = _lines(tmp_path / "elsewhere" / "x.jsonl", [{"type": "user"}])
    for kind, raw in (
        ("claude-code", str(outside)),
        ("codex", str(good)),
        ("cursor", str(good)),
        ("claude-code", str(tmp_path / ".claude" / "projects" / "missing.jsonl")),
    ):
        with pytest.raises(importer.ImportRefused):
            importer.resolve_candidate(tmp_path, kind, raw)


def test_import_writes_one_session_and_a_second_import_is_refused(
    tmp_path: Path, store: StoreHandle
) -> None:
    path = _claude(tmp_path)
    done = importer.import_file(store, tmp_path, "claude-code", str(path))
    assert done.message_count == 5 and done.title == "Run the tests please"
    with store.read() as conn:
        row = conn.execute(
            "SELECT source, cwd FROM sessions WHERE id = ?", (done.session_id,)
        ).fetchone()
    assert row["source"] == "import-claude-code" and row["cwd"] == "/work/app"
    conversation = messages.as_conversation(store, done.session_id)
    assert [m["role"] for m in conversation] == [
        "user",
        "assistant",
        "assistant",
        "tool",
        "assistant",
    ]
    assert conversation[3]["tool_call_id"] == "tu_1"
    entries = session_meta.list_entries(store, done.session_id, "import")
    assert len(entries) == 1 and next(iter(entries.values()))["source"] == "claude-code"
    with pytest.raises(importer.ImportRefused) as again:
        importer.import_file(store, tmp_path, "claude-code", str(path))
    assert again.value.code == "already_imported" and again.value.session_id == done.session_id


def test_a_title_already_in_use_gets_a_number(tmp_path: Path, store: StoreHandle) -> None:
    first = importer.import_file(store, tmp_path, "claude-code", str(_claude(tmp_path, "one")))
    second_path = _claude(tmp_path, "two")
    with second_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "x"}})
            + "\n"
        )
    second = importer.import_file(store, tmp_path, "claude-code", str(second_path))
    assert first.title == "Run the tests please" and second.title == "Run the tests please #2"
