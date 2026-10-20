"""Media tags in replies and the outbound path policy (P15-18, R06.22).

The agent attaches files by writing ``MEDIA:<path>`` (quote the path when it has spaces), adds
``[[audio_as_voice]]`` or ``[[as_document]]`` to change how they are sent, or embeds image
Markdown ``![alt](target)``. ``extract_media`` pulls these out of the reply text (never from
fenced or inline code) and removes them.

The path comes from the model, or from text the model was fed, so delivery is a security
boundary. ``MediaPolicy.check`` delivers a local file only when, after every symlink is resolved:

- it is a regular file that passes the P05 read checks (no device or ``/proc`` file, no Indus
  secret store or control file, no secret env file) and is outside the deny-list of credential
  and system locations (both the path as written and the resolved path are checked);
- and it lives in a trusted place: the profile's gateway caches, an operator-listed folder, or —
  only when ``gateway.media_trust_recent`` is on (off by default; lead decision, D-16) and not in
  strict mode — it was modified in the last ``recent_seconds`` (600 by default). Linux keeps no
  birth time, so "recent" is the modification time, which any process can bump with ``touch``.

Strict mode trusts the listed folders only. Files inside a remote terminal sandbox are copied
into the document cache only after the sandbox path passes the same deny-list before and after
the sandbox resolves it; strict mode never fetches. Each refusal becomes a short notice.

The policy lives below the surfaces (layer rule D-16.246): the gateway delivers with it and
``indus mcp serve`` (P19-14) checks the paths it reports with it.
"""

from __future__ import annotations

import os
import posixpath
import re
import secrets
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import ValidationError

from indus.foundation import config as config_mod
from indus.foundation.config.schema import GatewaySection
from indus.foundation.home import secure_dir
from indus.foundation.scope import Scope
from indus.safety.path_guards import (
    INDUS_CONTROL_FILES,
    INDUS_SECRET_FILES,
    blocked_device,
    homes_for,
    indus_dirs,
    nt_namespace_error,
    read_denial,
)

__all__ = [
    "CACHE_DIRS",
    "FETCH_MAX_BYTES",
    "MediaItem",
    "MediaKind",
    "MediaPolicy",
    "MediaRef",
    "RemoteFiles",
    "Verdict",
    "extract_media",
    "fetch_remote",
    "gateway_section",
    "screen_remote_path",
    "strip_media_tags",
]

type MediaKind = Literal["image", "video", "audio", "voice", "document"]

CACHE_DIRS = ("images", "audio", "video", "documents")
FETCH_MAX_BYTES = 50 * 1024 * 1024
RECENT_SECONDS_DEFAULT = 600.0
_FUTURE_SKEW = 60.0

_IMAGE = frozenset([".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"])
_VIDEO = frozenset([".mp4", ".mov", ".webm", ".mkv", ".avi", ".3gp", ".m4v"])
_AUDIO = frozenset([".mp3", ".wav", ".ogg", ".opus", ".m4a", ".flac", ".aac"])

SYSTEM_PREFIXES = (
    "/etc",
    "/proc",
    "/sys",
    "/dev",
    "/boot",
    "/root",
    "/run",
    "/var/run",
    "/var/log",
    "/var/lib",
    "/private/etc",
    "/private/var",
)
HOME_DENIED = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    ".docker",
    ".azure",
    ".config",
    ".gcloud",
    ".netrc",
    ".password-store",
    ".local/share/keyrings",
    "Library/Keychains",
)
_STORE_FILES = tuple(f"state.db{suffix}" for suffix in ("", "-wal", "-shm", "-journal"))
INDUS_DENIED = (
    *INDUS_SECRET_FILES,
    *INDUS_CONTROL_FILES,
    *_STORE_FILES,
    "pairing",
    "sessions",
    "platforms",
    "auth",
    "credentials",
    "backups",
    "cache/browser",
)


def gateway_section(scope: Scope) -> GatewaySection:
    """The profile's ``gateway`` section, schema defaults filled in (an invalid section gives the
    built-in settings)."""
    view = scope.config if scope.config is not None else config_mod.load(scope)
    raw = view.get("gateway")
    try:
        return GatewaySection.model_validate(dict(raw) if isinstance(raw, Mapping) else {})
    except ValidationError:
        return GatewaySection()


# -- extraction ------------------------------------------------------------------------------------

