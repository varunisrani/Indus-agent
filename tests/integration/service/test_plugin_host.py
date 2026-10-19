"""Plugins and shell hooks through the real CLI session host (P13-13..23 integration).

``cli.runtime.build_service`` → ``SessionService(plugins=PluginUpkeep)``: an enabled plugin in a
temporary home is loaded before the first agent (its tool joins the selection, its prompt section
is frozen into the system prompt, its ``pre_llm_call`` answer rides the user message, its session
hooks fire, its slash command runs); a configured shell hook adds context and blocks a tool once
``--accept-hooks`` accepted it; a finite run never asks and skips an unconsented hook; closing the
host takes every registration back.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.backends.wait import FOREGROUND
from indus.cli.args import HostOverrides
from indus.cli.runtime import ServiceSpec, make_host
from indus.cli.session_select import LaunchPolicy
from indus.cli.slash.dispatch import dispatch
from indus.cli.slash.plugin_cmds import plugin_commands
from indus.cli.slash.port import HostPort
from indus.foundation.clock import SystemClock
from indus.foundation.scope import EnvView, Scope, make_scope
from indus.foundation.secrets import open_secret_store
from indus.plugins.prompt_sections import PluginSectionsBuilder
from indus.protocol.methods import SessionCreateParams
from indus.service import plugin_host
from indus.state import registry
from indus.tools.builtin import terminal
from tests.fakes.agent import any_window
from tests.fakes.model import FakeModelServer, fake_route, script_of, text_turn, tool_batch
from tests.unit.cli.slash._fakes import make_ctx, with_config

WAIT = 30.0

PLUGIN_YAML = """\
name: greeter
version: 1.0.0
description: Greets and adds context
manifest_version: 2
"""
PLUGIN_CODE = '''\
"""A test plugin: a tool, a prompt section, pre_llm_call context, session hooks, a command."""

SCHEMA = {
    "description": "Greet someone.",
    "parameters": {"type": "object", "properties": {"name": {"type": "string"}}},
}


def register(ctx):
    log = ctx.data_dir() / "events.log"

    def note(event):
        def hook(session_id, **_):
            with log.open("a", encoding="utf-8") as out:
                out.write(f"{event} {session_id}\\n")

        return hook

    ctx.register_tool("greet", "greeter", SCHEMA, lambda args, **_: "hello " + args.get("name", ""))
    ctx.register_prompt_section("greeter.notes", lambda info: "Greeter section: speak plainly.")
    ctx.register_hook("pre_llm_call", lambda **_: {"context": "Plugin context: the moon is full."})
    ctx.register_hook("on_session_start", note("start"))
    ctx.register_hook("on_session_end", note("end"))
    ctx.register_command("hello", lambda raw_args, **_: "hello " + raw_args)
'''
HOOK_SCRIPT = """\
import json, sys

payload = json.load(sys.stdin)
if sys.argv[1] == "context":
    print(json.dumps({"context": "Shell context: tea at four."}))
elif "curl" in json.dumps(payload.get("tool_input")):
    print(json.dumps({"decision": "block", "reason": "no curl from this machine"}))
