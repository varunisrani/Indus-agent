"""search_files over a real local environment and over a scripted host (P05-21)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from indus.backends.local import LocalEnvironment
from indus.foundation.scope import Scope
from indus.tools.builtin import search_files as sf
from indus.tools.builtin.search_files import (
    Admission,
    SearchFilesTool,
    is_sensitive_path,
    macos_exclusions,
    read_request,
)
from indus.tools.registry import ToolRegistry
from tests.conftest import FAKE_CLOCK_START
from tests.fakes.clock import FakeClock

needs_rg = pytest.mark.skipif(
    shutil.which("rg") is None or shutil.which("bash") is None, reason="needs ripgrep and bash"
)


# -- a real local environment over a temp tree ---------------------------------------------------


def _tree(root: Path) -> Path:
    files = {
        "src/app.py": "import os\n\ndef main():\n    print('hello world')\n    return 0\n",
        "src/util.py": "def helper():\n    return 'hello'\n",
        "src/-dash.txt": "-flag here\n",
        "docs/readme.md": "# Hello\nhello again\n",
        "prod.env": "API_TOKEN=hello-secret\n",
        ".env": "TOKEN=hello-hidden\n",
        "multi.txt": "alpha\nbeta\n",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _search(
    scope: Scope,
    tmp_path: Path,
    args: Mapping[str, Any],
    *,
    env_overrides: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    work = tmp_path / "work"
    if not work.exists():
        _tree(work)
    temp = tmp_path / "t"
    temp.mkdir(exist_ok=True)

    async def go() -> str:
        env = LocalEnvironment(
            scope, cwd=str(work), timeout=30, temp_dir=str(temp), env_overrides=env_overrides
        )
        await env.start()
        try:
            return await SearchFilesTool().search(read_request(args), env, scope=scope)
        finally:
            await env.cleanup()

    return json.loads(asyncio.run(go()))


@needs_rg
def test_content_mode_lines_and_context(scope: Scope, tmp_path: Path) -> None:
    body = _search(scope, tmp_path, {"pattern": "hello wor", "path": "src"})
    assert body["target"] == "content" and body["output_mode"] == "content"
    assert body["matches"] == [
        {"path": "src/app.py", "line": 4, "text": "    print('hello world')"}
    ]
    assert body["total_count"] == 1 and body["truncated"] is False
    with_context = _search(scope, tmp_path, {"pattern": "hello wor", "path": "src", "context": 1})
    lines = [(m["line"], m.get("context", False)) for m in with_context["matches"]]
    assert lines == [(3, True), (4, False), (5, True)]


@needs_rg
def test_files_only_and_count_modes(scope: Scope, tmp_path: Path) -> None:
    files = _search(scope, tmp_path, {"pattern": "hello", "output_mode": "files_only"})
    assert sorted(files["files"]) == ["docs/readme.md", "src/app.py", "src/util.py"]
    counts = _search(scope, tmp_path, {"pattern": "(?i)hello", "output_mode": "count"})
    table = {c["path"]: c["count"] for c in counts["counts"]}
    assert table == {"docs/readme.md": 2, "src/app.py": 1, "src/util.py": 1}
    assert counts["total_matching_lines"] == 4


@needs_rg
def test_paging_with_next_offset(scope: Scope, tmp_path: Path) -> None:
    first = _search(scope, tmp_path, {"pattern": "(?i)hello", "limit": 2})
    assert first["shown"] == 2 and first["truncated"] is True and first["next_offset"] == 2
    rest = _search(scope, tmp_path, {"pattern": "(?i)hello", "limit": 2, "offset": 2})
    assert rest["shown"] == 2 and "next_offset" not in rest
    seen = {(m["path"], m["line"]) for m in first["matches"] + rest["matches"]}
    assert len(seen) == 4


@needs_rg
def test_env_file_hits_are_filtered(scope: Scope, tmp_path: Path) -> None:
    body = _search(scope, tmp_path, {"pattern": "secret"})
    assert body["matches"] == [] and body["filtered"] == 1
    assert "credential or env files" in body["notes"][0]
    direct = _search(scope, tmp_path, {"pattern": "hidden", "path": ".env"})
    assert direct["matches"] == [] and direct["filtered"] == 1
    names = _search(scope, tmp_path, {"pattern": "*.env", "target": "files"})
    assert names["files"] == [] and names["filtered"] >= 1  # an include glob shows dotfiles


@needs_rg
def test_file_name_search_discovery_and_modified(scope: Scope, tmp_path: Path) -> None:
    found = _search(scope, tmp_path, {"pattern": "*.py", "target": "files"})
    assert sorted(found["files"]) == ["src/app.py", "src/util.py"]
    word = _search(scope, tmp_path, {"pattern": "UTIL", "target": "find"})
    assert [Path(f).name for f in word["files"]] == ["util.py"]
    work = tmp_path / "work"
    oldest_first = ["multi.txt", "src/-dash.txt", "docs/readme.md", "src/util.py", "src/app.py"]
    for age, rel in enumerate(oldest_first):
        stamp = 1_700_000_000 + age * 1000
        os.utime(work / rel, (stamp, stamp))
    newest = _search(
        scope, tmp_path, {"pattern": "*.*", "target": "files", "order": "modified", "limit": 3}
    )
    assert newest["order"] == "modified"
    assert newest["files"] == ["src/app.py", "src/util.py", "docs/readme.md"]
    assert newest["next_offset"] == 3


@needs_rg
def test_rg_missing_is_a_clear_error(
    scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The local backend completes PATH with the standard bin folders, so a PATH override cannot
    # hide a system rg; a command name nobody has stands in for "not on PATH".
    monkeypatch.setattr(sf, "RG", "rg-not-installed-anywhere")
    body = _search(scope, tmp_path, {"pattern": "x"})
    assert "needs ripgrep" in body["error"] and "indus doctor" in body["error"]


@needs_rg
def test_missing_path_dash_pattern_and_newline_regex(scope: Scope, tmp_path: Path) -> None:
    missing = _search(scope, tmp_path, {"pattern": "x", "path": "nope"})
    assert missing == {"error": "Path not found: nope"}
    dash = _search(scope, tmp_path, {"pattern": "-flag", "path": "src"})
    assert [m["path"] for m in dash["matches"]] == ["src/-dash.txt"]
    newline = _search(scope, tmp_path, {"pattern": "alpha\\nbeta"})
    assert [m["path"] for m in newline["matches"]] == ["multi.txt"]
    assert any("multiline" in note for note in newline["notes"])


@needs_rg
def test_bad_regex_is_reported(scope: Scope, tmp_path: Path) -> None:
    body = _search(scope, tmp_path, {"pattern": "(unclosed"})
    assert body["error"].startswith("ripgrep failed (exit 2)")
    assert "regex" in body["error"]


# -- a scripted host --------------------------------------------------------------------------


@dataclass(frozen=True)
class Out:
    output: str
    returncode: int = 0
    timed_out: bool = False
    interrupted: bool = False


class ScriptHost:
    def __init__(self, answer: Callable[[str], Out], name: str = "fake") -> None:
        self.answer = answer
        self.name = name
        self.commands: list[tuple[str, bool]] = []
        self.gate: asyncio.Event | None = None

    @property
    def cwd(self) -> str:
        return "/work"

    async def execute(
        self,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        stdin: str | None = None,
        bounded: bool = True,
        cancel: Any = None,
    ) -> Out:
        self.commands.append((command, bounded))
        if self.gate is not None:
            await self.gate.wait()
        return self.answer(command)


def _match(path: str, line: int, text: str) -> str:
    return json.dumps(
        {
            "type": "match",
            "data": {"path": {"text": path}, "lines": {"text": text + "\n"}, "line_number": line},
        }
    )


def _run(host: ScriptHost, args: Mapping[str, Any], tool: SearchFilesTool | None = None) -> Any:
    tool = tool or SearchFilesTool()
    return json.loads(asyncio.run(tool.search(read_request(args), host)))


def test_command_is_unbounded_and_quotes_everything() -> None:
    host = ScriptHost(lambda cmd: Out("", 1))
    body = _run(host, {"pattern": "a'b", "path": "~/my dir", "file_glob": "*.py"})
    assert body["matches"] == [] and body["total_count"] == 0
    command, bounded = host.commands[0]
    assert bounded is False
    assert "-e 'a'\"'\"'b' -- \"$HOME\"/'my dir'" in command
    assert "--glob '*.py'" in command and "--no-config" in command


def test_timeout_keeps_partial_results_with_a_marker() -> None:
    partial = "\n".join([_match("a.py", 1, "x"), '{"type":"match","da'])
    host = ScriptHost(lambda cmd: Out(partial + "\n[Command timed out after 60s]", 124, True))
    body = _run(host, {"pattern": "x"})
    assert body["limit_reason"] == "search_timeout" and body["truncated"] is True
    assert body["matches"] == [{"path": "a.py", "line": 1, "text": "x"}]


def test_exit_two_with_and_without_results() -> None:
    err = "__INDUS_SEARCH_ERR__rg: secret/: Permission denied (os error 13)"
    some = ScriptHost(lambda cmd: Out(_match("a.py", 2, "x") + "\n" + err, 2))
    body = _run(some, {"pattern": "x"})
    assert body["shown"] == 1 and "could not be searched" in body["notes"][0]
    none = ScriptHost(lambda cmd: Out(err, 2))
    assert "Permission denied" in _run(none, {"pattern": "x"})["error"]


def test_page_bound_sigpipe_is_a_full_page_not_an_error() -> None:
    host = ScriptHost(lambda cmd: Out("a\nb\nc\n", 141))
    body = _run(host, {"pattern": "x", "output_mode": "files_only", "limit": 2})
    assert body["files"] == ["a", "b"] and body["next_offset"] == 2 and body["truncated"]
    assert f"head -n {2 + 1 + sf.FILTER_SLACK}" in host.commands[0][0]


def test_dropped_results_at_the_page_bound_still_offer_a_next_page() -> None:
    names = "".join(f"{i}.env\n" for i in range(sf.FILTER_SLACK + 1)) + "keep.py\n"
    host = ScriptHost(lambda cmd: Out(names, 141))
    body = _run(host, {"pattern": "*", "target": "files", "limit": 2})
    assert body["files"] == ["keep.py"] and body["filtered"] == sf.FILTER_SLACK + 1
    assert body["next_offset"] == 1 and body["truncated"]


def test_old_ripgrep_refuses_modified_order() -> None:
    host = ScriptHost(lambda cmd: Out("__INDUS_SEARCH_OLD_RG__13.0.0\n", 3))
    body = _run(host, {"pattern": "*.py", "target": "files", "order": "modified"})
    assert "ripgrep 14 or newer (found 13.0.0)" in body["error"]
    assert "--sortr=modified" in host.commands[0][0]


def test_interrupted_search_is_an_error() -> None:
    host = ScriptHost(lambda cmd: Out("[Command interrupted]", 130, interrupted=True))
    assert _run(host, {"pattern": "x"}) == {"error": "The search was interrupted."}


def test_bytes_paths_and_long_lines() -> None:
    raw = json.dumps(
        {
            "type": "match",
            "data": {"path": {"bytes": "Yi5weQ=="}, "lines": {"text": "y" * 900}, "line_number": 3},
        }
    )
    body = _run(ScriptHost(lambda cmd: Out(raw)), {"pattern": "y"})
    (match,) = body["matches"]
    assert match["path"] == "b.py" and match["text"].endswith("[line cut]")
    assert len(match["text"]) < 520


def test_second_file_name_search_waits_and_gives_up_on_stop() -> None:
    clock = FakeClock(FAKE_CLOCK_START)
    tool = SearchFilesTool(clock=clock)
    first_host = ScriptHost(lambda cmd: Out("a.py\n"))
    second_host = ScriptHost(lambda cmd: Out("b.py\n"))

    async def go() -> tuple[str, str]:
        first_host.gate = asyncio.Event()
        stop = asyncio.Event()
        request = read_request({"pattern": "*.py", "target": "files"})
        first = asyncio.create_task(tool.search(request, first_host))
        await clock.run_until_idle()
        second = asyncio.create_task(tool.search(request, second_host, cancel=stop))
        await clock.run_until_idle()
        assert second_host.commands == []  # still waiting for the root
        await clock.advance_and_settle(sf.ADMISSION_SLICE_S)
        assert second_host.commands == []
        stop.set()
        await clock.advance_and_settle(sf.ADMISSION_SLICE_S)
        first_host.gate.set()
        return await first, await second

    first, second = asyncio.run(go())
    assert json.loads(first)["files"] == ["a.py"]
    assert "stopped while waiting" in json.loads(second)["error"]


def test_waiting_search_runs_once_the_first_finishes() -> None:
    clock = FakeClock(FAKE_CLOCK_START)
    tool = SearchFilesTool(clock=clock)
    first_host = ScriptHost(lambda cmd: Out("a.py\n"))
    second_host = ScriptHost(lambda cmd: Out("b.py\n"))

    async def go() -> str:
        first_host.gate = asyncio.Event()
        request = read_request({"pattern": "*.py", "target": "files"})
        first = asyncio.create_task(tool.search(request, first_host))
        await clock.run_until_idle()
        second = asyncio.create_task(tool.search(request, second_host))
        await clock.run_until_idle()
        first_host.gate.set()
        await first
        await clock.advance_and_settle(sf.ADMISSION_SLICE_S)
        return await second

    assert json.loads(asyncio.run(go()))["files"] == ["b.py"]


def test_content_searches_do_not_queue() -> None:
    admission = Admission()
    tool = SearchFilesTool(admission=admission)
    host = ScriptHost(lambda cmd: Out(""))
    _run(host, {"pattern": "x"}, tool)
    assert not admission.busy(("fake", "/work"))


# -- pure parts -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "sensitive"),
    [
        (".env", True),
        ("app/.env.production", True),
        ("app/.env.example", False),
        ("deploy/prod.env", True),
        (".envrc", True),
        ("/home/u/.netrc", True),
        ("/home/u/.ssh/id_ed25519", True),
        ("/home/u/.ssh/id_ed25519.pub", False),
        ("/home/u/.aws/credentials", True),
        ("src/environment.py", False),
        ("src/config.yaml", False),
    ],
)
def test_sensitive_paths(path: str, sensitive: bool) -> None:
    assert is_sensitive_path(path) is sensitive


def test_indus_home_secret_files_only_inside_that_home() -> None:
    assert is_sensitive_path("/h/.indus/auth.json", secret_home="/h/.indus")
    assert not is_sensitive_path("/proj/auth.json", secret_home="/h/.indus")


def test_macos_exclusions_only_below_a_broad_root() -> None:
    assert macos_exclusions("/Users/me", "/Users/me")[:2] == ["Desktop", "Documents"]
    assert macos_exclusions("/Users", "/Users/me")[0] == "me/Desktop"
    assert macos_exclusions("/Users/me/Documents", "/Users/me") == []
    assert macos_exclusions("/Users/me/code", "/Users/me") == []
    assert macos_exclusions("/x", None) == []


def test_macos_exclusions_reach_the_command_on_a_local_mac(
    monkeypatch: pytest.MonkeyPatch, scope: Scope
) -> None:
    monkeypatch.setattr(sf, "os_family", lambda: "darwin")
    home = scope.env.get("HOME") or "/home/u"

    class LocalHost(ScriptHost):
        @property
        def cwd(self) -> str:
            return home

        @property
        def flags(self) -> Any:
            return type("F", (), {"host_local": True})()

    host = LocalHost(lambda cmd: Out("", 1))
    body = json.loads(
        asyncio.run(SearchFilesTool().search(read_request({"pattern": "x"}), host, scope=scope))
    )
    assert "--glob '!Desktop/**'" in host.commands[0][0]
    assert "privacy-protected" in body["notes"][0]


@pytest.mark.parametrize(
    ("args", "words"),
    [
        ({}, "pattern is required"),
        ({"pattern": "x", "target": "web"}, "target must be one of"),
        ({"pattern": "x", "output_mode": "lines"}, "output_mode must be one of"),
    ],
)
def test_bad_arguments(args: dict[str, Any], words: str) -> None:
    with pytest.raises(ValueError, match=words):
        read_request(args)


def test_request_defaults_and_clamps() -> None:
    request = read_request(
        {"pattern": "x", "target": "grep", "limit": 99999, "offset": -3, "context": 500}
    )
    assert request.target == "content" and request.path == "."
    assert (request.limit, request.offset, request.context) == (sf.MAX_LIMIT, 0, sf.MAX_CONTEXT)


def test_handler_without_an_environment_explains() -> None:
    body = json.loads(asyncio.run(SearchFilesTool().handle({"pattern": "x"})))
    assert "no terminal environment" in body["error"]


def test_handler_uses_the_environment_source() -> None:
    host = ScriptHost(lambda cmd: Out(_match("a.py", 1, "x")))

    class Source:
        async def acquire(
            self, scope: Any = None, *, task_id: Any = None, session_id: Any = None
        ) -> tuple[Any, Any]:
            assert task_id == "t1"
            return "key", host

    text = asyncio.run(
        SearchFilesTool().handle({"pattern": "x"}, task_id="t1", environments=Source())
    )
    assert json.loads(text)["shown"] == 1


def test_registered_read_only_in_the_file_toolset() -> None:
    registry = ToolRegistry()
    sf.register_tools(registry)
    entry = registry.get("search_files")
    assert entry is not None and entry.toolset == "file" and entry.traits.read_only
    assert entry.is_async
