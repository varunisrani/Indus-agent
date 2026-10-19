"""Malware preflight for package-runner servers (P13-11)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.mcp.config import McpServer
from indus.mcp.connect import ConnectOptions, LiveSession, is_permanent
from indus.mcp.osv import (
    CACHE_TTL_S,
    WALL_BOUND_S,
    McpMalwareRefused,
    OsvChecker,
    PackageRef,
    guarded_opener,
    package_for,
)
from tests.fakes.clock import FakeClock, checkpoint
from tests.fakes.terminal import terminal_scope

START = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


class FakeOsv:
    def __init__(self, malicious: dict[str, list[str]] | None = None) -> None:
        self.malicious = malicious or {}
        self.queries: list[dict[str, Any]] = []
        self.hang = False
        self.fail = False

    async def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.queries.append(body)
        if self.hang:
            await asyncio.Event().wait()
        if self.fail:
            raise httpx.ConnectError("no route to host")
        name = body["package"]["name"]
        vulns: list[dict[str, Any]] = [
            {"id": i, "summary": "malicious"} for i in self.malicious.get(name, [])
        ]
        vulns.append({"id": "GHSA-xxxx-yyyy-zzzz", "aliases": ["CVE-2020-1"]})  # not malware
        return httpx.Response(200, json={"vulns": vulns})

    def checker(self, home: Path, clock: Clock) -> OsvChecker:
        transport = httpx.MockTransport(self.handle)
        return OsvChecker(
            home, clock=clock, client_factory=lambda: httpx.AsyncClient(transport=transport)
        )


@pytest.mark.parametrize(
    ("command", "args", "expected"),
    [
        (
            "npx",
            ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            ("npm", "@modelcontextprotocol/server-filesystem", None),
        ),
        ("npx", ["--yes", "evil-pkg@1.2.3"], ("npm", "evil-pkg", "1.2.3")),
        ("/usr/local/bin/npx.cmd", ["-p", "tool@2.0.0", "tool-bin"], ("npm", "tool", "2.0.0")),
        ("npx", ["--package=tool@latest", "x"], ("npm", "tool", None)),
        ("npx", ["@scope/pkg@^1.0.0"], ("npm", "@scope/pkg", None)),
        ("bunx", ["--bun", "pkg@0.1.0-beta.1"], ("npm", "pkg", "0.1.0-beta.1")),
        ("pnpm", ["dlx", "pkg@3.1.4"], ("npm", "pkg", "3.1.4")),
        ("uvx", ["mcp-server-fetch"], ("PyPI", "mcp-server-fetch", None)),
        (
            "uvx",
            ["--python", "3.12", "mcp_server.Time==0.6.2"],
            ("PyPI", "mcp-server-time", "0.6.2"),
        ),
        ("uvx", ["--from", "pkg[extra]==1.0", "pkg-cli"], ("PyPI", "pkg", "1.0")),
        ("uvx", ["tool@1.4.0"], ("PyPI", "tool", "1.4.0")),
        ("uvx", ["tool>=1.0"], ("PyPI", "tool", None)),
        ("pipx", ["run", "--spec", "black==24.1.0", "black"], ("PyPI", "black", "24.1.0")),
        ("pipx", ["run", "cowsay"], ("PyPI", "cowsay", None)),
    ],
)
def test_runner_arguments_name_the_package(
    command: str, args: list[str], expected: tuple[str, str, str | None]
) -> None:
    ref = package_for(command, args)
    assert ref is not None
    assert (ref.ecosystem, ref.name, ref.version) == expected


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("node", ["server.js"]),
        ("python", ["-m", "server"]),
        ("npx", ["-y"]),
        ("npx", ["./local-dir"]),
        ("npx", ["github:user/repo"]),
        ("pnpm", ["install"]),
        ("pipx", ["install", "black"]),
        ("uvx", ["git+https://example.test/repo"]),
    ],
)
def test_other_commands_are_not_checked(command: str, args: list[str]) -> None:
    assert package_for(command, args) is None


def test_malicious_package_is_refused_with_its_advisory_id(tmp_path: Path) -> None:
    osv = FakeOsv({"evil-pkg": ["MAL-2026-1234"]})
    checker = osv.checker(tmp_path, FakeClock(START))
    with pytest.raises(McpMalwareRefused) as caught:
        asyncio.run(checker.preflight("files", "npx", ["-y", "evil-pkg@1.2.3"]))
    assert "MAL-2026-1234" in str(caught.value) and caught.value.advisories == ("MAL-2026-1234",)
    assert is_permanent(caught.value)
    assert osv.queries == [
        {"package": {"name": "evil-pkg", "ecosystem": "npm"}, "version": "1.2.3"}
    ]


def test_clean_package_starts_and_unversioned_is_queried_by_name(tmp_path: Path) -> None:
    osv = FakeOsv()
    checker = osv.checker(tmp_path, FakeClock(START))
    ref = asyncio.run(checker.preflight("fetch", "uvx", ["mcp-server-fetch"]))
    assert ref == PackageRef("PyPI", "mcp-server-fetch", None)
    assert osv.queries == [{"package": {"name": "mcp-server-fetch", "ecosystem": "PyPI"}}]


def test_timeout_fails_open_after_twelve_seconds(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = FakeClock(START)
    osv = FakeOsv({"slow": ["MAL-1"]})
    osv.hang = True
    checker = osv.checker(tmp_path, clock)

    async def go() -> PackageRef | None:
        task = asyncio.ensure_future(checker.preflight("files", "npx", ["slow@1.0.0"]))
        while not clock.pending:
            await checkpoint()
        clock.advance(WALL_BOUND_S - 0.5)
        await checkpoint()
        assert not task.done()
        clock.advance(0.5)
        return await task

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(go()) == PackageRef("npm", "slow", "1.0.0")
    assert "starting anyway" in caplog.text
    osv.hang = False
    with pytest.raises(McpMalwareRefused):  # a failure is not cached: the next start asks again
        asyncio.run(checker.preflight("files", "npx", ["slow@1.0.0"]))


def test_network_error_fails_open(tmp_path: Path) -> None:
    osv = FakeOsv({"pkg": ["MAL-9"]})
    osv.fail = True
    checker = osv.checker(tmp_path, FakeClock(START))
    assert asyncio.run(checker.preflight("files", "npx", ["pkg"])) is not None


def test_answers_are_cached_for_a_day_on_disk(tmp_path: Path) -> None:
    clock = FakeClock(START)
    osv = FakeOsv({"evil": ["MAL-5"]})
    checker = osv.checker(tmp_path, clock)
    asyncio.run(checker.preflight("a", "npx", ["clean@1.0.0"]))
    asyncio.run(checker.preflight("a", "npx", ["clean@1.0.0"]))
    for _ in range(2):
        with pytest.raises(McpMalwareRefused):
            asyncio.run(checker.preflight("b", "npx", ["evil@2.0.0"]))
    assert len(osv.queries) == 2
    # A new process (new checker) on the same home reads the cache file.
    again = osv.checker(tmp_path, clock)
    asyncio.run(again.preflight("a", "npx", ["clean@1.0.0"]))
    assert len(osv.queries) == 2
    assert (tmp_path / "cache" / "mcp" / "osv.json").is_file()
    clock.advance(CACHE_TTL_S + 1)
    asyncio.run(again.preflight("a", "npx", ["clean@1.0.0"]))
    assert len(osv.queries) == 3


def test_non_runner_command_makes_no_request(tmp_path: Path) -> None:
    osv = FakeOsv()
    checker = osv.checker(tmp_path, FakeClock(START))
    assert asyncio.run(checker.preflight("files", "node", ["server.js"])) is None
    assert osv.queries == []


class Recorder:
    def __init__(self) -> None:
        self.opened: list[str] = []

    @asynccontextmanager
    async def __call__(
        self,
        name: str,
        server: McpServer,
        scope: Scope,
        *,
        clock: Clock,
        options: ConnectOptions | None = None,
    ) -> AsyncGenerator[LiveSession]:
        self.opened.append(name)
        yield cast(LiveSession, object())


def test_guarded_opener_refuses_before_spawning(tmp_path: Path) -> None:
    clock = FakeClock(START)
    osv = FakeOsv({"evil": ["MAL-7"]})
    checker = osv.checker(tmp_path, clock)
    inner = Recorder()
    opener = guarded_opener(inner, checker=lambda scope: checker)
    scope = terminal_scope(tmp_path)

    async def open_one(name: str, server: McpServer, at: Scope) -> None:
        async with opener(name, server, at, clock=clock):
            pass

    with pytest.raises(McpMalwareRefused):
        asyncio.run(open_one("bad", McpServer(command="npx", args=["-y", "evil@1.0.0"]), scope))
    asyncio.run(open_one("good", McpServer(command="npx", args=["-y", "fine@1.0.0"]), scope))
    asyncio.run(open_one("web", McpServer(url="https://mcp.example.test/mcp"), scope))
    off = terminal_scope(tmp_path, {"mcp": {"osv_preflight": False}}, name="off")
    asyncio.run(open_one("off", McpServer(command="npx", args=["evil@1.0.0"]), off))
    assert inner.opened == ["good", "web", "off"]
    assert [q["package"]["name"] for q in osv.queries] == ["evil", "fine"]
