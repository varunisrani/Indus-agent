# pyright: strict
"""Bash scripts around every command, and reading their cwd marker back (P05-01).

Pure text builders, no processes. A shell backend runs one ``bash`` per command; the shell state
the user expects to persist (exported variables, functions, aliases) lives in a snapshot file
that each command sources first and rewrites last, and the directory a command ended in comes
back as a marker line on stdout.

Per-session variables (the session id, ``INDUS_HOME``, the agent markers) are removed with
``unset`` in a subshell *before* ``export -p`` writes the snapshot. Filtering the ``declare -x``
lines afterwards is not safe: a value with an embedded newline prints over several lines, and the
extra lines would run as commands at the next ``source`` (R02.1). They are injected fresh for
every command by the child environment builder, so the snapshot never needs them.
"""

from __future__ import annotations

import secrets
import shlex
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = [
    "SESSION_VAR_NAMES",
    "SESSION_VAR_PREFIXES",
    "ScriptPaths",
    "bootstrap_script",
    "cwd_marker",
    "embed_stdin_heredoc",
    "quote_cd_target",
    "rewrite_compound_background",
    "split_cwd_marker",
    "wrap_command",
]

# Names that belong to one session or one call and must never be frozen into a snapshot.
SESSION_VAR_PREFIXES: tuple[str, ...] = (
    "INDUS_SESSION_",
    "INDUS_UI_SESSION_",
    "INDUS_CRON_",
    "INDUS_RPC_",
    "INDUS_DELEGATE_",
)
SESSION_VAR_NAMES: tuple[str, ...] = ("INDUS_HOME", "AI_AGENT", "INDUS_AGENT")

_TMP_VAR = '"$__indus_snap_tmp"'
_MARKER_LOOKBACK = 4096  # a cwd path is never longer than this


def cwd_marker(env_id: str) -> str:
    """The token that brackets the cwd on the marker line of environment ``env_id``."""
    return f"__INDUS_CWD_{env_id}__"


def quote_cd_target(cwd: str) -> str:
    """Quote a ``cd`` target for bash, keeping ``~`` meaning the home directory.

    ``~/rest`` becomes ``"$HOME"/'rest'`` so a suffix with spaces stays one word.
    """
    if cwd == "~":
        return '"$HOME"'
    if cwd.startswith("~/"):
        rest = cwd[2:]
        return '"$HOME"' if not rest else f'"$HOME"/{shlex.quote(rest)}'
    return shlex.quote(cwd)


@dataclass(frozen=True, slots=True)
class ScriptPaths:
    """Where one environment keeps its snapshot, already quoted for bash."""

    snapshot: str  # quoted final path
    snapshot_tmp_template: str  # quoted mktemp template next to it
    marker: str


def _session_unset(extra_names: Iterable[str]) -> str:
    words = [f"${{!{prefix}*}}" for prefix in SESSION_VAR_PREFIXES]
    words += list(SESSION_VAR_NAMES)
    wanted = sorted({n for n in extra_names if n and n.isidentifier() and n.isascii()})
    words += [shlex.quote(n) for n in wanted]
    return "unset " + " ".join(words) + " 2>/dev/null"


def _dump_exports(target: str, extra_names: Iterable[str]) -> str:
    # The subshell keeps the unset local to the dump; the redirect sits on the whole group so the
    # temp path variable is expanded by this shell, not inside a pipeline stage.
    return f"{{ ( {_session_unset(extra_names)}; export -p ) || true; }} > {target}"


def _marker_line(marker: str) -> str:
    # Leading newline: the command's output may not end with one.
    return f"printf '\\n%s%s%s\\n' '{marker}' \"$(pwd -P)\" '{marker}'"


