"""Pinned downloads for the container images (P23-08, P23-09, P23-12).

Standard library only: the image build runs ``fetch`` with the distribution's bare ``python3``.

The pins live in ``docker/pins.json``:

- ``images``: where the images are published (registry, namespace, image names); ``check`` holds
  them equal to ``RELEASE_IMAGE`` / ``SANDBOX_IMAGE`` in ``src/indus/foundation/versions.py``, the
  one release-base constant (D-16.690);
- ``base``: the Debian base image (name, tag, index digest) every Dockerfile builds from;
- ``bundles``: release artefacts by name (s6-overlay, Node, the CI linters), each with a version, a
  URL template (``{version}``, ``{file}``) and per-key files with their SHA-256. A key is an
  architecture (``amd64``, ``arm64``) or a shared part (any other key, e.g. ``noarch``).

uv is not pinned here: ``fetch uv`` reads the installer's ``scripts/install/pins.json`` (P23-02),
so the image and the installers run the same uv.

Commands::

    pins.py check [--strict]       shape, Dockerfile base lines; --strict: no digest may be empty
    pins.py render                 rewrite each Dockerfile's ``ARG BASE_IMAGE=`` default
    pins.py fetch NAME... --arch A --out DIR
                                   download and verify (any failure exits 1, nothing half-kept)
    pins.py image product|sandbox  print registry/namespace/name (the publishing workflow)
    pins.py refresh               maintainers only (network): fill every digest, then render

A download that fails on the way (connection, timeout, HTTP error) is tried again, then from the
mirror when the pin names one; a download whose digest does not match fails at once — the mirror is
never a way around a mismatch.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

_HERE = Path(__file__).resolve()
ROOT = _HERE.parents[2] if len(_HERE.parents) > 2 else _HERE.parent
DOCKER_PINS = Path("docker/pins.json")
INSTALL_PINS = Path("scripts/install/pins.json")
DOCKERFILES = (Path("docker/Dockerfile"), Path("docker/sandbox/Dockerfile"))
VERSIONS = Path("src/indus/foundation/versions.py")
ARCHES = ("amd64", "arm64")
BASE_ARG = "BASE_IMAGE"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
INDEX_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
NAME = re.compile(r"^[a-z0-9][a-z0-9._/-]*$")
ATTEMPTS = 3
TIMEOUT_S = 60.0
CHUNK = 1 << 16

# The installer's uv target per image architecture (``scripts/install/pins.json`` names glibc Linux
# ``linux-x64`` / ``linux-arm64``; the musl builds carry a ``-musl`` suffix).
UV_TARGETS: dict[str, tuple[str, ...]] = {
    "amd64": ("linux-x64",),
    "arm64": ("linux-arm64",),
}

type Opener = Callable[[str, float], Any]
type Sleeper = Callable[[float], None]


class PinError(Exception):
    """A pin is malformed, missing, or a download failed its check."""


@dataclass(frozen=True)
class Artefact:
    """One file to download: where from, its expected SHA-256 and an optional mirror."""

    name: str
    url: str
    sha256: str | None
    mirror: str | None = None

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[-1] or self.name


# -- reading -----------------------------------------------------------------------------------


def load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PinError(f"{path}: cannot read: {exc}") from exc
    if not isinstance(data, dict):
        raise PinError(f"{path}: must hold a JSON object")
    return cast(dict[str, Any], data)


def _obj(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PinError(f"{where} must be an object")
    return cast(dict[str, Any], value)


def base_reference(pins: dict[str, Any]) -> str:
    """``image:tag@sha256:…`` (``image:tag`` while the digest is still empty)."""
    base = _obj(pins.get("base"), "base")
    ref = f"{base['image']}:{base['tag']}"
    digest = base.get("digest")
    return f"{ref}@{digest}" if digest else ref


def image_name(pins: dict[str, Any], which: str) -> str:
    """``registry/namespace/name`` for ``which`` in ``product`` / ``sandbox``."""
    images = _obj(pins.get("images"), "images")
    return f"{images['registry']}/{images['namespace']}/{images[which]}"


def bundle_artefacts(pins: dict[str, Any], name: str, arch: str) -> list[Artefact]:
    """The files of bundle ``name`` that an ``arch`` image needs: the shared parts, then its own."""
    if arch not in ARCHES:
        raise PinError(f"unsupported architecture {arch!r} (supported: {', '.join(ARCHES)})")
    bundles = _obj(pins.get("bundles"), "bundles")
    if name not in bundles:
        raise PinError(f"no bundle named {name!r} (known: {', '.join(sorted(bundles))}, uv)")
    bundle = _obj(bundles[name], f"bundles.{name}")
    files = _obj(bundle.get("files"), f"bundles.{name}.files")
    if arch not in files and any(key in ARCHES for key in files):
        raise PinError(f"bundle {name!r} has no file for {arch}")
    out: list[Artefact] = []
    for key in [k for k in files if k not in ARCHES] + [arch]:
        if key not in files:
            continue
        entry = _obj(files[key], f"bundles.{name}.files.{key}")
        version = str(bundle["version"])
        file = str(entry["file"]).format(version=version)
        url = str(bundle["url"]).format(version=version, file=file)
        out.append(Artefact(f"{name}:{key}", url, entry.get("sha256")))
    return out


def uv_artefact(install_pins: dict[str, Any], arch: str) -> Artefact:
    """The installer's pinned uv for a glibc Linux ``arch`` (see ``UV_TARGETS``)."""
    if arch not in ARCHES:
        raise PinError(f"unsupported architecture {arch!r} (supported: {', '.join(ARCHES)})")
    uv = _obj(install_pins.get("uv"), "uv (installer pins)")
    targets = _obj(uv.get("targets"), "uv.targets (installer pins)")
    for key in UV_TARGETS[arch]:
        if key in targets:
            entry = _obj(targets[key], f"uv.targets.{key}")
            mirror = entry.get("mirror")
            return Artefact(
                f"uv:{key}",
                str(entry["url"]),
                entry.get("sha256"),
                str(mirror) if mirror else None,
            )
    raise PinError(
        f"the installer pins have no uv target for linux {arch} (tried {UV_TARGETS[arch]})"
    )


