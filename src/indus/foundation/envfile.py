"""Reading and writing a profile's ``.env`` file.

The reader and the writer share :func:`parse_line`, so every line the reader treats as an assignment
is a line the writer finds when it replaces or deletes that name. Writes run under a cross-process
lock beside the file and land through an atomic replace that keeps the file's mode.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from indus.foundation.fsio import FileLock, atomic_write_bytes

log = logging.getLogger(__name__)

LOCK_TIMEOUT_SECONDS = 15.0
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Names whose value decides what a child process loads or runs, or where and how Indus itself runs.
# Writing them from a settings screen would turn that screen into code execution, so the writer
# refuses them; a person who really wants one edits the file by hand.
DENIED_PREFIXES: tuple[str, ...] = ("LD_", "DYLD_", "GIT_CONFIG_")
DENIED_NAMES: frozenset[str] = frozenset(
    {
        # search paths and shells
        "PATH",
        "SHELL",
        "BROWSER",
        "EDITOR",
        "VISUAL",
        "PAGER",
        "MANPAGER",
        # interpreters and toolchains
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "PYTHONEXECUTABLE",
        "PYTHONNOUSERSITE",
        "PYTHONBREAKPOINT",
        "PYTHONCASEOK",
        "NODE_OPTIONS",
        "NODE_PATH",
        "PERL5OPT",
        "PERL5LIB",
        "PERLLIB",
        "RUBYOPT",
        "RUBYLIB",
        "CLASSPATH",
        "JAVA_TOOL_OPTIONS",
        "_JAVA_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "GOFLAGS",
        "RUSTFLAGS",
        # git and askpass helpers
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_ASKPASS",
        "GIT_EDITOR",
        "GIT_SEQUENCE_EDITOR",
        "GIT_PAGER",
        "GIT_EXTERNAL_DIFF",
        "GIT_PROXY_COMMAND",
        "GIT_TEMPLATE_DIR",
        "GIT_EXEC_PATH",
        "GIT_SHELL",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "SSH_ASKPASS",
        "SUDO_ASKPASS",
        # shell and editor start-up hooks
        "BASH_ENV",
        "ENV",
        "ZDOTDIR",
        "PROMPT_COMMAND",
        "VIMINIT",
        "EXINIT",
        # where Indus runs from, and its policy switches
        "INDUS_HOME",
        "INDUS_PROFILE",
        "INDUS_CONFIG",
        "INDUS_CONFIG_PATH",
        "INDUS_ENV",
        "INDUS_ENV_PATH",
        "INDUS_OPTIONAL_MCPS",
        "INDUS_ACP_COMMAND",
        "INDUS_ACP_ARGS",
        "INDUS_YOLO_MODE",
        "INDUS_ACCEPT_HOOKS",
        "INDUS_REDACT_SECRETS",
        "INDUS_INTERACTIVE",
        "INDUS_EXEC_ASK",
        "INDUS_GATEWAY_SESSION",
        "INDUS_CRON_SESSION",
        "INDUS_SINGLE_QUERY_SESSION",
        "INDUS_SESSION_KEY",
        "INDUS_SESSION_PLATFORM",
    }
)

_MAX_REPORTED_POSITIONS = 5


class EnvNameError(ValueError):
    """A name that is not a valid environment variable name."""


class EnvNameDenied(EnvNameError):
    """A name the writer refuses because it steers how processes run."""


@dataclass
class EnvWriteReport:
    """What :func:`set_values` did: names whose line changed, and warnings for the user."""

    changed: list[str] = field(default_factory=list[str])
    warnings: list[str] = field(default_factory=list[str])


def _on_windows(windows: bool | None) -> bool:
    return sys.platform == "win32" if windows is None else windows


def _policy_name(name: str, windows: bool) -> str:
    return name.upper() if windows else name


# -- reading -----------------------------------------------------------------------------------


def _strip_export(text: str) -> str:
    if text.startswith("export") and len(text) > 6 and text[6] in " \t":
        return text[6:].lstrip()
    return text


def _unquote_double(body: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body) and body[i + 1] in '"\\':
            out.append(body[i + 1])
            i += 2
            continue
        if ch == '"':
            break
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_value(raw: str) -> str:
    text = raw.lstrip(" \t")
    if text.startswith('"'):
        return _unquote_double(text[1:])
    if text.startswith("'"):
        end = text.find("'", 1)
        return text[1:] if end < 0 else text[1:end]
    if raw[: len(raw) - len(text)] and text.startswith("#"):
        return ""  # "K= # note": whitespace then '#' is a comment
    for i in range(1, len(text)):
        if text[i] == "#" and text[i - 1] in " \t":
            text = text[:i]
            break
    return text.strip()


def parse_line(line: str) -> tuple[str, str] | None:
    """Return ``(name, value)`` when ``line`` is an assignment, else ``None``."""
    text = line.strip().lstrip("﻿")
    if not text or text.startswith("#"):
        return None
    text = _strip_export(text)
    name, sep, raw = text.partition("=")
    if not sep:
        return None
    name = name.strip()
    if not NAME_RE.match(name):
        return None
    return name, _parse_value(raw)


def _decode(data: bytes, source: str) -> str:
    if b"\x00" in data:
        log.warning("removed NUL bytes from %s", source)
        data = data.replace(b"\x00", b"")
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8", errors="replace")


def parse_text(text: str) -> dict[str, str]:
    """Parse ``.env`` text; a name assigned twice keeps its last value."""
    values: dict[str, str] = {}
    for line in text.lstrip("﻿").splitlines():
        parsed = parse_line(line.replace("\x00", ""))
        if parsed is not None:
            values[parsed[0]] = parsed[1]
    return values


def read_env_file(path: Path) -> dict[str, str]:
    """Read ``path`` into a dict; a missing file reads as empty."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return {}
    return parse_text(_decode(data, str(path)))


