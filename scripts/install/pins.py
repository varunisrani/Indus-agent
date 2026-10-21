"""Bootstrap pins for the installers (P23-02).

``scripts/install/pins.json`` is the one place that names what the installers download: the uv
version and, per target, its upstream URL, the mirror URL on our own release assets and the
SHA-256; the portable Git for Windows archive per arch; the Python minor to provision; and the
release repository every installer URL derives from. That repository is a copy of
``RELEASE_REPOSITORY`` in ``src/indus/foundation/versions.py`` (the one release-base constant,
D-16.690): ``--check`` fails when they differ, and ``update`` writes the constant.

    python scripts/install/pins.py --check [--strict]   # JSON valid, install.sh section fresh
    python scripts/install/pins.py render               # print the generated install.sh section
    python scripts/install/pins.py update --uv X [--git Y] [--open-pr]

``render_sh`` gives the section ``build.py`` puts into ``install.sh`` (between ``>>> generated:
pins`` markers); ``install.ps1`` embeds ``pins.json`` itself (``build.py``). ``--check`` fails when
the JSON is malformed, names another release repository than the constant, or the section in
``install.sh`` is not what the JSON renders (a hand-edited digest). A digest of ``null`` means "not
recorded yet" (``"provisional": true``): the installers refuse to download such a tool, and
``--strict`` (release lanes) fails. ``update`` downloads every target, computes the digests and
rewrites the JSON and the built scripts; it never commits on its own — ``--open-pr`` creates a
branch, commits and opens a pull request for review (never merged automatically).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
PINS = HERE / "pins.json"
INSTALL_SH = ROOT / "scripts" / "install.sh"
VERSIONS = ROOT / "src" / "indus" / "foundation" / "versions.py"

# Our target keys and how each maps onto the Rust target triple uv names its archives by.
TARGETS = (
    "linux-x64",
    "linux-arm64",
    "linux-x64-musl",
    "linux-arm64-musl",
    "darwin-x64",
    "darwin-arm64",
    "windows-x64",
    "windows-arm64",
)
_CPU = {"x64": "x86_64", "arm64": "aarch64"}
_SYSTEM = {
    "linux": "unknown-linux-gnu",
    "linux-musl": "unknown-linux-musl",
    "darwin": "apple-darwin",
    "windows": "pc-windows-msvc",
}
GIT_ARCHES = {"windows-x64": "64-bit", "windows-arm64": "arm64"}
SH_TARGETS = tuple(t for t in TARGETS if not t.startswith("windows-"))
MIRROR_TAG = "bootstrap-mirror"

SHA256 = re.compile(r"^[0-9a-f]{64}$")
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
GIT_VERSION = re.compile(r"^([0-9]+\.[0-9]+\.[0-9]+)\.windows\.([0-9]+)$")
MINOR = re.compile(r"^3\.[0-9]{2}$")
# Safe inside single quotes in both shells: no quotes, spaces or shell metacharacters.
URL = re.compile(r"^https://[A-Za-z0-9.-]+/[A-Za-z0-9._~/%+-]+$")

SH_BEGIN = "# >>> generated: pins (scripts/install/pins.py from pins.json; do not edit) >>>"
SH_END = "# <<< generated: pins <<<"

Fetch = Callable[[str], bytes]
Runner = Callable[[Sequence[str]], int]


# -- URLs -----------------------------------------------------------------------------------------


def release_repository(versions: Path = VERSIONS) -> str:
    """``RELEASE_REPOSITORY`` from the versions module, loaded by path (standard library only, so
    this works without the project environment)."""
    spec = importlib.util.spec_from_file_location("indus_release_versions", versions)
    if spec is None or spec.loader is None:
        raise OSError(f"cannot load {versions}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return str(module.RELEASE_REPOSITORY)


def uv_triple(target: str) -> str:
    system, cpu, *libc = target.split("-")
    return f"{_CPU[cpu]}-{_SYSTEM[system + ('-' + libc[0] if libc else '')]}"


def uv_asset(target: str) -> str:
    suffix = "zip" if target.startswith("windows-") else "tar.gz"
    return f"uv-{uv_triple(target)}.{suffix}"


def uv_url(version: str, target: str) -> str:
    return f"https://github.com/astral-sh/uv/releases/download/{version}/{uv_asset(target)}"


def git_asset(version: str, target: str) -> str:
    found = GIT_VERSION.match(version)
    if found is None:
        raise ValueError(f"not a Git for Windows version: {version!r}")
    base, build = found.groups()
    shown = base if build == "1" else f"{base}.{build}"
    return f"PortableGit-{shown}-{GIT_ARCHES[target]}.7z.exe"


def git_url(version: str, target: str) -> str:
    return (
        f"https://github.com/git-for-windows/git/releases/download/v{version}/"
        f"{git_asset(version, target)}"
    )


def mirror_base(repository: str) -> str:
    return f"https://github.com/{repository}/releases/download/{MIRROR_TAG}/"


def mirror_url(repository: str, asset: str) -> str:
    """Our copy of an upstream archive; the asset name carries the tool's version."""
    return f"{mirror_base(repository)}{asset}"


