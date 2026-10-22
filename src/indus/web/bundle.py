"""Where the built dashboard pages live, and publishing them into the package (P18-29).

``locate()`` picks the directory the dashboard serves:

1. in a source checkout (the package sits at ``<repo>/src/indus`` beside
   ``<repo>/apps/web/package.json``) a built ``apps/web/dist`` wins — the developer's latest
   ``pnpm --filter @indus/web build``;
2. else the runtime tree inside the package, ``indus/web/static`` — what an installed tree carries
   (the release build copies the pages there and the wheel ships them, ``pyproject.toml``
   ``artifacts``);
3. else nothing is built yet: the answer is the directory a build would fill (``apps/web/dist`` in a
   source checkout, the runtime tree otherwise) with ``found=False``, so the server's "not built"
   answer clears by itself once the files appear.

A directory counts only when it holds ``index.html`` (a half-finished copy does not).

Fetched pages carry a ``.indus-release`` stamp (below). A runtime tree stamped with another release
than the one this install runs is ``stale`` (wave 47): it is not served, and the answer is the
directory a build would fill, as for ``missing``, so ``indus dashboard`` builds or says how to
refresh it instead of serving pages made for another API. The release this install runs
(``installed_release``) is, in a checkout, the release its HEAD is on (the stdlib-only file probes
of ``indus.cli.version``, loaded by name as ``web.routes.system`` does: ``cli`` is a sibling layer)
or ``0.0.0`` off a release; elsewhere the package's own version. An unstamped tree (the wheel's, or
one published from a build) is never judged, nor is any tree when the release cannot be read.

``publish(source, target)`` copies a finished build into the runtime tree: it is copied next to the
target first and swapped in with two renames, so a failed copy leaves the old pages in place; a
leftover stage or old copy from an interrupted run is removed first. ``python -m indus.web.bundle
publish [--source DIR] [--target DIR]`` runs it (the release build calls it after the SPA build and
before building the wheel).

``fetch_release(repo)`` gives a script install (a git checkout with no ``pnpm``) the pages its
release was built with (F-3, wave 46): the release workflow attaches ``indus-web-<version>.tar.gz``
(``RELEASE_ASSET``; the built ``apps/web/dist`` with ``index.html`` at the archive root) to every
release, so it is listed in the release's ``manifest.json``, whose SHA-256 the annotated release tag
records (``manifest_sha256``). The chain is checked from the tag the checkout already holds: HEAD's
newest release tag → its record → ``manifest.json`` (digest equal) → the archive (digest equal) →
unpacked with tarfile's ``data`` filter (no absolute paths, no ``..``, no links out, no devices) →
``publish`` into the runtime tree with a ``.indus-release`` stamp. Downloads are HTTPS only
(redirects too), size-capped and time-limited. A tree already stamped with the release is left
alone. ``python -m indus.web.bundle fetch [--repo DIR]`` runs it (``install.sh``'s products stage
calls it): exit 0 published or already there, 3 HEAD is on no release (a ``main`` install builds
its own), 1 anything else, with the reason on stderr.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from indus.foundation.versions import (
    RELEASE_REPOSITORY,
    SOURCE_VERSION,
    Version,
    parse_tag,
    tag_record_text,
)
from indus.platform.locate import which

__all__ = [
    "INDEX",
    "RELEASE_ASSET",
    "RELEASE_MANIFEST",
    "RELEASE_STAMP",
    "BundleLocation",
    "FetchError",
    "NotOnRelease",
    "PublishError",
    "asset_name",
    "dev_bundle_dir",
    "fetch_release",
    "installed_release",
    "locate",
    "main",
    "missing_pages_hint",
    "origin_slug",
    "publish",
    "runtime_bundle_dir",
    "source_checkout",
    "stale_pages_hint",
    "stamped_release",
]

INDEX: Final = "index.html"
RELEASE_ASSET: Final = "indus-web-{version}.tar.gz"
RELEASE_MANIFEST: Final = "manifest.json"
RELEASE_STAMP: Final = ".indus-release"
ASSET_CAP: Final = 64 * 1024 * 1024
MANIFEST_CAP: Final = 1024 * 1024
NETWORK_TIMEOUT_S: Final = 60.0
EXIT_NOT_ON_RELEASE: Final = 3
_SLUG: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
_ORIGIN_FORMS: Final = (
    re.compile(r"https://github\.com/(?P<slug>[^/?#]+/[^/?#]+?)(?:\.git)?/?"),
    re.compile(r"(?:ssh://)?git@github\.com[:/](?P<slug>[^/?#]+/[^/?#]+?)(?:\.git)?/?"),
)
_PACKAGE_DIR: Final = Path(__file__).resolve().parent.parent

type BundleKind = Literal["source", "installed", "stale", "missing"]


class PublishError(RuntimeError):
    """The build cannot be published (no finished build at the source)."""


@dataclass(frozen=True, slots=True)
class BundleLocation:
    """The bundle directory to serve and where it came from."""

    directory: Path
    kind: BundleKind
    """``source`` (``apps/web/dist`` of a checkout), ``installed`` (the package's runtime tree),
    ``stale`` (the runtime tree holds another release's fetched pages) or ``missing`` (nothing
    built); for the last two ``directory`` is where a build would land."""
    stamped: str | None = None
    """The release a ``stale`` tree was fetched for."""

    @property
    def found(self) -> bool:
        return self.kind in ("source", "installed")


def _built(directory: Path) -> bool:
    return (directory / INDEX).is_file()


def runtime_bundle_dir(package_dir: Path = _PACKAGE_DIR) -> Path:
    """The pages inside the package: ``<package>/web/static``."""
    return package_dir / "web" / "static"


def source_checkout(package_dir: Path = _PACKAGE_DIR) -> Path | None:
    """The repository root when the package runs from a source checkout, else None."""
    if package_dir.parent.name != "src":
        return None
    root = package_dir.parent.parent
    return root if (root / "apps" / "web" / "package.json").is_file() else None


def dev_bundle_dir(repo_root: Path) -> Path:
    """Where ``pnpm --filter @indus/web build`` writes in a checkout."""
    return repo_root / "apps" / "web" / "dist"


def installed_release(package_dir: Path = _PACKAGE_DIR) -> str | None:
    """The release this install runs (see the module notes); None when it cannot be read."""
    repo = source_checkout(package_dir)
    if repo is None:
        from indus import __version__

        return __version__
    try:
        probes: Any = importlib.import_module("indus.cli.version")
        found = probes.checkout_release(repo)
    except Exception:  # a probe that cannot read never hides the pages
        return None
    return str(found) if found else SOURCE_VERSION


def locate(
    package_dir: Path = _PACKAGE_DIR,
    *,
    release: Callable[[Path], str | None] = installed_release,
) -> BundleLocation:
    """The bundle to serve (see the module notes)."""
    runtime = runtime_bundle_dir(package_dir)
    repo = source_checkout(package_dir)
    unbuilt = dev_bundle_dir(repo) if repo is not None else runtime
    if repo is not None and _built(unbuilt):
        return BundleLocation(unbuilt, "source")
    if not _built(runtime):
        return BundleLocation(unbuilt, "missing")
    stamp = stamped_release(runtime)
    if stamp is not None:
        running = release(package_dir)
        if running is not None and running != stamp:
            return BundleLocation(unbuilt, "stale", stamped=stamp)
    return BundleLocation(runtime, "installed")


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def publish(source: Path, target: Path) -> Path:
    """Copy the finished build at ``source`` into ``target`` (stage, then swap); ``target``."""
    if not _built(source):
        raise PublishError(f"no finished build at {source} ({INDEX} is missing)")
    if source.resolve() == target.resolve():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.with_name(target.name + ".new")
    old = target.with_name(target.name + ".old")
    for leftover in (stage, old):
        _remove(leftover)
    try:
        shutil.copytree(source, stage, symlinks=False)
    except BaseException:
        _remove(stage)
        raise
    had_old = target.exists() or target.is_symlink()
    if had_old:
        target.rename(old)
    try:
        stage.rename(target)
    except BaseException:
        if had_old:
            old.rename(target)
        _remove(stage)
        raise
    if had_old:
        _remove(old)
    return target


# -- the release's prebuilt pages (F-3) ------------------------------------------------------------


class FetchError(RuntimeError):
    """The release's pages could not be fetched or did not check out."""


class NotOnRelease(FetchError):
    """The checkout's HEAD is on no release tag (nothing was published for it)."""


type GitRunner = Callable[[Path, Sequence[str]], str]
type Download = Callable[[str, int], bytes]


def asset_name(version: Version) -> str:
    return RELEASE_ASSET.format(version=version)


_GIT_ENV: Final = {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C"}


def _run_git(repo: Path, args: Sequence[str]) -> str:
    """Local reads only (tags, the origin URL): an explicit, minimal child env (no person's
    settings or secrets are needed to read a tag); ``git`` is found on PATH first."""
    program = which("git")
    if program is None:
        raise FetchError("git is not on PATH")
    done = subprocess.run(
        [program, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env=dict(_GIT_ENV),
    )
    if done.returncode != 0:
        raise FetchError(f"git {args[0]} failed: {done.stderr.strip()[:200]}")
    return done.stdout


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        if not newurl.startswith("https://"):
            raise FetchError(f"refused a redirect away from HTTPS: {newurl[:120]}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)  # type: ignore[arg-type]


def _download(url: str, cap: int) -> bytes:
    if not url.startswith("https://"):
        raise FetchError(f"not an HTTPS address: {url}")
    opener = urllib.request.build_opener(_HttpsOnly())
    request = urllib.request.Request(url, headers={"User-Agent": "indus-installer"})
    try:
        with opener.open(request, timeout=NETWORK_TIMEOUT_S) as answer:
            data: bytes = answer.read(cap + 1)
    except FetchError:
        raise
    except (OSError, ValueError) as exc:
        raise FetchError(f"download failed: {url}: {exc}") from exc
    if len(data) > cap:
        raise FetchError(f"{url} is larger than {cap} bytes")
    return data


def origin_slug(url: str) -> str | None:
    """``owner/repo`` of a GitHub ``origin`` (HTTPS or SSH form), else None."""
    for form in _ORIGIN_FORMS:
        found = form.fullmatch(url.strip())
        if found and _SLUG.fullmatch(found.group("slug")):
            return found.group("slug")
    return None


def _release_on_head(repo: Path, git: GitRunner) -> tuple[Version, str]:
    """HEAD's newest release tag and the manifest digest its record carries."""
    names = git(repo, ["tag", "--points-at", "HEAD", "--list", "v*"]).split()
    versions = sorted(v for v in (parse_tag(n) for n in names) if v is not None)
    if not versions:
        raise NotOnRelease("this checkout's HEAD is not a release")
    version = versions[-1]
    message = git(repo, ["tag", "--list", version.tag, "--format=%(contents)"])
    try:
        record = json.loads(tag_record_text(message))
    except ValueError:
        record = None
    digest = record.get("manifest_sha256") if isinstance(record, dict) else None  # type: ignore[union-attr]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise FetchError(f"the {version.tag} tag records no release manifest digest")
    return version, digest


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unpack(data: bytes, into: Path) -> Path:
    """Unpack the archive under ``into``; the directory holding ``index.html``."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            archive.extractall(into, filter="data")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise FetchError(f"the pages archive could not be unpacked: {exc}") from exc
    if _built(into):
        return into
    entries = [p for p in into.iterdir()]
    if len(entries) == 1 and entries[0].is_dir() and _built(entries[0]):
        return entries[0]
    raise FetchError(f"the pages archive has no {INDEX}")


def stamped_release(target: Path) -> str | None:
    try:
        text = (target / RELEASE_STAMP).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text or None


def fetch_release(
    repo: Path,
    target: Path | None = None,
    *,
    git: GitRunner = _run_git,
    download: Download = _download,
) -> tuple[Version, bool]:
    """Publish the release's prebuilt pages into ``target`` (the runtime tree by default).

    ``(version, fetched)``; ``fetched`` is False when the tree already holds that release."""
    target = target if target is not None else runtime_bundle_dir()
    version, manifest_digest = _release_on_head(repo, git)
    if _built(target) and stamped_release(target) == str(version):
        return version, False
    origin = git(repo, ["config", "--get", "remote.origin.url"]).strip()
    slug = origin_slug(origin) or (RELEASE_REPOSITORY if not origin else None)
    if slug is None:
        raise FetchError(f"origin {origin!r} is not a GitHub repository; no release to fetch from")
    base = f"https://github.com/{slug}/releases/download/{version.tag}"
    manifest_bytes = download(f"{base}/{RELEASE_MANIFEST}", MANIFEST_CAP)
    if _sha256(manifest_bytes) != manifest_digest:
        raise FetchError(
            f"{RELEASE_MANIFEST} does not match the digest the {version.tag} tag records"
        )
    try:
        manifest = json.loads(manifest_bytes)
    except ValueError as exc:
        raise FetchError(f"{RELEASE_MANIFEST} is not JSON") from exc
    name = asset_name(version)
    expected = manifest.get(name) if isinstance(manifest, dict) else None  # type: ignore[union-attr]
    if not isinstance(expected, str):
        raise FetchError(f"the {version.tag} release has no {name}")
    archive = download(f"{base}/{name}", ASSET_CAP)
    if _sha256(archive) != expected:
        raise FetchError(f"{name} does not match the release manifest")
    target.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".indus-web-fetch-", dir=target.parent))
    try:
        pages = _unpack(archive, work / "unpacked")
        (pages / RELEASE_STAMP).write_text(f"{version}\n", encoding="utf-8")
        publish(pages, target)
    finally:
        _remove(work)
    return version, True


def stale_pages_hint(stamped: str | None) -> str:
    """What to do when the runtime tree holds another release's fetched pages."""
    return (
        f"The dashboard pages here were fetched for release {stamped or '(unknown)'}, not the one "
        "this install runs, so they are not served. Run `python -m indus.web.bundle fetch` with "
        "this install's Python (or `indus update`, or rerun the installer) to fetch this "
        "release's pages."
    )


def missing_pages_hint(repo: Path | None) -> str:
    """What to do when no pages are built and none can be built here (no ``pnpm``)."""
    if repo is None:
        return "This install carries no dashboard pages; reinstall from a release."
    return (
        "No prebuilt pages for this checkout, and pnpm is not on PATH. On a release, run "
        "`python -m indus.web.bundle fetch` with this install's Python (or rerun the installer); "
        "otherwise install Node.js and pnpm and run `pnpm install && pnpm --filter @indus/web "
        "build` in " + str(repo) + "."
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m indus.web.bundle",
        description="Publish the built dashboard pages into the package's runtime tree.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    publish_cmd = commands.add_parser("publish", help="copy apps/web/dist into indus/web/static")
    publish_cmd.add_argument("--source", type=Path, default=None, help="the finished build")
    publish_cmd.add_argument("--target", type=Path, default=None, help="the runtime tree")
    fetch_cmd = commands.add_parser(
        "fetch", help="download this release's prebuilt pages into indus/web/static"
    )
    fetch_cmd.add_argument("--repo", type=Path, default=None, help="the source checkout")
    fetch_cmd.add_argument("--target", type=Path, default=None, help="the runtime tree")
    args = parser.parse_args(argv)
    if args.command == "fetch":
        return _fetch_main(args.repo, args.target)
    source: Path | None = args.source
    if source is None:
        repo = source_checkout()
        if repo is None:
            print("Not a source checkout: pass --source.", file=sys.stderr)
            return 2
        source = dev_bundle_dir(repo)
    target: Path = args.target or runtime_bundle_dir()
    try:
        publish(source, target)
    except (PublishError, OSError) as exc:
        print(f"Could not publish the dashboard pages: {exc}", file=sys.stderr)
        return 1
    print(f"Published the dashboard pages to {target}")
    return 0


def _fetch_main(repo: Path | None, target: Path | None) -> int:
    repo = repo or source_checkout()
    if repo is None:
        print("Not a source checkout: pass --repo.", file=sys.stderr)
        return 2
    try:
        version, fetched = fetch_release(repo, target)
    except NotOnRelease as exc:
        print(f"No prebuilt dashboard pages: {exc}.", file=sys.stderr)
        return EXIT_NOT_ON_RELEASE
    except (FetchError, OSError) as exc:
        print(f"Could not fetch the dashboard pages: {exc}", file=sys.stderr)
        return 1
    where = target or runtime_bundle_dir()
    if fetched:
        print(f"Installed the dashboard pages of {version.tag} in {where}")
    else:
        print(f"The dashboard pages of {version.tag} are already in {where}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
