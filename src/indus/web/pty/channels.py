"""Chat channels and their active-session files; the working directory a fresh chat may pick
(P18-08).

A channel id (``[A-Za-z0-9._-]{1,128}``) pairs one Chat tab's PTY with its event feed. Per channel
the backend keeps one small file the TUI writes ``{"session_id": …}`` into whenever its live
session changes; a later socket on the same channel without ``resume`` reopens that session.

``ChannelFiles`` keeps those files in one private directory (0700) it creates (a temp directory
made on first use when none is given, so a backend that never opens a chat makes none); each file is
created 0600 with ``O_EXCL | O_NOFOLLOW``. ``claim(channel)`` returns the channel's file and
whether it already existed (a file that existed may belong to a live PTY on that channel; only a
file this socket created is its own to drop on failure). ``read`` answers the stored session id
(None for a missing, unreadable or malformed file), ``clear`` empties the channel (``fresh``),
``release(channel, path)`` forgets it only while ``path`` is still the channel's file.

``resolve_cwd(raw)``: a folder picked for a fresh chat must be an absolute path to an existing
directory; anything else raises ``CwdRefused`` (the chat never falls back to the dashboard's own
launch directory).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Final

__all__ = [
    "CHANNEL_PATTERN",
    "ChannelFiles",
    "CwdRefused",
    "resolve_cwd",
    "valid_channel",
]

CHANNEL_PATTERN: Final = re.compile(r"[A-Za-z0-9._-]{1,128}")
_MAX_FILE_BYTES: Final = 4096


def valid_channel(value: object) -> str | None:
    """``value`` when it is a usable channel id, else None."""
    if isinstance(value, str) and CHANNEL_PATTERN.fullmatch(value):
        return value
    return None


class CwdRefused(ValueError):
    """The picked working directory cannot be used; the message is shown to the person."""


def resolve_cwd(raw: object) -> Path | None:
    """The working directory a fresh chat asked for (None when it asked for none)."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise CwdRefused("The chosen folder is not a path.")
    text = raw.strip()
    if not text:
        return None
    if "\x00" in text:
        raise CwdRefused("The chosen folder is not a valid path.")
    path = Path(text)
    if not path.is_absolute():
        raise CwdRefused("The chosen folder must be an absolute path.")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise CwdRefused("The chosen folder does not exist.") from None
    if not resolved.is_dir():
        raise CwdRefused("The chosen folder is not a directory.")
    return resolved


class ChannelFiles:
    """The active-session files of every channel this backend has seen (see the module notes)."""

    def __init__(self, directory: Path | None = None) -> None:
        self._owns_dir = directory is None
        self._directory = directory
        self._dir_lock = threading.Lock()
        if directory is not None:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(directory, 0o700)
        self._lock = threading.Lock()
        self._files: dict[str, Path] = {}
        self._serial = 0

    @property
    def directory(self) -> Path:
        """The private directory (an owned temp directory is made on first use)."""
        with self._dir_lock:
            if self._directory is None:
                made = Path(tempfile.mkdtemp(prefix="indus-web-chat-"))
                with contextlib.suppress(OSError):
                    os.chmod(made, 0o700)
                self._directory = made
            return self._directory

    def path(self, channel: str) -> Path | None:
        """The channel's file, when it has one."""
        with self._lock:
            return self._files.get(channel)

    def known(self, channel: str) -> bool:
        return self.path(channel) is not None

    def claim(self, channel: str) -> tuple[Path, bool]:
        """``(file, existed)`` for ``channel``, creating an empty owner-only file when needed."""
        if valid_channel(channel) is None:
            raise ValueError("not a channel id")
        with self._lock:
            found = self._files.get(channel)
            if found is not None and found.is_file():
                return found, True
            self._serial += 1
            path = self.directory / f"{self._serial:06d}-{channel}.json"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            os.close(os.open(path, flags, 0o600))
            self._files[channel] = path
            return path, False

    def read(self, channel: str) -> str | None:
        """The session id the channel's TUI last reported."""
        path = self.path(channel)
        if path is None:
            return None
        try:
            with path.open("rb") as handle:
                raw = handle.read(_MAX_FILE_BYTES + 1)
        except OSError:
            return None
        if not raw or len(raw) > _MAX_FILE_BYTES:
            return None
        try:
            data: object = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            return None
        value: object = data.get("session_id") if isinstance(data, dict) else None  # pyright: ignore[reportUnknownMemberType]
        if not isinstance(value, str):
            return None
        return value.strip() or None

    def clear(self, channel: str) -> None:
        """Forget the channel's session (``fresh``)."""
        with self._lock:
            path = self._files.pop(channel, None)
        if path is not None:
            with contextlib.suppress(OSError):
                path.unlink()

    def release(self, channel: str, path: Path) -> None:
        """Remove ``path`` and forget the channel while ``path`` is still its file."""
        with contextlib.suppress(OSError):
            path.unlink()
        with self._lock:
            if self._files.get(channel) == path:
                del self._files[channel]

    def close(self) -> None:
        """Remove every file (backend stop)."""
        with self._lock:
            self._files.clear()
        with self._dir_lock:
            directory = self._directory
        if directory is None:
            return
        if self._owns_dir:
            shutil.rmtree(directory, ignore_errors=True)
        else:
            for entry in directory.glob("*.json"):
                with contextlib.suppress(OSError):
                    entry.unlink()
