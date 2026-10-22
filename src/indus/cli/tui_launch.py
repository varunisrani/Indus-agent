"""``indus --tui``: choose the interface, start the full-screen renderer under Node, tidy up after.

Choice (R04.19), first match wins: ``--cli`` → classic; ``--tui`` → renderer; stdin or stdout not
a terminal → classic (a pipe or a scheduled run is never taken over by a setting);
``--safe-mode``/``--ignore-user-config`` → classic (no stored preference picks the surface);
``INDUS_TUI=1`` → renderer; ``display.interface: tui`` → renderer; otherwise classic.

A launch prepares the child environment from this process's env view: the profile home, the
folder the run belongs to (``INDUS_TUI_CWD`` and ``INDUS_LAUNCH_CWD``; values inherited from an
outer renderer are replaced), ``NODE_ENV=production`` (D-04.2), the bridge command, a temp file
the renderer writes the live session id into, and the launch flags under ``LAUNCH_NAMES`` (every
one of them is cleared first, so nothing stale from the shell survives). Node's old-space heap is
raised through ``NODE_OPTIONS`` unless the user already set one, and kept under a container memory
limit when one is found. The renderer attaches to the profile's daemon by default through
``indus gateway grant`` (``INDUS_TUI_GRANT_COMMAND``, P17-19); ``--local`` exports
``INDUS_TUI_LOCAL=1`` and the renderer runs the bridge child instead.

The renderer is found in this order: ``--dev`` runs the checkout's TypeScript through ``tsx``;
``INDUS_TUI_DIR/dist/entry.js``; the bundle shipped in the package (``indus/_tui_dist``); the
checkout's ``apps/tui/dist/entry.js``, rebuilt first when a source file is newer.

Under the dashboard's Chat tab (P18-09) the PTY child names its channel's active-session file in
``INDUS_TUI_DASHBOARD_SESSION_FILE``: when that is an absolute path to a regular file (not a
link) of this user, the renderer writes into it instead of a temp file and the file is kept.

The child runs in the foreground: Ctrl+C reaches it directly (this process ignores SIGINT while
it waits so the renderer can restore the terminal), SIGTERM is passed on. Its exit code is
returned (killed by signal n → 128+n). After a clean end or Ctrl+C the resume command is printed;
the temp file is always removed; a ``-w`` worktree is kept for its session or removed when unused;
exit code 42 means "update me" and runs ``indus update``.

When the renderer cannot start (no Node, Node too old, nothing built, native Windows), one
explanation goes to stderr and ``run`` returns None: the caller runs the classic prompt.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import click

from indus.cli.args import AUTO_WORKTREE, ChatArgs, CliState, GlobalArgs, split_csv
from indus.platform import locate

__all__ = [
    "CGROUP_FILES",
    "DEFAULT_HEAP_MB",
    "LAUNCH_NAMES",
    "UPDATE_EXIT",
    "Renderer",
    "System",
    "build_child_env",
    "cgroup_limit",
    "exit_code_of",
    "find_renderer",
    "heap_mb",
    "node_major",
    "run",
    "wants_tui",
    "with_heap",
]

UPDATE_EXIT = 42
"""The renderer's exit code asking for ``indus update``."""

DEFAULT_HEAP_MB = 8192
PREFERRED_NODE = 24
OLDEST_NODE = 22
HEAP_FLAG = "--max-old-space-size"

CGROUP_FILES: tuple[Path, ...] = (
    Path("/sys/fs/cgroup/memory.max"),
    Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
)
"""Container memory limit files: cgroup v2 first, then v1."""

_NO_LIMIT_FLOOR = 1 << 50
"""cgroup v1 reports "no limit" as a huge number; anything this big counts as none."""

BRIDGE_ENV = "INDUS_TUI_BRIDGE_COMMAND"
DIR_ENV = "INDUS_TUI_DIR"
FORCE_BUILD_ENV = "INDUS_TUI_FORCE_BUILD"
SESSION_FILE_ENV = "INDUS_TUI_ACTIVE_SESSION_FILE"
RESUME_ENV = "INDUS_TUI_RESUME"
GRANT_ENV = "INDUS_TUI_GRANT_COMMAND"
LOCAL_ENV = "INDUS_TUI_LOCAL"
DASHBOARD_FILE_ENV = "INDUS_TUI_DASHBOARD_SESSION_FILE"
CWD_ENVS = ("INDUS_TUI_CWD", "INDUS_LAUNCH_CWD")

