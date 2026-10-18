"""Command wrapper, snapshot scripts, cwd marker, compound rewrite, heredoc stdin (P05-01)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from indus.backends.contract import BackendFlags, ExecResult
from indus.backends.wrapper import (
    ScriptPaths,
    bootstrap_script,
    cwd_marker,
    embed_stdin_heredoc,
    quote_cd_target,
    rewrite_compound_background,
    split_cwd_marker,
    wrap_command,
)

BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash is not installed")

PATHS = ScriptPaths(
    snapshot="'/tmp/indus-snap-abc.sh'",
    snapshot_tmp_template="'/tmp/indus-snap-abc.sh.tmp.XXXXXXXXXX'",
    marker=cwd_marker("abc"),
)

GOLDEN_WRAP = """\
source '/tmp/indus-snap-abc.sh' >/dev/null 2>&1 || true
export AI_AGENT="${AI_AGENT:-indus-agent}" INDUS_AGENT="${INDUS_AGENT:-true}"
export GIT_PAGER="${GIT_PAGER:-cat}" PAGER="${PAGER:-cat}"
builtin cd -- "$HOME"/'my dir' || exit 126
eval 'echo '\\''hi'\\'''
__indus_rc=$?
umask 077
__indus_snap_tmp=$(mktemp '/tmp/indus-snap-abc.sh.tmp.XXXXXXXXXX') && \
{ { ( unset ${!INDUS_SESSION_*} ${!INDUS_UI_SESSION_*} ${!INDUS_CRON_*} ${!INDUS_RPC_*} \
${!INDUS_DELEGATE_*} INDUS_HOME AI_AGENT INDUS_AGENT MY_TOKEN 2>/dev/null; export -p ) || true; } \
> "$__indus_snap_tmp" && mv -f "$__indus_snap_tmp" '/tmp/indus-snap-abc.sh'; } 2>/dev/null \
|| rm -f "$__indus_snap_tmp" 2>/dev/null || true
printf '\\n%s%s%s\\n' '__INDUS_CWD_abc__' "$(pwd -P)" '__INDUS_CWD_abc__'
exit $__indus_rc"""

GOLDEN_BOOTSTRAP = """\
umask 077
__indus_snap_tmp=$(mktemp '/tmp/indus-snap-abc.sh.tmp.XXXXXXXXXX') || exit 1
{ ( unset ${!INDUS_SESSION_*} ${!INDUS_UI_SESSION_*} ${!INDUS_CRON_*} ${!INDUS_RPC_*} \
${!INDUS_DELEGATE_*} INDUS_HOME AI_AGENT INDUS_AGENT 2>/dev/null; export -p ) || true; } \
> "$__indus_snap_tmp"
for __indus_fn in $(compgen -A function); do
  case "$__indus_fn" in _[!_]*) continue ;; esac
  declare -f "$__indus_fn"
