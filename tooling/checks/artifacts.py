"""Clean-room sweep of built artefacts (P25-19): the source guards, run over what we ship.

``python -m tooling.checks artifacts --artifact PATH [--artifact PATH …]`` (or
``INDUS_ARTIFACTS`` holding paths separated by the OS path separator) walks each built output:

* a folder — the web ``dist``, the built docs site, an unpacked desktop release
  (``linux-unpacked``, ``*.app``, ``win-unpacked``), generated installers;
* an ASAR archive (Electron's ``app.asar``), read with our own small reader, members included;
* a tar archive, compressed or not — ``docker save`` output: the manifest, the image config (its
  labels) and every layer, layer tars recursed;
* a zip archive (``.zip``, ``.msix``, ``.appx``, ``.nupkg``) and any other single file.

Every member's path and every text member are matched against the banned identifiers
(``banned_patterns.json``: reference names, vendor hosts, brand fonts); every media member is
hashed and compared with the reference's media (``assets``, when the reference checkout is
present); and text members must not hold the building machine's home folder (R10.29: personal
data never ships). ``artifacts_allow.json`` lists member globs that may match, each with a reason.
Nothing named is not a failure (a note says so); the release gate names its outputs.
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import os
import re
import struct
import sys
import tarfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from tooling.checks import assets, banned, similarity
from tooling.checks.common import CHECKS_DIR, Context, Result

NAME = "artifacts"
ENV = "INDUS_ARTIFACTS"
ALLOW_FILE = CHECKS_DIR / "artifacts_allow.json"
TEXT_LIMIT = 32 * 1024 * 1024  # members larger than this are hashed and path-checked only
ZIP_SUFFIXES = (".zip", ".msix", ".appx", ".nupkg", ".msixbundle")
TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar.zst")
MAX_DEPTH = 4


@dataclass(frozen=True)
class Member:
    where: str  # display path: the artefact, then `!`-separated archive members
    data: bytes


class AsarError(ValueError):
    pass


def read_asar(data: bytes) -> Iterator[tuple[str, bytes]]:
    """(path, bytes) for every packed file of an ASAR archive; unpacked files live beside it."""
    try:
        size_len, header_size = struct.unpack_from("<II", data, 0)
        payload_size, json_len = struct.unpack_from("<II", data, 8)
    except struct.error as exc:
        raise AsarError(f"too short for an ASAR header: {exc}") from exc
    if size_len != 4 or json_len > payload_size or 16 + json_len > len(data):
        raise AsarError("not an ASAR archive")
    try:
        header = json.loads(data[16 : 16 + json_len].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AsarError(f"unreadable ASAR header: {exc}") from exc
    base = 8 + header_size

    def walk(node: dict[str, object], prefix: str) -> Iterator[tuple[str, bytes]]:
        files = node.get("files")
        if not isinstance(files, dict):
            return
        for name, entry in sorted(files.items()):
            if not isinstance(entry, dict):
                continue
            path = f"{prefix}{name}"
            if "files" in entry:
                yield from walk(entry, f"{path}/")
            elif "link" in entry or entry.get("unpacked"):
                continue
            else:
                start = base + int(str(entry.get("offset", "0")))
                yield path, data[start : start + int(str(entry.get("size", 0)))]

    yield from walk(header, "")


def _is_tar(name: str, data: bytes) -> bool:
    if name.lower().endswith(TAR_SUFFIXES):
        return True
    return len(data) > 262 and data[257:262] == b"ustar"


def _is_zip(name: str, data: bytes) -> bool:
    return name.lower().endswith(ZIP_SUFFIXES) or data[:4] == b"PK\x03\x04"


def _tar_members(where: str, tar: tarfile.TarFile, depth: int) -> Iterator[Member]:
    for info in tar:
        if not info.isfile():
            yield Member(f"{where}!{info.name}", b"")  # the path still counts
            continue
        handle = tar.extractfile(info)
        body = handle.read() if handle is not None else b""
        yield from expand(f"{where}!{info.name}", body, depth + 1)


def expand(where: str, data: bytes, depth: int = 0) -> Iterator[Member]:
    """The member itself, and when it is an archive, everything inside it."""
    yield Member(where, data)
    if depth >= MAX_DEPTH:
        return
    name = where.rsplit("!", 1)[-1]
    low = name.lower()
    try:
        if low.endswith(".asar"):
            for path, body in read_asar(data):
                yield from expand(f"{where}!{path}", body, depth + 1)
        elif _is_tar(name, data):
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
                yield from _tar_members(where, tar, depth)
        elif _is_zip(name, data):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    yield from expand(f"{where}!{info.filename}", archive.read(info), depth + 1)
    except (AsarError, tarfile.TarError, zipfile.BadZipFile, OSError, EOFError) as exc:
        yield Member(f"{where}!(unreadable: {type(exc).__name__}: {exc})", b"")


def members(path: Path) -> Iterator[Member]:
    """Every file under an artefact path (folders walked in full: bundles ship node_modules)."""
    if path.is_dir():
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames.sort()
            for name in sorted(filenames):
                file = Path(dirpath, name)
                if file.is_symlink() or not file.is_file():
                    continue
                shown = f"{path.name}/{file.relative_to(path).as_posix()}"
                yield from expand(shown, file.read_bytes())
    elif path.is_file():
        with path.open("rb") as fh:
            head = fh.read(512)
        if _is_tar(path.name, head):  # an image tar can be large: stream it, members one by one
            yield Member(path.name, b"")
            try:
                with tarfile.open(path, mode="r:*") as tar:
                    yield from _tar_members(path.name, tar, 0)
            except (tarfile.TarError, OSError, EOFError) as exc:
                yield Member(f"{path.name}!(unreadable: {type(exc).__name__}: {exc})", b"")
        else:
            yield from expand(path.name, path.read_bytes())


def text_of(data: bytes) -> str | None:
    if not data or len(data) > TEXT_LIMIT or b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def home_patterns() -> list[banned.Pattern]:
    """The building machine's home folder, as it would appear in a leaked absolute path."""
    out: list[banned.Pattern] = []
    home = os.path.realpath(os.path.expanduser("~"))
    if len(Path(home).parts) >= 3:  # never "/" or "/root"-style one-part homes alone
        out.append(
            banned.Pattern(
                re.compile(re.escape(home.rstrip("/")) + r"(?=[/\\])"),
                "the build machine's home folder (personal data, R10.29): build with "
                "relative paths",
            )
        )
    return out


