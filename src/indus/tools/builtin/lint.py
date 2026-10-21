# pyright: strict
"""Lint after edit (P05-20): report only the problems an edit introduced.

Two kinds of checker. In-process checkers read the text itself: ``.py`` (``compile``, nothing is
run), ``.json``, ``.yaml``/``.yml`` (event-level parse) and ``.toml``. Shell checkers run in the
task's environment on the file as written: ``node --check`` for JavaScript and ``tsc --noEmit``
for TypeScript, each only when the tool is on that environment's PATH.

``lint_edit`` compares the problems after the edit with the problems before it. Messages are
compared with numbers masked, because an edit above a problem moves its line; a problem whose
masked message was already there counts as pre-existing. A shell checker lints the old content
only when the new content fails, in a hidden temporary sibling (same directory, same extension)
that is removed straight after.

``gate`` is the fail-closed rule for the formats that are pure data: a write that would turn a
parsable JSON, YAML or TOML file (or a new one) into one that does not parse is refused before
anything is written. A file that was already broken may still be edited, so it can be repaired
in steps.

LSP tier (P24-16): when a language-server checker is installed for the process
(``indus.tools.lsp.diagnostics.install_active``) and the file lies on this host, the edit is also
checked by its language server. A verdict that comes within the write budget (0.08 s) joins the
report (``introduced``, linter ``…+lsp:<server>``); a later one reaches the session as a note on
its next tool result or at the end of the turn. Either way the file is recorded as edited this
turn for the stop gates.
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import re
import secrets
import sys
import tomllib
import warnings
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from indus.foundation import yamlio
from indus.tools.builtin.exec_host import CancelLike, ExecHost, quote_path

__all__ = [
    "FAIL_CLOSED_SUFFIXES",
    "IN_PROCESS",
    "SHELL_LINTERS",
    "LintReport",
    "LintStatus",
    "check_text",
    "gate",
    "lint_edit",
]

log = logging.getLogger(__name__)

type LintStatus = Literal["clean", "introduced", "preexisting", "skipped"]

SHELL_TIMEOUT_S = 30.0
MAX_REPORTED = 20
_MISSING = "__INDUS_LINT_TOOL_MISSING__"
_NO_TEMP = "__INDUS_LINT_NO_TEMP__"
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_NUMBER = re.compile(r"\d+")
_BOM = "\ufeff"
_IN_PROJECT = "__INDUS_LINT_TS_PROJECT__"


@dataclass(frozen=True, slots=True)
class LintReport:
    """The verdict on one edited file.

    ``clean``: no problems. ``introduced``: ``errors`` are new with this edit (``existing``
    counts the ones that were there before). ``preexisting``: problems remain, none of them new.
    ``skipped``: no checker could look at this file (``note`` says why).
    """

    status: LintStatus
    linter: str = ""
    errors: tuple[str, ...] = ()
    existing: int = 0
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status}
        if self.linter:
            out["linter"] = self.linter
        if self.errors:
            out["errors"] = list(self.errors)
        if self.existing:
            out["existing"] = self.existing
        if self.note:
            out["note"] = self.note
        return out


# -- in-process checkers ----------------------------------------------------------------------


def _python(path: str, text: str) -> list[str]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            compile(text, path, "exec", dont_inherit=True)
    except SyntaxError as exc:
        where = f"{exc.lineno}:{exc.offset}" if exc.lineno else "?"
        return [f"{path}:{where}: {type(exc).__name__}: {exc.msg}"]
    except (ValueError, RecursionError, MemoryError) as exc:
        return [f"{path}: {type(exc).__name__}: {exc}"]
    return []


def _json(path: str, text: str) -> list[str]:
    try:
        json.loads(text.removeprefix(_BOM))
    except json.JSONDecodeError as exc:
        return [f"{path}:{exc.lineno}:{exc.colno}: invalid JSON: {exc.msg}"]
    except RecursionError:
        return [f"{path}: invalid JSON: nested too deeply to read"]
    return []


def _yaml(path: str, text: str) -> list[str]:
    try:
        yamlio.check_syntax(text)
    except yamlio.YamlError as exc:
        where = f"{exc.line}:{exc.column}" if exc.line is not None else "?"
        return [f"{path}:{where}: invalid YAML: {exc.reason}"]
    return []


def _toml(path: str, text: str) -> list[str]:
    try:
        tomllib.loads(text.removeprefix(_BOM))
    except tomllib.TOMLDecodeError as exc:
        return [f"{path}: invalid TOML: {exc}"]
    return []


type Checker = Callable[[str, str], list[str]]

IN_PROCESS: dict[str, tuple[str, Checker]] = {
    ".py": ("python", _python),
    ".pyw": ("python", _python),
    ".json": ("json", _json),
    ".yaml": ("yaml", _yaml),
    ".yml": ("yaml", _yaml),
    ".toml": ("toml", _toml),
}
FAIL_CLOSED_SUFFIXES = frozenset({".json", ".yaml", ".yml", ".toml"})

# suffix -> (tool that must be on PATH, command the quoted path is appended to)
SHELL_LINTERS: dict[str, tuple[str, str]] = {
    ".js": ("node", "node --check"),
    ".mjs": ("node", "node --check"),
    ".cjs": ("node", "node --check"),
    ".ts": ("tsc", "tsc --noEmit --pretty false"),
    ".mts": ("tsc", "tsc --noEmit --pretty false"),
    ".cts": ("tsc", "tsc --noEmit --pretty false"),
}


def _suffix(path: str) -> str:
    base = posixpath.basename(path.replace("\\", "/"))
    dot = base.rfind(".")
    return base[dot:].lower() if dot > 0 else ""


def check_text(path: str, text: str) -> list[str] | None:
    """Problems an in-process checker finds in ``text``; ``None`` when none applies to ``path``."""
    found = IN_PROCESS.get(_suffix(path))
    return None if found is None else found[1](path, text)


def gate(path: str, before: str | None, after: str) -> str | None:
    """Refusal text when writing ``after`` would break a parsable data file, else ``None``."""
    suffix = _suffix(path)
    if suffix not in FAIL_CLOSED_SUFFIXES:
        return None
    problems = check_text(path, after) or []
    if not problems:
        return None
    if before is not None and check_text(path, before):
        return None  # already broken: editing it is how it gets repaired
    kind = IN_PROCESS[suffix][0].upper()
    return (
        f"Refused: the new content of {path} is not valid {kind} ({problems[0]}), so nothing "
        "was written. Fix the content and send the edit again."
    )


# -- comparing before and after ---------------------------------------------------------------


def _key(message: str, *paths: str) -> str:
    for path in paths:
        if path:
            message = message.replace(path, "")
    return _NUMBER.sub("#", message).strip()


def _delta(linter: str, path: str, after: list[str], before: list[str] | None) -> LintReport:
    if not after:
        return LintReport("clean", linter)
    if before is None:
        return LintReport("introduced", linter, tuple(after[:MAX_REPORTED]))
    old = Counter(_key(m, path) for m in before)
    new: list[str] = []
    existing = 0
    for message in after:
        key = _key(message, path)
        if old[key] > 0:
            old[key] -= 1
            existing += 1
        else:
            new.append(message)
    if not new:
        return LintReport(
            "preexisting",
            linter,
            existing=existing,
            note="The file still has problems, but this edit did not add any.",
        )
    return LintReport("introduced", linter, tuple(new[:MAX_REPORTED]), existing=existing)


# -- shell checkers ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ShellRun:
    state: Literal["ok", "problems", "missing", "unusable"]
    lines: tuple[str, ...] = ()
    reason: str = ""


def _lines(text: str) -> tuple[str, ...]:
    clean = _ANSI.sub("", text)
    return tuple(line.rstrip() for line in clean.splitlines() if line.strip())


async def _shell(
    host: ExecHost,
    tool: str,
    script: str,
    *,
    stdin: str | None,
    timeout: float,
    cancel: CancelLike | None,
) -> _ShellRun:
    command = f"command -v {tool} >/dev/null 2>&1 || {{ printf '%s\\n' {_MISSING}; exit 127; }}\n"
    command += script
    try:
        result = await host.execute(
            command, timeout=timeout, stdin=stdin, bounded=False, cancel=cancel
        )
    except Exception as exc:  # a checker must never break the edit that already happened
        log.warning("lint with %s could not run: %s: %s", tool, type(exc).__name__, exc)
        return _ShellRun("unusable", reason=f"{tool} could not run ({type(exc).__name__})")
    if result.returncode == 127 and _MISSING in result.output:
        return _ShellRun("missing", reason=f"{tool} is not installed in this environment")
    if result.timed_out:
        return _ShellRun("unusable", reason=f"{tool} did not finish within {timeout:g}s")
    if result.interrupted:
        return _ShellRun("unusable", reason=f"{tool} was interrupted")
    if _NO_TEMP in result.output:
        return _ShellRun("unusable", reason="could not stage the old content for comparison")
    if result.returncode == 0:
        return _ShellRun("ok")
    return _ShellRun("problems", _lines(result.output))


def _project_check(path: str) -> str:
    """Shell lines that stop with a marker when a tsconfig.json sits beside or above ``path``."""
    folder = posixpath.dirname(path) or "."
    return (
        f"d=$(cd {quote_path(folder)} 2>/dev/null && pwd -P) && while :; do\n"
        f"  [ -f \"$d/tsconfig.json\" ] && {{ printf '%s\\n' {_IN_PROJECT}; exit 3; }}\n"
        '  [ "$d" = / ] && break; d=$(dirname -- "$d")\n'
        "done\n"
    )


async def _lint_shell(
    path: str,
    before: str | None,
    host: ExecHost,
    *,
    timeout: float,
    cancel: CancelLike | None,
) -> LintReport:
    tool, command = SHELL_LINTERS[_suffix(path)]
    script = f"{command} {quote_path(path)} 2>&1"
    if tool == "tsc":
        script = _project_check(path) + script
    after = await _shell(host, tool, script, stdin=None, timeout=timeout, cancel=cancel)
    if after.state == "problems" and _IN_PROJECT in after.lines:
        return LintReport(
            "skipped",
            tool,
            note="the file belongs to a project with tsconfig.json; tsc on one file would "
            "report errors the project does not have",
        )
    if after.state == "ok":
        return LintReport("clean", tool)
    if after.state != "problems":
        return LintReport("skipped", tool, note=after.reason)
    if before is None:
        return _delta(tool, path, list(after.lines), None)
    folder, base = posixpath.split(path)
    temp_base = f".indus-lint-{secrets.token_hex(4)}-{base}"
    temp = posixpath.join(folder, temp_base) if folder else temp_base
    quoted = quote_path(temp)
    script = (
        f"cat > {quoted} || {{ rm -f {quoted}; printf '%s\\n' {_NO_TEMP}; exit 126; }}\n"
        f"{command} {quoted} 2>&1; rc=$?; rm -f {quoted}; exit $rc"
    )
    old = await _shell(host, tool, script, stdin=before, timeout=timeout, cancel=cancel)
    if old.state == "ok":
        return _delta(tool, path, list(after.lines), [])
    if old.state != "problems":
        report = _delta(tool, path, list(after.lines), None)
        return LintReport(
            report.status,
            tool,
            report.errors,
            note=f"Could not lint the old content ({old.reason}); every problem is listed.",
        )
    renamed = [line.replace(temp_base, base) for line in old.lines]
    return _delta(tool, path, list(after.lines), renamed)


async def _lint_base(
    path: str,
    before: str | None,
    after: str,
    *,
    host: ExecHost | None = None,
    timeout: float = SHELL_TIMEOUT_S,
    cancel: CancelLike | None = None,
) -> LintReport:
    """Lint ``path`` after it was written with ``after``; ``before`` is the old text (``None``
    for a new file). Shell checkers need ``host`` and read the file as it is on disk now."""
    suffix = _suffix(path)
    found = IN_PROCESS.get(suffix)
    if found is not None:
        linter, checker = found
        problems = checker(path, after)
        if not problems:
            return LintReport("clean", linter)
        return _delta(linter, path, problems, None if before is None else checker(path, before))
    if suffix in SHELL_LINTERS:
        if host is None:
            return LintReport(
                "skipped", SHELL_LINTERS[suffix][0], note="no environment to run it in"
            )
        return await _lint_shell(path, before, host, timeout=timeout, cancel=cancel)
    return LintReport("skipped", note=f"no linter for {suffix or 'files without a suffix'}")


async def lint_edit(
    path: str,
    before: str | None,
    after: str,
    *,
    host: ExecHost | None = None,
    timeout: float = SHELL_TIMEOUT_S,
    cancel: CancelLike | None = None,
) -> LintReport:
    """Lint ``path`` after it was written with ``after``; ``before`` is the old text (``None``
    for a new file). Shell checkers need ``host`` and read the file as it is on disk now. The
    LSP tier joins in when a checker is installed (module notes)."""
    started = _lsp_clock_start()
    report = await _lint_base(path, before, after, host=host, timeout=timeout, cancel=cancel)
    try:
        return await _with_lsp(path, before, after, host, report, started)
    except Exception as exc:  # the language server must never break an edit that happened
        log.debug("LSP check of %s failed: %s: %s", path, type(exc).__name__, exc)
        return report


_LSP_MODULE = "indus.tools.lsp.diagnostics"


def _lsp_clock_start() -> float | None:
    if _LSP_MODULE not in sys.modules:  # never imported: no checker can be installed
        return None
    from indus.tools.lsp import diagnostics

    checker = diagnostics.active()
    return checker.clock.monotonic() if checker is not None else None


def _host_path(path: str, host: ExecHost | None) -> str | None:
    """``path`` as a path on this machine, or None when the edit happened elsewhere."""
    flags = getattr(host, "flags", None) if host is not None else None
    if flags is not None and (
        getattr(flags, "container_paths", False) or not getattr(flags, "host_local", True)
    ):
        return None
    if os.path.isabs(path):
        return path
    base = getattr(host, "cwd", None) if host is not None else None
    return os.path.join(base, path) if isinstance(base, str) and os.path.isabs(base) else None


async def _with_lsp(
    path: str,
    before: str | None,
    after: str,
    host: ExecHost | None,
    report: LintReport,
    started: float | None,
) -> LintReport:
    if _LSP_MODULE not in sys.modules:
        return report
    from indus.tools.lsp import diagnostics

    local = _host_path(path, host)
    if local is None:
        return report
    diagnostics.record_edit(local)
    checker = diagnostics.active()
    if checker is None:
        return report
    owner, _turn = diagnostics.current_owner()
    outcome = await checker.check_edit(local, before, after, owner=owner, started=started)
    found = outcome.text(path)
    if found is None:
        return report
    lines = tuple(found.splitlines())
    linter = f"{report.linter}+lsp:{outcome.server}" if report.linter else f"lsp:{outcome.server}"
    if report.status == "introduced":
        return LintReport("introduced", linter, report.errors + lines, report.existing, report.note)
    return LintReport("introduced", linter, lines, report.existing)
