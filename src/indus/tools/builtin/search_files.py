# pyright: strict
"""``search_files``: find text in files, or files by name, with ripgrep (P05-21).

The search runs in the task's terminal environment (a sandboxed task searches its sandbox), as
one shell script per call that checks ripgrep and the path, runs ``rg`` with its standard error
kept apart, and stops reading at the page bound with ``head``. The script asks for the whole
output (``bounded=False``, R02.2); the page bound is the only cut. ripgrep is required: when it
is missing the model is told so and pointed at ``indus doctor``.

Content search (``target: content``) parses ``rg --json`` lines (``match`` and ``context``
messages), so file names with colons or dashes never confuse the parser. ``output_mode``
``files_only`` uses ``--files-with-matches`` and ``count`` uses ``--count``. A regex that wants a
newline (a real newline, or ``\\n`` after an odd number of backslashes) turns on ripgrep's
multiline mode, with a note.

File-name search (``target: files``) lists files whose name matches a glob (a bare word is
searched as ``*word*``, case-insensitively): ``order: discovery`` takes them as ripgrep finds
them and stops at the page end; ``order: modified`` sorts the whole tree newest first
(ripgrep 14 or newer). Two file-name searches of one root in one backend never run together: the
second waits in 50 ms steps and gives up when the turn is stopped.

Results drop hits inside credential and env files (``.env``, ``.envrc``, ``.netrc``, SSH keys,
cloud credentials, the Indus home's secret files) and say how many were dropped. A broad search
from a macOS home (on the host itself) skips the privacy-protected folders so no permission
prompt appears unattended; searching inside one of them directly still works. A search that runs
out of time returns what it found with ``limit_reason: "search_timeout"``.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import posixpath
import re
import shlex
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast

from indus.foundation.clock import Clock, SystemClock
from indus.foundation.errors import IndusError
from indus.foundation.scope import Scope
from indus.platform.facts import os_family
from indus.safety.redaction import CREDENTIAL_FILES, INDUS_SECRET_FILES
from indus.tools.builtin.exec_host import CancelLike, ExecHost, ExecOutput, quote_path
from indus.tools.registry import ToolRegistry, ToolTraits

__all__ = [
    "SEARCH_SCHEMA",
    "TOOL_NAME",
    "Admission",
    "SearchFilesTool",
    "SearchRequest",
    "is_sensitive_path",
    "macos_exclusions",
    "read_request",
    "register_tools",
]

log = logging.getLogger(__name__)

TOOL_NAME = "search_files"
TOOLSET = "file"
DEFAULT_LIMIT = 50
MAX_LIMIT = 1_000
MAX_CONTEXT = 20
TIMEOUT_S = 60.0
MAX_TEXT_CHARS = 500
BYTE_CAP = 8_000_000
ADMISSION_SLICE_S = 0.05
MIN_SORT_MAJOR = 14
MAX_RESULT_CHARS = 100_000
RG = "rg"  # looked up on the environment's PATH
FILTER_SLACK = 16  # extra results read so a dropped credential file does not shorten the page

_NO_RG = "__INDUS_SEARCH_NO_RG__"
_NO_PATH = "__INDUS_SEARCH_NO_PATH__"
_OLD_RG = "__INDUS_SEARCH_OLD_RG__"
_ERR = "__INDUS_SEARCH_ERR__"
_SIGPIPE_RC = 141
_GLOB_CHARS = re.compile(r"[*?\[{]")
_NEWLINE_ESCAPE = re.compile(r"(?<!\\)(?:\\\\)*\\n")
# Home folders macOS guards with a privacy prompt (Apple's "Files and Folders" consent list).
PROTECTED_MACOS_DIRS = (
    "Desktop",
    "Documents",
    "Downloads",
    "Pictures",
    "Movies",
    "Music",
    "Library",
)
_TEMPLATE_ENDINGS = (".example", ".sample", ".template", ".dist", ".defaults")

type Target = Literal["content", "files"]
type OutputMode = Literal["content", "files_only", "count"]
type Order = Literal["discovery", "modified"]

SEARCH_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Search the working tree. target 'content' (default) finds lines matching a regular "
        "expression (ripgrep syntax) and returns them with file and line number, or only the "
        "file names (output_mode 'files_only'), or a count per file ('count'). target 'files' "
        "finds files whose name matches a glob such as '*.py' (a plain word matches names "
        "containing it). Hidden and git-ignored files are skipped. Results come in pages: "
        "pass the returned next_offset as offset for more. Prefer this over running grep, rg "
        "or find in the terminal."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regex for content search; glob or word for file-name search.",
            },
            "target": {
                "type": "string",
                "enum": ["content", "files"],
                "description": "'content' searches inside files, 'files' searches file names.",
            },
            "path": {
                "type": "string",
                "description": "File or directory to search (default: the working directory).",
            },
            "file_glob": {
                "type": "string",
                "description": "Content search only: limit to files matching this glob.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_LIMIT,
                "description": f"Results per page (default {DEFAULT_LIMIT}).",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": "Results to skip, for the next page (default 0).",
            },
            "order": {
                "type": "string",
                "enum": ["discovery", "modified"],
                "description": (
                    "File-name search: 'discovery' (fast, as found) or 'modified' (newest "
                    "first; reads the whole tree)."
                ),
            },
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_only", "count"],
                "description": "Content search: matching lines, file names only, or counts.",
            },
            "context": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_CONTEXT,
                "description": "Content search: lines of context around each match (default 0).",
            },
        },
        "required": ["pattern"],
    },
}


# -- request ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SearchRequest:
    pattern: str
    target: Target = "content"
    path: str = "."
    file_glob: str | None = None
    limit: int = DEFAULT_LIMIT
    offset: int = 0
    order: Order = "discovery"
    output_mode: OutputMode = "content"
    context: int = 0


_TARGETS: dict[str, Target] = {
    "content": "content",
    "files": "files",
    "grep": "content",  # older name
    "find": "files",  # older name
}
_MODES: dict[str, OutputMode] = {"content": "content", "files_only": "files_only", "count": "count"}
_ORDERS: dict[str, Order] = {"discovery": "discovery", "modified": "modified"}


def _int(value: object, default: int, low: int, high: int | None) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(str(value).strip())
    except ValueError:
        return default
    number = max(low, number)
    return min(high, number) if high is not None else number


def _choice[T](table: Mapping[str, T], value: object, default: T, name: str) -> T:
    if value is None or value == "":
        return default
    found = table.get(str(value).strip().lower())
    if found is None:
        raise ValueError(f"{name} must be one of {', '.join(sorted(set(table)))}; got {value!r}")
    return found


def read_request(args: Mapping[str, Any]) -> SearchRequest:
    """The call's arguments, defaulted and clamped; ``ValueError`` explains a bad one."""
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("pattern is required: a regex for content search or a file-name glob")
    raw_path = args.get("path")
    path = raw_path.strip() if isinstance(raw_path, str) and raw_path.strip() else "."
    glob = args.get("file_glob")
    return SearchRequest(
        pattern=pattern,
        target=_choice(_TARGETS, args.get("target"), "content", "target"),
        path=path,
        file_glob=glob.strip() if isinstance(glob, str) and glob.strip() else None,
        limit=_int(args.get("limit"), DEFAULT_LIMIT, 1, MAX_LIMIT),
        offset=_int(args.get("offset"), 0, 0, None),
        order=_choice(_ORDERS, args.get("order"), "discovery", "order"),
        output_mode=_choice(_MODES, args.get("output_mode"), "content", "output_mode"),
        context=_int(args.get("context"), 0, 0, MAX_CONTEXT),
    )


