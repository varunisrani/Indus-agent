"""P25-16: the runnable examples in the user guide run green against the fake model.

An example is a fenced ``console`` block right after a ``<!-- test -->`` marker
(``<!-- test script=NAME -->`` picks a fake-model script from ``tests/fixtures/model_scripts``;
the default is ``hello``). Lines starting with ``$ indus`` are commands; the lines after a command
are its expected output. A line ``...`` matches any number of lines, and a line ending in ``...``
matches any line that starts with the text before it. All commands of a block run in order in one
fresh home and working folder, through the real ``indus`` entry point with the fake model route
(``tests.integration.cli.fake_runner``).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
DOCS = ROOT / "docs" / "user"
MARKER = re.compile(r"^<!--\s*test(?:\s+script=(?P<script>[\w-]+))?\s*-->\s*$")
FENCE = re.compile(r"^```console\s*$")
TIMEOUT_S = 120
MIN_EXAMPLES = 8


@dataclass
class Step:
    argv: list[str]
    expected: list[str] = field(default_factory=list[str])


@dataclass
class Example:
    page: str
    line: int
    script: str
    steps: list[Step]

    @property
    def id(self) -> str:
        return f"{self.page}:{self.line}"


def parse_examples(text: str, page: str) -> list[Example]:
    lines = text.splitlines()
    found: list[Example] = []
    for index, line in enumerate(lines):
        marker = MARKER.match(line.strip())
        if marker is None:
            continue
        if index + 1 >= len(lines) or not FENCE.match(lines[index + 1].strip()):
            raise ValueError(
                f"{page}:{index + 1}: a test marker must sit right above a console block"
            )
        steps: list[Step] = []
        cursor = index + 2
        while cursor < len(lines) and lines[cursor].strip() != "```":
            body = lines[cursor]
            if body.startswith("$ "):
                argv = shlex.split(body[2:])
                if not argv or argv[0] != "indus":
                    raise ValueError(f"{page}:{cursor + 1}: examples run `indus` commands only")
                steps.append(Step(argv[1:]))
            elif steps:
                steps[-1].expected.append(body.rstrip())
            cursor += 1
        if not steps:
            raise ValueError(f"{page}:{index + 1}: a tested block needs at least one command")
        found.append(Example(page, index + 1, marker.group("script") or "hello", steps))
    return found


def all_examples() -> list[Example]:
    out: list[Example] = []
    for path in sorted(DOCS.rglob("*.md")):
        out.extend(
            parse_examples(path.read_text(encoding="utf-8"), path.relative_to(DOCS).as_posix())
        )
    return out


def matches(expected: list[str], actual: list[str]) -> bool:
    """Expected lines against actual ones, with ``...`` wildcards (see the module notes)."""
    if not expected:
        return all(not line.strip() for line in actual)
    head, rest = expected[0], expected[1:]
    if head == "...":
        return any(matches(rest, actual[i:]) for i in range(len(actual) + 1))
    if not actual:
        return False
    line = actual[0].rstrip()
    ok = line.startswith(head[:-3]) if head.endswith("...") else line == head
    return ok and matches(rest, actual[1:])


def run_example(example: Example, base: Path) -> None:
    home, work = base / "example-home", base / "example-work"
    home.mkdir()
    work.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("INDUS_")}
    env.update(
        {
            "INDUS_HOME": str(home),
            "INDUS_TEST_MODEL_SCRIPT": example.script,
            "PYTHONPATH": str(ROOT),
            "NO_COLOR": "1",
            "TERM": "dumb",
        }
    )
    env.pop("COLUMNS", None)
    for step in example.steps:
        done = subprocess.run(
            [sys.executable, "-m", "tests.integration.cli.fake_runner", *step.argv],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            check=False,
        )
        output = (done.stdout + done.stderr).splitlines()
        shown = " ".join(["indus", *step.argv])
        assert "Traceback (most recent call last)" not in done.stderr, f"{shown}\n{done.stderr}"
        assert matches(step.expected, output), (
            f"{example.id}: `{shown}` printed:\n"
            + "\n".join(output)
            + "\n--- expected ---\n"
            + "\n".join(step.expected)
        )


EXAMPLES = all_examples()


def test_the_guide_has_runnable_examples() -> None:
    assert len(EXAMPLES) >= MIN_EXAMPLES
    assert len({e.id for e in EXAMPLES}) == len(EXAMPLES)


@pytest.mark.parametrize("example", EXAMPLES, ids=[e.id for e in EXAMPLES])
def test_example_runs_green(example: Example, tmp_path: Path) -> None:
    run_example(example, tmp_path)


# -- the harness itself -------------------------------------------------------------------------


def test_parse_reads_commands_expected_lines_and_script() -> None:
    text = (
        "intro\n<!-- test script=demo1 -->\n```console\n$ indus --version\nIndus Agent ...\n"
        "$ indus config get agent.max_turns\n150\n```\n"
    )
    (example,) = parse_examples(text, "x.md")
    assert example.script == "demo1"
    assert [s.argv for s in example.steps] == [["--version"], ["config", "get", "agent.max_turns"]]
    assert example.steps[0].expected == ["Indus Agent ..."]


def test_parse_refuses_other_programs_and_loose_markers() -> None:
    with pytest.raises(ValueError, match="`indus` commands only"):
        parse_examples("<!-- test -->\n```console\n$ rm -rf x\n```\n", "x.md")
    with pytest.raises(ValueError, match="right above a console block"):
        parse_examples("<!-- test -->\n\n```console\n$ indus --version\n```\n", "x.md")


@pytest.mark.parametrize(
    ("expected", "actual", "ok"),
    [
        (["a", "b"], ["a", "b"], True),
        (["a"], ["a", "b"], False),
        (["a", "..."], ["a", "b", "c"], True),
        (["...", "c"], ["a", "b", "c"], True),
        (["Set x ..."], ["Set x = 1 in /tmp/h/config.yaml"], True),
        (["Set y ..."], ["Set x = 1"], False),
        ([], ["anything"], False),
        ([], ["", "  "], True),
    ],
)
def test_matching(expected: list[str], actual: list[str], ok: bool) -> None:
    assert matches(expected, actual) is ok
