"""What the media tools share: settings and secrets, their folders, and the output path rules.

Folders (under the profile home, created owner-only): ``cache/media`` (generated images and
videos), ``cache/audio`` (speech), ``cache/vision`` (scratch), ``voice-memos`` (speech made on the
CLI). ``sandbox_path`` gives the in-sandbox name of a cached file when the terminal runs in Docker.

``checked_output_path`` is the producing side of the media path policy (R06.22): a path a caller
asked us to write must be plain text (no control characters, no ``..`` part, no embedded
``MEDIA:`` directive that a delivery scanner would take for a second attachment) and, after
resolving symlinks, may not land on a credential folder, a system path or an Indus secret or
control file. What we hand back in a ``MEDIA:`` line is always that resolved path.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from indus.foundation.config.schema import default_for
from indus.foundation.home import secure_dir
from indus.foundation.scope import Scope
from indus.safety.path_guards import write_denial, write_needs_approval

__all__ = [
    "AUDIO_DIR",
    "MEDIA_DIR",
    "VISION_DIR",
    "VOICE_MEMOS_DIR",
    "OutputRefused",
    "checked_output_path",
    "folder",
    "json_text",
    "media_line",
    "sandbox_path",
    "secret",
    "setting",
    "unique_name",
]

log = logging.getLogger(__name__)

MEDIA_DIR = Path("cache") / "media"
AUDIO_DIR = Path("cache") / "audio"
VISION_DIR = Path("cache") / "vision"
VOICE_MEMOS_DIR = Path("voice-memos")
SANDBOX_HOME = {"docker": "/root/.indus"}

# A ``MEDIA:`` directive followed by something path-like or quoted (case-insensitive).
_DIRECTIVE = re.compile(r"media:\s*[`'\"*_]*(?:[`'\"]|[a-z]:[/\\]|~?/)", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f  ]")


def setting(scope: Scope | None, key: str, fallback: Any = None) -> Any:
    """The profile's value at ``key``, else the schema default, else ``fallback``."""
    value: Any = None
    if scope is not None and scope.config is not None:
        try:
            value = scope.config.get(key)
        except Exception:
            log.debug("config %s unreadable", key, exc_info=True)
    if value is None:
        value = default_for(key, fallback)
    return fallback if value is None else value


def setting_text(scope: Scope | None, key: str, fallback: str = "") -> str:
    value = setting(scope, key, fallback)
    return value.strip() if isinstance(value, str) else fallback


def setting_number(scope: Scope | None, key: str, fallback: float) -> float:
    value = setting(scope, key, fallback)
    if isinstance(value, bool):
        return fallback
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def secret(scope: Scope | None, name: str) -> str:
    """A key from the profile's secret store, else its environment; ``""`` when unset."""
    if scope is None or not name:
        return ""
    value: str | None = None
    if scope.secrets is not None:
        try:
            value = scope.secrets.get(name)
        except Exception:
            log.debug("secret %s unreadable", name, exc_info=True)
    if not value:
        value = scope.env.get(name)
    return (value or "").strip()


def folder(scope: Scope, relative: Path) -> Path:
    return secure_dir(scope.home / relative, home=scope.home, env=scope.env)


def unique_name(prefix: str, extension: str, scope: Scope | None = None) -> str:
    """``<prefix>-<utc stamp>-<random>.<ext>``; the stamp comes from the profile's clock."""
    stamp = ""
    if scope is not None:
        try:
            stamp = scope.clock.now().strftime("%Y%m%d-%H%M%S") + "-"
        except Exception:
            stamp = ""
    return f"{prefix}-{stamp}{secrets.token_hex(4)}.{extension.lstrip('.')}"


def sandbox_path(scope: Scope, host_path: Path) -> str | None:
    """Where ``host_path`` (inside the profile home) sits for a container terminal, else None."""
    backend = setting_text(scope, "terminal.backend", "local").lower()
    base = SANDBOX_HOME.get(backend)
    if base is None:
        return None
    try:
        relative = host_path.resolve().relative_to(scope.home.resolve())
    except ValueError:
        return None
    return f"{base}/{relative.as_posix()}"


class OutputRefused(ValueError):
    """A caller-chosen output path broke the media path policy (the message says how)."""


def checked_output_path(raw: str, scope: Scope, *, base: Path | None = None) -> Path:
    """``raw`` resolved and checked for writing media to (see the module notes)."""
    if not raw.strip():
        raise OutputRefused("output_path is empty")
    if _CONTROL.search(raw):
        raise OutputRefused("output_path contains control characters or line breaks")
    if _DIRECTIVE.search(raw):
        raise OutputRefused("output_path must not contain a MEDIA: directive")
    parts = re.split(r"[\\/]", raw)
    if ".." in parts:
        raise OutputRefused("output_path must not contain '..'; give a direct path")
    text = raw.strip()
    if text.startswith("~"):
        home = scope.env.get("HOME")
        if not home:
            raise OutputRefused("output_path starts with ~ but no home folder is known here")
        text = home + text[1:]
    path = Path(text)
    if not path.is_absolute():
        path = (base or Path.cwd()) / path
    resolved = Path(os.path.realpath(path))
    reason = write_denial([str(resolved)], scope) or write_needs_approval([str(resolved)], scope)
    if reason:
        raise OutputRefused(f"output_path is a protected location: {reason}")
    return resolved


def media_line(path: Path) -> str:
    return f"MEDIA:{path}"


def json_text(body: Mapping[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=False)


def error_text(message: str, **extra: Any) -> str:
    return json_text({"success": False, "error": message, **extra})