def uv_mirror_asset(version: str, target: str) -> str:
    return uv_asset(target).replace("uv-", f"uv-{version}-", 1)


def fresh(repository: str, uv_version: str, git_version: str) -> dict[str, Any]:
    """A pins document with every URL filled in and no digest recorded yet."""
    uv = {
        target: {
            "url": uv_url(uv_version, target),
            "mirror": mirror_url(repository, uv_mirror_asset(uv_version, target)),
            "sha256": None,
        }
        for target in TARGETS
    }
    git = {
        target: {
            "url": git_url(git_version, target),
            "mirror": mirror_url(repository, git_asset(git_version, target)),
            "sha256": None,
        }
        for target in GIT_ARCHES
    }
    return {
        "schema": 1,
        "provisional": True,
        "release": {"repository": repository, "mirror_tag": MIRROR_TAG},
        "python": {"provision": "3.14", "floor": "3.13"},
        "uv": {"version": uv_version, "targets": uv},
        "git_for_windows": {"version": git_version, "targets": git},
    }


# -- validation -----------------------------------------------------------------------------------


def load(path: Path = PINS) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _targets(
    data: dict[str, Any], tool: str, expected: Sequence[str], mirror: str, out: list[str]
) -> None:
    targets = data.get(tool, {}).get("targets")
    if not isinstance(targets, dict):
        out.append(f"{tool}.targets is missing")
        return
    for name in sorted(set(expected) - set(targets)):
        out.append(f"{tool}.targets.{name} is missing")
    for name in sorted(set(targets) - set(expected)):
        out.append(f"{tool}.targets.{name} is not a known target")
    for name, entry in targets.items():
        where = f"{tool}.targets.{name}"
        if not isinstance(entry, dict):
            out.append(f"{where} must be an object")
            continue
        for key in ("url", "mirror"):
            value = entry.get(key)
            if not isinstance(value, str) or not URL.match(value):
                out.append(f"{where}.{key} must be an https URL, not {value!r}")
        if isinstance(entry.get("mirror"), str) and not entry["mirror"].startswith(mirror):
            out.append(f"{where}.mirror must be one of our release assets ({mirror}…)")
        digest = entry.get("sha256")
        if digest is None:
            if data.get("provisional") is not True:
                out.append(f"{where}.sha256 is null but the pins are not marked provisional")
        elif not isinstance(digest, str) or not SHA256.match(digest):
            out.append(f"{where}.sha256 must be 64 lowercase hex, not {digest!r}")


