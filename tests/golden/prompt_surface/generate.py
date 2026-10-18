"""Render the stock system prompt and tool array of each surface (P04-22, trap R01.1).

Every input is pinned so the output depends on our code alone: a fake clock, a fixed home and
working directory, explicit runtime facts (never the build machine's OS), the fake-model route,
the stock config defaults, and a file reader that finds nothing (no persona or project file on
disk can leak in). Each surface gets ``<surface>/prompt.txt`` (the prompt plus one newline) and
``<surface>/tools.json`` (the tool array, sorted keys, the compact layout of
``protocol.codegen.dump_json``, trailing newline).

    uv run python -m tests.golden.prompt_surface.generate                  # compare; 1 on drift
    uv run python -m tests.golden.prompt_surface.generate --update-golden  # rewrite the files

Until P05 registers its tools every tool array is ``[]`` and no prompt carries tool guidance;
regenerate (with a note) when the tools land.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from indus.core.prompt.builder import BuildContext, PromptBuilder, RuntimeInfo
from indus.foundation.clock import TIMEZONE_ENV
from indus.foundation.config import defaults_tree
from indus.foundation.config.loader import ConfigView
from indus.foundation.scope import Scope, make_scope
from indus.protocol.codegen import Json, dump_json
from indus.providers.route import RuntimeRoute
from indus.tools import toolsets
from indus.tools.builtin import load_builtins
from indus.tools.definitions import definitions
from indus.tools.registry import ToolRegistry
from tests.fakes.clock import FakeClock
from tests.fakes.model import Script
from tests.fakes.model.server import FakeModelServer, fake_route

GOLDEN_DIR = Path(__file__).resolve().parent
PROMPT_FILE = "prompt.txt"
TOOLS_FILE = "tools.json"
REGENERATE = "uv run pytest tests/golden/prompt_surface --update-golden"

# The pinned world every surface is rendered in.
CLOCK_START = datetime(2026, 3, 14, 10, 30, tzinfo=UTC)
ZONE = "UTC"
INDUS_HOME = Path("/home/golden/.indus")
USER_HOME = "/home/golden"
WORKDIR = "/home/golden/project"
HOST = "Linux"


@dataclass(frozen=True)
class Surface:
    """One place the agent runs, with what changes its stock prompt and tools."""

    name: str
    platform: str
    bundle: str
    context_files: bool = True
    persona_without_context_files: bool = False


# Surfaces known at P04; later phases add theirs.
SURFACES: tuple[Surface, ...] = (
    Surface("cli", "cli", "indus-cli"),
    # Scheduled jobs keep the persona but never read the working directory's files.
    Surface(
        "cron",
        "cron",
        "indus-cron",
        context_files=False,
        persona_without_context_files=True,
    ),
    Surface("api_server", "api_server", "indus-api-server"),
    Surface("acp", "acp", "indus-acp"),
)
SURFACE_NAMES = tuple(s.name for s in SURFACES)


def surface_named(name: str) -> Surface:
    for surface in SURFACES:
        if surface.name == name:
            return surface
    raise KeyError(f"unknown surface {name!r}; known: {', '.join(SURFACE_NAMES)}")


# -- the pinned inputs ----------------------------------------------------------------------------


def no_files(path: Path) -> str:
    """The golden reader: no file exists, whatever the disk holds."""
    raise FileNotFoundError(path)


def golden_scope() -> Scope:
    scope = make_scope(INDUS_HOME, env={TIMEZONE_ENV: ZONE}, clock=FakeClock(CLOCK_START))
    stock = ConfigView(INDUS_HOME / "config.yaml", defaults_tree(), {}, set())
    return scope.with_config(stock)


def golden_route() -> RuntimeRoute:
    return fake_route(FakeModelServer(Script(turns=(), name="golden")))


def golden_builder() -> PromptBuilder:
    return PromptBuilder(reader=no_files, install=None)


def tool_array(surface: Surface, scope: Scope, route: RuntimeRoute) -> list[dict[str, Any]]:
    """The surface's tool array from a fresh registry holding the built-in tools."""
    registry = ToolRegistry()
    load_builtins(registry)
    selection = toolsets.select(
        [surface.bundle], registry=registry, scope=scope, platform=surface.platform
    )
    return definitions(registry, selection.tools, route, scope=scope)