# -- writing -----------------------------------------------------------------------------------


def is_denied(name: str, *, windows: bool | None = None) -> bool:
    """True when the writer refuses ``name`` (case-insensitive on Windows)."""
    win = _on_windows(windows)
    policy = _policy_name(name, win)
    if win:
        return policy in DENIED_NAMES or policy.startswith(DENIED_PREFIXES)
    return name in DENIED_NAMES or name.startswith(DENIED_PREFIXES)


def check_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise EnvNameError(f"{name!r} is not a valid environment variable name")


def check_writable_name(name: str, *, windows: bool | None = None) -> None:
    """Raise unless ``name`` may be written through the settings writer."""
    check_name(name)
    if is_denied(name, windows=windows):
        raise EnvNameDenied(
            f"{name} changes how programs start or where Indus runs, so it cannot be saved from "
            "here. If you really need it, edit the .env file by hand."
        )


def clean_value(name: str, value: str) -> tuple[str, str | None]:
    """Strip line breaks and non-ASCII characters; return the value and an optional warning."""
    value = value.replace("\r", "").replace("\n", "")
    positions = [i for i, ch in enumerate(value) if ord(ch) > 127]
    if not positions:
        return value, None
    shown = ", ".join(str(i) for i in positions[:_MAX_REPORTED_POSITIONS])
    more = (
        f" and {len(positions) - _MAX_REPORTED_POSITIONS} more"
        if len(positions) > _MAX_REPORTED_POSITIONS
        else ""
    )
    warning = (
        f"{name}: removed non-ASCII characters at positions {shown}{more}; "
        "if the key stops working, copy it again from the provider"
    )
    return "".join(ch for ch in value if ord(ch) <= 127), warning


def quote_value(value: str) -> str:
    """Serialise a value so :func:`parse_line` reads it back unchanged."""
    if value == "":
        return ""
    if any(ch in value for ch in "#\"'") or any(ch.isspace() for ch in value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def keys_record_path(path: Path) -> Path:
    """Side file listing the names this writer has written into ``path`` (names only)."""
    return path.with_name(path.name + ".keys")


def read_written_names(path: Path) -> set[str]:
    """Names recorded by :func:`set_values` for the env file at ``path``."""
    try:
        text = keys_record_path(path).read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
        return set()
    return {line.strip() for line in text.splitlines() if NAME_RE.match(line.strip())}


def _defines(line: str, name: str, windows: bool) -> bool:
    parsed = parse_line(line)
    return parsed is not None and _policy_name(parsed[0], windows) == _policy_name(name, windows)


def _apply(lines: list[str], name: str, rendered: str | None, windows: bool) -> list[str]:
    out: list[str] = []
    placed = False
    for line in lines:
        if not _defines(line, name, windows):
            out.append(line)
            continue
        if rendered is not None and not placed:
            ending = "\r\n" if line.endswith("\r\n") else "\n"
            out.append(rendered + ending)
            placed = True
    if rendered is not None and not placed:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append(rendered + "\n")
    return out


def set_values(
    path: Path,
    values: Mapping[str, str | None],
    *,
    windows: bool | None = None,
) -> EnvWriteReport:
    """Set (``str``) or remove (``None``) names in the env file at ``path``.

    Existing assignment lines of every shape the reader accepts are replaced in place; removals
    drop every line that defines the name. Other lines keep their bytes. The whole
    read-modify-write runs under ``<path>.lock``.
    """
    win = _on_windows(windows)
    report = EnvWriteReport()
    rendered: dict[str, str | None] = {}
    for name, value in values.items():
        if value is None:
            check_name(name)
            rendered[name] = None
            continue
        check_writable_name(name, windows=win)
        cleaned, warning = clean_value(name, value)
        if warning:
            log.warning("%s", warning)
            report.warnings.append(warning)
        rendered[name] = f"{name}={quote_value(cleaned)}"

    lock_path = path.with_name(path.name + ".lock")
    with FileLock(lock_path, timeout=LOCK_TIMEOUT_SECONDS):
        try:
            original = _decode(path.read_bytes(), str(path))
        except FileNotFoundError:
            original = ""
        lines = original.splitlines(keepends=True)
        for name, line in rendered.items():
            updated = _apply(lines, name, line, win)
            if updated != lines:
                report.changed.append(name)
            lines = updated
        text = "".join(lines)
        if text != original:
            atomic_write_bytes(path, text.encode("utf-8"))
        _record_names(path, (n for n, v in rendered.items() if v is not None))
    return report


def _record_names(path: Path, names: Iterable[str]) -> None:
    new = set(names)
    known = read_written_names(path)
    if new <= known:
        return
    body = "".join(f"{name}\n" for name in sorted(known | new))
    atomic_write_bytes(keys_record_path(path), body.encode("utf-8"))


def set_value(path: Path, name: str, value: str | None) -> EnvWriteReport:
    """Shorthand for one name."""
    return set_values(path, {name: value})


def custom_key_name(slug: str) -> str:
    """Env name holding the API key of a custom endpoint identified by ``slug``."""
    core = re.sub(r"[^A-Z0-9]+", "_", (slug or "").upper()).strip("_")
    return f"INDUS_CUSTOM_{core}_API_KEY" if core else "INDUS_CUSTOM_API_KEY"