def problems(data: dict[str, Any], *, strict: bool = False) -> list[str]:
    """Everything wrong with a pins document (empty when it is usable)."""
    out: list[str] = []
    if data.get("schema") != 1:
        out.append("schema must be 1")
    release = data.get("release", {})
    repository = release.get("repository") if isinstance(release, dict) else None
    if not isinstance(repository, str) or not SLUG.match(repository):
        out.append(f"release.repository must be owner/name, not {repository!r}")
        repository = "invalid/invalid"
    python = data.get("python", {})
    for key in ("provision", "floor"):
        value = python.get(key) if isinstance(python, dict) else None
        if not isinstance(value, str) or not MINOR.match(value):
            out.append(f"python.{key} must be a 3.NN minor, not {value!r}")
    if not isinstance(data.get("uv", {}).get("version"), str) or not VERSION.match(
        data["uv"]["version"]
    ):
        out.append("uv.version must be X.Y.Z")
    git_version = data.get("git_for_windows", {}).get("version")
    if not isinstance(git_version, str) or not GIT_VERSION.match(git_version):
        out.append("git_for_windows.version must be X.Y.Z.windows.N")
    mirror = mirror_base(repository)
    _targets(data, "uv", TARGETS, mirror, out)
    _targets(data, "git_for_windows", tuple(GIT_ARCHES), mirror, out)
    if strict:
        if data.get("provisional") is not False:
            out.append("the pins are provisional: run `pins.py update` before a release")
        for tool in ("uv", "git_for_windows"):
            for name, entry in data.get(tool, {}).get("targets", {}).items():
                if isinstance(entry, dict) and entry.get("sha256") is None:
                    out.append(f"{tool}.targets.{name}.sha256 is not recorded")
    return out


# -- rendering ------------------------------------------------------------------------------------


def python_range(data: dict[str, Any]) -> str:
    major, minor = data["python"]["provision"].split(".")
    return f">={data['python']['floor']},<{major}.{int(minor) + 1}"


def render_sh(data: dict[str, Any]) -> str:
    """The section `install.sh` carries: constants and `ins_uv_pin TARGET`."""
    lines = [
        SH_BEGIN,
        f"INS_REPO_SLUG='{data['release']['repository']}'",
        f"INS_UV_VERSION='{data['uv']['version']}'",
        f"INS_PYTHON_VERSION='{data['python']['provision']}'",
        f"INS_PYTHON_RANGE='{python_range(data)}'",
        f"INS_PINS_PROVISIONAL={1 if data.get('provisional') else 0}",
        "# ins_uv_pin TARGET: sets INS_PIN_URL, INS_PIN_MIRROR and INS_PIN_SHA256 (empty while",
        "# the digest is not recorded); returns 1 for a target without a pin.",
        "ins_uv_pin() {",
        '    case "$1" in',
    ]
    for target in SH_TARGETS:
        entry = data["uv"]["targets"][target]
        lines += [
            f"        {target})",
            f"            INS_PIN_URL='{entry['url']}'",
            f"            INS_PIN_MIRROR='{entry['mirror']}'",
            f"            INS_PIN_SHA256='{entry['sha256'] or ''}'",
            "            ;;",
        ]
    lines += [
        "        *)",
        "            INS_PIN_URL='' INS_PIN_MIRROR='' INS_PIN_SHA256=''",
        "            return 1",
        "            ;;",
        "    esac",
        "}",
        SH_END,
    ]
    return "\n".join(lines) + "\n"


def section(text: str, begin: str = SH_BEGIN, end: str = SH_END) -> str | None:
    """The generated section (markers included) inside a built script, or None."""
    start = text.find(begin)
    if start < 0:
        return None
    stop = text.find(end, start)
    if stop < 0:
        return None
    return text[start : stop + len(end)] + "\n"


def check(
    pins: Path = PINS,
    script: Path = INSTALL_SH,
    *,
    strict: bool = False,
    versions: Path = VERSIONS,
) -> list[str]:
    try:
        data = load(pins)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{pins.name}: cannot read: {exc}"]
    found = problems(data, strict=strict)
    if found:
        return found
    try:
        constant = release_repository(versions)
    except (OSError, AttributeError) as exc:
        return [f"{versions.name}: cannot read RELEASE_REPOSITORY: {exc}"]
    if data["release"]["repository"] != constant:
        return [
            f"{pins.name}: release.repository is {data['release']['repository']!r} but "
            f"RELEASE_REPOSITORY is {constant!r} -> run `pins.py update` (one release base)"
        ]
    try:
        built = script.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{script.name}: cannot read: {exc}"]
    if section(built) != render_sh(data):
        return [
            f"{script.name}: the pins section differs from {pins.name}"
            " -> run `python scripts/install/build.py` (never edit the built script)"
        ]
    return []