_VALUE_NAMES = {
    "model": "INDUS_MODEL",
    "provider": "INDUS_TUI_PROVIDER",
    "reasoning": "INDUS_TUI_REASONING",
    "toolsets": "INDUS_TUI_TOOLSETS",
    "skills": "INDUS_TUI_SKILLS",
    "query": "INDUS_TUI_QUERY",
    "max_turns": "INDUS_TUI_MAX_TURNS",
    "tool_progress": "INDUS_TUI_TOOL_PROGRESS",
}
_SWITCH_NAMES = {
    "yolo": "INDUS_TUI_YOLO",
    "checkpoints": "INDUS_TUI_CHECKPOINTS",
    "pass_session_id": "INDUS_TUI_PASS_SESSION_ID",
    "ignore_rules": "INDUS_TUI_IGNORE_RULES",
    "ignore_user_config": "INDUS_TUI_IGNORE_USER_CONFIG",
    "safe_mode": "INDUS_TUI_SAFE_MODE",
}

LAUNCH_NAMES: tuple[str, ...] = (
    RESUME_ENV,
    SESSION_FILE_ENV,
    DASHBOARD_FILE_ENV,
    LOCAL_ENV,
    *_VALUE_NAMES.values(),
    *_SWITCH_NAMES.values(),
)
"""Names this launcher owns in the renderer's env: always cleared, then set for this run only."""

_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_SKIP_DIRS = frozenset({"node_modules", "dist", ".turbo", ".cache"})
_PKG_DIR = Path(__file__).resolve().parents[1]

_WINDOWS_NOTE = (
    "The full-screen interface runs under WSL2 on Windows (install WSL2 and run `indus --tui` "
    "there). Using the classic prompt."
)
_NODE_HINT = (
    "The full-screen interface needs Node.js {want} LTS, and `node` was not found on PATH. "
    "Install it from https://nodejs.org, or run `indus --cli` for the classic prompt."
)
_NODE_OLD = (
    "The full-screen interface needs Node.js {want} LTS; `{node}` reports {seen}. "
    "Update Node, or run `indus --cli` for the classic prompt."
)
_FALLBACK = "Using the classic prompt."


def _err(*lines: str) -> None:
    for line in lines:
        click.echo(line, err=True)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUE_WORDS


# -- the choice ------------------------------------------------------------------------------------


def wants_tui(
    args: GlobalArgs,
    *,
    terminal: bool,
    env: Mapping[str, str],
    config: Any = None,
) -> bool:
    """Should this chat open the renderer? (see the module notes for the order)."""
    if args.interface == "cli":
        return False
    if args.interface == "tui":
        return True
    if not terminal or args.effective_ignore_user_config:
        return False
    if env.get("INDUS_TUI", "").strip() == "1":
        return True
    if config is None:
        return False
    try:
        chosen = config.get("display.interface")
    except Exception:
        return False
    return isinstance(chosen, str) and chosen.strip().lower() == "tui"


# -- the machine around the launch (tests swap pieces) ---------------------------------------------


class Child(Protocol):
    def wait(self, timeout: float | None = None) -> int: ...
    def send_signal(self, sig: int) -> None: ...
    def kill(self) -> None: ...