"""


@pytest.fixture(autouse=True)
def in_tmp(tmp_path: Path) -> Iterator[None]:
    here = os.getcwd()
    os.chdir(tmp_path)
    yield
    os.chdir(here)
    # ``indus -z`` shuts the terminal runtime down as at process exit, which raises the
    # process-wide foreground fence; later tests in this process must be able to spawn again.
    terminal._shared.clear()  # pyright: ignore[reportPrivateUsage]
    FOREGROUND.reset_for_tests()


def real_scope(isolated_home: Path) -> Scope:
    """A profile on the system clock: the hook subprocess bounds run in real time."""
    home = isolated_home / "profiles" / "work"
    home.mkdir(parents=True, exist_ok=True)
    (home / "profile.yaml").touch()
    env = EnvView.from_process().with_overlay({"INDUS_HOME": str(home)})
    scope = make_scope(home, env=env, clock=SystemClock())
    return scope.with_secrets(open_secret_store(home, scope.profile_id, mode="file"))


def setup_home(scope: Scope, tmp_path: Path, *, plugin: bool = True) -> Scope:
    if plugin:
        folder = scope.home / "plugins" / "greeter"
        folder.mkdir(parents=True)
        (folder / "plugin.yaml").write_text(PLUGIN_YAML, encoding="utf-8")
        (folder / "__init__.py").write_text(PLUGIN_CODE, encoding="utf-8")
    script = tmp_path / "hook.py"
    script.write_text(HOOK_SCRIPT, encoding="utf-8")
    python = json.dumps(sys.executable)
    lines = ["plugins:", "  enabled: [greeter]"] if plugin else []
    lines += [
        "hooks:",
        "  pre_llm_call:",
        f"    - command: '{python} {script} context'",
        "  pre_tool_call:",
        f"    - command: '{python} {script} guard'",
        "      matcher: terminal",
    ]
    return with_config(scope, "\n".join(lines) + "\n")


def overrides(server: FakeModelServer) -> HostOverrides:
    return HostOverrides(
        route=fake_route(server),
        client_factory=lambda route: server.client(),
        list_models=any_window,
    )


def sent(server: FakeModelServer, index: int) -> list[dict[str, Any]]:
    return server.model_requests[index].body["messages"]


def has_sections(builder: object) -> bool:
    """Is a ``PluginSectionsBuilder`` anywhere in the wrapper chain?"""
    seen = 0
    while builder is not None and seen < 20:
        if isinstance(builder, PluginSectionsBuilder):
            return True
        inner = getattr(builder, "inner", None) or getattr(builder, "_inner", None)
        builder, seen = inner, seen + 1
    return False


def text_of(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(str(p.get("text", "")) for p in content or [] if isinstance(p, dict))


def test_plugins_and_shell_hooks_run_in_a_cli_session(isolated_home: Path, tmp_path: Path) -> None:
    scope = setup_home(real_scope(isolated_home), tmp_path)
    model = FakeModelServer(
        script_of(
            tool_batch(("terminal", {"command": "curl http://example.invalid/x"})),
            text_turn("done"),
            *[text_turn("more")] * 4,
        )
    )
    store = registry.acquire(scope)
    spec = ServiceSpec(
        scope=scope,
        route=fake_route(model),
        policy=LaunchPolicy(cwd=str(tmp_path)),
        store=store,
        overrides=overrides(model),
        accept_hooks=True,
    )
    events = scope.home / "plugin-data" / "greeter" / "events.log"
    host = make_host(spec, call_timeout=WAIT)
    with host:
        service = host.service
        sid = host.create(SessionCreateParams(cwd=str(tmp_path))).session_id
        upkeep = plugin_host.upkeep_for(scope)
        assert upkeep is not None and service.plugins is upkeep
        assert upkeep.report is not None and upkeep.report.loaded() == ["greeter"]
        assert upkeep.shell is not None and len(upkeep.shell.registered) == 2
        agent = host.run(lambda: service.agent(sid))
        # The plugin's toolset joins the platform bundle; its tool is in the first array.
        assert agent.toolset_selection == ["indus-cli", "greeter"]
        assert "greet" in agent.tool_names
        # Top-level sessions get the seams; a subagent starts from the plain builder.
        assert agent.options.llm_context is not None
        assert has_sections(agent.options.builder)
        assert not has_sections(agent.options.subagent_builder)
        assert events.read_text(encoding="utf-8").split() == ["start", sid]

        host.submit(sid, "fetch it")
        host.wait(sid, timeout=WAIT)
        first = sent(model, 0)
        system, user = text_of(first[0]), first[-1]
        assert "Greeter section: speak plainly." in system
        assert "<!-- indus-plugin-sections:start -->" in system
        assert "moon" not in system and "tea" not in system
        assert user["role"] == "user"
        assert text_of(user).startswith("fetch it")
        assert "Plugin context: the moon is full." in text_of(user)
        assert "Shell context: tea at four." in text_of(user)
        rows = host.call(service.store.load(sid))
        [answer] = [r for r in rows if r.role == "tool"]
        assert "no curl from this machine" in str(answer.content)
        # The second request replays the same system prompt bytes (frozen sections).
        assert text_of(sent(model, 1)[0]) == system

        # /plugins and the plugin's own slash command.
        port = HostPort(host, scope)
        ctx = make_ctx(scope, port=port, session_id=sid)
        dispatch("/plugins", ctx)
        assert any(line.startswith("greeter") for line in ctx.lines)
        assert "Loaded in this chat: greeter" in ctx.lines
        ctx = make_ctx(scope, port=port, session_id=sid)
        ctx.extensions.plugin_commands = plugin_commands(scope)
        dispatch("/hello world", ctx)
        assert ctx.lines == ["hello world"]

        # A new session renders the section again and gets its own start hook.
        other = host.create(SessionCreateParams(cwd=str(tmp_path))).session_id
        host.call(service.close(other))
    assert events.read_text(encoding="utf-8").split() == [
        "start", sid, "start", other, "end", other, "end", sid,
    ]  # fmt: skip
    # Closing the host took every registration back.
    assert plugin_host.upkeep_for(scope) is None
    assert upkeep.registry.get("greet", scope) is None


def test_a_finite_run_never_asks_and_skips_an_unconsented_hook(
    isolated_home: Path, tmp_path: Path
) -> None:
    scope = setup_home(real_scope(isolated_home), tmp_path, plugin=False)
    model = FakeModelServer(script_of(text_turn("done")))
    spec = ServiceSpec(
        scope=scope,
        route=fake_route(model),
        policy=LaunchPolicy(cwd=str(tmp_path)),
        store=registry.acquire(scope),
        run_context="single_query",
        overrides=overrides(model),
    )
    host = make_host(spec, call_timeout=WAIT)
    with host:
        sid = host.create(SessionCreateParams(cwd=str(tmp_path))).session_id
        upkeep = plugin_host.upkeep_for(scope)
        assert upkeep is not None and upkeep.shell is not None
        assert upkeep.shell.registered == [] and len(upkeep.shell.skipped) == 2
        assert all("never asks" in why for _, why in upkeep.shell.skipped)
        agent = host.run(lambda: host.service.agent(sid))
        assert agent.options.llm_context is None  # nothing live: options untouched
        host.submit(sid, "hi")
        host.wait(sid, timeout=WAIT)
        assert text_of(sent(model, 0)[-1]) == "hi"
    assert not (scope.home / "hooks_consent.json").exists()


def test_a_profile_without_plugins_or_hooks_loads_nothing(
    isolated_home: Path, tmp_path: Path
) -> None:
    scope = real_scope(isolated_home)
    model = FakeModelServer(script_of(text_turn("done")))
    spec = ServiceSpec(
        scope=scope,
        route=fake_route(model),
        policy=LaunchPolicy(cwd=str(tmp_path)),
        store=registry.acquire(scope),
        overrides=overrides(model),
    )
    host = make_host(spec, call_timeout=WAIT)
    with host:
        host.create(SessionCreateParams(cwd=str(tmp_path)))
        upkeep = plugin_host.upkeep_for(scope)
        assert upkeep is not None and upkeep.bus is None and upkeep.host is None
        assert not (scope.home / "plugins").exists()


def test_indus_z_with_accept_hooks_runs_the_plugin_and_the_hooks(
    isolated_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from indus.cli import app as app_mod
    from indus.cli import oneshot
    from indus.cli.args import CliState, GlobalArgs

    # The flag reaches GlobalArgs from click's values (and is dropped when off).
    assert app_mod.globals_from({"accept_hooks": True}).accept_hooks is True
    assert app_mod.globals_from({"accept_hooks": False}) == GlobalArgs()
    names = {opt for p in app_mod.cli.params for opt in p.opts}
    assert "--accept-hooks" in names

    scope = setup_home(real_scope(isolated_home), tmp_path)
    model = FakeModelServer(script_of(text_turn("answer")))
    state = CliState(scope=scope, overrides=overrides(model))
    state.globals = GlobalArgs(oneshot="hello there", accept_hooks=True)
    assert oneshot.run_from_cli(state) == 0
    assert capsys.readouterr().out == "answer\n"
    first = sent(model, 0)
    assert "Greeter section: speak plainly." in text_of(first[0])
    assert "Plugin context: the moon is full." in text_of(first[-1])
    assert "Shell context: tea at four." in text_of(first[-1])
    # Accepting recorded the consent, so a later run of the same hooks needs no flag.
    assert (scope.home / "hooks_consent.json").is_file()


HELLO_CODE = '''\
"""The P13 demo plugin: a tool and a guard that keeps curl out of the terminal."""

SCHEMA = {
    "description": "Say hello.",
    "parameters": {"type": "object", "properties": {"name": {"type": "string"}}},
}


def guard(tool_name, args, **_):
    if tool_name == "terminal" and "curl" in str(args.get("command", "")):
        return {"action": "block", "message": "the hello plugin keeps curl out of the terminal"}
    return None


def register(ctx):
    ctx.register_tool("hello", "hello", SCHEMA, lambda args, **_: "hello " + args.get("name", ""))
    ctx.register_hook("pre_tool_call", guard)
'''


def test_done_when_demo_installed_plugin_adds_a_tool_and_blocks_curl(
    isolated_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """P13 "Done when": ``indus plugins install <dir> --enable`` adds a tool and a
    ``pre_tool_call`` hook that blocks ``terminal`` commands containing ``curl``."""
    from indus.cli import oneshot
    from indus.cli.args import CliState, GlobalArgs
    from indus.foundation.config import loader
    from tests.unit.cli.commands._helpers_d import run

    scope = real_scope(isolated_home)
    source = tmp_path / "hello-plugin"
    source.mkdir()
    (source / "plugin.yaml").write_text(
        "name: hello\nversion: 1.0.0\nmanifest_version: 2\n", encoding="utf-8"
    )
    (source / "__init__.py").write_text(HELLO_CODE, encoding="utf-8")
    installed = run(scope, "plugins", "install", str(source), "--enable")
    assert installed.code == 0, installed.err
    path = loader.config_path(scope)
    loader.invalidate(path)
    scope = scope.with_config(loader.load(scope))

    model = FakeModelServer(
        script_of(
            tool_batch(
                ("hello", {"name": "you"}),
                ("terminal", {"command": "curl http://example.invalid/x"}),
            ),
            text_turn("done"),
        )
    )
    state = CliState(scope=scope, overrides=overrides(model))
    state.globals = GlobalArgs(oneshot="go")
    assert oneshot.run_from_cli(state) == 0
    assert capsys.readouterr().out == "done\n"
    tools = {t["function"]["name"] for t in model.model_requests[0].body.get("tools", [])}
    assert "hello" in tools
    answers = [m for m in sent(model, 1) if m.get("role") == "tool"]
    texts = [text_of(m) for m in answers]
    assert any("hello you" in t for t in texts)
    assert any("keeps curl out of the terminal" in t for t in texts)