# -- update ---------------------------------------------------------------------------------------


def fetch_url(url: str) -> bytes:  # pragma: no cover - network
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - https URLs only
        return response.read()


def update(data: dict[str, Any], uv_version: str, git_version: str, fetch: Fetch) -> dict[str, Any]:
    """A new pins document for these versions with every digest computed from a download.

    Raises on the first failed download, before anything is written."""
    new = fresh(release_repository(), uv_version, git_version)
    new["python"] = dict(data.get("python") or new["python"])
    for tool in ("uv", "git_for_windows"):
        for entry in new[tool]["targets"].values():
            entry["sha256"] = hashlib.sha256(fetch(entry["url"])).hexdigest()
    new["provisional"] = False
    found = problems(new, strict=True)
    if found:
        raise ValueError("; ".join(found))
    return new


def write(data: dict[str, Any], path: Path = PINS) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def open_pr(uv_version: str, git_version: str, run: Runner) -> int:
    """Branch, commit and open a pull request; merging stays a reviewer's decision."""
    branch = f"pins/uv-{uv_version}-git-{git_version}"
    title = f"Bootstrap pins: uv {uv_version}, Git for Windows {git_version}"
    body = (
        "Digests computed by `scripts/install/pins.py update`. Check each against the upstream "
        "release page before merging; this change is never merged automatically."
    )
    steps: list[list[str]] = [
        ["git", "switch", "-c", branch],
        ["git", "add", "scripts/install/pins.json", "scripts/install.sh", "scripts/install.ps1"],
        ["git", "commit", "-m", title],
        ["gh", "pr", "create", "--draft", "--title", title, "--body", body],
    ]
    for step in steps:
        code = run(step)
        if code != 0:
            print(f"stopped: {' '.join(step)} exited {code}", file=sys.stderr)
            return code
    return 0


def _run(argv: Sequence[str]) -> int:  # pragma: no cover - real git/gh
    return subprocess.run(list(argv), cwd=ROOT, check=False).returncode


def main(argv: Sequence[str] | None = None, *, fetch: Fetch = fetch_url, run: Runner = _run) -> int:
    parser = argparse.ArgumentParser(prog="pins.py", description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="validate pins.json and install.sh")
    parser.add_argument("--strict", action="store_true", help="with --check: no provisional pins")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("render", help="print the generated install.sh section")
    upd = sub.add_parser("update", help="download, compute digests, rewrite pins.json")
    upd.add_argument("--uv", required=True, help="uv version, X.Y.Z")
    upd.add_argument("--git", help="Git for Windows version, X.Y.Z.windows.N (default: keep)")
    upd.add_argument("--open-pr", action="store_true", help="branch, commit and open a draft PR")
    args = parser.parse_args(argv)

    if args.check:
        found = check(strict=args.strict)
        for line in found:
            print(f"pins: {line}", file=sys.stderr)
        if not found:
            print("pins: ok")
        return 1 if found else 0
    if args.command == "render":
        data = load()
        sys.stdout.write(render_sh(data))
        return 0
    if args.command == "update":
        data = load()
        git_version = args.git or data["git_for_windows"]["version"]
        try:
            new = update(data, args.uv, git_version, fetch)
        except (OSError, ValueError) as exc:
            print(f"pins: update failed, nothing written: {exc}", file=sys.stderr)
            return 1
        write(new)
        sys.path.insert(0, str(HERE))
        import build  # noqa: PLC0415 - sibling script, loaded after the pins are written

        build.main([])
        print("pins: updated pins.json and the built installers; review the diff.")
        if args.open_pr:
            return open_pr(args.uv, git_version, run)
        print(
            "Open a pull request for review (pins.py update --open-pr does it); never auto-merge."
        )
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
