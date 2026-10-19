# pyright: strict
"""The ``execute_code`` schema, rebuilt per session, and the failure hints (P10-09).

``ALLOWED`` is the fixed set of tools a script may reach. A session gets the intersection with
its own tools: ``allowed_for(enabled)``. The description names only that intersection (R02.21);
``rewrite`` is the registry hook that rebuilds it once the tool array knows which tools survived
filtering. ``failure_hint`` turns a well-known script failure into one sentence of advice.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

from indus.tools.builtin.code.settings import (
    MODULE_NAME,
    STDOUT_INLINE_BYTES,
    TOOL_NAME,
    Mode,
)
from indus.tools.builtin.code.stub_module import HELPERS, STUBS, offered

# Tools never offered inside a script whatever the session has (no recursion, no agents, no MCP).
NEVER: frozenset[str] = frozenset({TOOL_NAME, "delegate_task"})
ALLOWED: frozenset[str] = frozenset(STUBS)

_SIGNATURES: dict[str, str] = {
    "read_file": "read_file(path, offset=1, limit=None) -> dict  # numbered lines from 1",
    "write_file": "write_file(path, content) -> dict  # replaces the whole file; read it first",
    "patch": "patch(path, old_string, new_string, replace_all=False) -> dict",
    "search_files": (
        'search_files(pattern, target="content", path=".", file_glob=None, limit=50) -> dict'
        '  # target="files" finds names'
    ),
    "web_search": "web_search(query, limit=5) -> dict  # title, url and snippet per result",
    "web_extract": "web_extract(urls, char_limit=None) -> dict  # page text per address",
    "terminal": (
        "terminal(command, timeout=None, workdir=None) -> dict  # foreground only: no background,"
        " pty or notify"
    ),
}


def allowed_for(enabled: Iterable[str] | None) -> tuple[str, ...]:
    """The script tools this session has, in description order (``None`` = every allowed one)."""
    if enabled is None:
        return tuple(offered(ALLOWED))
    names = {n for n in enabled if not n.startswith("mcp__") and n not in NEVER}
    return tuple(offered(names & ALLOWED))


def _cwd_sentence(mode: Mode) -> str:
    if mode == "strict":
        return (
            "The script starts in a private empty directory, not the session's working "
            "directory: give absolute paths for files elsewhere."
        )
    return (
        "The script starts in the session's working directory, under the project's active "
        "virtualenv or conda Python when there is one, otherwise under Indus's own Python "
        "(standard library plus Indus's dependencies; check an import before relying on it)."
    )


def describe(tools: Iterable[str], mode: Mode = "project", *, kernel: bool = True) -> str:
    """The model-facing description for exactly ``tools`` (already intersected)."""
    names = offered(tools)
    if names:
        listing = "\n".join(f"  {_SIGNATURES[n]}" for n in names)
        sample = ", ".join([n for n in ("read_file", "terminal") if n in names] or names[:2])
        tools_part = (
            f"Functions available with `from {MODULE_NAME} import {sample}, ...`:\n{listing}\n"
            "Each returns the tool's result as a dict; a failed call returns a dict with an "
            "'error' key. A call Indus refuses (budget used up, bad arguments) raises "
            "ToolCallRefused."
        )
    else:
        tools_part = (
            f"No Indus tools are offered to scripts in this session; `{MODULE_NAME}` holds only "
            "the helpers."
        )
    state = (
        "Variables, imports and loaded data stay in memory between calls in this conversation, "
        "so build on earlier results; reset=true starts over, and a call that times out or is "
        "interrupted loses that state."
        if kernel
        else "Each call runs in a fresh Python process."
    )
    helpers = ", ".join(HELPERS)
    return (
        "Run a Python script that calls Indus tools from code. Worth it when a job needs three "
        "or more tool calls with logic between them: looping over files or pages, filtering a "
        "large result down before it reaches the conversation, retrying on failure. For a single "
        "call, or anything that needs the user, call the tool directly. Only what the script "
        "prints comes back.\n\n"
        f"{tools_part}\n\n"
        f"Helpers (import them too): {helpers}. json_parse(text) is a lenient json.loads, "
        "shell_quote(s) quotes a value for a shell command line, retry(fn, max_attempts=3, "
        "delay=2) retries with doubling waits.\n\n"
        f"{state}\n\n"
        f"Limits: 5 minutes per call, 50 tool calls per call. Printed output over "
        f"{STDOUT_INLINE_BYTES // 1000} kB shows its beginning and end; the full text is saved "
        "to a file named in the result.\n\n"
        f"{_cwd_sentence(mode)}"
    )


def build_schema(
    tools: Iterable[str], mode: Mode = "project", *, kernel: bool = True
) -> dict[str, Any]:
    names = offered(tools)
    sample = (
        ", ".join([n for n in ("read_file", "terminal") if n in names] or names[:2]) or "json_parse"
    )
    return {
        "name": TOOL_NAME,
        "description": describe(names, mode, kernel=kernel),
        "parameters": {
            "type": "object",
            "required": ["code"],
            "properties": {
                "code": {
                    "type": "string",
                    "description": (
                        f"Python source. Import tools with `from {MODULE_NAME} import {sample}` "
                        "and print what you want back."
                    ),
                },
                "reset": {
                    "type": "boolean",
                    "default": False,
                    "description": "Throw away the kept interpreter state before running.",
                },
            },
        },
    }


def schema_rewrite(
    mode_of: Callable[[], Mode], kernel_of: Callable[[], bool]
) -> Callable[[dict[str, Any], frozenset[str]], dict[str, Any] | None]:
    """The registry ``rewrite`` hook: the schema for the tools that survived filtering."""

    def rewrite(function: dict[str, Any], present: frozenset[str]) -> dict[str, Any] | None:
        built = build_schema(allowed_for(present), mode_of(), kernel=kernel_of())
        return {**function, "description": built["description"], "parameters": built["parameters"]}

    return rewrite


# -- failure hints ---------------------------------------------------------------------------------

_SCAN_CHARS = 6000


def _missing_name(match: re.Match[str], tools: tuple[str, ...]) -> str:
    name = match.group(1)
    if name in HELPERS:
        return (
            f"`{name}` is a helper of {MODULE_NAME}; import it with `from {MODULE_NAME} import "
            f"{name}`. If that import itself failed, another module named {MODULE_NAME} may come "
            "first on sys.path; retry with reset=true."
        )
    available = ", ".join(tools) if tools else "none"
    return (
        f"`{name}` cannot be called from a script in this session. Tools importable from "
        f"{MODULE_NAME} here: {available}. Call any other tool directly, outside execute_code."
    )


def _helper_not_imported(match: re.Match[str], tools: tuple[str, ...]) -> str:
    return f"Import the helper before using it: `from {MODULE_NAME} import {match.group(1)}`."


def _not_installed(match: re.Match[str], tools: tuple[str, ...]) -> str:
    return (
        f"The module '{match.group(1)}' is not installed for the interpreter the script runs "
        "under. Use the standard library here, or run the code with the project's own Python "
        "through terminal()."
    )


def _result_is_dict(match: re.Match[str], tools: tuple[str, ...]) -> str:
    return (
        f"Functions from {MODULE_NAME} already return parsed dicts, not JSON text: index them "
        "directly (for example read_file(path)['content']) instead of calling json.loads or "
        "treating them as strings."
    )


type _Rule = tuple[re.Pattern[str], Callable[[re.Match[str], tuple[str, ...]], str]]

_RULES: tuple[_Rule, ...] = (
    (re.compile(rf"cannot import name '(\w+)' from '{MODULE_NAME}'"), _missing_name),
    (
        re.compile(r"NameError: name '(json_parse|shell_quote|retry)' is not defined"),
        _helper_not_imported,
    ),
    (re.compile(r"ModuleNotFoundError: No module named '([\w.]+)'"), _not_installed),
    (
        re.compile(
            r"TypeError: (?:string indices must be integers|the JSON object must be str, bytes "
            r"or bytearray, not dict)|AttributeError: 'str' object has no attribute 'get'"
        ),
        _result_is_dict,
    ),
)


def failure_hint(text: str, tools: Iterable[str] = ()) -> str | None:
    """One hint for a recognised failure in ``text`` (stderr or a traceback); first rule wins."""
    if not text:
        return None
    window = text[-_SCAN_CHARS:]
    available = tuple(offered(tools))
    for pattern, explain in _RULES:
        found = pattern.search(window)
        if found is not None:
            if pattern is _RULES[2][0] and found.group(1).split(".")[0] == MODULE_NAME:
                return (
                    f"{MODULE_NAME} could not be imported; retry with reset=true. If it keeps "
                    "failing, run the code without execute_code."
                )
            return explain(found, available)
    return None