def artefact_paths(ctx: Context) -> list[Path]:
    named = list(ctx.artifacts)
    env = os.environ.get(ENV, "")
    named += [p for p in env.split(os.pathsep) if p.strip()]
    return [Path(p).expanduser() for p in dict.fromkeys(named)]


def run(
    ctx: Context, patterns_file: Path = banned.PATTERNS_FILE, allow_path: Path = ALLOW_FILE
) -> Result:
    result = Result(NAME)
    patterns, problems = banned.load_patterns(patterns_file)
    entries, allow_problems = similarity.load_allow(allow_path)
    for p in problems + allow_problems:
        result.setup(p)
    if problems or allow_problems:
        return result
    paths = artefact_paths(ctx)
    if not paths:
        result.notes.append(f"no built artefacts named (--artifact PATH or {ENV}); nothing swept")
        return result
    theirs: dict[str, list[str]] = {}
    if ctx.ref is not None and ctx.ref.is_dir():
        theirs = assets.ref_hashes(ctx.ref, result.notes)
    elif ctx.require_ref:
        result.setup(f"reference checkout not found: {ctx.ref}")
        return result
    else:
        result.warnings.append("reference not found: media hashes not compared")
    personal = home_patterns()
    counts = {"members": 0, "text": 0, "media": 0, "allowed": 0}
    for path in paths:
        if not path.exists():
            result.setup(f"{path}: no such artefact")
            continue
        for member in members(path):
            counts["members"] += 1
            if any(fnmatch.fnmatchcase(member.where, e["path"]) for e in entries):
                counts["allowed"] += 1
                continue
            for _, col, match, hint in banned.scan_text(member.where, patterns):
                result.fail(f"{member.where}: path contains {match!r} (col {col}); {hint}")
            name = member.where.rsplit("!", 1)[-1]
            if theirs and member.data and assets.is_media(Path(name)):
                counts["media"] += 1
                digest = hashlib.sha256(member.data).hexdigest()
                if digest in theirs:
                    result.fail(
                        f"{member.where} is identical to reference {theirs[digest][0]} "
                        f"(sha256 {digest[:16]}…); ship our own asset"
                    )
            text = text_of(member.data)
            if text is None:
                continue
            counts["text"] += 1
            for line, col, match, hint in banned.scan_text(text, patterns + personal):
                result.fail(f"{member.where}:{line}:{col}: {match!r}; {hint}")
    result.notes.append(
        f"swept {len(paths)} artefact(s): {counts['members']} members, {counts['text']} text, "
        f"{counts['media']} media compared, {counts['allowed']} allow-listed"
    )
    result.data = {"artifacts": [str(p) for p in paths], **counts}
    return result


if __name__ == "__main__":
    from tooling.checks.__main__ import main

    raise SystemExit(main([NAME, *sys.argv[1:]]))