def bootstrap_script(paths: ScriptPaths, *, cwd: str, excluded_names: Iterable[str] = ()) -> str:
    """The login-shell script that writes the snapshot, then reports the configured cwd.

    The snapshot is assembled in a ``mktemp`` file and moved over the final name in one step, so
    a command sourcing it never reads half a file. Function names starting with a single
    underscore (completion helpers and similar) are left out.
    """
    lines = [
        "umask 077",
        f"__indus_snap_tmp=$(mktemp {paths.snapshot_tmp_template}) || exit 1",
        _dump_exports(_TMP_VAR, excluded_names),
        "for __indus_fn in $(compgen -A function); do",
        '  case "$__indus_fn" in _[!_]*) continue ;; esac',
        '  declare -f "$__indus_fn"',
        f"done >> {_TMP_VAR} 2>/dev/null",
        f"alias -p >> {_TMP_VAR}",
        f"printf '%s\\n' 'shopt -s expand_aliases' 'set +e' 'set +u' >> {_TMP_VAR}",
        f"mv -f {_TMP_VAR} {paths.snapshot} || rm -f {_TMP_VAR}",
        f"builtin cd -- {quote_cd_target(cwd)} 2>/dev/null || true",
        _marker_line(paths.marker),
    ]
    return "\n".join(lines) + "\n"


def wrap_command(
    command: str,
    paths: ScriptPaths,
    *,
    cwd: str,
    snapshot_ready: bool,
    persist_env: bool,
    excluded_names: Iterable[str] = (),
) -> str:
    """The script one command runs as.

    Source the snapshot (quietly: some bash builds echo while sourcing) → agent markers and
    pager defaults (never overriding an outer value) → ``cd`` (exit 126 when it fails) → the
    command through ``eval`` → keep its status → private umask → re-dump the environment when
    ``persist_env`` → the cwd marker → exit with the command's status.
    """
    excluded = tuple(excluded_names)
    quoted_command = command.replace("'", "'\\''")
    lines: list[str] = []
    if snapshot_ready:
        lines.append(f"source {paths.snapshot} >/dev/null 2>&1 || true")
    lines += [
        'export AI_AGENT="${AI_AGENT:-indus-agent}" INDUS_AGENT="${INDUS_AGENT:-true}"',
        'export GIT_PAGER="${GIT_PAGER:-cat}" PAGER="${PAGER:-cat}"',
        f"builtin cd -- {quote_cd_target(cwd)} || exit 126",
        f"eval '{quoted_command}'",
        "__indus_rc=$?",
        "umask 077",
    ]
    if snapshot_ready and persist_env:
        # A failed dump never replaces a good snapshot: ``mv`` only runs after the dump worked.
        lines.append(
            f"__indus_snap_tmp=$(mktemp {paths.snapshot_tmp_template}) && "
            f"{{ {_dump_exports(_TMP_VAR, excluded)} && mv -f {_TMP_VAR} {paths.snapshot}; }} "
            f"2>/dev/null || rm -f {_TMP_VAR} 2>/dev/null || true"
        )
    lines += [_marker_line(paths.marker), "exit $__indus_rc"]
    return "\n".join(lines)


def split_cwd_marker(output: str, marker: str) -> tuple[str | None, str] | None:
    """Find the last ``marker<path>marker`` pair in ``output``.

    Returns ``(path or None, output without the marker line)``, or None when no complete pair is
    there (the command was killed or exited before the wrapper printed it). The removed span starts
    at the newline the wrapper put before the marker and ends after the marker line's newline.
    """
    end = output.rfind(marker)
    if end == -1:
        return None
    start = output.rfind(marker, max(0, end - _MARKER_LOOKBACK), end)
    if start == -1:
        return None
    path = output[start + len(marker) : end].strip() or None
    cut_from = output.rfind("\n", 0, start)
    if cut_from == -1:
        cut_from = start
    line_end = output.find("\n", end + len(marker))
    cut_to = len(output) if line_end == -1 else line_end + 1
    return path, output[:cut_from] + output[cut_to:]


