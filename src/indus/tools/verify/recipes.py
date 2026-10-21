# pyright: strict
"""How to check a workspace (P24-17): a recipe of commands found from the files in it.

The first lockfile present decides the package manager: ``pnpm-lock.yaml`` (pnpm), ``bun.lock`` /
``bun.lockb`` (bun), ``yarn.lock`` (yarn), ``package-lock.json`` (npm), ``uv.lock`` (uv),
``poetry.lock`` (poetry), ``Pipfile.lock`` (pipenv).

Detectors, first match wins: a readable ``package.json`` (Node: the framework comes from the
dependencies, phases from the scripts), then Python (Django, FastAPI/Uvicorn, Flask, or plain),
Go, Rust, Maven, Gradle, a ``Makefile`` (its targets) and a compose file. Each recipe lists
``bootstrap``, ``build`` and ``test`` commands (run in that order), an optional ``start`` command
with the port it serves on and the path that answers when it is ready. Servers started by a
recipe bind 127.0.0.1.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

__all__ = [
    "LOCKFILES",
    "Recipe",
    "detect_recipe",
    "make_targets",
    "package_manager",
]

_LOCKS_BY_MANAGER: dict[str, tuple[str, ...]] = {
    "pnpm": ("pnpm-lock.yaml",),
    "bun": ("bun.lock", "bun.lockb"),
    "yarn": ("yarn.lock",),
    "npm": ("package-lock.json",),
    "uv": ("uv.lock",),
    "poetry": ("poetry.lock",),
    "pipenv": ("Pipfile.lock",),
}
LOCKFILES: tuple[tuple[str, str], ...] = tuple(
    (lock, manager) for manager, locks in _LOCKS_BY_MANAGER.items() for lock in locks
)
"""``(lockfile, manager)`` in the order they are looked for."""

COMPOSE_FILES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")


@dataclass(slots=True)
class Recipe:
    name: str
    kind: str
    bootstrap: list[str] = field(default_factory=lambda: list[str]())
    build: list[str] = field(default_factory=lambda: list[str]())
    test: list[str] = field(default_factory=lambda: list[str]())
    start: str | None = None
    port: int | None = None
    ready_path: str = "/"
    evidence: list[str] = field(default_factory=lambda: list[str]())

    def commands(self, phase: str) -> list[str]:
        return list(getattr(self, phase)) if phase in ("bootstrap", "build", "test") else []

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _text(root: Path, name: str) -> str | None:
    try:
        return (root / name).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None


def _exists(root: Path, *names: str) -> str | None:
    return next((n for n in names if (root / n).exists()), None)


def package_manager(root: Path | str) -> str | None:
    folder = Path(root)
    return next((manager for name, manager in LOCKFILES if (folder / name).exists()), None)


# -- Node ----------------------------------------------------------------------------------------

_FRAMEWORKS: tuple[tuple[tuple[str, ...], str, str, int], ...] = (
    (("next",), "nextjs", "Next.js app", 3000),
    (("@sveltejs/kit",), "sveltekit", "SvelteKit app", 5173),
    (("astro",), "astro", "Astro site", 4321),
    (("@remix-run/dev", "@remix-run/react"), "remix", "Remix app", 3000),
    (("react-scripts",), "cra", "Create React App", 3000),
    (("vite",), "vite", "Vite app", 5173),
)
_RUN = {"pnpm": "pnpm {}", "bun": "bun run {}", "yarn": "yarn {}"}
_INSTALL = {"pnpm": "pnpm install", "bun": "bun install", "yarn": "yarn install"}
_PORT = (re.compile(r"(?:--port|-p)[ =](\d{2,5})\b"), re.compile(r"\bPORT=(\d{2,5})\b"))


def _port_in(command: str) -> int | None:
    for pattern in _PORT:
        found = pattern.search(command)
        if found is not None:
            value = int(found.group(1))
            if 0 < value < 65536:
                return value
    return None


def _package_json(root: Path) -> dict[str, Any] | None:
    raw = _text(root, "package.json")
    if raw is None:
        return None
    try:
        parsed: object = json.loads(raw)
    except ValueError:
        return None
    return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else None


def _mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def _node(root: Path, package: Mapping[str, Any]) -> Recipe:
    scripts = {
        k: v for k, v in _mapping(package.get("scripts")).items() if isinstance(v, str) and v
    }
    deps = {**_mapping(package.get("dependencies")), **_mapping(package.get("devDependencies"))}
    manager = package_manager(root)
    kind, label, port = "node", "Node.js project", None
    for names, fw_kind, fw_label, fw_port in _FRAMEWORKS:
        if any(n in deps for n in names):
            kind, label, port = fw_kind, fw_label, fw_port
            break
    runner = _RUN.get(manager or "", "npm run {}")

    def run(*names: str) -> list[str]:
        return [runner.format(n) for n in names if n in scripts]

    start_script = next((s for s in ("dev", "start") if s in scripts), None)
    evidence = ["package.json"]
    if manager:
        evidence.append(f"package manager: {manager}")
    evidence.append("scripts: " + (", ".join(scripts) or "(none)"))
    return Recipe(
        name=label,
        kind=kind,
        bootstrap=[_INSTALL.get(manager or "", "npm install")],
        build=run("build", "typecheck"),
        test=run("test", "check", "lint"),
        start=runner.format(start_script) if start_script else None,
        port=(_port_in(scripts[start_script]) or port) if start_script else None,
        evidence=evidence,
    )


# -- Python ---------------------------------------------------------------------------------------

_PY_INSTALL = {"uv": "uv sync", "poetry": "poetry install", "pipenv": "pipenv install"}


def _python(root: Path) -> Recipe | None:
    pyproject = _text(root, "pyproject.toml")
    requirements = _text(root, "requirements.txt")
    manage = (root / "manage.py").exists()
    setup = (root / "setup.py").exists()
    if pyproject is None and requirements is None and not manage and not setup:
        return None
    words = f"{pyproject or ''}\n{requirements or ''}".lower()
    install = _PY_INSTALL.get(package_manager(root) or "")
    if install is None:
        install = (
            "pip install -r requirements.txt" if requirements is not None else "pip install -e ."
        )
    pytest = ["pytest"] if (root / "tests").is_dir() else []
    seen = [n for n in ("pyproject.toml", "requirements.txt", "setup.py") if (root / n).exists()]
    if manage or "django" in words:
        return Recipe(
            "Django app",
            "django",
            bootstrap=[install],
            test=["python manage.py test"],
            start="python manage.py runserver 127.0.0.1:8000",
            port=8000,
            evidence=["manage.py" if manage else "Django dependency", *seen],
        )
    if "fastapi" in words or "uvicorn" in words:
        module = (_exists(root, "main.py", "app.py") or "main.py").removesuffix(".py")
        return Recipe(
            "FastAPI app",
            "fastapi",
            bootstrap=[install],
            test=pytest,
            start=f"uvicorn {module}:app --host 127.0.0.1 --port 8000",
            port=8000,
            evidence=["FastAPI/Uvicorn dependency", *seen],
        )
    if "flask" in words:
        app = _exists(root, "app.py", "main.py") or "app.py"
        return Recipe(
            "Flask app",
            "flask",
            bootstrap=[install],
            test=pytest,
            start=f"flask --app {app} run --host 127.0.0.1 --port 5000",
            port=5000,
            evidence=["Flask dependency", *seen],
        )
    return Recipe(
        "Python project",
        "python",
        bootstrap=[install],
        test=pytest or ["python -m unittest discover"],
        evidence=seen or ["manage.py"],
    )


# -- compiled toolchains -------------------------------------------------------------------------


def _toolchain(root: Path) -> Recipe | None:
    if (root / "go.mod").exists():
        return Recipe(
            "Go module",
            "go",
            build=["go build ./..."],
            test=["go test ./..."],
            start="go run ." if (root / "main.go").exists() else None,
            evidence=["go.mod"],
        )
    if (root / "Cargo.toml").exists():
        return Recipe(
            "Rust crate",
            "rust",
            build=["cargo build"],
            test=["cargo test"],
            start="cargo run" if (root / "src" / "main.rs").exists() else None,
            evidence=["Cargo.toml"],
        )
    if (root / "pom.xml").exists():
        return Recipe(
            "Maven project", "maven", build=["mvn package"], test=["mvn test"], evidence=["pom.xml"]
        )
    gradle_file = _exists(root, "build.gradle", "build.gradle.kts")
    if gradle_file is not None:
        gradle = "./gradlew" if (root / "gradlew").exists() else "gradle"
        return Recipe(
            "Gradle project",
            "gradle",
            build=[f"{gradle} build"],
            test=[f"{gradle} test"],
            evidence=[gradle_file],
        )
    return None


# -- make and compose -----------------------------------------------------------------------------

_TARGET = re.compile(r"^([A-Za-z0-9_.-]+)\s*:(?![=:])")
_MAKE_PHASES: dict[str, tuple[str, ...]] = {
    "bootstrap": ("install", "setup", "bootstrap"),
    "build": ("build", "compile"),
    "test": ("test", "check"),
    "start": ("run", "start", "serve", "dev"),
}


def make_targets(text: str) -> list[str]:
    """Target names defined at the start of a line (variable assignments are not targets)."""
    found: list[str] = []
    for line in text.splitlines():
        match = _TARGET.match(line)
        if match is not None and not match.group(1).startswith(".") and match.group(1) not in found:
            found.append(match.group(1))
    return found


def _first(targets: Sequence[str], names: Sequence[str]) -> list[str]:
    hit = next((n for n in names if n in targets), None)
    return [f"make {hit}"] if hit else []


def _make(root: Path) -> Recipe | None:
    text = _text(root, "Makefile")
    if text is None:
        return None
    targets = make_targets(text)
    start = _first(targets, _MAKE_PHASES["start"])
    return Recipe(
        "Makefile project",
        "make",
        bootstrap=_first(targets, _MAKE_PHASES["bootstrap"]),
        build=_first(targets, _MAKE_PHASES["build"]),
        test=_first(targets, _MAKE_PHASES["test"]),
        start=start[0] if start else None,
        evidence=["Makefile", "targets: " + (", ".join(targets) or "(none)")],
    )


def _compose(root: Path) -> Recipe | None:
    found = _exists(root, *COMPOSE_FILES)
    if found is None:
        return None
    return Recipe(
        "Compose project",
        "compose",
        build=["docker compose build"],
        start="docker compose up",
        evidence=[found],
    )


def detect_recipe(root: Path | str) -> Recipe | None:
    """The recipe for the workspace at ``root``, or None when nothing is recognised."""
    folder = Path(root)
    if not folder.is_dir():
        return None
    package = _package_json(folder)
    if package is not None:
        return _node(folder, package)
    return _python(folder) or _toolchain(folder) or _make(folder) or _compose(folder)