# -- checking ----------------------------------------------------------------------------------


def problems(pins: dict[str, Any], *, strict: bool = False) -> list[str]:
    """Everything wrong with ``pins``; ``strict`` also reports digests that are still empty."""
    out: list[str] = []
    if pins.get("schema") != 1:
        out.append("schema must be 1")
    images = pins.get("images")
    if not isinstance(images, dict):
        out.append("images must be an object")
    else:
        for key in ("registry", "namespace", "product", "sandbox"):
            value = cast(dict[str, Any], images).get(key)
            if not isinstance(value, str) or not NAME.match(value):
                out.append(f"images.{key} must be a lowercase registry name")
    base = pins.get("base")
    if not isinstance(base, dict):
        out.append("base must be an object")
    else:
        base = cast(dict[str, Any], base)
        for key in ("image", "tag"):
            if not isinstance(base.get(key), str) or not base[key]:
                out.append(f"base.{key} must be a non-empty string")
        if base.get("tag") == "latest":
            out.append("base.tag must not be latest")
        digest = base.get("digest")
        if digest is None:
            if strict:
                out.append("base.digest is empty: run `pins.py refresh`")
        elif not isinstance(digest, str) or not INDEX_DIGEST.match(digest):
            out.append("base.digest must be sha256:<64 hex>")
    bundles = pins.get("bundles")
    if not isinstance(bundles, dict) or not bundles:
        out.append("bundles must be a non-empty object")
        return out
    for name, raw in cast(dict[str, Any], bundles).items():
        if not isinstance(raw, dict):
            out.append(f"bundles.{name} must be an object")
            continue
        bundle = cast(dict[str, Any], raw)
        if not isinstance(bundle.get("version"), str) or not bundle["version"]:
            out.append(f"bundles.{name}.version must be a non-empty string")
        url = bundle.get("url")
        if not isinstance(url, str) or not url.startswith("https://") or "{file}" not in url:
            out.append(f"bundles.{name}.url must be an https template with {{file}}")
        files = bundle.get("files")
        if not isinstance(files, dict) or not files:
            out.append(f"bundles.{name}.files must be a non-empty object")
            continue
        for arch in ARCHES:
            if arch not in files:
                out.append(f"bundles.{name}.files has no {arch} entry")
        for key, entry in cast(dict[str, Any], files).items():
            where = f"bundles.{name}.files.{key}"
            if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
                out.append(f"{where} must name a file")
                continue
            sha = cast(dict[str, Any], entry).get("sha256")
            if sha is None:
                if strict:
                    out.append(f"{where}.sha256 is empty: run `pins.py refresh`")
            elif not isinstance(sha, str) or not SHA256.match(sha):
                out.append(f"{where}.sha256 must be 64 lowercase hex digits")
    return out


def dockerfile_base(text: str) -> str | None:
    """The default of the first ``ARG BASE_IMAGE=`` line, or None."""
    for line in text.splitlines():
        m = re.match(rf"^ARG\s+{BASE_ARG}=(\S+)\s*$", line)
        if m:
            return m.group(1)
    return None


def render_dockerfile(text: str, reference: str) -> str:
    """``text`` with every ``ARG BASE_IMAGE=`` default replaced by ``reference``."""
    return re.sub(
        rf"^ARG\s+{BASE_ARG}=\S+[ \t]*$", f"ARG {BASE_ARG}={reference}", text, flags=re.MULTILINE
    )