_FENCED = re.compile(r"^[^\n]*```[^\n]*\n.*?^\s*```[^\n]*$", re.DOTALL | re.MULTILINE)
_OPEN_FENCE = re.compile(r"^[ \t]*```", re.MULTILINE)
_INLINE_CODE = re.compile(r"(`+)[^\n]*?\1")
# Upper case only: "Social media: /r/python" in prose is not a directive.
_TAG = re.compile(
    r"[*_\"']{0,3}MEDIA:[ \t]*"
    r"(?P<path>\"[^\"\n]+\"|'[^'\n]+'|(?:~/|/)(?:(?!MEDIA:)\S)+)"
    r"[*_\"']{0,3}"
)
_IMAGE_MD = re.compile(r"!\[[^\]\n]*\]\(\s*<?(?P<target>[^)\s>]+)>?\s*\)")
_VOICE_FLAG = "[[audio_as_voice]]"
_DOCUMENT_FLAG = "[[as_document]]"
_TRAILING = ".,;:!?)]}*_\"'"
_UNSAFE_NAME = re.compile(r"[\x00-\x1f\x7f\u0085  ]")


@dataclass(frozen=True)
class MediaRef:
    """One attachment the reply asked for: a path as written, or an http(s) URL."""

    raw: str
    is_url: bool = False
    as_voice: bool = False
    as_document: bool = False

    @property
    def kind(self) -> MediaKind:
        suffix = posixpath.splitext(self.raw.split("?", 1)[0])[1].lower()
        if self.as_document:
            return "document"
        if suffix in _IMAGE:
            return "image"
        if suffix in _VIDEO:
            return "video"
        if suffix in _AUDIO:
            return "voice" if self.as_voice else "audio"
        return "document"

    @property
    def name(self) -> str:
        base = posixpath.basename(self.raw.rstrip("/")) or self.raw
        return _UNSAFE_NAME.sub("?", base)[:80]


@dataclass(frozen=True)
class MediaItem:
    """An attachment cleared for sending: the host path (or URL) and how to send it."""

    target: str
    kind: MediaKind
    name: str


def _blank(chars: list[str], start: int, end: int) -> None:
    for i in range(start, end):
        if chars[i] != "\n":
            chars[i] = " "


def _mask(text: str) -> str:
    """``text`` with fenced and inline code blanked (offsets and newlines kept); a fence left
    open runs to the end."""
    chars = list(text)
    for match in _FENCED.finditer(text):
        _blank(chars, *match.span())
    unclosed = _OPEN_FENCE.search("".join(chars))
    if unclosed is not None:
        _blank(chars, unclosed.start(), len(chars))
    for match in _INLINE_CODE.finditer("".join(chars)):
        _blank(chars, *match.span())
    return "".join(chars)


def _clean_path(raw: str) -> str:
    path = raw.strip()
    if len(path) >= 2 and path[0] == path[-1] and path[0] in "\"'":
        return path[1:-1].strip()
    return path.rstrip(_TRAILING)


def extract_media(text: str) -> tuple[str, list[MediaRef]]:
    """The reply with its media directives removed, and the attachments they named."""
    if not text or ("MEDIA:" not in text and "![" not in text and "[[" not in text):
        return text, []
    masked = _mask(text)
    as_voice = _VOICE_FLAG in masked
    as_document = _DOCUMENT_FLAG in masked
    found: list[tuple[int, int, MediaRef]] = []
    for match in _TAG.finditer(masked):
        path = _clean_path(text[match.start("path") : match.end("path")])
        if path:
            ref = MediaRef(path, as_voice=as_voice, as_document=as_document)
            found.append((match.start(), match.end(), ref))
    for match in _IMAGE_MD.finditer(masked):
        target = text[match.start("target") : match.end("target")]
        is_url = target.lower().startswith(("https://", "http://"))
        if is_url or target.startswith(("/", "~/")):
            ref = MediaRef(target, is_url=is_url, as_document=as_document)
            found.append((match.start(), match.end(), ref))
    spans = [(s, e) for s, e, _ in found]
    for flag in (_VOICE_FLAG, _DOCUMENT_FLAG):
        start = masked.find(flag)
        while start >= 0:
            spans.append((start, start + len(flag)))
            start = masked.find(flag, start + 1)
    if not spans:
        return text, []
    out: list[str] = []
    last = 0
    for start, end in sorted(spans):
        if start >= last:
            out.append(text[last:start])
            last = end
        else:
            last = max(last, end)
    out.append(text[last:])
    cleaned = re.sub(r"[ \t]+\n", "\n", "".join(out))
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    refs = [ref for _, _, ref in sorted(found, key=lambda item: item[0])]
    return cleaned, refs


