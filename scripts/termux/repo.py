# pyright: strict
"""The Termux APT channel: release tag → Debian version, and the static repository (P24-21).

Version mapping (the tag grammar is ``indus.foundation.versions``; nothing here re-types it):

- a stable tag ``v1.2.3`` is Debian version ``1.2.3-1``;
- a canary tag ``v1.2.4-canary.<YYYYMMDDTHHMMSSZ>`` is ``1.2.4~canary.<stamp>-1``. A ``~`` sorts
  before anything in dpkg order, so the canary lands below its own stable and, being the next
  patch, above the stable before it;
- every other string is refused.

The repository is a plain directory tree any static host can serve::

    key.asc                                   public half of the signing key
    pool/main/<initial>/<package>/<package>_<version>_<arch>.deb
    dists/indus-<channel>/Release             SHA-256 of every index below
    dists/indus-<channel>/InRelease           Release, clearsigned
    dists/indus-<channel>/Release.gpg         Release, detached armoured signature
    dists/indus-<channel>/main/binary-aarch64/Packages[.gz]

Building twice into the same tree accumulates versions (a canary and a stable live side by side);
a version already listed may only be re-added with identical bytes. Without a signing key the
Release is still written but the command exits 3: CI treats that as failure, so an unsigned
channel is never published.

Usage::

    python scripts/termux/repo.py version v1.2.3            # 1.2.3-1
    python scripts/termux/repo.py channel v1.2.3            # stable
    python scripts/termux/repo.py build --debs DIR --out DIR --tag TAG [--key-file F] [--date S]

Exit codes: 0 done (signed), 2 refused (bad tag, bad package, bad arguments), 3 Release unsigned.
The signing key's passphrase, when it has one, is read from ``TERMUX_APT_SIGNING_PASSPHRASE`` and
handed to gpg on stdin only.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import lzma
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zlib
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cmp_to_key
from pathlib import Path
from typing import Protocol

if __package__ in (None, ""):  # run as a script: make the repository and src importable
    _ROOT = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(_ROOT), str(_ROOT / "src")]

from indus.foundation.versions import CANARY, RELEASE_CHANNELS, parse_tag  # noqa: E402

__all__ = [
    "ARCH",
    "COMPONENT",
    "EXIT_OK",
    "EXIT_REFUSED",
    "EXIT_UNSIGNED",
    "PACKAGE",
    "PASSPHRASE_ENV",
    "BuildResult",
    "GpgSigner",
    "RepoError",
    "Signer",
    "build_repository",
    "channel_of",
    "compare_versions",
    "deb_version",
    "main",
    "read_control",
    "suite_for",
]

PACKAGE = "indus-agent"
ARCH = "aarch64"
COMPONENT = "main"
SUITE_PREFIX = "indus-"
PASSPHRASE_ENV = "TERMUX_APT_SIGNING_PASSPHRASE"

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_UNSIGNED = 3

CONTROL_LIMIT = 1024 * 1024
"""Largest decompressed control archive we read: a control member is a few kilobytes."""

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9+.-]+")
_DEB_VERSION_RE = re.compile(r"(?:[0-9]+:)?[0-9][A-Za-z0-9.+~-]*")
_REQUIRED = ("Package", "Version", "Architecture")
_POOL_FIELDS = frozenset({"Filename", "Size", "SHA256", "SHA1", "MD5sum", "SHA512"})
_DATE_FORMAT = "%a, %d %b %Y %H:%M:%S UTC"


class RepoError(Exception):
    """A refusal: the message says what was wrong; nothing was published."""


# -- versions --------------------------------------------------------------------------------------


def deb_version(tag: str) -> str:
    """The Debian version for a release tag; ``RepoError`` for anything that is not one."""
    found = parse_tag(tag)
    if found is None:
        raise RepoError(
            f"malformed release tag {tag!r}: expected vX.Y.Z or vX.Y.Z-{CANARY}.<YYYYMMDDTHHMMSSZ>"
        )
    core = f"{found.major}.{found.minor}.{found.patch}"
    if found.stamp is None:
        return f"{core}-1"
    return f"{core}~{CANARY}.{found.stamp}-1"


def channel_of(tag: str) -> str:
    """``stable`` or ``canary``, from the same parse as ``deb_version``."""
    found = parse_tag(tag)
    if found is None:
        raise RepoError(f"malformed release tag {tag!r}")
    return found.channel


def suite_for(channel: str) -> str:
    if channel not in RELEASE_CHANNELS:
        raise RepoError(f"no APT suite for channel {channel!r}")
    return SUITE_PREFIX + channel


def _rank(ch: str) -> int:
    """dpkg's character weight inside a non-digit run: ``~`` lowest, then the end of the run,
    then letters, then every other character."""
    if ch == "~":
        return -1
    if ch.isalpha():
        return ord(ch)
    return ord(ch) + 256


def _runs(text: str) -> list[tuple[list[int], int]]:
    """Split a version part into (non-digit weights, number) pairs, in order."""
    pairs: list[tuple[list[int], int]] = []
    for match in re.finditer(r"([^0-9]*)([0-9]*)", text):
        letters, digits = match.group(1), match.group(2)
        if not letters and not digits:
            continue
        pairs.append(([_rank(c) for c in letters], int(digits) if digits else 0))
    return pairs


def _compare_part(left: str, right: str) -> int:
    a, b = _runs(left), _runs(right)
    for index in range(max(len(a), len(b))):
        la, na = a[index] if index < len(a) else ([], 0)
        lb, nb = b[index] if index < len(b) else ([], 0)
        width = max(len(la), len(lb))
        wa = la + [0] * (width - len(la))  # the end of a run weighs 0: above `~`, below letters
        wb = lb + [0] * (width - len(lb))
        if wa != wb:
            return -1 if wa < wb else 1
        if na != nb:
            return -1 if na < nb else 1
    return 0


def _split(version: str) -> tuple[int, str, str]:
    epoch_text, colon, rest = version.partition(":")
    if not colon:
        epoch_text, rest = "0", version
    upstream, dash, revision = rest.rpartition("-")
    if not dash:
        upstream, revision = rest, ""
    return int(epoch_text or "0"), upstream, revision


def compare_versions(left: str, right: str) -> int:
    """-1, 0 or 1 as dpkg orders two versions (Debian policy §5.6.12)."""
    ea, ua, ra = _split(left)
    eb, ub, rb = _split(right)
    if ea != eb:
        return -1 if ea < eb else 1
    return _compare_part(ua, ub) or _compare_part(ra, rb)


# -- reading a .deb --------------------------------------------------------------------------------


def _ar_members(data: bytes) -> Iterator[tuple[str, bytes]]:
    if not data.startswith(b"!<arch>\n"):
        raise RepoError("not a Debian package (no ar header)")
    offset = 8
    while offset + 60 <= len(data):
        header = data[offset : offset + 60]
        if header[58:60] != b"`\n":
            raise RepoError("damaged ar member header")
        name = header[:16].decode("ascii", "replace").strip().rstrip("/")
        try:
            size = int(header[48:58].decode("ascii").strip())
        except ValueError as exc:
            raise RepoError("damaged ar member size") from exc
        start = offset + 60
        if size < 0 or start + size > len(data):
            raise RepoError(f"ar member {name!r} runs past the end of the file")
        yield name, data[start : start + size]
        offset = start + size + (size & 1)


def _inflate(name: str, payload: bytes) -> bytes:
    """The control tarball, decompressed with a hard size bound."""
    if name == "control.tar":
        raw = payload
    elif name == "control.tar.gz":
        raw = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(payload, CONTROL_LIMIT + 1)
    elif name == "control.tar.xz":
        raw = lzma.LZMADecompressor().decompress(payload, max_length=CONTROL_LIMIT + 1)
    else:
        raise RepoError(f"unsupported control member {name!r} (build with dpkg-deb -Zxz)")
    if len(raw) > CONTROL_LIMIT:
        raise RepoError(f"control member larger than {CONTROL_LIMIT} bytes")
    return raw


@dataclass(frozen=True)
class Paragraph:
    """One deb822 paragraph, fields in their original order, values with continuation lines."""

    fields: tuple[tuple[str, str], ...]

    def get(self, name: str) -> str | None:
        for key, value in self.fields:
            if key.lower() == name.lower():
                return value
        return None

    def render(self) -> str:
        return "".join(f"{key}: {value}\n" for key, value in self.fields)


def parse_paragraphs(text: str) -> list[Paragraph]:
    found: list[Paragraph] = []
    current: list[tuple[str, str]] = []
    for line in text.splitlines():
        if not line.strip():
            if current:
                found.append(Paragraph(tuple(current)))
                current = []
            continue
        if line[0] in " \t":
            if not current:
                raise RepoError("continuation line before any field")
            key, value = current[-1]
            current[-1] = (key, f"{value}\n{line}")
            continue
        key, colon, value = line.partition(":")
        if not colon or not key.strip():
            raise RepoError(f"not a control field: {line!r}")
        if any(name.lower() == key.strip().lower() for name, _value in current):
            raise RepoError(f"field {key.strip()} appears twice in one paragraph")
        current.append((key.strip(), value.strip()))
    if current:
        found.append(Paragraph(tuple(current)))
    return found


def read_control(deb: bytes) -> Paragraph:
    """The control paragraph of a ``.deb`` (checked for the fields a repository needs)."""
    member = next(
        ((name, body) for name, body in _ar_members(deb) if name.startswith("control.tar")), None
    )
    if member is None:
        raise RepoError("no control member in the package")
    raw = _inflate(*member)
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            names = ("control", "./control")
            entry = next((m for m in archive.getmembers() if m.isfile() and m.name in names), None)
            handle = archive.extractfile(entry) if entry is not None else None
            text = handle.read(CONTROL_LIMIT).decode("utf-8") if handle is not None else None
    except (tarfile.TarError, UnicodeDecodeError) as exc:
        raise RepoError(f"unreadable control member: {exc}") from exc
    if text is None:
        raise RepoError("the control member has no control file")
    paragraphs = parse_paragraphs(text)
    if len(paragraphs) != 1:
        raise RepoError("the control file must hold exactly one paragraph")
    control = paragraphs[0]
    for name in _REQUIRED:
        if not control.get(name):
            raise RepoError(f"the control file has no {name}")
    _check_identity(control)
    return control


def _check_identity(control: Paragraph) -> None:
    name, version, arch = (control.get(f) or "" for f in _REQUIRED)
    if not _NAME_RE.fullmatch(name):
        raise RepoError(f"package name {name!r} is not a Debian package name")
    if not _DEB_VERSION_RE.fullmatch(version):
        raise RepoError(f"version {version!r} is not a Debian version")
    if arch != ARCH:
        raise RepoError(f"architecture {arch!r}: the Termux channel ships {ARCH} only")


# -- signing ---------------------------------------------------------------------------------------


class Signer(Protocol):
    def sign(self, release: Path, *, inline: Path, detached: Path) -> None:
        """Write ``inline`` (clearsigned Release) and ``detached`` (armoured signature)."""
        ...

    def public_key(self) -> bytes: ...


Runner = Callable[[list[str], bytes | None, Mapping[str, str]], tuple[int, bytes, bytes]]
"""(argv, stdin, env) → (exit code, stdout, stderr)."""


def _spawn(
    argv: list[str], stdin: bytes | None, env: Mapping[str, str]
) -> tuple[int, bytes, bytes]:
    # Output goes to files, not pipes: a gpg-agent started by gpg can keep a pipe open after gpg
    # itself has exited, and a reader waiting for end-of-file would then never return.
    with tempfile.TemporaryDirectory(prefix="indus-apt-io-") as box:
        out_path, err_path = Path(box, "out"), Path(box, "err")
        with out_path.open("wb") as out, err_path.open("wb") as err:
            done = subprocess.run(  # noqa: S603 - fixed gpg argv, no shell
                argv,
                input=stdin,
                stdin=None if stdin is not None else subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                env=dict(env),
                check=False,
                timeout=300,
            )
        return done.returncode, out_path.read_bytes(), err_path.read_bytes()


_FPR_RE = re.compile(r"[0-9A-F]{40}|[0-9A-F]{64}")


class GpgSigner:
    """Signs with one key imported into a throwaway gpg home (the user's keyring is never read).

    Open it with ``GpgSigner.open(key_file)``; every signature is verified against the imported
    key's fingerprint before ``sign`` returns."""

    def __init__(
        self, *, home: Path, fingerprint: str, unlock: bytes | None, gpg: str, runner: Runner
    ) -> None:
        self.home = home
        self.fingerprint = fingerprint
        self._unlock = unlock
        self._gpg = gpg
        self._runner = runner

    @classmethod
    @contextmanager
    def open(
        cls,
        key_file: Path,
        *,
        unlock: bytes | None = None,
        gpg: str = "gpg",
        runner: Runner = _spawn,
    ) -> Generator[GpgSigner]:
        with tempfile.TemporaryDirectory(prefix="indus-apt-gpg-") as box:
            home = Path(box, "home")
            home.mkdir(mode=0o700)
            signer = cls(home=home, fingerprint="", unlock=unlock, gpg=gpg, runner=runner)
            try:
                signer.call(["--import"], stdin=key_file.read_bytes())
                signer.fingerprint = signer.only_secret_key()
                yield signer
            finally:
                signer.stop_agent()

    def env(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "GNUPGHOME": str(self.home),
            "LC_ALL": "C",
        }

    def call(self, args: list[str], *, stdin: bytes | None = None, unlocked: bool = False) -> bytes:
        argv = [self._gpg, "--batch", "--yes", "--no-tty", "--homedir", str(self.home)]
        if unlocked and self._unlock is not None:
            if stdin is not None:
                raise RepoError("internal: stdin carries the passphrase, not data")
            argv += ["--pinentry-mode", "loopback", "--passphrase-fd", "0"]
            stdin = self._unlock
        code, out, err = self._runner([*argv, *args], stdin, self.env())
        if code != 0:
            message = err.decode("utf-8", "replace").strip().splitlines()
            raise RepoError(f"gpg {args[0]} failed: {message[-1] if message else f'exit {code}'}")
        return out

    def only_secret_key(self) -> str:
        listing = self.call(["--with-colons", "--list-secret-keys"]).decode("utf-8", "replace")
        primaries: list[str] = []
        expect_primary = False
        for line in listing.splitlines():
            kind, _sep, rest = line.partition(":")
            if kind in ("sec", "ssb"):
                expect_primary = kind == "sec"
            elif kind == "fpr" and expect_primary:
                primaries.extend(_FPR_RE.findall(rest))
                expect_primary = False
        distinct = sorted(set(primaries))
        if not distinct:
            raise RepoError("the key file holds no secret key")
        if len(distinct) > 1:
            raise RepoError(f"the key file holds {len(distinct)} secret keys; it must hold one")
        return distinct[0]

    def sign(self, release: Path, *, inline: Path, detached: Path) -> None:
        user = ["--local-user", self.fingerprint, "--digest-algo", "SHA512"]
        self.call([*user, "--clearsign", "--output", str(inline), str(release)], unlocked=True)
        self.call(
            [*user, "--armor", "--detach-sign", "--output", str(detached), str(release)],
            unlocked=True,
        )
        self.verify([str(inline)])
        self.verify([str(detached), str(release)])

    def verify(self, files: list[str]) -> None:
        status = self.call(["--status-fd", "1", "--verify", *files]).decode("utf-8", "replace")
        signers: set[str] = set()
        for line in status.splitlines():
            if line.startswith("[GNUPG:] VALIDSIG "):
                signers.update(_FPR_RE.findall(line))
        if self.fingerprint not in signers:
            raise RepoError(f"{Path(files[0]).name}: no valid signature by the imported key")

    def public_key(self) -> bytes:
        key = self.call(["--armor", "--export", self.fingerprint])
        if not key.strip():
            raise RepoError("gpg exported an empty public key")
        return key

    def stop_agent(self) -> None:
        gpgconf = shutil.which("gpgconf")
        if gpgconf is None:
            return
        with suppress(OSError):
            self._runner([gpgconf, "--homedir", str(self.home), "--kill", "all"], None, self.env())


# -- the repository --------------------------------------------------------------------------------


@dataclass(frozen=True)
class BuildResult:
    suite: str
    signed: bool
    added: tuple[str, ...]
    """``<package>_<version>`` for each package this run added (identical re-adds excluded)."""


@dataclass(frozen=True)
class _Entry:
    package: str
    version: str
    sha256: str
    text: str


def _pool_path(control: Paragraph) -> str:
    name, version, arch = (control.get(f) or "" for f in _REQUIRED)
    file_version = version.split(":", 1)[-1]  # an epoch never appears in a file name
    return f"pool/{COMPONENT}/{name[0]}/{name}/{name}_{file_version}_{arch}.deb"


def _stanza(control: Paragraph, filename: str, data: bytes) -> str:
    kept = [(k, v) for k, v in control.fields if k not in _POOL_FIELDS]
    kept += [("Filename", filename), ("Size", str(len(data)))]
    kept.append(("SHA256", hashlib.sha256(data).hexdigest()))
    return Paragraph(tuple(kept)).render()


def _published(index: Path) -> dict[tuple[str, str], _Entry]:
    if not index.is_file():
        return {}
    entries: dict[tuple[str, str], _Entry] = {}
    for paragraph in parse_paragraphs(index.read_text(encoding="utf-8")):
        package, version = paragraph.get("Package") or "", paragraph.get("Version") or ""
        entry = _Entry(package, version, paragraph.get("SHA256") or "", paragraph.render())
        entries[(package, version)] = entry
    return entries


def _release_text(
    suite: str, channel: str, when: datetime, base: Path, indexes: Sequence[Path]
) -> str:
    lines = [
        "Origin: Indus Agent",
        f"Label: {PACKAGE}",
        f"Suite: {suite}",
        f"Codename: {suite}",
        f"Date: {when.astimezone(UTC).strftime(_DATE_FORMAT)}",
        f"Architectures: {ARCH}",
        f"Components: {COMPONENT}",
        f"Description: Indus Agent for Termux ({channel} channel)",
        "SHA256:",
    ]
    for path in indexes:
        data = path.read_bytes()
        name = path.relative_to(base).as_posix()
        lines.append(f" {hashlib.sha256(data).hexdigest()} {len(data):>16} {name}")
    return "\n".join(lines) + "\n"


def build_repository(
    debs: Sequence[Path],
    out: Path,
    tag: str,
    *,
    signer: Signer | None,
    when: datetime,
) -> BuildResult:
    """Add ``debs`` (all of the tag's version) to the suite for the tag's channel under ``out``.

    Refusals raise ``RepoError`` before anything in ``out`` changes."""
    version = deb_version(tag)
    channel = channel_of(tag)
    suite = suite_for(channel)
    if not debs:
        raise RepoError("no .deb files to publish")

    dists = out / "dists" / suite
    binary = dists / COMPONENT / f"binary-{ARCH}"
    entries = _published(binary / "Packages")

    staged: list[tuple[str, bytes, _Entry]] = []
    for deb in debs:
        data = deb.read_bytes()
        try:
            control = read_control(data)
        except RepoError as exc:
            raise RepoError(f"{deb.name}: {exc}") from exc
        package, found = control.get("Package") or "", control.get("Version") or ""
        if found != version:
            raise RepoError(f"{deb.name}: version {found} is not {version}, the version of {tag}")
        filename = _pool_path(control)
        digest = hashlib.sha256(data).hexdigest()
        entry = _Entry(package, found, digest, _stanza(control, filename, data))
        known = entries.get((package, found))
        if known is not None and known.sha256 != entry.sha256:
            raise RepoError(
                f"{package} {found} is already published in {suite} with other bytes;"
                " published versions never change"
            )
        if any(other.package == package for _f, _d, other in staged):
            raise RepoError(f"{package} {found} is given twice")
        staged.append((filename, data, entry))

    added: list[str] = []
    for filename, data, entry in staged:
        target = out / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.read_bytes() != data:
            target.write_bytes(data)
        if (entry.package, entry.version) not in entries:
            added.append(f"{entry.package}_{entry.version}")
        entries[(entry.package, entry.version)] = entry

    by_version = cmp_to_key(compare_versions)
    ordered = sorted(entries.values(), key=lambda e: (e.package, by_version(e.version)))
    text = "\n".join(e.text for e in ordered)
    binary.mkdir(parents=True, exist_ok=True)
    packages = binary / "Packages"
    packages.write_text(text, encoding="utf-8", newline="\n")
    packed = binary / "Packages.gz"
    packed.write_bytes(gzip.compress(text.encode("utf-8"), compresslevel=9, mtime=0))

    release = dists / "Release"
    summary = _release_text(suite, channel, when, dists, [packages, packed])
    release.write_text(summary, encoding="utf-8", newline="\n")
    inline, detached = dists / "InRelease", dists / "Release.gpg"
    for stale in (inline, detached):
        stale.unlink(missing_ok=True)  # a signature over an older Release must never remain
    if signer is None:
        return BuildResult(suite, False, tuple(added))
    signer.sign(release, inline=inline, detached=detached)
    (out / "key.asc").write_bytes(signer.public_key())
    return BuildResult(suite, True, tuple(added))


# -- command line ----------------------------------------------------------------------------------


def _parse_when(text: str | None) -> datetime:
    if text is None:
        return datetime.now(UTC)
    try:
        return datetime.fromtimestamp(int(text), UTC)
    except (ValueError, OverflowError, OSError) as exc:
        raise RepoError(f"--date wants seconds since the epoch, not {text!r}") from exc


def _say(message: str) -> None:
    print(f"termux repo: {message}", file=sys.stderr)


def _build(args: argparse.Namespace) -> int:
    debs_dir: Path = args.debs
    if not debs_dir.is_dir():
        raise RepoError(f"--debs {debs_dir} is not a directory")
    debs = sorted(debs_dir.glob("*.deb"))
    when = _parse_when(args.date)
    key_file: Path | None = args.key_file
    if key_file is not None and not key_file.is_file():
        raise RepoError(f"--key-file {key_file} does not exist")
    gpg = shutil.which("gpg")
    if key_file is None or gpg is None:
        result = build_repository(debs, args.out, args.tag, signer=None, when=when)
        why = "no --key-file given" if key_file is None else "gpg is not installed"
        _say(f"{result.suite}: Release written UNSIGNED ({why}); refusing to call this published")
        return EXIT_UNSIGNED
    unlock = os.environ.get(PASSPHRASE_ENV)
    with GpgSigner.open(key_file, unlock=unlock.encode() if unlock else None, gpg=gpg) as signer:
        result = build_repository(debs, args.out, args.tag, signer=signer, when=when)
    added = ", ".join(result.added) or "nothing new"
    _say(f"{result.suite}: signed by {signer.fingerprint}; added {added}")
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repo.py", description="Termux APT channel tools.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (("version", "print the Debian version"), ("channel", "print the channel")):
        one = sub.add_parser(name, help=f"{text} for a release tag")
        one.add_argument("tag")
    build = sub.add_parser("build", help="add packages to the static APT repository")
    build.add_argument("--debs", type=Path, required=True, help="folder of .deb files")
    build.add_argument("--out", type=Path, required=True, help="repository root")
    build.add_argument("--tag", required=True, help="the release tag the packages belong to")
    build.add_argument("--key-file", type=Path, default=None, help="armoured secret signing key")
    build.add_argument("--date", default=None, help="Release date, seconds since the epoch")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code == 0 else EXIT_REFUSED
    try:
        if args.command == "version":
            print(deb_version(args.tag))
            return EXIT_OK
        if args.command == "channel":
            print(channel_of(args.tag))
            return EXIT_OK
        return _build(args)
    except (RepoError, OSError) as exc:
        _say(str(exc))
        return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