def _version_of(node: str, env: Mapping[str, str]) -> str | None:
    try:
        done = subprocess.run(
            [node, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=dict(env),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _start(argv: list[str], cwd: Path, env: dict[str, str]) -> Child:
    return subprocess.Popen(argv, cwd=cwd, env=env)


def _call(argv: list[str], cwd: Path, env: dict[str, str]) -> int:
    try:
        return subprocess.call(argv, cwd=cwd, env=env)
    except OSError as exc:
        _err(f"indus: could not run {argv[0]}: {exc.strerror or exc}")
        return 127


def _checkout_root() -> Path | None:
    root = _PKG_DIR.parents[1]
    return root if (root / "apps" / "tui" / "package.json").is_file() else None


@dataclass
class System:
    """Everything the launcher touches outside Python: tests replace single fields."""

    platform: str = field(default_factory=lambda: sys.platform)
    node_version: Callable[[str, Mapping[str, str]], str | None] = _version_of
    start: Callable[[list[str], Path, dict[str, str]], Child] = _start
    call: Callable[[list[str], Path, dict[str, str]], int] = _call
    cgroup_files: tuple[Path, ...] = CGROUP_FILES
    package_dir: Path = _PKG_DIR
    checkout_root: Path | None = field(default_factory=_checkout_root)
    temp_dir: Path = field(default_factory=lambda: Path(tempfile.gettempdir()))
    script_dir: Path = field(default_factory=lambda: Path(sys.executable).parent)
    pid: int = field(default_factory=os.getpid)


# -- Node ------------------------------------------------------------------------------------------


def node_major(version: str | None) -> int | None:
    """``"v24.1.0"`` → 24; None when the text is not a Node version."""
    found = re.match(r"\s*v?(\d+)\.", version or "")
    return int(found.group(1)) if found else None


def _find_node(env: Mapping[str, str], system: System) -> str | None:
    """The ``node`` to run, or None after explaining why there is none."""
    node = locate.which("node", path=env.get("PATH"))
    if node is None:
        _err(_NODE_HINT.format(want=PREFERRED_NODE), _FALLBACK)
        return None
    seen = system.node_version(node, env)
    major = node_major(seen)
    if major is None or major < OLDEST_NODE:
        _err(
            _NODE_OLD.format(want=PREFERRED_NODE, node=node, seen=seen or "no version"),
            _FALLBACK,
        )
        return None
    if major != PREFERRED_NODE:
        _err(
            f"Note: the full-screen interface is built for Node.js {PREFERRED_NODE}; found {seen}."
        )
    return node


# -- the renderer ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Renderer:
    """How to start the renderer: ``argv`` after the Node binary (or whole, for ``tsx``)."""

    argv: tuple[str, ...]
    cwd: Path | None
    """Fixed working folder (``--dev`` needs the app folder); None = the run's folder."""
    via_node: bool = True


def _newest(folders: list[Path]) -> float:
    newest = 0.0
    for folder in folders:
        for base, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for name in files:
                with contextlib.suppress(OSError):
                    newest = max(newest, (Path(base) / name).stat().st_mtime)
    return newest


def _stale(root: Path, bundle: Path, env: Mapping[str, str]) -> bool:
    if _truthy(env.get(FORCE_BUILD_ENV)) or not bundle.is_file():
        return True
    sources = [root / "apps" / "tui" / "src", *sorted((root / "packages").glob("*/src"))]
    return _newest([p for p in sources if p.is_dir()]) > bundle.stat().st_mtime


def _rebuild(root: Path, env: Mapping[str, str], system: System) -> bool:
    pnpm = locate.which("pnpm", path=env.get("PATH"))
    if pnpm is None:
        _err("The full-screen interface needs a build, and `pnpm` was not found on PATH.")
        return False
    _err("Building the full-screen interface (sources changed)…")
    argv = [pnpm, "--filter", "@indus/tui", "run", "build"]
    return system.call(argv, root, dict(env)) == 0


def find_renderer(env: Mapping[str, str], system: System, *, dev: bool = False) -> Renderer | None:
    """The renderer to start, or None after saying why there is none."""
    explicit = (env.get(DIR_ENV) or "").strip()
    root = system.checkout_root
    if dev:
        if explicit:
            _err(
                f"--dev runs the renderer from its sources, but {DIR_ENV}={explicit} names a "
                f"prebuilt one. Unset {DIR_ENV} to use --dev.",
                _FALLBACK,
            )
            return None
        if root is None or not (root / "apps" / "tui" / "src" / "entry.tsx").is_file():
            _err("--dev needs a source checkout of Indus Agent.", _FALLBACK)
            return None
        app = root / "apps" / "tui"
        for tsx in (app / "node_modules" / ".bin" / "tsx", root / "node_modules" / ".bin" / "tsx"):
            if tsx.is_file():
                return Renderer((str(tsx), "src/entry.tsx"), app, via_node=False)
        _err("--dev needs `tsx` in the workspace: run `pnpm add -D tsx` in apps/tui.", _FALLBACK)
        return None
    if explicit:
        entry = Path(explicit).expanduser() / "dist" / "entry.js"
        if entry.is_file():
            return Renderer((str(entry),), None)
    bundled = system.package_dir / "_tui_dist" / "entry.js"
    if bundled.is_file():
        return Renderer((str(bundled),), None)
    if root is not None:
        built = root / "apps" / "tui" / "dist" / "entry.js"
        if _stale(root, built, env) and not _rebuild(root, env, system):
            if built.is_file():
                _err("The rebuild failed; starting the previous build of the interface.")
            else:
                _err("The full-screen interface is not built.", _FALLBACK)
                return None
        if built.is_file():
            return Renderer((str(built),), None)
    _err("The full-screen interface is not part of this installation.", _FALLBACK)
    return None


# -- memory ----------------------------------------------------------------------------------------


def cgroup_limit(files: tuple[Path, ...] = CGROUP_FILES) -> int | None:
    """The container memory limit in bytes, or None when there is none to respect."""
    for path in files:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if text == "max":
            return None
        try:
            value = int(text)
        except ValueError:
            continue
        if value <= 0:
            continue
        return None if value >= _NO_LIMIT_FLOOR else value
    return None


def heap_mb(limit_bytes: int | None, default: int = DEFAULT_HEAP_MB) -> int:
    """Old-space size for Node: ``default``, or three quarters of a smaller container limit."""
    if limit_bytes is None:
        return default
    share = (limit_bytes // (1 << 20)) * 3 // 4
    return max(1, min(default, share))


def with_heap(node_options: str | None, megabytes: int) -> str:
    """``NODE_OPTIONS`` with a heap size added, unless the user already chose one."""
    words = (node_options or "").split()
    if not any(w == HEAP_FLAG or w.startswith(HEAP_FLAG + "=") for w in words):
        words.append(f"{HEAP_FLAG}={megabytes}")
    return " ".join(words)


# -- the child environment -------------------------------------------------------------------------


def _bridge_command(env: Mapping[str, str], system: System) -> str | None:
    own = (env.get(BRIDGE_ENV) or "").strip()
    if own:
        return own
    return _indus_program(env, system)


def _grant_command(env: Mapping[str, str], system: System) -> str | None:
    """The program the renderer runs as ``gateway grant --json`` (P17-19)."""
    own = (env.get(GRANT_ENV) or "").strip()
    if own:
        return own
    return _indus_program(env, system)


def _indus_program(env: Mapping[str, str], system: System) -> str | None:
    name = "indus.exe" if system.platform == "win32" else "indus"
    beside = system.script_dir / name
    if beside.is_file() and os.access(beside, os.X_OK):
        return str(beside)
    return locate.which("indus", path=env.get("PATH"))


def build_child_env(
    base: Mapping[str, str],
    *,
    home: Path,
    cwd: Path,
    args: GlobalArgs,
    chat: ChatArgs,
    query: str | None,
    resume_id: str | None,
    session_file: Path,
    heap: int,
    bridge: str | None,
    grant: str | None = None,
) -> dict[str, str]:
    """The renderer's env: ``base`` with this run's names pinned (see the module notes)."""
    env = {k: v for k, v in base.items() if k not in LAUNCH_NAMES}
    env["INDUS_HOME"] = str(home)
    for name in CWD_ENVS:
        env[name] = str(cwd)
    env["NODE_ENV"] = "production"
    env["NODE_OPTIONS"] = with_heap(env.get("NODE_OPTIONS"), heap)
    env[SESSION_FILE_ENV] = str(session_file)
    if bridge:
        env[BRIDGE_ENV] = bridge
    if grant:
        env[GRANT_ENV] = grant
    if getattr(args, "local", False):  # ``--local`` (P17-18 adds the flag): keep the bridge child
        env[LOCAL_ENV] = "1"
    if resume_id:
        env[RESUME_ENV] = resume_id
    values: dict[str, str | None] = {
        "model": args.model,
        "provider": args.provider,
        "reasoning": args.reasoning,
        "toolsets": ",".join(split_csv(args.toolsets) or []),
        "skills": ",".join(split_csv(args.skills) or []),
        "query": query,
        "max_turns": None if chat.max_turns is None else str(chat.max_turns),
        "tool_progress": "off" if chat.quiet else None,
    }
    for key, value in values.items():
        if value:
            env[_VALUE_NAMES[key]] = value
    for key, name in _SWITCH_NAMES.items():
        if getattr(args, key):
            env[name] = "1"
    return env


def _run_folder(env: Mapping[str, str], home: Path) -> Path:
    try:
        return Path.cwd()
    except OSError:
        pwd = (env.get("PWD") or "").strip()
        return Path(pwd) if pwd and Path(pwd).is_dir() else home


# -- the child process -----------------------------------------------------------------------------


def exit_code_of(code: int) -> int:
    """A ``Popen`` return code as a shell would report it (signal n → 128+n)."""
    return 128 - code if code < 0 else code


@contextlib.contextmanager
def _foreground(child: Child) -> Iterator[None]:
    """While the renderer runs: Ctrl+C is the renderer's, SIGTERM is passed on to it."""
    saved: list[tuple[int, Any]] = []

    def pass_on(signum: int, _frame: object) -> None:
        with contextlib.suppress(OSError):
            child.send_signal(signum)

    wanted: list[tuple[int, Any]] = [(signal.SIGINT, signal.SIG_IGN)]
    if hasattr(signal, "SIGTERM"):
        wanted.append((signal.SIGTERM, pass_on))
    try:
        for signum, handler in wanted:
            saved.append((signum, signal.signal(signum, handler)))
    except ValueError:  # not the main thread: leave the handlers alone
        pass
    try:
        yield
    finally:
        for signum, handler in saved:
            signal.signal(signum, handler)


def _wait(child: Child) -> int:
    with _foreground(child):
        try:
            return exit_code_of(child.wait())
        except KeyboardInterrupt:
            pass
    # Only reached when the handlers could not be set: the renderer got the same Ctrl+C.
    try:
        child.wait(timeout=5.0)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        with contextlib.suppress(OSError):
            child.kill()
    return 130


def _make_session_file(system: System) -> Path:
    path = system.temp_dir / f"indus-tui-active-session-{system.pid}.json"
    with contextlib.suppress(FileNotFoundError):
        path.unlink()  # a leftover from a dead run with the same pid, or a planted link
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    os.close(os.open(path, flags, 0o600))
    return path


def _dashboard_session_file(env: Mapping[str, str]) -> Path | None:
    """The dashboard's channel file (see the module notes), or None to make our own."""
    raw = (env.get(DASHBOARD_FILE_ENV) or "").strip()
    if not raw or not Path(raw).is_absolute():
        return None
    path = Path(raw)
    try:
        info = path.lstat()
    except OSError:
        return None
    getuid = getattr(os, "getuid", None)
    if not stat.S_ISREG(info.st_mode) or (getuid is not None and info.st_uid != getuid()):
        return None
    return path


def _session_in(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    except (OSError, ValueError):
        return None
    value: object = data.get("session_id") if isinstance(data, dict) else None
    if not isinstance(value, str):
        return None
    return value.strip() or None


# -- before and after ------------------------------------------------------------------------------


def _resolve_resume(state: CliState, args: GlobalArgs, cwd: Path) -> str | None:
    """The session ``-c``/``-r`` name for this run (raises ``SelectionError`` on a miss)."""
    if args.resume is None and args.continue_ is None:
        return None
    from indus.cli import resume, session_select
    from indus.state import registry

    latest = (args.resume or "").strip().lower() == session_select.LATEST or (
        args.resume is None and args.continue_ == session_select.BARE_CONTINUE
    )
    crumb = resume.read_breadcrumb(state.scope) if latest else None
    store = registry.acquire(state.scope)
    try:
        return session_select.select_session(store, args, cwd=cwd, breadcrumb=crumb)
    finally:
        registry.release(store)


def _session_row(state: CliState, session_id: str) -> dict[str, Any] | None:
    from indus.state import registry, sessions

    try:
        store = registry.acquire(state.scope)
    except Exception:
        return None
    try:
        return sessions.get(store, session_id)
    except Exception:
        return None
    finally:
        registry.release(store)


def _print_resume_hint(state: CliState, session_id: str | None) -> None:
    if not session_id:
        return
    row = _session_row(state, session_id)
    if row is None or int(row.get("message_count") or 0) <= 0:
        return
    from indus.foundation.profiles import DEFAULT_PROFILE

    profile = state.scope.profile_id
    tail = "" if profile == DEFAULT_PROFILE else f" -p {profile}"
    click.echo("")
    click.echo("Pick this chat up again with:")
    click.echo(f"  indus --tui -r {session_id}{tail}")
    title = row.get("title")
    if isinstance(title, str) and title.strip():
        click.echo(f'  indus --tui -c "{title.strip()}"{tail}')


def _finish_worktree(state: CliState, info: Any, session_id: str | None) -> None:
    from indus.backends import worktrees
    from indus.service.lsp_control import gateway_releaser

    if session_id is not None and _session_row(state, session_id) is not None:
        with contextlib.suppress(Exception):
            worktrees.bind_session(state.scope, info.path, session_id)
        _err(f"Worktree kept for this session: {info.path}")
        return
    try:
        # The session's language servers run in the profile's daemon when it attached there
        # (wave 45): they stop before the tree goes.
        releaser = gateway_releaser(state.scope.home)
        kept = worktrees.remove(state.scope, info.repo, info.path, before_remove=releaser)
    except Exception as exc:
        kept = str(exc)
    if kept is None:
        _err(f"Worktree removed (nothing in it): {info.path}")
    else:
        _err(f"Worktree kept ({kept}): {info.path}")


def _enter_worktree(state: CliState, args: GlobalArgs) -> tuple[Any, int | None]:
    from indus.backends import worktrees
    from indus.cli.session_select import SelectionError, enter_in_dir

    if args.resume is not None or args.continue_ is not None:
        _err(
            "indus: -w starts a new session in a new worktree; a resumed session keeps its own "
            "folder. Drop -w, or drop -c/-r."
        )
        return None, 2
    name = None if args.worktree in (None, AUTO_WORKTREE) else args.worktree
    try:
        info = worktrees.create(state.scope, Path.cwd(), name)
        enter_in_dir(info.path)
    except worktrees.WorktreeError as exc:
        _err(f"indus: {exc}", "Run `indus -w` from inside a git repository.")
        return None, 1
    except SelectionError as exc:
        _err(*exc.lines())
        return None, exc.code
    _err(f"Worktree: {info.path} (branch {info.branch}, from {info.base})")
    return info, None


def _run_update(env: Mapping[str, str], system: System) -> int:
    program = _indus_program(env, system)
    if program is None:
        _err("indus: the update cannot start: the `indus` program was not found.")
        return 1
    clean = {k: v for k, v in env.items() if k not in LAUNCH_NAMES and k not in CWD_ENVS}
    _err("", "Starting the update…")
    return system.call([program, "update"], _run_folder(env, Path.home()), clean)


# -- the launch ------------------------------------------------------------------------------------


def run(
    state: CliState,
    args: GlobalArgs,
    chat: ChatArgs,
    *,
    query: str | None = None,
    dev: bool = False,
    system: System | None = None,
) -> int | None:
    """Start the renderer and return its exit code; None means "run the classic prompt"."""
    machine = system if system is not None else System()
    if machine.platform == "win32":
        _err(_WINDOWS_NOTE)
        return None
    base: MutableMapping[str, str] = dict(state.scope.env)
    node = _find_node(base, machine)
    if node is None:
        return None
    renderer = find_renderer(base, machine, dev=dev)
    if renderer is None:
        return None

    from indus.cli.session_select import SelectionError, enter_in_dir

    if args.in_dir is not None:
        try:
            enter_in_dir(args.in_dir)
        except SelectionError as exc:
            _err(*exc.lines())
            return exc.code
    worktree: Any = None
    if args.worktree is not None:
        worktree, stop = _enter_worktree(state, args)
        if stop is not None:
            return stop
    cwd = _run_folder(base, state.scope.home)
    try:
        resume_id = _resolve_resume(state, args, cwd)
    except SelectionError as exc:
        _err(*exc.lines())
        return exc.code

    given = _dashboard_session_file(base)
    session_file = given if given is not None else _make_session_file(machine)
    code: int | None = None
    try:
        env = build_child_env(
            base,
            home=state.scope.home,
            cwd=cwd,
            args=args,
            chat=chat,
            query=query,
            resume_id=resume_id,
            session_file=session_file,
            heap=heap_mb(cgroup_limit(machine.cgroup_files)),
            bridge=_bridge_command(base, machine),
            grant=_grant_command(base, machine),
        )
        argv = [node, *renderer.argv] if renderer.via_node else list(renderer.argv)
        try:
            child = machine.start(argv, renderer.cwd or cwd, env)
        except OSError as exc:
            _err(f"indus: the full-screen interface could not start: {exc.strerror or exc}")
            return 1
        code = _wait(child)
        live = _session_in(session_file) or resume_id
        if code in (0, 130):
            _print_resume_hint(state, live)
        if worktree is not None:
            _finish_worktree(state, worktree, live)
    finally:
        if given is None:
            with contextlib.suppress(OSError):
                session_file.unlink()
    if code == UPDATE_EXIT:
        return _run_update(base, machine)
    return code