# -- filters ----------------------------------------------------------------------------------


def is_sensitive_path(path: str, *, secret_home: str | None = None) -> bool:
    """True for credential and env files whose lines must not reach the model."""
    norm = path.replace("\\", "/")
    parts = [part for part in norm.split("/") if part and part != "."]
    if not parts:
        return False
    base = parts[-1].lower()
    parent = parts[-2].lower() if len(parts) > 1 else ""
    if base in (".env", ".envrc"):
        return True
    if base.startswith(".env."):
        return not base.endswith(_TEMPLATE_ENDINGS)
    if base.endswith(".env") or base in CREDENTIAL_FILES:
        return True
    if parent == ".ssh" and base.startswith("id_") and not base.endswith(".pub"):
        return True
    if parent in (".aws", ".docker", ".kube") and base in ("credentials", "config.json", "config"):
        return True
    if secret_home and base in INDUS_SECRET_FILES:
        home = secret_home.replace("\\", "/").rstrip("/")
        return posixpath.dirname(posixpath.normpath(norm)) == home
    return False


def macos_exclusions(root: str, home: str | None) -> list[str]:
    """Protected home folders below ``root``, relative to it (none unless ``root`` is the home
    or one of its ancestors: a search inside a protected folder is the user's choice)."""
    if not home:
        return []
    root_n = posixpath.normpath(root)
    home_n = posixpath.normpath(home)
    out: list[str] = []
    for name in PROTECTED_MACOS_DIRS:
        target = posixpath.join(home_n, name)
        rel = posixpath.relpath(target, root_n)
        if rel != "." and not rel.startswith(".."):
            out.append(rel)
    return out