def tool_names(tools: Iterable[dict[str, Any]]) -> frozenset[str]:
    return frozenset(str(entry["function"]["name"]) for entry in tools)


def build_context(
    surface: Surface,
    scope: Scope,
    route: RuntimeRoute,
    tools: Iterable[dict[str, Any]],
    *,
    session_id: str = "",
) -> BuildContext:
    return BuildContext(
        scope=scope,
        model=route.model,
        provider=route.provider,
        platform=surface.platform,
        session_id=session_id,
        tool_names=tool_names(tools),
        cwd=Path(WORKDIR),
        context_files=surface.context_files,
        persona_without_context_files=surface.persona_without_context_files,
        started_at=CLOCK_START,
        runtime=RuntimeInfo(host=HOST, home=USER_HOME, cwd=WORKDIR),
    )


# -- rendering ------------------------------------------------------------------------------------


def prompt_text(prompt: str) -> str:
    return prompt + "\n"


def tools_text(tools: Sequence[dict[str, Any]]) -> str:
    # Same compact layout as the protocol schema: a container that fits in 100 columns stays on
    # one line, so the file reads as our tool array rather than a run of schema keywords.
    return dump_json(cast(Json, list(tools))) + "\n"


@dataclass(frozen=True)
class Rendered:
    surface: Surface
    prompt: str
    tools: str

    def files(self) -> dict[str, str]:
        return {PROMPT_FILE: self.prompt, TOOLS_FILE: self.tools}


async def render_async(surface: Surface) -> Rendered:
    scope, route = golden_scope(), golden_route()
    tools = tool_array(surface, scope, route)
    parts = await golden_builder().build(build_context(surface, scope, route, tools))
    return Rendered(surface, prompt_text(parts.text), tools_text(tools))


def render(surface: Surface) -> Rendered:
    return asyncio.run(render_async(surface))


# -- comparing and writing ------------------------------------------------------------------------


def fixture_path(surface: Surface, file_name: str) -> Path:
    return GOLDEN_DIR / surface.name / file_name


def read_fixture(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return ""


def drift(expected: str, actual: str, label: str) -> str:
    """A unified diff from the checked-in text to the rendered one ("" when identical)."""
    if expected == actual:
        return ""
    lines = difflib.unified_diff(
        expected.splitlines(keepends=True),
        actual.splitlines(keepends=True),
        fromfile=f"{label} (checked in)",
        tofile=f"{label} (rendered now)",
    )
    body = "".join(line if line.endswith("\n") else line + "\n" for line in lines)
    return f"{body}golden file {label} is stale; if the change is intended, run: {REGENERATE}\n"


def check(rendered: Rendered) -> list[str]:
    """One diff per file that differs from its fixture."""
    problems: list[str] = []
    for file_name, text in rendered.files().items():
        path = fixture_path(rendered.surface, file_name)
        label = path.relative_to(GOLDEN_DIR).as_posix()
        found = drift(read_fixture(path), text, label)
        if found:
            problems.append(found)
    return problems


def write(rendered: Rendered) -> list[Path]:
    """Rewrite the fixtures whose bytes changed (LF endings); return the paths written."""
    written: list[Path] = []
    for file_name, text in rendered.files().items():
        path = fixture_path(rendered.surface, file_name)
        if read_fixture(path) == text and path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        written.append(path)
    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--update-golden", action="store_true", help="rewrite the golden files instead"
    )
    parser.add_argument(
        "--surface", action="append", choices=SURFACE_NAMES, help="only this surface (repeatable)"
    )
    args = parser.parse_args(argv)
    chosen = [surface_named(n) for n in args.surface] if args.surface else list(SURFACES)
    failed = False
    for surface in chosen:
        rendered = render(surface)
        if args.update_golden:
            for path in write(rendered):
                print(f"wrote {path.relative_to(GOLDEN_DIR).as_posix()}")
            continue
        for problem in check(rendered):
            failed = True
            sys.stdout.write(problem)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
