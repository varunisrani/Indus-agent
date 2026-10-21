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

``publish(source, target)`` copies a finished build into the runtime tree: it is copied next to the
target first and swapped in with two renames, so a failed copy leaves the old pages in place; a
leftover stage or old copy from an interrupted run is removed first. ``python -m indus.web.bundle
publish [--source DIR] [--target DIR]`` runs it (the release build calls it after the SPA build and
before building the wheel).
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

__all__ = [
    "INDEX",
    "BundleLocation",
    "PublishError",
    "dev_bundle_dir",
    "locate",
    "main",
    "publish",
    "runtime_bundle_dir",
    "source_checkout",
]

INDEX: Final = "index.html"
_PACKAGE_DIR: Final = Path(__file__).resolve().parent.parent

type BundleKind = Literal["source", "installed", "missing"]


class PublishError(RuntimeError):
    """The build cannot be published (no finished build at the source)."""


@dataclass(frozen=True, slots=True)
class BundleLocation:
    """The bundle directory to serve and where it came from."""

    directory: Path
    kind: BundleKind
    """``source`` (``apps/web/dist`` of a checkout), ``installed`` (the package's runtime tree) or
    ``missing`` (nothing built; ``directory`` is where a build would land)."""

    @property
    def found(self) -> bool:
        return self.kind != "missing"


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


def locate(package_dir: Path = _PACKAGE_DIR) -> BundleLocation:
    """The bundle to serve (see the module notes)."""
    runtime = runtime_bundle_dir(package_dir)
    repo = source_checkout(package_dir)
    if repo is not None:
        dev = dev_bundle_dir(repo)
        if _built(dev):
            return BundleLocation(dev, "source")
        if _built(runtime):
            return BundleLocation(runtime, "installed")
        return BundleLocation(dev, "missing")
    if _built(runtime):
        return BundleLocation(runtime, "installed")
    return BundleLocation(runtime, "missing")


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m indus.web.bundle",
        description="Publish the built dashboard pages into the package's runtime tree.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    publish_cmd = commands.add_parser("publish", help="copy apps/web/dist into indus/web/static")
    publish_cmd.add_argument("--source", type=Path, default=None, help="the finished build")
    publish_cmd.add_argument("--target", type=Path, default=None, help="the runtime tree")
    args = parser.parse_args(argv)
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


if __name__ == "__main__":
    raise SystemExit(main())