def strip_media_tags(text: str) -> str:
    """Display form of streamed text: directives removed, nothing resolved."""
    return extract_media(text)[0]


# -- the local policy ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """``path`` when the file may be sent, else None with a short ``reason``."""

    path: str | None
    reason: str = ""
    not_found: bool = False

    @property
    def ok(self) -> bool:
        return self.path is not None


def _refuse(reason: str, *, not_found: bool = False) -> Verdict:
    return Verdict(None, reason, not_found)


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _real(path: Path | str) -> str:
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return os.path.normpath(path)


def _indus_secret(path: str, scope: Scope) -> bool | None:
    """Inside an Indus root or profile: whether ``path`` is one of its secret or history stores
    (any profile under the root, not just this one); None when ``path`` is not inside one."""
    for base in indus_dirs(scope):
        if not _under(path, base) or path == base:
            continue
        rel = path[len(base.rstrip("/")) + 1 :]
        parts = rel.split("/")
        if len(parts) > 2 and parts[0] == "profiles":
            rel = "/".join(parts[2:])
        return any(rel == name or rel.startswith(name + "/") for name in INDUS_DENIED)
    return None


def _denied(path: str, scope: Scope) -> bool:
    inside = _indus_secret(path, scope)
    if inside is not None:
        # our own homes may live under a denied prefix (a root-run gateway, an XDG layout):
        # inside them only their secret stores are denied
        return inside
    homes = homes_for(scope)
    for prefix in SYSTEM_PREFIXES:
        own_home = any(_under(home, prefix) and _under(path, home) for home in homes)
        if _under(path, prefix) and not own_home:
            return True
    return any(_under(path, posixpath.join(home, rel)) for home in homes for rel in HOME_DENIED)


@dataclass(frozen=True)
class MediaPolicy:
    scope: Scope
    safe_roots: tuple[Path, ...]
    extra_dirs: tuple[Path, ...] = ()
    strict: bool = False
    trust_recent: bool = False
    recent_seconds: float = RECENT_SECONDS_DEFAULT

    @classmethod
    def for_scope(
        cls, scope: Scope, settings: GatewaySection | Mapping[str, Any] | None = None
    ) -> MediaPolicy:
        """The policy for a profile from its ``gateway`` section (``media_strict``,
        ``media_allow_dirs``, ``media_trust_recent``, ``media_trust_recent_seconds``); with no
        ``settings`` the profile's config is read. Unset rows take the schema defaults."""
        if settings is None:
            section = gateway_section(scope)
        elif isinstance(settings, GatewaySection):
            section = settings
        else:
            try:
                section = GatewaySection.model_validate(dict(settings))
            except ValidationError:
                section = GatewaySection()
        extra: list[Path] = []
        for raw in section.media_allow_dirs:
            text = str(raw)
            if text.startswith("~/"):
                text = str(Path(scope.env.get("HOME") or Path.home()) / text[2:])
            if os.path.isabs(text):
                extra.append(Path(text))
        return cls(
            scope=scope,
            safe_roots=tuple(scope.home / "cache" / name for name in CACHE_DIRS),
            extra_dirs=tuple(extra),
            strict=section.media_strict,
            trust_recent=section.media_trust_recent,
            recent_seconds=max(0.0, section.media_trust_recent_seconds),
        )

    @property
    def document_cache(self) -> Path:
        return self.scope.home / "cache" / "documents"

    def _expand(self, raw: str) -> str | None:
        if raw == "~" or raw.startswith("~/"):
            home = self.scope.env.get("HOME") or str(Path.home())
            return home + raw[1:]
        if raw.startswith("~"):
            return None  # another user's home
        return raw

    def _recent(self, info: os.stat_result) -> bool:
        if self.strict or not self.trust_recent or self.recent_seconds <= 0:
            return False
        born = getattr(info, "st_birthtime", None)
        made = born if isinstance(born, int | float) and born > 0 else info.st_mtime
        age = self.scope.clock.now().timestamp() - made
        return -_FUTURE_SKEW <= age <= self.recent_seconds

    def check(self, raw: str) -> Verdict:
        """Whether the local file ``raw`` (as the reply wrote it) may be sent."""
        text = _clean_path(raw)
        if not text or "\x00" in text or nt_namespace_error(text) is not None:
            return _refuse("not a usable path")
        expanded = self._expand(text)
        if expanded is None or not os.path.isabs(expanded):
            return _refuse("not an absolute path")
        literal = os.path.normpath(expanded)
        if blocked_device(literal):
            return _refuse("not allowed")
        try:
            resolved = os.path.realpath(literal, strict=True)
            info = os.stat(resolved)
        except (OSError, ValueError):
            return _refuse("file not found", not_found=True)
        if not stat.S_ISREG(info.st_mode):
            return _refuse("not a regular file")
        scope = self.scope
        if _denied(literal, scope) or _denied(resolved, scope):
            return _refuse("not allowed")
        if read_denial(resolved, scope) is not None or read_denial(literal, scope) is not None:
            return _refuse("not allowed")
        roots = (*self.safe_roots, *self.extra_dirs)
        if any(_under(resolved, _real(root)) for root in roots):
            return Verdict(resolved)
        if self._recent(info):
            return Verdict(resolved)
        return _refuse("not in a folder media may be sent from")

    async def resolve(
        self, refs: Sequence[MediaRef], remote: RemoteFiles | None = None
    ) -> tuple[list[MediaItem], list[str]]:
        """Cleared attachments, and one short notice per refused one."""
        items: list[MediaItem] = []
        notices: list[str] = []
        for ref in refs:
            if ref.is_url:
                items.append(MediaItem(ref.raw, ref.kind, ref.name))
                continue
            verdict = self.check(ref.raw)
            if not verdict.ok and verdict.not_found and remote is not None:
                verdict = await fetch_remote(self, remote, ref.raw)
            if verdict.path is not None:
                items.append(MediaItem(verdict.path, ref.kind, ref.name))
            else:
                notices.append(f"Could not attach {ref.name}: {verdict.reason}.")
        return items, notices


