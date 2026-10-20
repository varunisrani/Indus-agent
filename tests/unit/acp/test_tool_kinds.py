"""P19-10: every tool has an ACP kind; titles, start content and results as the editor sees them."""

from __future__ import annotations

from acp.schema import ToolKind
from pydantic import TypeAdapter

from indus.acp import tool_kinds
from indus.tools import toolsets
from indus.tools.builtin import load_builtins
from indus.tools.registry import ToolRegistry

VENDOR_KINDS = TypeAdapter(ToolKind)


def _our_tools() -> set[str]:
    registry = ToolRegistry()
    load_builtins(registry)
    names = set(registry.names())
    names.update(toolsets.CORE_TOOLS)
    for toolset in toolsets.TOOLSETS.values():
        names.update(toolset.tools)
    return names


def test_every_tool_in_our_registry_and_toolsets_has_an_explicit_kind() -> None:
    missing = sorted(_our_tools() - set(tool_kinds.KINDS))
    assert missing == []


def test_every_kind_is_a_vendor_kind() -> None:
    for kind in set(tool_kinds.KINDS.values()):
        VENDOR_KINDS.validate_python(kind)


def test_kind_groups() -> None:
    expected = {
        "read_file": "read",
        "skill_view": "read",
        "browser_snapshot": "read",
        "vision_analyze": "read",
        "write_file": "edit",
        "patch": "edit",
        "skill_manage": "edit",
        "search_files": "search",
        "terminal": "execute",
        "process_manage": "execute",
        "execute_code": "execute",
        "browser_click": "execute",
        "delegate_task": "execute",
        "image_generate": "execute",
        "text_to_speech": "execute",
        "web_search": "fetch",
        "web_extract": "fetch",
        "browser_navigate": "fetch",
        "todo_list": "other",
        "mcp__docs__search": "other",
        "something_new": "other",
    }
    assert {name: tool_kinds.kind_of(name) for name in expected} == expected


def test_titles_name_what_the_call_is_about() -> None:
    assert tool_kinds.title("terminal", {"command": "ls -la"}) == "terminal: ls -la"
    assert tool_kinds.title("read_file", {"path": "/w/a.py"}) == "read_file: /w/a.py"
    assert tool_kinds.title("todo_list", {}) == "todo_list"
    long = tool_kinds.title("terminal", {"command": "x" * 200})
    assert len(long) == len("terminal: ") + 80 and long.endswith("…")


def test_edits_start_as_diffs() -> None:
    write = tool_kinds.start_content("write_file", {"path": "/w/a.txt", "content": "new"})
    assert write == [{"type": "diff", "path": "/w/a.txt", "newText": "new"}]
    patch = tool_kinds.start_content(
        "patch", {"path": "/w/a.txt", "old_string": "old", "new_string": "new"}
    )
    assert patch == [{"type": "diff", "path": "/w/a.txt", "oldText": "old", "newText": "new"}]


def test_terminal_starts_as_the_command_and_reads_have_no_content() -> None:
    shown = tool_kinds.start_content("terminal", {"command": "make test"})
    assert shown == [tool_kinds.text_content("$ make test")]
    assert tool_kinds.start_content("read_file", {"path": "/w/a.py"}) is None
    assert tool_kinds.locations({"path": "/w/a.py", "offset": 3}) == [
        {"path": "/w/a.py", "line": 3}
    ]


def test_output_is_cut_at_five_thousand_characters() -> None:
    content = tool_kinds.result_content("terminal", "y" * 6000)
    assert content is not None
    text = content[0]["content"]["text"]
    assert len(text) == tool_kinds.MAX_OUTPUT
    assert text.endswith("(6,000 characters in all)")
    read = tool_kinds.result_content("read_file", "1|x\n" * 3000)
    assert read is not None and len(read[0]["content"]["text"]) <= tool_kinds.MAX_OUTPUT
    assert tool_kinds.result_content("terminal", "  ") is None


def test_fences_outgrow_backticks_inside() -> None:
    assert tool_kinds.fenced("a ```` b").startswith("`````\n")