def _wants_newline(pattern: str) -> bool:
    return "\n" in pattern or bool(_NEWLINE_ESCAPE.search(pattern))


# -- admission --------------------------------------------------------------------------------


class Admission:
    """One file-name search per (backend, root) at a time; others wait in short slices."""

    def __init__(self) -> None:
        self._busy: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    def _claim(self, key: tuple[str, str]) -> bool:
        with self._lock:
            if key in self._busy:
                return False
            self._busy.add(key)
            return True

    async def enter(self, key: tuple[str, str], *, clock: Clock, cancel: CancelLike | None) -> bool:
        """Claim ``key``; ``False`` when the turn was stopped while waiting."""
        while True:
            if cancel is not None and cancel.is_set():
                return False
            if self._claim(key):
                return True
            await clock.sleep(ADMISSION_SLICE_S)

    def leave(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._busy.discard(key)

    def busy(self, key: tuple[str, str]) -> bool:
        with self._lock:
            return key in self._busy


# -- running ----------------------------------------------------------------------------------


@dataclass
class _Run:
    lines: list[str] = field(default_factory=lambda: [])
    errors: list[str] = field(default_factory=lambda: [])
    returncode: int = 0
    timed_out: bool = False
    interrupted: bool = False
    cut: bool = False
    failure: str = ""


def _script(rg_args: Sequence[str], path: str, fetch: int, *, need_sort: bool) -> str:
    rg = " ".join(shlex.quote(a) for a in rg_args)
    qpath = quote_path(path)
    steps = [
        f"command -v {RG} >/dev/null 2>&1 || {{ printf '%s\\n' {_NO_RG}; exit 127; }}",
        f"[ -e {qpath} ] || {{ printf '%s\\n' {_NO_PATH}; exit 2; }}",
    ]
    if need_sort:
        steps += [
            f"ver=$({RG} --version 2>/dev/null | head -n 1)",
            "ver=${ver#ripgrep }; major=${ver%%.*}",
            'case "$major" in ""|*[!0-9]*) ;; *) [ "$major" -ge '
            f"{MIN_SORT_MAJOR} ] || {{ printf '%s%s\\n' {_OLD_RG} \"$ver\"; exit 3; }} ;; esac",
        ]
    steps += [
        "set -o pipefail",
        'errf=$(mktemp 2>/dev/null || printf "%s" "${TMPDIR:-/tmp}/indus-rg-$$.err")',
        f'{rg} -- {qpath} 2>"$errf" | head -n {fetch} | head -c {BYTE_CAP}',
        "rc=$?",
        f'if [ -s "$errf" ]; then head -n 20 "$errf" | sed "s/^/{_ERR}/"; fi',
        'rm -f "$errf"',
        "exit $rc",
    ]
    return "\n".join(steps)


async def _run(host: ExecHost, script: str, fetch: int, cancel: CancelLike | None) -> _Run:
    result: ExecOutput = await host.execute(script, timeout=TIMEOUT_S, bounded=False, cancel=cancel)
    run = _Run(
        returncode=result.returncode,
        timed_out=result.timed_out,
        interrupted=result.interrupted,
    )
    output = result.output
    if result.returncode == 127 and _NO_RG in output:
        run.failure = (
            "search_files needs ripgrep (rg), which is not installed in this environment. "
            "Install ripgrep 14 or newer (`indus doctor` shows how); until then use the "
            "terminal."
        )
        return run
    if _NO_PATH in output:
        run.failure = "missing_path"
        return run
    if _OLD_RG in output:
        version = output.split(_OLD_RG, 1)[1].splitlines()[0].strip()
        run.failure = (
            f"order 'modified' needs ripgrep {MIN_SORT_MAJOR} or newer (found {version or '?'}); "
            "upgrade ripgrep or use order 'discovery'."
        )
        return run
    for line in output.split("\n"):
        line = line.removesuffix("\r")
        if line.startswith(_ERR):
            run.errors.append(line[len(_ERR) :])
        elif line.startswith("[Command timed out after") or line == "[Command interrupted]":
            continue
        elif line:
            run.lines.append(line)
    run.cut = (
        result.returncode == _SIGPIPE_RC
        or len(run.lines) >= fetch
        or len(output.encode("utf-8", "replace")) >= BYTE_CAP
    )
    return run


def _decode(value: object) -> str:
    if not isinstance(value, Mapping):
        return ""
    data = cast(Mapping[str, Any], value)
    text = data.get("text")
    if isinstance(text, str):
        return text
    raw = data.get("bytes")
    if isinstance(raw, str):
        try:
            return base64.b64decode(raw).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            return ""
    return ""


@dataclass(frozen=True, slots=True)
class _Line:
    kind: Literal["match", "context"]
    path: str
    line: int
    text: str


def _parse_json(lines: Sequence[str]) -> list[_Line]:
    out: list[_Line] = []
    for raw in lines:
        if not raw.startswith("{"):
            continue
        try:
            parsed: object = json.loads(raw)
        except ValueError:
            continue  # a line cut by the byte cap
        if not isinstance(parsed, dict):
            continue
        message = cast(dict[str, Any], parsed)
        kind = message.get("type")
        data: object = message.get("data")
        if kind not in ("match", "context") or not isinstance(data, dict):
            continue
        fields = cast(dict[str, Any], data)
        number = fields.get("line_number")
        text = _decode(fields.get("lines")).rstrip("\n").removesuffix("\r")
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS] + " [line cut]"
        out.append(
            _Line(
                "match" if kind == "match" else "context",
                _decode(fields.get("path")),
                number if isinstance(number, int) else 0,
                text,
            )
        )
    return out