# -- remote sandboxes ------------------------------------------------------------------------------


class RemoteFiles(Protocol):
    """A non-local terminal backend's file access (P11 backends)."""

    @property
    def home(self) -> str | None: ...

    async def realpath(self, path: str) -> str | None: ...

    async def fetch(self, path: str, dest: Path, max_bytes: int) -> None: ...


_REMOTE_HOME_DENIED = (*HOME_DENIED, *(f".indus/{name}" for name in INDUS_DENIED))


def screen_remote_path(path: str, home: str | None) -> bool:
    """True when the sandbox path ``path`` may be fetched (pure text check; the sandbox cannot be
    stat'ed from here). With no known sandbox home, home-relative entries match anywhere."""
    if not path.startswith("/") or "\x00" in path:
        return False
    target = posixpath.normpath(path)
    if any(_under(target, prefix) for prefix in SYSTEM_PREFIXES):
        return False
    if home:
        base = posixpath.normpath(home)
        return not any(_under(target, posixpath.join(base, rel)) for rel in _REMOTE_HOME_DENIED)
    parts = target.split("/")
    for rel in _REMOTE_HOME_DENIED:
        rel_parts = rel.split("/")
        for i in range(len(parts) - len(rel_parts) + 1):
            if parts[i : i + len(rel_parts)] == rel_parts:
                return False
    return True


async def fetch_remote(policy: MediaPolicy, remote: RemoteFiles, raw: str) -> Verdict:
    """Copy a sandbox file into the document cache after screening its path before and after the
    sandbox resolves it, then check the copy like any local file."""
    if policy.strict:
        return _refuse("file not found", not_found=True)
    path = _clean_path(raw)
    home = remote.home
    if path == "~" or path.startswith("~/"):
        if not home:
            return _refuse("not allowed")
        path = posixpath.join(home, path[2:])
    if not screen_remote_path(path, home):
        return _refuse("not allowed")
    try:
        resolved = await remote.realpath(posixpath.normpath(path))
    except Exception:
        resolved = None
    if not resolved or not screen_remote_path(resolved, home):
        return _refuse("not allowed")
    cache = secure_dir(policy.document_cache, env=policy.scope.env)
    name = re.sub(r"[^\w.\-]", "_", posixpath.basename(resolved)) or "file"
    dest = cache / f"remote_{secrets.token_hex(6)}_{name}"
    try:
        await remote.fetch(resolved, dest, FETCH_MAX_BYTES)
    except Exception:
        dest.unlink(missing_ok=True)
        return _refuse("could not be fetched from the sandbox")
    verdict = policy.check(str(dest))
    if not verdict.ok:
        dest.unlink(missing_ok=True)
    return verdict