done >> "$__indus_snap_tmp" 2>/dev/null
alias -p >> "$__indus_snap_tmp"
printf '%s\\n' 'shopt -s expand_aliases' 'set +e' 'set +u' >> "$__indus_snap_tmp"
mv -f "$__indus_snap_tmp" '/tmp/indus-snap-abc.sh' || rm -f "$__indus_snap_tmp"
builtin cd -- /work 2>/dev/null || true
printf '\\n%s%s%s\\n' '__INDUS_CWD_abc__' "$(pwd -P)" '__INDUS_CWD_abc__'
"""


def _unwrap(golden: str) -> str:
    # The goldens above are folded with "\\\n" to stay inside the line length.
    return golden.replace(" \\\n", " ")


def test_golden_command_wrapper() -> None:
    script = wrap_command(
        "echo 'hi'",
        PATHS,
        cwd="~/my dir",
        snapshot_ready=True,
        persist_env=True,
        excluded_names=["MY_TOKEN", "not a name", ""],
    )
    assert script == _unwrap(GOLDEN_WRAP)


def test_golden_bootstrap() -> None:
    assert bootstrap_script(PATHS, cwd="/work") == _unwrap(GOLDEN_BOOTSTRAP)


def test_wrapper_without_snapshot_or_persistence() -> None:
    no_snapshot = wrap_command("ls", PATHS, cwd="/w", snapshot_ready=False, persist_env=True)
    assert "source" not in no_snapshot and "mktemp" not in no_snapshot
    no_persist = wrap_command("ls", PATHS, cwd="/w", snapshot_ready=True, persist_env=False)
    assert no_persist.startswith("source ") and "mktemp" not in no_persist
    assert no_persist.rstrip().endswith("exit $__indus_rc")


@pytest.mark.parametrize(
    ("cwd", "quoted"),
    [
        ("~", '"$HOME"'),
        ("~/", '"$HOME"'),
        ("~/src", '"$HOME"/src'),
        ("~/a b", "\"$HOME\"/'a b'"),
        ("/plain/path", "/plain/path"),
        ("/with space", "'/with space'"),
        ("-dash", "-dash"),
        ("~other/x", "'~other/x'"),
        ("/it's", "'/it'\"'\"'s'"),
    ],
)
def test_cd_target_quoting_keeps_tilde(cwd: str, quoted: str) -> None:
    assert quote_cd_target(cwd) == quoted


MARK = cwd_marker("e1")


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (f"hello\n\n{MARK}/srv/app{MARK}\n", ("/srv/app", "hello\n")),
        (f"no newline\n{MARK}/x{MARK}\n", ("/x", "no newline")),
        (f"��bin\x00ary\n{MARK}/b{MARK}\ntrailing", ("/b", "��bin\x00arytrailing")),
        (f"\n{MARK}/only{MARK}\n", ("/only", "")),
        (f"x\n{MARK}{MARK}\n", (None, "x")),
        (f"a\n{MARK}/old{MARK}\nb\n{MARK}/new{MARK}\n", ("/new", f"a\n{MARK}/old{MARK}\nb")),
        (f"{MARK}/start{MARK}", ("/start", "")),
    ],
)
def test_marker_split(output: str, expected: tuple[str | None, str]) -> None:
    assert split_cwd_marker(output, MARK) == expected


@pytest.mark.parametrize(
    "output",
    [
        "killed before the wrapper printed anything",
        f"half a marker {MARK}/tmp",
        f"{MARK}" + "x" * 5000 + f"{MARK}",  # longer than any path: not a pair
        f"other env {cwd_marker('zz')}/x{cwd_marker('zz')}\n",
    ],
)
def test_marker_absent(output: str) -> None:
    assert split_cwd_marker(output, MARK) is None


@pytest.mark.parametrize(
    ("command", "rewritten"),
    [
        ("make && ./serve &", "make && { ./serve & }"),
        ("a || b &", "a || { b & }"),
        ("a && b & c", "a && { b & } ; c"),
        ("a && b &\nc && d &", "a && { b & }\nc && { d & }"),
        ("a && b > log 2>&1 &", "a && { b > log 2>&1 & }"),
        ("a && b &> log &", "a && { b &> log & }"),
        ("a && b | c &", "a && { b | c & }"),
        ("a &&\tb &", "a &&\t{ b & }"),
        ("a && b & &> x c", "a && { b & } ; &> x c"),
        ("a && b &;", "a && { b & };"),
        ("x; a && b &", "x; a && { b & }"),
        ("a && b & c && d & e", "a && { b & } ; c && { d & } ; e"),
        ("case x in y) a && b &;; esac", "case x in y) a && { b & };; esac"),
    ],
)
def test_compound_background_rewritten(command: str, rewritten: str) -> None:
    assert rewrite_compound_background(command) == rewritten
    assert rewrite_compound_background(rewritten) == rewritten  # idempotent


@pytest.mark.parametrize(
    "command",
    [
        "sleep 10 &",
        "a && b",
        "cmd > out 2>&1 &",
        "echo 'a && b &'",
        'echo "a && b &"',
        "echo a \\&& b",
        "# a && b &",
        "a && b # trailing &",
        "(a && b &)",
        "{ a && b & }",
        "x=$(a && b &)",
        "a; b &",
        "a &&\n b",
        "",
    ],
)
def test_compound_background_left_alone(command: str) -> None:
    assert rewrite_compound_background(command) == command


@needs_bash
@pytest.mark.parametrize(
    "command",
    ["true && sleep 0 &", "a && b & c", "a && b | c &", "a && b &> x &", "a || b &;"],
)
def test_rewrite_parses(command: str) -> None:
    assert BASH is not None
    done = subprocess.run(
        [BASH, "-n", "-c", rewrite_compound_background(command)], capture_output=True, timeout=10
    )
    assert done.returncode == 0, done.stderr


@needs_bash
def test_rewritten_trailing_statement_runs_and_does_not_wait(tmp_path: Path) -> None:
    assert BASH is not None
    script = rewrite_compound_background("true && sleep 0.1 >/dev/null & echo after; wait")
    done = subprocess.run(
        [BASH, "-c", script], capture_output=True, text=True, timeout=10, start_new_session=True
    )
    assert done.stdout == "after\n"


def _run_bash(script: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    assert BASH is not None
    done = subprocess.run(
        [BASH, "-c", script],
        capture_output=True,
        cwd=cwd,
        env=env if env is not None else {"PATH": "/usr/bin:/bin", "HOME": str(cwd)},
        timeout=20,
    )
    return done.stdout.decode("utf-8", "replace")


@needs_bash
@pytest.mark.parametrize(
    "data",
    [
        "plain",
        "ends with newline\n",
        "two trailing\n\n",
        "",
        "quotes ' \" and $HOME and `cmd` and \\backslash",
        "INDUS_STDIN_looks_like_a_delimiter\n.\n",
        "ünïcödé ✓",
    ],
)
def test_heredoc_stdin_is_byte_exact(tmp_path: Path, data: str) -> None:
    script = embed_stdin_heredoc('cat; echo; echo "var=${__indus_stdin-unset}"', data)
    assert "INDUS_STDIN_" in script
    assert _run_bash(script, tmp_path) == data + "\nvar=unset\n"


def test_heredoc_delimiter_is_fresh_each_time() -> None:
    first = embed_stdin_heredoc("cat", "x").splitlines()[0]
    second = embed_stdin_heredoc("cat", "x").splitlines()[0]
    assert first != second


@needs_bash
def test_session_var_with_newline_never_reaches_a_later_source(tmp_path: Path) -> None:
    """R02.1: a hostile per-session value is unset before the dump, so the next ``source``
    neither runs its embedded command nor brings the value back."""
    snap = tmp_path / "indus-snap-t.sh"
    paths = ScriptPaths(
        snapshot=f"'{snap}'", snapshot_tmp_template=f"'{snap}.tmp.XXXXXXXXXX'", marker=MARK
    )
    pwned = tmp_path / "pwned"
    hostile = f"chat\ndeclare -x SMUGGLED=1\ntouch {pwned}\n$(touch {pwned})"
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "INDUS_SESSION_TITLE": hostile,
        "INDUS_HOME": "/profile/a",
        "KEEP_ME": "yes",
    }
    out = _run_bash(bootstrap_script(paths, cwd=str(tmp_path)), tmp_path, env)
    assert MARK in out
    text = snap.read_text()
    assert "SMUGGLED" not in text and "INDUS_SESSION_TITLE" not in text
    assert "INDUS_HOME" not in text and "KEEP_ME" in text
    # A command exports another hostile session value; the re-dump drops it too.
    first = wrap_command(
        f"export INDUS_SESSION_NAME=$'x\\ntouch {pwned}'; export MINE=1",
        paths,
        cwd=str(tmp_path),
        snapshot_ready=True,
        persist_env=True,
    )
    _run_bash(first, tmp_path, {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    later = wrap_command(
        'echo "name=${INDUS_SESSION_NAME-unset} title=${INDUS_SESSION_TITLE-unset} mine=$MINE"',
        paths,
        cwd=str(tmp_path),
        snapshot_ready=True,
        persist_env=True,
    )
    out = _run_bash(later, tmp_path, {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert "name=unset title=unset mine=1" in out
    assert not pwned.exists()
    assert snap.stat().st_mode & 0o077 == 0  # private snapshot


def test_contract_defaults() -> None:
    result = ExecResult(output="x", returncode=0)
    assert result.cwd_observed is None and not result.timed_out and not result.interrupted
    assert result.full_output_path is None and result.pid is None
    assert not result.environment_recreated and result.stdin_error is None
    flags = BackendFlags()
    assert flags.stdin_mode == "pipe" and not flags.isolated and not flags.container_paths