def release_images(root: Path) -> dict[str, str]:
    """``product`` / ``sandbox`` image names from the versions module (loaded by path: standard
    library only, so this works with a bare ``python3``)."""
    spec = importlib.util.spec_from_file_location("indus_release_versions", root / VERSIONS)
    if spec is None or spec.loader is None:
        raise OSError(f"cannot load {VERSIONS}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return {"product": str(module.RELEASE_IMAGE), "sandbox": str(module.SANDBOX_IMAGE)}


def check(root: Path, *, strict: bool = False) -> list[str]:
    pins = load(root / DOCKER_PINS)
    out = problems(pins, strict=strict)
    if out:
        return out
    try:
        expected = release_images(root)
    except (OSError, AttributeError) as exc:
        return [f"{VERSIONS}: cannot read the release images: {exc}"]
    for which, name in expected.items():
        if image_name(pins, which) != name:
            out.append(
                f"images.{which} names {image_name(pins, which)!r} but the release base says "
                f"{name!r} (src/indus/foundation/versions.py)"
            )
    want = base_reference(pins)
    for rel in DOCKERFILES:
        path = root / rel
        try:
            found = dockerfile_base(path.read_text(encoding="utf-8"))
        except OSError as exc:
            out.append(f"{rel}: cannot read: {exc}")
            continue
        if found != want:
            out.append(
                f"{rel}: ARG {BASE_ARG} is {found!r}, docker/pins.json says {want!r}"
                " (run `pins.py render`)"
            )
    return out


def render(root: Path) -> list[Path]:
    """Rewrite the Dockerfiles' base lines from the pins; the files that changed."""
    reference = base_reference(load(root / DOCKER_PINS))
    changed: list[Path] = []
    for rel in DOCKERFILES:
        path = root / rel
        text = path.read_text(encoding="utf-8")
        new = render_dockerfile(text, reference)
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed.append(rel)
    return changed


# -- downloading -------------------------------------------------------------------------------


def _open(url: str, timeout: float) -> Any:
    if not url.startswith(("https://", "file://")):
        raise PinError(f"refusing a non-https download: {url}")
    return urllib.request.urlopen(url, timeout=timeout)  # noqa: S310 - scheme checked above


def _wait(seconds: float) -> None:
    threading.Event().wait(seconds)


def _download_once(url: str, dest: Path, opener: Opener) -> str:
    """Stream ``url`` into ``dest``; its SHA-256. Raises OSError/URLError on transport trouble."""
    digest = hashlib.sha256()
    with opener(url, TIMEOUT_S) as response, dest.open("wb") as fh:
        while chunk := response.read(CHUNK):
            digest.update(chunk)
            fh.write(chunk)
    return digest.hexdigest()


def download(
    artefact: Artefact,
    out_dir: Path,
    *,
    opener: Opener = _open,
    sleeper: Sleeper = _wait,
    allow_unpinned: bool = False,
) -> Path:
    """Download ``artefact`` into ``out_dir`` and verify it; the file's path.

    Transport failures are retried (``ATTEMPTS`` per origin, 2 s then 4 s apart) and then tried
    from the mirror; a digest mismatch fails at once. Nothing is left behind on failure."""
    if artefact.sha256 is None and not allow_unpinned:
        raise PinError(f"{artefact.name}: no SHA-256 pinned yet; run `pins.py refresh`")
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / artefact.filename
    origins = [artefact.url] + ([artefact.mirror] if artefact.mirror else [])
    last: Exception | None = None
    for origin in origins:
        for attempt in range(ATTEMPTS):
            if attempt:
                sleeper(2.0 * attempt)
            fd, tmp_name = tempfile.mkstemp(prefix=".part-", dir=out_dir)
            os.close(fd)
            tmp = Path(tmp_name)
            try:
                got = _download_once(origin, tmp, opener)
            except (OSError, urllib.error.URLError, TimeoutError) as exc:
                tmp.unlink(missing_ok=True)
                last = exc
                continue
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            if artefact.sha256 is not None and got != artefact.sha256:
                tmp.unlink(missing_ok=True)
                raise PinError(
                    f"{artefact.name}: SHA-256 mismatch for {origin}: got {got},"
                    f" pinned {artefact.sha256}"
                )
            tmp.replace(dest)
            return dest
    raise PinError(f"{artefact.name}: download failed from every origin: {last}")


def artefacts_for(
    names: Iterable[str], arch: str, docker_pins: dict[str, Any], install_pins: Path
) -> list[Artefact]:
    out: list[Artefact] = []
    for name in names:
        if name == "uv":
            out.append(uv_artefact(load(install_pins), arch))
        else:
            out.extend(bundle_artefacts(docker_pins, name, arch))
    return out


def fetch(
    names: Sequence[str],
    arch: str,
    out_dir: Path,
    *,
    docker_pins: Path,
    install_pins: Path,
    opener: Opener = _open,
    sleeper: Sleeper = _wait,
) -> list[Path]:
    """Download and verify every artefact of ``names`` for ``arch``. On any failure every file
    this call wrote is removed again."""
    wanted = artefacts_for(names, arch, load(docker_pins), install_pins)
    done: list[Path] = []
    try:
        for artefact in wanted:
            done.append(download(artefact, out_dir, opener=opener, sleeper=sleeper))
    except BaseException:
        for path in done:
            path.unlink(missing_ok=True)
        raise
    return done


# -- refreshing (maintainers, network) ---------------------------------------------------------


def _hash_url(url: str, opener: Opener) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        return _download_once(url, Path(tmp) / "artefact", opener)


def registry_digest(image: str, tag: str, opener: Callable[[urllib.request.Request], Any]) -> str:
    """The index digest of an official Docker Hub image (anonymous pull token)."""
    repo = image if "/" in image else f"library/{image}"
    token_url = (
        f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull"
    )
    with opener(urllib.request.Request(token_url)) as resp:
        token = json.loads(resp.read().decode("utf-8"))["token"]
    request = urllib.request.Request(
        f"https://registry-1.docker.io/v2/{repo}/manifests/{tag}",
        method="HEAD",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.oci.image.index.v1+json,"
            "application/vnd.docker.distribution.manifest.list.v2+json",
        },
    )
    with opener(request) as resp:
        digest = resp.headers.get("Docker-Content-Digest", "")
    if not INDEX_DIGEST.match(digest):
        raise PinError(f"{image}:{tag}: the registry answered no index digest")
    return digest