# -- the tool ---------------------------------------------------------------------------------


class EnvironmentSource(Protocol):
    """Where the task's environment comes from (``EnvironmentRegistry`` fits)."""

    async def acquire(
        self,
        scope: Scope | None = None,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> tuple[Any, Any]: ...


type HostResolver = Callable[
    [Scope | None, str | None, str | None, EnvironmentSource | None], Awaitable[ExecHost]
]


class _NoHost(Exception):
    pass


async def default_host(
    scope: Scope | None,
    task_id: str | None,
    session_id: str | None,
    environments: EnvironmentSource | None,
) -> ExecHost:
    """The task's terminal environment from the registry the tool context offers."""
    if environments is None:
        raise _NoHost("no terminal environment is available to search in from here")
    _key, env = await environments.acquire(scope, task_id=task_id, session_id=session_id)
    return env


def _json(body: Mapping[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=False)


def _error(text: str) -> str:
    return _json({"error": text})


class SearchFilesTool:
    """The tool with its shared state: admission table, clock and how hosts are found."""

    def __init__(
        self,
        *,
        resolve_host: HostResolver = default_host,
        clock: Clock | None = None,
        admission: Admission | None = None,
    ) -> None:
        self.resolve_host = resolve_host
        self.clock = clock if clock is not None else SystemClock()
        self.admission = admission if admission is not None else Admission()

    async def handle(
        self,
        args: Mapping[str, Any],
        *,
        scope: Scope | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        environments: EnvironmentSource | None = None,
        cancel: CancelLike | None = None,
    ) -> str:
        try:
            request = read_request(args)
        except ValueError as exc:
            return _error(str(exc))
        try:
            host = await self.resolve_host(scope, task_id, session_id, environments)
        except (_NoHost, IndusError) as exc:
            return _error(f"search_files cannot run: {exc}")
        return await self.search(request, host, scope=scope, cancel=cancel)

    async def search(
        self,
        request: SearchRequest,
        host: ExecHost,
        *,
        scope: Scope | None = None,
        cancel: CancelLike | None = None,
    ) -> str:
        notes: list[str] = []
        home = scope.env.get("HOME") if scope is not None else None
        root = _absolute(request.path, host.cwd, home)
        excluded = self._macos_excluded(host, root, home)
        if excluded:
            notes.append(
                "Skipped macOS privacy-protected folders on this broad search ("
                + ", ".join(posixpath.basename(e) for e in excluded)
                + "); search one of them directly when you mean to."
            )
        secret_home = str(scope.home) if scope is not None else None
        if request.target == "files":
            body = await self._files(request, host, root, excluded, cancel, secret_home)
        else:
            body = await self._content(request, host, root, excluded, cancel, secret_home)
        if "error" in body:
            return _json(body)
        notes.extend(body.pop("notes", []))
        if notes:
            body["notes"] = notes
        return _json(body)

    @staticmethod
    def _macos_excluded(host: ExecHost, root: str, home: str | None) -> list[str]:
        flags = getattr(host, "flags", None)
        if not getattr(flags, "host_local", False) or os_family() != "darwin":
            return []
        return macos_exclusions(root, home)

    async def _content(
        self,
        request: SearchRequest,
        host: ExecHost,
        root: str,
        excluded: Sequence[str],
        cancel: CancelLike | None,
        secret_home: str | None,
    ) -> dict[str, Any]:
        mode = request.output_mode
        args = [RG, "--no-config"]
        notes: list[str] = []
        if mode == "files_only":
            args.append("--files-with-matches")
            fetch = request.offset + request.limit + 1 + FILTER_SLACK
        elif mode == "count":
            args += ["--count", "--with-filename"]
            fetch = request.offset + request.limit + 1 + FILTER_SLACK
        else:
            args.append("--json")
            if request.context:
                args += ["--context", str(request.context)]
            per_match = 2 * request.context + 3
            fetch = (request.offset + request.limit + 1 + FILTER_SLACK) * per_match + 2
        if _wants_newline(request.pattern):
            args.append("--multiline")
            notes.append(
                "The pattern contains a newline, so ripgrep's multiline mode was turned on."
            )
        if request.file_glob:
            args += ["--glob", request.file_glob]
        for rel in excluded:
            args += ["--glob", f"!{rel}/**"]
        args += ["-e", request.pattern]
        run = await _run(host, _script(args, request.path, fetch, need_sort=False), fetch, cancel)
        problem = _problem(run, request.path)
        if problem is not None:
            return problem
        notes += _run_notes(run)
        body: dict[str, Any] = {"target": "content", "output_mode": mode}
        if mode == "content":
            page = _content_page(run, request, host.cwd, secret_home)
        elif mode == "files_only":
            page = _files_page(run, request, host.cwd, secret_home, key="files")
        else:
            page = _count_page(run, request, host.cwd, secret_home)
        notes += page.pop("notes", [])
        body.update(page)
        body["notes"] = notes
        return body

    async def _files(
        self,
        request: SearchRequest,
        host: ExecHost,
        root: str,
        excluded: Sequence[str],
        cancel: CancelLike | None,
        secret_home: str | None,
    ) -> dict[str, Any]:
        glob = request.pattern
        if not _GLOB_CHARS.search(glob):
            glob = f"*{glob}*"
        args = [RG, "--no-config", "--files", "--iglob", glob]
        if request.order == "modified":
            args.append("--sortr=modified")
        for rel in excluded:
            args += ["--glob", f"!{rel}/**"]
        fetch = request.offset + request.limit + 1 + FILTER_SLACK
        key = (str(getattr(host, "name", type(host).__name__)), root)
        if not await self.admission.enter(key, clock=self.clock, cancel=cancel):
            return {
                "error": "The search was stopped while waiting for another file-name search of "
                "the same folder to finish."
            }
        try:
            script = _script(args, request.path, fetch, need_sort=request.order == "modified")
            run = await _run(host, script, fetch, cancel)
        finally:
            self.admission.leave(key)
        problem = _problem(run, request.path)
        if problem is not None:
            return problem
        body: dict[str, Any] = {"target": "files", "order": request.order}
        page = _files_page(run, request, host.cwd, secret_home, key="files")
        notes = [*_run_notes(run), *page.pop("notes", [])]
        body.update(page)
        body["notes"] = notes
        return body

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            TOOL_NAME,
            TOOLSET,
            SEARCH_SCHEMA,
            self.handle,
            max_result_chars=MAX_RESULT_CHARS,
            traits=ToolTraits(read_only=True),
        )


def _absolute(path: str, cwd: str, home: str | None) -> str:
    if path == "~" or path.startswith("~/"):
        if not home:
            return path
        path = home + path[1:]
    if not posixpath.isabs(path):
        path = posixpath.join(cwd or "/", path)
    return posixpath.normpath(path)


def _problem(run: _Run, path: str) -> dict[str, Any] | None:
    if run.failure == "missing_path":
        return {"error": f"Path not found: {path}"}
    if run.failure:
        return {"error": run.failure}
    if run.interrupted:
        return {"error": "The search was interrupted."}
    if run.timed_out:
        return None
    if run.returncode in (0, 1, _SIGPIPE_RC):
        return None
    if run.returncode == 2 and run.lines:
        return None
    detail = "; ".join(e.strip() for e in run.errors[:6] if e.strip())
    return {"error": f"ripgrep failed (exit {run.returncode}): {detail or 'no message'}"}


def _run_notes(run: _Run) -> list[str]:
    notes: list[str] = []
    if run.returncode == 2 and run.lines and run.errors:
        notes.append("Some files could not be searched: " + run.errors[0].strip())
    return notes


def _flags(
    body: dict[str, Any], run: _Run, total: int, offset: int, shown: int, dropped: int
) -> None:
    body["total_count"] = total
    body["shown"] = shown
    # Results were dropped from a read that hit the page bound: more may follow.
    more = total > offset + shown or (run.cut and dropped > 0 and shown > 0)
    body["truncated"] = more or run.cut or run.timed_out
    if more:
        body["next_offset"] = offset + shown
    if run.timed_out:
        body["limit_reason"] = "search_timeout"
    elif run.cut and not more:
        body["limit_reason"] = "page_bound"


def _shown(path: str) -> str:
    """ripgrep starts every path with the operand; ``./`` from the default ``.`` is noise."""
    return path[2:] if path.startswith("./") and len(path) > 2 else path


def _sensitive(path: str, cwd: str, secret_home: str | None) -> bool:
    # ripgrep prints paths as reached from the operand, so relative ones start at the cwd.
    full = path if posixpath.isabs(path) else posixpath.join(cwd, path)
    return is_sensitive_path(full, secret_home=secret_home)


def _content_page(
    run: _Run, request: SearchRequest, cwd: str, secret_home: str | None
) -> dict[str, Any]:
    lines = _parse_json(run.lines)
    filtered = sum(1 for e in lines if e.kind == "match" and _sensitive(e.path, cwd, secret_home))
    lines = [e for e in lines if not _sensitive(e.path, cwd, secret_home)]
    matches = [e for e in lines if e.kind == "match"]
    page = matches[request.offset : request.offset + request.limit]
    chosen = {id(e) for e in page}
    near: dict[str, list[int]] = {}
    for e in page:
        near.setdefault(e.path, []).append(e.line)
    out: list[dict[str, Any]] = []
    for e in lines:
        if e.kind == "match":
            if id(e) in chosen:
                out.append({"path": _shown(e.path), "line": e.line, "text": e.text})
        elif any(abs(e.line - n) <= request.context for n in near.get(e.path, ())):
            out.append({"path": _shown(e.path), "line": e.line, "text": e.text, "context": True})
    body: dict[str, Any] = {"matches": out}
    _flags(body, run, len(matches), request.offset, len(page), filtered)
    if filtered:
        body["filtered"] = filtered
        body["notes"] = [f"{filtered} match(es) in credential or env files were left out."]
    return body


def _files_page(
    run: _Run, request: SearchRequest, cwd: str, secret_home: str | None, *, key: str
) -> dict[str, Any]:
    names = list(run.lines)
    kept = [_shown(n) for n in names if not _sensitive(n, cwd, secret_home)]
    page = kept[request.offset : request.offset + request.limit]
    body: dict[str, Any] = {key: page}
    _flags(body, run, len(kept), request.offset, len(page), len(names) - len(kept))
    if len(kept) != len(names):
        dropped = len(names) - len(kept)
        body["filtered"] = dropped
        body["notes"] = [f"{dropped} credential or env file(s) were left out."]
    return body


def _count_page(
    run: _Run, request: SearchRequest, cwd: str, secret_home: str | None
) -> dict[str, Any]:
    counts: list[dict[str, Any]] = []
    dropped = 0
    for line in run.lines:
        path, sep, number = line.rpartition(":")
        if not sep or not number.isdigit():
            continue
        if _sensitive(path, cwd, secret_home):
            dropped += 1
            continue
        counts.append({"path": _shown(path), "count": int(number)})
    page = counts[request.offset : request.offset + request.limit]
    body: dict[str, Any] = {"counts": page, "total_matching_lines": sum(c["count"] for c in counts)}
    _flags(body, run, len(counts), request.offset, len(page), dropped)
    if dropped:
        body["filtered"] = dropped
        body["notes"] = [f"{dropped} credential or env file(s) were left out."]
    return body


DEFAULT_TOOL = SearchFilesTool()


def register_tools(registry: ToolRegistry) -> None:
    DEFAULT_TOOL.register(registry)
