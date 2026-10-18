"""V4A envelope parsing and all-or-nothing application over in-memory files (P05-20)."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Mapping
from typing import Any

import pytest

from indus.tools.builtin import v4a
from indus.tools.builtin.v4a import PatchSyntaxError, apply_patch, parse, run_patch_mode


class MemFiles:
    """``PatchFiles`` over a dict, with switches for failures and guard refusals."""

    def __init__(self, files: Mapping[str, str] | None = None) -> None:
        self.files: dict[str, str] = dict(files or {})
        self.fail_write: set[str] = set()
        self.fail_restore: set[str] = set()
        self.refuse: dict[str, str] = {}
        self.unreadable: set[str] = set()
        self.log: list[tuple[str, str]] = []

    def resolve(self, path: str) -> str:
        return path.removeprefix("./")

    async def read_text(self, path: str) -> str | None:
        key = self.resolve(path)
        if key in self.unreadable:
            raise OSError("permission denied")
        return self.files.get(key)

    async def write_text(self, path: str, content: str) -> None:
        key = self.resolve(path)
        restoring = any(op == "write" and p == key for op, p in self.log)
        if key in self.fail_write and not restoring:
            raise OSError("disk full")
        if key in self.fail_restore and restoring:
            raise OSError("still broken")
        self.log.append(("write", key))
        self.files[key] = content

    async def delete(self, path: str) -> None:
        key = self.resolve(path)
        self.log.append(("delete", key))
        if key in self.fail_restore and key not in self.files:
            raise OSError("cannot delete")
        self.files.pop(key, None)

    async def refusal(self, path: str, action: str, content: str | None) -> str | None:
        return self.refuse.get(self.resolve(path))


def _apply(text: str, files: MemFiles) -> v4a.PatchOutcome:
    return asyncio.run(apply_patch(parse(text), files))


def _envelope(*body: str) -> str:
    return "\n".join(["*** Begin Patch", *body, "*** End Patch"]) + "\n"


# -- parsing ----------------------------------------------------------------------------------


def test_parse_every_section_kind() -> None:
    patch = parse(
        _envelope(
            "*** Add File: new.txt",
            "+hello",
            "+",
            "+world",
            "*** Delete File: old.txt",
            "*** Update File: src/app.py",
            "*** Move to: src/main.py",
            "@@ class App:",
            "@@ def run(self):",
            "     x = 1",
            "-    y = 2",
            "+    y = 3",
            "@@",
            " tail",
            "+more",
            "*** End of File",
            "*** Move File: a.txt -> b.txt",
        )
    )
    add, delete, update, move = patch.ops
    assert (add.kind, add.path, add.content) == ("add", "new.txt", ("hello", "", "world"))
    assert (delete.kind, delete.path) == ("delete", "old.txt")
    assert (update.kind, update.path, update.new_path) == ("update", "src/app.py", "src/main.py")
    first, second = update.hunks
    assert first.anchors == ("class App:", "def run(self):")
    assert first.old == ["    x = 1", "    y = 2"]
    assert first.new == ["    x = 1", "    y = 3"]
    assert second.at_end and second.anchors == ()
    assert (move.kind, move.path, move.new_path) == ("move", "a.txt", "b.txt")


def test_parse_ignores_fences_and_crlf_and_trailing_blank_lines() -> None:
    text = "```\r\n*** Begin Patch\r\n*** Add File: a\r\n+x\r\n\r\n*** End Patch\r\n```\r\n"
    (op,) = parse(text).ops
    assert op.content == ("x",)


def test_anchor_with_closing_marks_and_no_newline_marker() -> None:
    (op,) = parse(
        _envelope(
            "*** Update File: f", "@@ def go(): @@", "-a", "+b", "\\ No newline at end of file"
        )
    ).ops
    assert op.hunks[0].anchors == ("def go():",)
    assert op.hunks[0].body == (("-", "a"), ("+", "b"))


@pytest.mark.parametrize(
    ("text", "line", "words"),
    [
        ("*** Add File: a\n+x\n*** End Patch\n", 1, "Begin Patch"),
        ("*** Begin Patch\n*** Add File: a\n+x\n", 4, "End Patch"),
        ("*** Begin Patch\n*** Frob File: a\n*** End Patch\n", 2, "unknown patch line"),
        ("*** Begin Patch\n*** Add File: a\nx\n*** End Patch\n", 3, "must start with '+'"),
        ("*** Begin Patch\n*** Update File: a\n@@\nbare\n*** End Patch\n", 4, "must start with"),
        ("*** Begin Patch\n*** Update File: a\n*** End Patch\n", 2, "has no hunks"),
        ("*** Begin Patch\n*** Delete File: a\n-x\n*** End Patch\n", 3, "takes no body"),
        ("*** Begin Patch\n*** Add File:\n*** End Patch\n", 2, "needs a path"),
        ("*** Begin Patch\nloose\n*** End Patch\n", 2, "before the first file section"),
        ("*** Begin Patch\n*** Move to: b\n*** End Patch\n", 2, "right after"),
        ("*** Begin Patch\n*** End Patch\n", 1, "no file sections"),
        ("*** Begin Patch\n*** Move File: a ->\n*** End Patch\n", 2, "<from> -> <to>"),
    ],
)
def test_malformed_envelope_names_the_line(text: str, line: int, words: str) -> None:
    with pytest.raises(PatchSyntaxError) as caught:
        parse(text)
    assert caught.value.line == line
    assert words in str(caught.value)


# -- applying ---------------------------------------------------------------------------------


def test_multi_file_patch_applies_with_diff() -> None:
    files = MemFiles({"a.py": "one\ntwo\nthree\n", "gone.txt": "bye\n"})
    outcome = _apply(
        _envelope(
            "*** Update File: a.py",
            "@@",
            " one",
            "-two",
            "+TWO",
            " three",
            "*** Add File: b.py",
            "+print('b')",
            "*** Delete File: gone.txt",
        ),
        files,
    )
    assert outcome.ok, outcome.error
    assert files.files == {"a.py": "one\nTWO\nthree\n", "b.py": "print('b')\n"}
    assert outcome.updated == ("a.py",) and outcome.added == ("b.py",)
    assert outcome.deleted == ("gone.txt",)
    assert "-two\n+TWO" in outcome.diff and "+++ b/b.py" in outcome.diff
    assert "--- a/gone.txt" in outcome.diff


def test_second_file_miss_writes_nothing() -> None:
    files = MemFiles({"a.txt": "alpha\n", "b.txt": "beta\n"})
    outcome = _apply(
        _envelope(
            "*** Update File: a.txt",
            "-alpha",
            "+ALPHA",
            "*** Update File: b.txt",
            "-gamma",
            "+GAMMA",
        ),
        files,
    )
    assert not outcome.ok
    assert "b.txt: hunk 1" in outcome.error and "'gamma'" in outcome.error
    assert files.files == {"a.txt": "alpha\n", "b.txt": "beta\n"}
    assert files.log == []


def test_write_failure_midway_rolls_back_earlier_writes() -> None:
    files = MemFiles({"a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n"})
    files.fail_write.add("b.txt")
    outcome = _apply(
        _envelope(
            "*** Update File: a.txt",
            "-a",
            "+A",
            "*** Update File: b.txt",
            "-b",
            "+B",
            "*** Add File: new.txt",
            "+n",
            "*** Delete File: c.txt",
        ),
        files,
    )
    assert not outcome.ok and outcome.rolled_back
    assert "every earlier change was undone" in outcome.error
    assert files.files == {"a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n"}


def test_rollback_that_fails_names_what_stays_changed() -> None:
    files = MemFiles({"a.txt": "a\n", "b.txt": "b\n"})
    files.fail_write.add("b.txt")
    files.fail_restore.add("a.txt")
    outcome = _apply(
        _envelope("*** Update File: a.txt", "-a", "+A", "*** Update File: b.txt", "-b", "+B"),
        files,
    )
    assert not outcome.ok and not outcome.rolled_back
    assert outcome.left_changed == ("a.txt",)


def test_rollback_deletes_a_file_the_patch_created() -> None:
    files = MemFiles({"b.txt": "b\n"})
    files.fail_write.add("b.txt")
    outcome = _apply(
        _envelope("*** Add File: new.txt", "+n", "*** Update File: b.txt", "-b", "+B"), files
    )
    assert outcome.rolled_back
    assert files.files == {"b.txt": "b\n"}


def test_move_and_update() -> None:
    files = MemFiles({"old/name.py": "def f():\n    return 1\n"})
    outcome = _apply(
        _envelope(
            "*** Update File: old/name.py",
            "*** Move to: new/name.py",
            "@@ def f():",
            "-    return 1",
            "+    return 2",
        ),
        files,
    )
    assert outcome.ok, outcome.error
    assert files.files == {"new/name.py": "def f():\n    return 2\n"}
    assert outcome.moved == ("old/name.py -> new/name.py",)
    assert outcome.added == () and outcome.deleted == ()
    assert "rename old/name.py -> new/name.py" in outcome.diff
    assert "+    return 2" in outcome.diff


def test_plain_move_and_a_later_update_of_the_new_name() -> None:
    files = MemFiles({"a.txt": "x\n"})
    outcome = _apply(
        _envelope("*** Move File: a.txt -> b.txt", "*** Update File: b.txt", "-x", "+y"), files
    )
    assert outcome.ok, outcome.error
    assert files.files == {"b.txt": "y\n"}


def test_move_onto_an_existing_file_is_refused() -> None:
    files = MemFiles({"a.txt": "a\n", "b.txt": "b\n"})
    outcome = _apply(_envelope("*** Move File: a.txt -> b.txt"), files)
    assert not outcome.ok and "destination exists" in outcome.error
    assert files.files == {"a.txt": "a\n", "b.txt": "b\n"}


def test_add_on_existing_and_delete_of_missing_are_refused() -> None:
    files = MemFiles({"here.txt": "x\n"})
    added = _apply(_envelope("*** Add File: here.txt", "+y"), files)
    assert not added.ok and "use '*** Update File:'" in added.error
    deleted = _apply(_envelope("*** Delete File: nowhere.txt"), files)
    assert not deleted.ok and "does not exist" in deleted.error
    assert files.files == {"here.txt": "x\n"}


def test_delete_then_add_of_one_path_in_one_patch() -> None:
    files = MemFiles({"x.txt": "old\n"})
    outcome = _apply(_envelope("*** Delete File: x.txt", "*** Add File: ./x.txt", "+new"), files)
    assert outcome.ok, outcome.error
    assert files.files == {"x.txt": "new\n"}


def test_already_applied_hunk_is_skipped() -> None:
    files = MemFiles({"a.txt": "one\nTWO\n"})
    outcome = _apply(_envelope("*** Update File: a.txt", " one", "-two", "+TWO"), files)
    assert outcome.ok
    assert any("already in the file" in note for note in outcome.notes)
    assert files.log == []


def test_context_only_patch_changes_nothing() -> None:
    files = MemFiles({"a.txt": "one\n"})
    outcome = _apply(_envelope("*** Update File: a.txt", " one"), files)
    assert not outcome.ok and "changes nothing" in outcome.error


def test_guard_refusal_stops_before_any_write() -> None:
    files = MemFiles({"a.txt": "a\n", "AGENTS.md": "rules\n"})
    files.refuse["AGENTS.md"] = "AGENTS.md is a protected instruction file."
    outcome = _apply(
        _envelope(
            "*** Update File: a.txt", "-a", "+A", "*** Update File: AGENTS.md", "-rules", "+x"
        ),
        files,
    )
    assert not outcome.ok and "protected instruction file" in outcome.error
    assert files.log == []


def test_unreadable_file_refuses_the_patch() -> None:
    files = MemFiles({"a.txt": "a\n"})
    files.unreadable.add("a.txt")
    outcome = _apply(_envelope("*** Update File: a.txt", "-a", "+b"), files)
    assert not outcome.ok and "could not read a.txt" in outcome.error


def test_json_that_stops_parsing_is_refused_fail_closed() -> None:
    files = MemFiles({"cfg.json": '{\n  "a": 1\n}\n', "a.txt": "a\n"})
    outcome = _apply(
        _envelope(
            "*** Update File: a.txt",
            "-a",
            "+A",
            "*** Update File: cfg.json",
            '-  "a": 1',
            '+  "a": ',
        ),
        files,
    )
    assert not outcome.ok and "not valid JSON" in outcome.error
    assert files.log == []


def test_python_lint_reports_only_new_problems() -> None:
    files = MemFiles({"m.py": "x = 1\n"})
    outcome = _apply(_envelope("*** Update File: m.py", "-x = 1", "+x = (1"), files)
    assert outcome.ok
    report = outcome.lint["m.py"]
    assert report["status"] == "introduced" and "SyntaxError" in report["errors"][0]


def test_crlf_and_missing_final_newline_are_kept() -> None:
    files = MemFiles({"w.txt": "a\r\nb\r\nc"})
    outcome = _apply(_envelope("*** Update File: w.txt", " a", "-b", "+B", " c"), files)
    assert outcome.ok, outcome.error
    assert files.files["w.txt"] == "a\r\nB\r\nc"


def test_whitespace_and_punctuation_drift_still_matches_with_a_note() -> None:
    files = MemFiles({"q.py": "say(\u201chi\u201d)   \n    keep = 1\n"})
    outcome = _apply(
        _envelope("*** Update File: q.py", '-say("hi")', '+say("bye")', "  keep = 1"), files
    )
    assert outcome.ok, outcome.error
    # the context line keeps the file's own indentation, not the patch's
    assert files.files["q.py"] == 'say("bye")\n    keep = 1\n'
    assert any("normalised" in note for note in outcome.notes)


def test_anchor_places_an_addition_and_end_of_file_appends() -> None:
    files = MemFiles({"c.py": "class A:\n    pass\nclass B:\n    pass\n"})
    outcome = _apply(
        _envelope(
            "*** Update File: c.py",
            "@@ class B:",
            "+    x = 1",
            "@@",
            "+# end",
            "*** End of File",
        ),
        files,
    )
    assert outcome.ok, outcome.error
    assert files.files["c.py"] == "class A:\n    pass\nclass B:\n    x = 1\n    pass\n# end\n"


def test_anchor_picks_the_right_one_of_two_identical_blocks() -> None:
    text = "def a():\n    return 0\n\ndef b():\n    return 0\n"
    files = MemFiles({"d.py": text})
    outcome = _apply(
        _envelope("*** Update File: d.py", "@@ def b():", "-    return 0", "+    return 1"), files
    )
    assert outcome.ok
    assert files.files["d.py"] == "def a():\n    return 0\n\ndef b():\n    return 1\n"


def test_missing_anchor_is_an_error() -> None:
    files = MemFiles({"d.py": "x\n"})
    outcome = _apply(_envelope("*** Update File: d.py", "@@ def nope():", "-x", "+y"), files)
    assert not outcome.ok and "anchor 'def nope():'" in outcome.error


def test_seeded_random_edits_round_trip() -> None:
    rng = random.Random(20261018)
    for _ in range(60):
        lines = [f"line {i} {rng.randint(0, 9)}" for i in range(rng.randint(3, 30))]
        start = rng.randrange(len(lines))
        end = min(len(lines), start + rng.randint(1, 3))
        new = [f"changed {rng.randint(0, 999)}" for _ in range(rng.randint(0, 3))]
        before = lines[max(0, start - 3) : start]
        after = lines[end : end + 3]
        body = (
            [f" {x}" for x in before]
            + [f"-{x}" for x in lines[start:end]]
            + [f"+{x}" for x in new]
            + [f" {x}" for x in after]
        )
        files = MemFiles({"f.txt": "\n".join(lines) + "\n"})
        outcome = _apply(_envelope("*** Update File: f.txt", "@@", *body), files)
        assert outcome.ok, outcome.error
        expected = lines[:start] + new + lines[end:]
        assert files.files["f.txt"] == ("\n".join(expected) + "\n" if expected else "")


# -- the patch tool's mode --------------------------------------------------------------------


def _mode(args: dict[str, Any], files: MemFiles) -> dict[str, Any]:
    return json.loads(asyncio.run(run_patch_mode(args, files)))


def test_run_patch_mode_success_and_errors() -> None:
    files = MemFiles({"a.txt": "a\n"})
    body = _mode({"mode": "patch", "patch": _envelope("*** Update File: a.txt", "-a", "+b")}, files)
    assert body["success"] and body["updated"] == ["a.txt"] and "+b" in body["diff"]
    bad = _mode({"mode": "patch", "patch": "*** Begin Patch\n*** Add File: x\n"}, files)
    assert not bad["success"] and bad["line"] == 3 and "Nothing was changed" in bad["error"]
    empty = _mode({"mode": "patch"}, files)
    assert not empty["success"] and "'patch' field" in empty["error"]


def test_wants_v4a_accepts_both_shapes() -> None:
    assert v4a.wants_v4a({"mode": "patch", "patch": "x"})
    assert v4a.wants_v4a({"patch": "*** Begin Patch"})
    assert not v4a.wants_v4a({"mode": "replace", "patch": "x"})
    assert not v4a.wants_v4a({"old_string": "a", "new_string": "b"})
    assert not v4a.wants_v4a({"patch": "x", "old_string": "a"})


def test_with_patch_mode_extends_the_schema() -> None:
    base = {
        "name": "patch",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "old_string": {"type": "string"}},
            "required": ["path", "old_string", "new_string"],
        },
    }
    out = v4a.with_patch_mode(base)
    assert set(out["parameters"]["properties"]) == {"path", "old_string", "mode", "patch"}
    assert out["parameters"]["required"] == ["path"]
    assert base["parameters"]["required"] == ["path", "old_string", "new_string"]


def test_added_lines_below_a_repeated_anchor_are_refused() -> None:
    files = MemFiles({"r.py": "def run():\n    a = 1\ndef run():\n    b = 2\n"})
    outcome = _apply(_envelope("*** Update File: r.py", "@@ def run():", "+    x = 0"), files)
    assert not outcome.ok and "appears 2 times" in outcome.error
    assert files.log == []