def embed_stdin_heredoc(command: str, data: str) -> str:
    """``command`` with ``data`` as its stdin, carried inside the script (``heredoc`` backends).

    The body ends with a sentinel ``.`` so the command substitution cannot eat trailing newlines
    of ``data``; ``${var%.}`` removes exactly that character again. The process substitution is
    set up before the group runs, so the group can drop the variable before the command sees it.
    """
    delimiter = f"INDUS_STDIN_{secrets.token_hex(8)}"
    while delimiter in data:  # practically never; a body line equal to it would end the heredoc
        delimiter = f"INDUS_STDIN_{secrets.token_hex(8)}"
    return (
        f"__indus_stdin=$(cat <<'{delimiter}'\n{data}.\n{delimiter}\n)\n"
        f"{{ unset __indus_stdin\n{command}\n}} < <(printf '%s' \"${{__indus_stdin%.}}\")"
    )


# -- `A && B &` ---------------------------------------------------------------------------------

_SAFE_AFTER_GROUP = frozenset(";\n&|)}")


def _skip_quoted(text: str, i: int) -> int:
    """Index just past the quoted string that opens at ``text[i]``."""
    quote = text[i]
    j = i + 1
    while j < len(text):
        ch = text[j]
        if quote == '"' and ch == "\\":
            j += 2
            continue
        if ch == quote:
            return j + 1
        j += 1
    return len(text)


def _background_rewrites(text: str) -> list[tuple[int, int]]:
    """(group start, ``&`` index) for every top-level ``X && Y &`` / ``X || Y &``."""
    found: list[tuple[int, int]] = []
    depth = 0  # inside ( ), $( ), { } nothing is top level
    chain_at = -1  # index just after the last top-level && / || of this statement
    word_start = True
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"":
            i = _skip_quoted(text, i)
            word_start = False
            continue
        if ch == "`":
            close = text.find("`", i + 1)
            i = n if close == -1 else close + 1
            word_start = False
            continue
        if ch == "\\":
            i += 2
            word_start = False
            continue
        if ch == "#" and word_start:
            close = text.find("\n", i)
            i = n if close == -1 else close
            continue
        if ch == "(":
            depth += 1
            i += 1
            word_start = True
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            i += 1
            word_start = False
            continue
        if ch == "{" and word_start and i + 1 < n and text[i + 1].isspace():
            depth += 1
            i += 1
            continue
        if ch == "}" and word_start and depth:
            depth -= 1
            i += 1
            word_start = False
            continue
        if depth:
            word_start = ch.isspace() or ch in ";&|"
            i += 1
            continue
        if text.startswith(("&&", "||"), i):
            chain_at = i + 2
            i += 2
            word_start = True
            continue
        if text.startswith("&>", i):
            i += 2
            word_start = False
            continue
        if ch == "&":
            back = i - 1
            while back >= 0 and text[back] in " \t":
                back -= 1
            redirect = back >= 0 and text[back] in "<>"
            if not redirect:
                if chain_at >= 0:
                    found.append((chain_at, i))
                chain_at = -1
            i += 1
            word_start = True
            continue
        if ch in ";\n":
            chain_at = -1
            i += 1
            word_start = True
            continue
        word_start = ch.isspace() or ch == "|"
        i += 1
    return found


def rewrite_compound_background(command: str) -> str:
    """Turn ``A && B &`` (or ``||``) into ``A && { B & }`` at the top level.

    The ``&`` applies to the whole ``A && B`` list, so bash forks a child shell for the list and
    that child sits waiting on B; it keeps our output pipe open as long as B lives, and the
    call hangs. With the brace group only B goes to the background and B still runs only when A
    succeeded. Quoted
    text, escapes, comments, redirects and existing groups are left alone, so the rewrite is
    idempotent. A plain ``B &`` has no such problem and is not touched.
    """
    result = command
    for group_at, amp in reversed(_background_rewrites(command)):
        start = group_at
        while start < amp and result[start] in " \t":
            start += 1
        rest = result[amp + 1 :]
        follow = rest.lstrip(" \t")
        needs_separator = bool(follow) and (
            follow[0] not in _SAFE_AFTER_GROUP or follow.startswith("&>")
        )
        result = (
            result[:start]
            + "{ "
            + result[start:amp]
            + "& }"
            + (" ;" if needs_separator else "")
            + rest
        )
    return result
