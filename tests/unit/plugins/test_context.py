"""P13-16: what a plugin can do through its context."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import click
import pytest

from indus.foundation.scope import Scope
from indus.plugins.context import (
    COMMAND_TIMEOUT_S,
    MCP_RESULT_CAP,
    CallSite,
    HostPorts,
    PluginClosed,
    PluginContext,
    PluginError,
    PluginHost,
    PluginPermissionError,
    bind_call,
)
from indus.plugins.hooks import HookBus
from indus.plugins.manifest import PluginManifest, manifest_from_mapping
from indus.tools.calls import ToolCall, ToolContext
from indus.tools.dispatch import dispatch
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import eventually
from tests.fakes.clock import FakeClock
from tests.unit.plugins._helpers import FIXTURES, with_config


class Recorder:
    """Fake host ports that remember what reached them."""

    def __init__(self, mcp_answer: str = '{"result": "fine"}') -> None:
        self.mcp: list[tuple[str, str, dict[str, Any], float]] = []
        self.posts: list[tuple[str, str]] = []
        self.injected: list[tuple[str, str | None, Mapping[str, Any] | None, str]] = []
        self.prompts: list[tuple[list[Mapping[str, Any]], Mapping[str, Any] | None]] = []
        self.mcp_answer = mcp_answer

    async def mcp_call(self, server: str, tool: str, args: dict[str, Any], timeout: float) -> str:
        self.mcp.append((server, tool, args, timeout))
        return self.mcp_answer

    async def post(self, conversation: str, text: str) -> str:
        self.posts.append((conversation, text))
        return f"msg-{len(self.posts)}"

    async def inject(
        self, text: str, session_key: str | None, origin: Mapping[str, Any] | None, role: str
    ) -> bool:
        self.injected.append((text, session_key, origin, role))
        return True

    async def complete(
        self, messages: list[Mapping[str, Any]], schema: Mapping[str, Any] | None
    ) -> str:
        self.prompts.append((messages, schema))
        return "a short answer"

    def ports(self) -> HostPorts:
        return HostPorts(
            mcp_call=self.mcp_call,
            llm_complete=self.complete,
            inject=self.inject,
            post=self.post,
            reserved_commands=lambda name: name in {"help", "model"},
        )


def manifest(name: str = "sample", **fields: Any) -> PluginManifest:
    made, _ = manifest_from_mapping({"name": name, **fields}, fallback_name=name, source="user")
    return made


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def host(scope: Scope, fake_clock: FakeClock, recorder: Recorder) -> PluginHost:
    configured = with_config(
        scope,
        {
            "plugins": {
                "entries": {
                    "sample": {
                        "mcp_allowlist": ["files", "*"],
                        "settings": {"colour": "green"},
                    }
                }
            }
        },
    )
    return PluginHost(
        configured,
        ToolRegistry(),
        HookBus(clock=fake_clock),
        ports=recorder.ports(),
        clock=fake_clock,
    )


@pytest.fixture
def ctx(host: PluginHost) -> PluginContext:
    return host.context(manifest("sample", path=str(FIXTURES / "good")))


class Voice:
    name = "voice-one"


def test_one_of_each_registration_is_reachable(host: PluginHost, ctx: PluginContext) -> None:
    async def tool(args: Mapping[str, Any]) -> str:
        return f"tool saw {args.get('q')}"

    ctx.register_tool("sample_tool", "sample", {"description": "t", "parameters": {}}, tool)
    ctx.register_hook("post_tool_call", lambda **_: None)
    ctx.register_command("/Say Hi", lambda raw_args: f"hi {raw_args}")
    group = click.Group("sample-cli")
    assert ctx.register_cli_command("sample-cli", group)
    qualified = ctx.register_skill("greet", FIXTURES / "good" / "skills" / "greet" / "SKILL.md")
    assert ctx.register_tts_provider(Voice())
    assert ctx.register_desktop_driver(object(), name="driver-one")
    assert ctx.register_context_engine(object(), name="engine-one")
    assert not ctx.register_context_engine(object(), name="engine-two")  # one only
    assert not ctx.register_tts_provider(Voice())  # same name twice
    ctx.register_prompt_section("sample.section", lambda info: "notes")
    ctx.listen("indus.session_started", lambda **_: None)

    entry = host.registry.get("sample_tool", host.scope)
    assert entry is not None
    result = asyncio.run(
        dispatch(
            ToolCall("sample_tool", {"q": "x"}),
            ToolContext(scope=host.scope),
            registry=host.registry,
        )
    )
    assert result.text == "tool saw x"
    assert host.bus.has("post_tool_call")
    assert asyncio.run(host.run_command("say-hi", "there")) == "hi there"
    assert host.cli_commands["sample-cli"].command is group
    assert qualified == "sample:greet"
    assert host.skills["sample:greet"] == FIXTURES / "good" / "skills" / "greet"
    assert set(host.providers["tts"]) == {"voice-one"}
    assert set(host.providers["desktop_driver"]) == {"driver-one"}
    assert set(host.providers["context_engine"]) == {"engine-one"}
    assert [s.id for s in host.render_prompt_inputs()] == ["sample.section"]
    provided = host.provided["sample"]
    assert provided.commands == ["say-hi"] and provided.tools == ["sample_tool"]

    host.undo("sample")
    assert host.registry.get("sample_tool", host.scope) is None
    assert not host.bus.has("post_tool_call")
    assert host.commands == {} and host.skills == {} and host.cli_commands == {}
    assert host.providers["tts"] == {} and host.prompt_sections == {}


def test_dispatch_tool_from_plugin_code(host: PluginHost, ctx: PluginContext) -> None:
    ctx.register_tool(
        "adder", "sample", {"description": "add", "parameters": {}}, lambda args: str(args["a"] + 1)
    )
    assert asyncio.run(ctx.dispatch_tool("adder", {"a": 1})) == "2"
    missing = json.loads(asyncio.run(ctx.dispatch_tool("nope")))
    assert "Unknown tool" in missing["error"]


def test_call_mcp_unlisted_server_names_the_key(ctx: PluginContext, recorder: Recorder) -> None:
    with pytest.raises(PluginPermissionError) as raised:
        asyncio.run(ctx.call_mcp("billing", "charge", {"amount": 1}))
    assert "plugins.entries.sample.mcp_allowlist" in str(raised.value)
    assert isinstance(raised.value, PermissionError)
    with pytest.raises(PluginPermissionError):  # "*" is a literal name, not a wildcard
        asyncio.run(ctx.call_mcp("anything", "t"))
    assert recorder.mcp == []


def test_call_mcp_allowed_server(ctx: PluginContext, recorder: Recorder) -> None:
    recorder.mcp_answer = json.dumps({"result": "listed", "structuredContent": {"n": 2}})
    answer = asyncio.run(ctx.call_mcp("files", "list", {"path": "/"}))
    assert answer == {"ok": True, "result": "listed", "structuredContent": {"n": 2}}
    assert recorder.mcp == [("files", "list", {"path": "/"}, 30.0)]
    recorder.mcp_answer = json.dumps({"error": "no such file"})
    assert asyncio.run(ctx.call_mcp("files", "read")) == {"ok": False, "error": "no such file"}
    recorder.mcp_answer = "plain words"
    assert asyncio.run(ctx.call_mcp("files", "read")) == {"ok": True, "result": "plain words"}


@pytest.mark.parametrize(("given", "used"), [(0.1, 1.0), (5000, 600.0), ("x", 30.0), (12, 12.0)])
def test_call_mcp_timeout_is_clamped(
    ctx: PluginContext, recorder: Recorder, given: Any, used: float
) -> None:
    asyncio.run(ctx.call_mcp("files", "list", timeout=given))
    assert recorder.mcp[-1][3] == used


def test_call_mcp_truncates_large_results(ctx: PluginContext, recorder: Recorder) -> None:
    recorder.mcp_answer = "x" * 100_000
    answer = asyncio.run(ctx.call_mcp("files", "dump"))
    assert answer["ok"] is True and answer["truncated"] is True
    assert len(answer["result"]) == MCP_RESULT_CAP


def test_call_mcp_without_a_client(scope: Scope, fake_clock: FakeClock) -> None:
    configured = with_config(
        scope, {"plugins": {"entries": {"lone": {"mcp_allowlist": ["files"]}}}}
    )
    host = PluginHost(configured, ToolRegistry(), HookBus(clock=fake_clock))
    lone = host.context(manifest("lone"))
    answer = asyncio.run(lone.call_mcp("files", "list"))
    assert answer["ok"] is False and "not available" in answer["error"]


def test_tool_override_needs_opt_in(scope: Scope, fake_clock: FakeClock) -> None:
    registry = ToolRegistry()
    registry.register("read_file", "file", {"description": "r"}, lambda args: "built-in")
    plain = PluginHost(scope, registry, HookBus(clock=fake_clock))
    with pytest.raises(PluginPermissionError, match="allow_tool_override"):
        plain.context(manifest("swap")).register_tool(
            "read_file", "file", {"description": "x"}, lambda args: "mine", override=True
        )
    bundled = plain.context(manifest("shipped").model_copy(update={"source": "bundled"}))
    assert bundled.register_tool(
        "read_file", "file", {"description": "x"}, lambda args: "bundled", override=True
    )
    granted = PluginHost(
        with_config(
            scope, {"plugins": {"entries": {"swap": {"granted_capabilities": ["tools.override"]}}}}
        ),
        ToolRegistry(),
        HookBus(clock=fake_clock),
    )
    granted.registry.register("read_file", "file", {"description": "r"}, lambda args: "built-in")
    assert granted.context(manifest("swap")).register_tool(
        "read_file", "file", {"description": "x"}, lambda args: "mine", override=True
    )


def test_command_name_clash_with_builtin_refused(host: PluginHost, ctx: PluginContext) -> None:
    assert ctx.register_command("help", lambda raw_args: "mine") is None
    assert ctx.register_command("  ", lambda raw_args: "empty") is None
    assert ctx.register_command("tally", lambda raw_args: "1") is not None
    other = host.context(manifest("other"))
    assert other.register_command("/TALLY", lambda raw_args: "2") is None
    assert asyncio.run(host.run_command("/tally")) == "1"
    assert asyncio.run(host.run_command("missing")) == "Unknown command: /missing"


def test_command_handler_gets_session_keywords_and_text(
    host: PluginHost, ctx: PluginContext
) -> None:
    def where(raw_args: str, session_key: str = "") -> dict[str, str]:
        return {"session": session_key, "cwd": ctx.session_cwd()}

    async def boom(raw_args: str) -> str:
        raise ValueError("bad input")

    ctx.register_command("where", where)
    ctx.register_command("boom", boom)
    site = CallSite(session_key="s-1", conversation="c-1", cwd="/work/here")
    text = asyncio.run(host.run_command("where", "", site=site, session_key="s-1", platform="cli"))
    assert text == str({"session": "s-1", "cwd": "/work/here"})
    assert asyncio.run(host.run_command("boom")) == "/boom failed: ValueError: bad input"


def test_slash_handler_over_thirty_seconds_is_cancelled(
    host: PluginHost, ctx: PluginContext, fake_clock: FakeClock
) -> None:
    cancelled = threading.Event()

    async def slow(raw_args: str) -> str:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "never"

    release = threading.Event()

    def stuck(raw_args: str) -> str:
        release.wait()
        return "late"

    ctx.register_command("slow", slow)
    ctx.register_command("stuck", stuck)

    async def go(name: str) -> str:
        task = asyncio.ensure_future(host.run_command(name))
        await eventually(lambda: fake_clock.pending >= 1)
        fake_clock.advance(COMMAND_TIMEOUT_S)
        return await task

    try:
        slow_text = asyncio.run(go("slow"))
        stuck_text = asyncio.run(go("stuck"))
    finally:
        release.set()
    assert slow_text.startswith("/slow did not finish within 30 s and was stopped")
    assert cancelled.is_set()
    assert stuck_text.startswith("/stuck did not finish within 30 s")


def test_post_message_reaches_only_the_bound_conversation(
    ctx: PluginContext, recorder: Recorder
) -> None:
    assert asyncio.run(ctx.post_message("nobody")) == {
        "ok": False,
        "error": "no conversation is bound to this call",
    }
    with bind_call(CallSite(session_key="s-a", conversation="conv-a")):
        sent = asyncio.run(ctx.post_message("hello a"))
    with bind_call(CallSite(session_key="s-b", conversation="conv-b")):
        asyncio.run(ctx.post_message("hello b"))
    assert sent == {"ok": True, "message": "msg-1"}
    assert recorder.posts == [("conv-a", "hello a"), ("conv-b", "hello b")]
    with pytest.raises(TypeError):
        ctx.post_message("x", conversation="conv-b")  # type: ignore[call-arg]  # pyright: ignore[reportCallIssue]


def test_inject_message_needs_exactly_one_target(ctx: PluginContext, recorder: Recorder) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        asyncio.run(ctx.inject_message("x"))
    with pytest.raises(ValueError, match="exactly one"):
        asyncio.run(ctx.inject_message("x", session_key="s", origin={"chat": 1}))
    assert asyncio.run(ctx.inject_message("ping", session_key="s-9"))
    assert recorder.injected == [("ping", "s-9", None, "user")]


def test_llm_complete_through_the_host(ctx: PluginContext, recorder: Recorder) -> None:
    schema = {"type": "object"}
    answer = asyncio.run(ctx.llm.complete([{"role": "user", "content": "sum up"}], schema))
    assert answer == "a short answer"
    assert recorder.prompts == [([{"role": "user", "content": "sum up"}], schema)]


def test_llm_missing_port(scope: Scope, fake_clock: FakeClock) -> None:
    host = PluginHost(scope, ToolRegistry(), HookBus(clock=fake_clock))
    with pytest.raises(PluginError, match="no model"):
        asyncio.run(host.context(manifest("x")).llm.complete([]))


def test_data_dir_outside_install_dir(ctx: PluginContext, host: PluginHost) -> None:
    folder = ctx.data_dir()
    assert folder == host.scope.home / "plugin-data" / "sample"
    assert folder.is_dir()
    assert not folder.resolve().is_relative_to((FIXTURES / "good").resolve())
    inside = host.context(manifest("odd").model_copy(update={"path": str(host.scope.home)}))
    with pytest.raises(PluginError, match="install folder"):
        inside.data_dir()


def test_category_plugin_data_dir(host: PluginHost) -> None:
    categorised = host.context(manifest("voice").model_copy(update={"key": "tts/voice"}))
    assert categorised.data_dir().name == "tts__voice"


def test_secrets_are_read_only(ctx: PluginContext, host: PluginHost) -> None:
    assert ctx.secrets.get("SAMPLE_UNSET_NAME") is None
    assert not hasattr(ctx.secrets, "set")


def test_setting_and_capabilities(host: PluginHost, ctx: PluginContext) -> None:
    assert ctx.setting("colour") == "green"
    assert ctx.setting("size", 3) == 3
    assert not ctx.has_capability("tools.override")
    shipped = host.context(manifest("shipped").model_copy(update={"source": "bundled"}))
    assert shipped.has_capability("tools.override")
    assert not shipped.has_capability("platform.actions")


def test_skill_names_are_checked(ctx: PluginContext, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must match"):
        ctx.register_skill("Bad Name", FIXTURES / "good" / "skills" / "greet")
    with pytest.raises(ValueError, match="no SKILL.md"):
        ctx.register_skill("empty", tmp_path)
    assert ctx.register_skill("greet", FIXTURES / "good" / "skills" / "greet") == "sample:greet"
    assert ctx.register_skill("greet", FIXTURES / "good" / "skills" / "greet") is None


def test_skill_namespace_from_manifest(host: PluginHost) -> None:
    spaced = host.context(manifest("spaced", skill_namespace="tools-kit"))
    assert (
        spaced.register_skill("greet", FIXTURES / "good" / "skills" / "greet") == "tools-kit:greet"
    )


def test_prompt_section_rules(host: PluginHost, ctx: PluginContext) -> None:
    with pytest.raises(ValueError, match="must match"):
        ctx.register_prompt_section("Bad Id", "x")
    with pytest.raises(ValueError, match="anchor"):
        ctx.register_prompt_section("ok.id", "x", anchor="top")
    ctx.register_prompt_section("ok.id", "x")
    with pytest.raises(ValueError, match="already belongs"):
        host.context(manifest("other")).register_prompt_section("ok.id", "y")


def test_unknown_provider_kind(ctx: PluginContext) -> None:
    with pytest.raises(ValueError, match="unknown provider kind"):
        ctx.register_provider("teleport", object(), name="x")
    with pytest.raises(ValueError, match="needs a name"):
        ctx.register_provider("tts", object())


def test_closed_context_refuses(ctx: PluginContext, host: PluginHost) -> None:
    ctx.close()
    with pytest.raises(PluginClosed):
        ctx.register_command("late", lambda raw_args: "x")
    with pytest.raises(PluginClosed):
        ctx.register_hook("post_tool_call", lambda **_: None)
    assert host.commands == {}


def test_emit_namespaces_events(host: PluginHost, ctx: PluginContext) -> None:
    heard: list[tuple[str, Any]] = []
    other = host.context(manifest("other"))
    other.listen("sample.ready", lambda **payload: heard.append(("ready", payload)))
    with pytest.raises(ValueError, match="bare event name"):
        ctx.emit("indus.ready")

    async def go() -> int:
        count = ctx.emit("ready", {"n": 1})
        assert await host.bus.events.drain()
        return count

    assert asyncio.run(go()) == 1
    assert heard == [("ready", {"n": 1})]


def test_run_waits_on_the_host_loop_from_a_worker(host: PluginHost, ctx: PluginContext) -> None:
    async def answer() -> int:
        return 42

    async def go() -> int:
        host.bind_loop()
        with pytest.raises(RuntimeError, match="await"):
            ctx.run(answer())
        return await asyncio.to_thread(lambda: ctx.run(answer(), timeout=5))

    assert asyncio.run(go()) == 42
