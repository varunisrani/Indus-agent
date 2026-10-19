"""The in-script module, the per-session description and the failure hints (P10-09)."""

from __future__ import annotations

import itertools
import json
import types
from pathlib import Path
from typing import Any

import pytest

from indus.tools import toolsets
from indus.tools.builtin import load_builtins
from indus.tools.builtin.code import ExecuteCodeTool, register_tools
from indus.tools.builtin.code.schema import (
    ALLOWED,
    allowed_for,
    build_schema,
    describe,
    failure_hint,
)
from indus.tools.builtin.code.stub_module import STUBS, module_source
from indus.tools.definitions import ToolDefinitions
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import DictConfig
from tests.unit.tools.builtin.code._rig import call, code_scope, fake_tools, run

GOLDEN = Path(__file__).parent / "golden" / "cli_description.txt"


def _description(registry: ToolRegistry, selection: list[str], scope: Any = None) -> str:
    tools = ToolDefinitions(registry).build(selection, scope=scope)
    (entry,) = [t["function"] for t in tools if t["function"]["name"] == "execute_code"]
    assert "x-indus-code" not in entry  # the carried settings never reach the provider
    return str(entry["description"])


def test_allowed_set_and_intersection() -> None:
    assert set(ALLOWED) == {
        "read_file", "write_file", "patch", "search_files", "web_search", "web_extract", "terminal"
    }  # fmt: skip
    assert allowed_for(
        ["terminal", "web_search", "execute_code", "delegate_task", "mcp__x__y"]
    ) == (
        "web_search",
        "terminal",
    )
    assert allowed_for([]) == ()


def test_session_without_web_tools_never_mentions_them(tmp_path: Path) -> None:
    registry, _ = fake_tools(tmp_path)
    register_tools(registry)
    text = _description(registry, ["execute_code", "read_file", "write_file", "terminal"])
    assert "web_search" not in text and "web_extract" not in text
    assert "read_file(path" in text and "terminal(command" in text
    source = module_source(allowed_for(["read_file", "terminal"]))
    assert "def web_search" not in source and "def read_file" in source

    scope = code_scope(tmp_path)
    code = (
        "import indus_tools\n"
        "print(hasattr(indus_tools, 'web_search'), hasattr(indus_tools, 'read_file'))"
    )
    result = run(
        call(
            ExecuteCodeTool(),
            scope,
            code,
            registry=registry,
            cwd=tmp_path,
            enabled_tools=["read_file", "terminal", "execute_code"],
        )
    )
    assert result["output"] == "False True\n"


def test_terminal_background_in_a_script_is_an_error(tmp_path: Path) -> None:
    scope = code_scope(tmp_path)
    registry, seen = fake_tools(tmp_path)
    code = (
        "from indus_tools import terminal, ToolCallRefused\n"
        "try:\n"
        "    terminal('sleep 100', background=True)\n"
        "except ToolCallRefused as exc:\n"
        "    print('refused:', exc)\n"
        "print(terminal('echo hi')['output'])\n"
    )
    result = run(call(ExecuteCodeTool(), scope, code, registry=registry, cwd=tmp_path))
    first, second = result["output"].splitlines()
    assert first.startswith("refused: terminal inside execute_code runs commands in the foreground")
    assert second == "ran echo hi"
    assert seen == [("terminal", {"command": "echo hi"})]


def test_empty_intersection_has_only_helpers() -> None:
    text = describe(())
    assert "No Indus tools are offered" in text
    for name in STUBS:
        assert f"{name}(" not in text
    source = module_source(())
    assert "def json_parse" in source and "def read_file" not in source


def test_description_follows_mode_and_kernel_settings(tmp_path: Path) -> None:
    registry, _ = fake_tools(tmp_path)
    register_tools(registry)
    selection = ["execute_code", "read_file"]
    default = _description(registry, selection)
    assert "session's working directory" in default and "stay in memory" in default
    strict_scope = code_scope(tmp_path, {"code_execution": {"mode": "strict", "kernel": "off"}})
    strict = _description(registry, selection, strict_scope)
    assert "private empty directory" in strict and "fresh Python process" in strict