def refresh(
    root: Path,
    *,
    opener: Opener = _open,
    request_opener: Callable[[urllib.request.Request], Any] | None = None,
) -> dict[str, Any]:
    """Recompute every digest from upstream, write the pins and render the Dockerfiles."""
    path = root / DOCKER_PINS
    pins = load(path)
    base = _obj(pins["base"], "base")
    req = request_opener or (lambda r: urllib.request.urlopen(r, timeout=TIMEOUT_S))  # noqa: S310
    base["digest"] = registry_digest(str(base["image"]), str(base["tag"]), req)
    for name, raw in _obj(pins["bundles"], "bundles").items():
        bundle = _obj(raw, f"bundles.{name}")
        version = str(bundle["version"])
        for key, entry in _obj(bundle["files"], f"bundles.{name}.files").items():
            entry = _obj(entry, f"bundles.{name}.files.{key}")
            file = str(entry["file"]).format(version=version)
            entry["sha256"] = _hash_url(
                str(bundle["url"]).format(version=version, file=file), opener
            )
    path.write_text(json.dumps(pins, indent=2) + "\n", encoding="utf-8")
    render(root)
    return pins


# -- command line ------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pins.py", description="Pinned downloads for the images.")
    p.add_argument("--root", type=Path, default=ROOT, help="repository root")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="check the pins and the Dockerfiles' base lines")
    c.add_argument("--strict", action="store_true", help="also fail on empty digests")
    sub.add_parser("render", help="rewrite the Dockerfiles' base lines from the pins")
    f = sub.add_parser("fetch", help="download and verify pinned artefacts")
    f.add_argument("names", nargs="+", help="bundle names from docker/pins.json, or uv")
    f.add_argument("--arch", required=True, help="amd64 or arm64")
    f.add_argument("--out", type=Path, required=True, help="directory to download into")
    f.add_argument(
        "--docker-pins", type=Path, default=None, help="default: <root>/docker/pins.json"
    )
    f.add_argument("--install-pins", type=Path, default=None, help="default: the installer's pins")
    i = sub.add_parser("image", help="print an image's registry/namespace/name")
    i.add_argument("which", choices=("product", "sandbox"))
    sub.add_parser("refresh", help="maintainers: recompute every digest (network)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root: Path = args.root
    try:
        if args.command == "check":
            found = check(root, strict=args.strict)
            for line in found:
                print(f"pins: {line}", file=sys.stderr)
            if not found:
                print("pins: ok")
            return 1 if found else 0
        if args.command == "image":
            print(image_name(load(root / DOCKER_PINS), args.which))
            return 0
        if args.command == "render":
            for rel in render(root):
                print(f"rendered {rel}")
            return 0
        if args.command == "fetch":
            paths = fetch(
                args.names,
                args.arch,
                args.out,
                docker_pins=args.docker_pins or root / DOCKER_PINS,
                install_pins=args.install_pins or root / INSTALL_PINS,
            )
            for path in paths:
                print(path)
            return 0
        refresh(root)
        print("pins: refreshed; review the diff before committing")
        return 0
    except PinError as exc:
        print(f"pins: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