def test_module_compiles_for_every_subset() -> None:
    names = sorted(STUBS)
    for size in range(len(names) + 1):
        for subset in itertools.combinations(names, size):
            for transport in ("socket", "file"):
                compile(module_source(subset, transport), "indus_tools.py", "exec")  # type: ignore[arg-type]


def _module() -> types.ModuleType:
    module = types.ModuleType("indus_tools")
    exec(compile(module_source(["read_file"]), "indus_tools.py", "exec"), module.__dict__)  # noqa: S102
    return module


def test_helpers() -> None:
    module = _module()
    assert module.json_parse('﻿{"a": "x\ty"}') == {"a": "x\ty"}
    assert module.json_parse(b'{"b": 1}') == {"b": 1}
    assert module.shell_quote("it's here") == "'it'\"'\"'s here'"
    attempts: list[int] = []

    def flaky() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError("not yet")
        return "done"

    assert module.retry(flaky, max_attempts=3, delay=0) == "done" and len(attempts) == 3
    with pytest.raises(OSError):
        module.retry(lambda: (_ for _ in ()).throw(OSError("never")), max_attempts=2, delay=0)
    assert module.__all__ == ["read_file", "json_parse", "shell_quote", "retry", "ToolCallRefused"]


def test_answers_are_parsed_or_raise() -> None:
    module = _module()
    assert module._answer(json.dumps({"result": '{"content": "x"}'})) == {"content": "x"}
    assert module._answer(json.dumps({"result": "plain text"})) == "plain text"
    with pytest.raises(module.ToolCallRefused, match="budget"):
        module._answer(json.dumps({"refused": "budget"}))
    with pytest.raises(module.ToolCallRefused):
        module._answer("not json")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "ImportError: cannot import name 'browser_navigate' from 'indus_tools' (/x/a.py)",
            "`browser_navigate` cannot be called from a script in this session. Tools importable "
            "from indus_tools here: read_file, terminal.",
        ),
        (
            "ImportError: cannot import name 'retry' from 'indus_tools'",
            "`retry` is a helper of indus_tools",
        ),
        ("NameError: name 'json_parse' is not defined", "from indus_tools import json_parse"),
        ("TypeError: string indices must be integers, not 'str'", "already return parsed dicts"),
        ("AttributeError: 'str' object has no attribute 'get'", "already return parsed dicts"),
        (
            "TypeError: the JSON object must be str, bytes or bytearray, not dict",
            "already return parsed dicts",
        ),
        ("ModuleNotFoundError: No module named 'pandas'", "'pandas' is not installed"),
        ("ModuleNotFoundError: No module named 'indus_tools'", "retry with reset=true"),
    ],
)
def test_failure_hints(text: str, expected: str) -> None:
    hint = failure_hint(
        f"Traceback (most recent call last):\n  ...\n{text}\n", ["read_file", "terminal"]
    )
    assert hint is not None and expected in hint


def test_no_hint_for_other_failures() -> None:
    assert failure_hint("ZeroDivisionError: division by zero") is None
    assert failure_hint("") is None


def test_hint_reaches_the_result(tmp_path: Path) -> None:
    scope = code_scope(tmp_path)
    registry, _ = fake_tools(tmp_path)
    code = "from indus_tools import web_extract\n"
    result = run(
        call(
            ExecuteCodeTool(),
            scope,
            code,
            registry=registry,
            cwd=tmp_path,
            enabled_tools=["read_file"],
        )
    )
    assert result["status"] == "error"
    assert "`web_extract` cannot be called from a script" in result["hint"]
    assert "importable from indus_tools here: read_file." in result["hint"]


def test_golden_description_for_the_cli_toolset(scope: Any) -> None:
    registry = ToolRegistry()
    load_builtins(registry)
    selection = toolsets.select(["indus-cli"], registry=registry, scope=scope).tools
    scope = scope.with_config(DictConfig({}))
    text = _description(registry, sorted(selection), scope)
    assert text == GOLDEN.read_text(), "regenerate golden/cli_description.txt and note why"


def test_schema_parameters() -> None:
    built = build_schema(["read_file"])
    assert built["parameters"]["required"] == ["code"]
    assert set(built["parameters"]["properties"]) == {"code", "reset"}
