"""Control and info commands (P06-10)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from indus.cli.slash.context import RouteInfo
from indus.cli.slash.dispatch import after_turn, dispatch
from indus.cli.slash.port import route_info
from indus.core.route import KEEP, RouteBinding, RouteState
from indus.foundation.config import loader
from indus.foundation.errors import NoUsableProvider
from indus.foundation.scope import Scope
from indus.providers import route as routes
from indus.providers.route import RuntimeRoute
from indus.safety.approvals import APPROVALS, session_key
from indus.safety.approvals.decision import approval_mode
from indus.state import sessions
from indus.state.registry import StoreHandle
from tests.fakes.agent import any_window
from tests.unit.cli.slash._fakes import FakePort, make_ctx, route_of, seed, with_config


def fake_resolve(
    scope: Scope,
    *,
    model: str | None = None,
    provider: str | None = None,
    registry: Any = None,
    **_: Any,
) -> RuntimeRoute:
    if provider == "missing":
        raise NoUsableProvider("unknown provider 'missing'", hint="pick a configured one")
    return route_of(model or "m1", provider or "fake")


@pytest.fixture
def resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "resolve", fake_resolve)


# -- /model ---------------------------------------------------------------------------------------


def test_model_shows_the_route(scope: Scope) -> None:
    ctx = make_ctx(scope)
    dispatch("/model", ctx)
    assert ctx.lines[0] == "Model: m1 (provider fake, context window 128,000 tokens)"


@pytest.mark.usefixtures("resolved")
def test_model_while_busy_rejected(scope: Scope) -> None:
    port = FakePort(busy=True)
    ctx = make_ctx(scope, port=port)
    dispatch("/model m2", ctx)
    assert port.calls == []
    assert port.route_now.model == "m1"


@pytest.mark.usefixtures("resolved")
def test_model_switch_and_provider_forms(scope: Scope) -> None:
    port = FakePort()
    ctx = make_ctx(scope, port=port)
    dispatch("/model m2", ctx)
    assert port.calls[-1] == ("switch_route", ("fake", "m2", None, True))
    assert ctx.lines[-1] == "Switched to m2 (fake), context window 200,000 tokens."
    dispatch("/model --provider other", ctx)
    assert port.calls[-1] == ("switch_route", ("other", "m2", None, True))
    ctx.lines.clear()
    dispatch("/model x --provider missing", ctx)
    assert ctx.lines == ["Cannot switch to x: unknown provider 'missing' (pick a configured one)"]


class BindingPort(FakePort):
    """A port over a real ``RouteBinding`` whose dependent fails for model ``bad``."""

    binding: RouteBinding

    @classmethod
    def build(cls, scope: Scope) -> BindingPort:
        async def dependent(before: RouteState, after: RouteState) -> None:
            if after.model == "bad":
                raise RuntimeError("the prompt could not follow the switch")

        async def bind() -> RouteBinding:
            return await RouteBinding.bind(
                scope,
                route_of("m1"),
                client_factory=lambda route: httpx.AsyncClient(),
                list_models=any_window,
                dependents=[dependent],
            )

        port = cls()
        port.binding = asyncio.run(bind())
        return port

    def route(self, session_id: str) -> RouteInfo:
        return route_info(self.binding.state)

    def switch_route(
        self,
        session_id: str,
        route: RuntimeRoute,
        *,
        effort: str | None = None,
        keep_effort: bool,
    ) -> RouteInfo:
        wanted = KEEP if keep_effort else effort
        return route_info(asyncio.run(self.binding.apply(route, requested_effort=wanted)))


@pytest.mark.usefixtures("resolved")
def test_failed_switch_rolls_back(scope: Scope) -> None:
    port = BindingPort.build(scope)
    ctx = make_ctx(scope, port=port)
    before = port.binding.state
    dispatch("/model bad", ctx)
    assert port.binding.state is before
    assert port.binding.generation == 0
    assert ctx.lines == [
        "Could not switch to bad (fake): the prompt could not follow the switch",
        "Still on m1 (fake).",
    ]
    dispatch("/model good", ctx)
    assert port.binding.state.model == "good" and port.binding.generation == 1


@pytest.mark.usefixtures("resolved")
def test_model_global_persists_and_clears_a_stale_custom_url(scope: Scope) -> None:
    scope = with_config(
        scope,
        "model:\n  provider: custom\n  base_url: http://10.0.0.5:8000/v1\n"
        "  api_mode: chat_completions\n  default: local-model\n",
    )
    ctx = make_ctx(scope)
    dispatch("/model openai:m7 --global", ctx)
    assert ctx.lines[-1] == "Saved as the default model."
    view = loader.load(scope)
    assert view.get("model.default") == "m7"
    assert view.get("model.provider") == "openai"
    assert view.get("model.base_url") == ""
    assert view.get("model.api_mode") == ""
    assert "10.0.0.5" not in loader.config_path(scope).read_text(encoding="utf-8")


@pytest.mark.usefixtures("resolved")
def test_model_global_on_a_managed_install_reports(scope: Scope) -> None:
    scope = with_config(scope, "model:\n  default: m1\n")
    (scope.home / ".managed").touch()
    ctx = make_ctx(scope)
    dispatch("/model m3 --global", ctx)
    assert ctx.lines[-2] == "Switched to m3 (fake), context window 200,000 tokens."
    assert ctx.lines[-1].startswith("The model was not saved: this installation is managed")


# -- /reasoning -----------------------------------------------------------------------------------


def test_reasoning_level_session_and_global(scope: Scope, store: StoreHandle) -> None:
    scope = with_config(scope, "agent:\n  max_turns: 100\n")
    seed(store, "s1", 1)
    port = FakePort()
    ctx = make_ctx(scope, store=store, port=port)
    dispatch("/reasoning high", ctx)
    assert port.calls[-1] == ("switch_route", ("fake", "m1", "high", False))
    assert ctx.lines[-1] == "Reasoning effort high for this session."
    row = sessions.get(store, "s1")
    assert row is not None and sessions.parse_model_config(row["model_config"]) == {
        "reasoning_effort": "high"
    }
    assert loader.load(scope).get("agent.reasoning_effort") is None
    dispatch("/reasoning none --global", ctx)
    assert port.calls[-1] == ("switch_route", ("fake", "m1", None, False))
    assert ctx.lines[-1] == "Reasoning effort off for this session and saved as the default."
    assert loader.load(scope).get("agent.reasoning_effort") == "none"


def test_reasoning_display_and_errors(scope: Scope) -> None:
    ctx = make_ctx(scope)
    dispatch("/reasoning", ctx)
    assert ctx.lines[0] == "Reasoning effort: medium; reasoning text is hidden."
    dispatch("/reasoning show", ctx)
    assert ctx.state.show_reasoning
    dispatch("/reasoning hide", ctx)
    assert not ctx.state.show_reasoning
    ctx.lines.clear()
    dispatch("/reasoning loud", ctx)
    assert ctx.lines[0] == "Unknown reasoning setting 'loud'."


# -- /yolo, /approvals ----------------------------------------------------------------------------


def test_yolo_survives_resume(scope: Scope, store: StoreHandle) -> None:
    seed(store, "s1", 1)
    key = session_key(scope, "s1")
    ctx = make_ctx(scope, store=store)
    dispatch("/yolo", ctx)
    assert APPROVALS.auto_approve(key)
    assert "Auto-approve is ON" in ctx.lines[0]
    row = sessions.get(store, "s1")
    assert sessions.yolo_enabled(row)
    # a later process resumes the session: the stored switch comes back
    APPROVALS.clear_session(key)
    assert APPROVALS.restore_auto_approve(key, sessions.yolo_enabled(sessions.get(store, "s1")))
    dispatch("/yolo", ctx)
    assert not APPROVALS.auto_approve(key)
    assert not sessions.yolo_enabled(sessions.get(store, "s1"))


def test_yolo_before_the_row_exists_is_written_later(scope: Scope, store: StoreHandle) -> None:
    ctx = make_ctx(scope, store=store)
    dispatch("/yolo", ctx)
    assert ctx.state.pending_config == {"yolo": True}
    seed(store, "s1", 1)
    after_turn(ctx)
    assert sessions.yolo_enabled(sessions.get(store, "s1"))
    assert ctx.state.pending_config == {}


def test_approvals_off_persists_and_is_read_back(scope: Scope) -> None:
    scope = with_config(scope, "approvals:\n  timeout: 120\n")
    ctx = make_ctx(scope)
    dispatch("/approvals", ctx)
    assert ctx.lines == ["Approval mode: manual (saved for this profile)."]
    dispatch("/approvals off", ctx)
    assert ctx.lines[-1] == "Approval mode: off (saved for this profile)."
    loader.reset()
    assert approval_mode(scope) == "off"
    assert loader.load(scope).get("approvals.timeout") == 120
    dispatch("/approvals SMART", ctx)
    assert approval_mode(scope) == "smart"
    ctx.lines.clear()
    dispatch("/approvals sometimes", ctx)
    assert ctx.lines == ["Usage: /approvals [manual|smart|off]"]


def test_approvals_managed_and_unreadable_are_reported(scope: Scope) -> None:
    scope = with_config(scope, "approvals:\n  mode: manual\n")
    (scope.home / ".managed").touch()
    ctx = make_ctx(scope)
    dispatch("/approvals off", ctx)
    assert ctx.lines[-1].startswith("The approval mode was not saved: this installation is managed")
    (scope.home / ".managed").unlink()
    loader.config_path(scope).write_text("approvals: [broken\n", encoding="utf-8")
    loader.invalidate(loader.config_path(scope))
    ctx.lines.clear()
    dispatch("/approvals off", ctx)
    assert ctx.lines[0].startswith("The approval mode was not saved:")
    assert "formatting error" in ctx.lines[0]


def test_approvals_that_do_not_take_effect_are_reported(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    from indus.cli.slash import control_cmds

    scope = with_config(scope, "approvals:\n  mode: manual\n")
    monkeypatch.setattr(control_cmds.writer, "save", lambda scope, patch: None)
    ctx = make_ctx(scope)
    dispatch("/approvals off", ctx)
    assert ctx.lines == ["Approval mode is still manual; the saved value did not take effect."]


# -- /tools ---------------------------------------------------------------------------------------


def test_tools_list_and_disable_resets(scope: Scope) -> None:
    tools = ["read_file", "write_file", "patch", "search_files", "terminal", "process_manage",
             "web_search", "web_extract"]  # fmt: skip
    port = FakePort(tools=tools)
    ctx = make_ctx(scope, port=port)
    dispatch("/tools", ctx)
    assert ctx.lines[0].startswith("Tools in this session (8): patch, process_manage")
    result = dispatch("/tools disable web", ctx)
    assert result.switch_to == "new1"
    assert port.created[0].toolsets == ["file", "terminal"]
    assert ctx.state.toolsets == ["file", "terminal"]
    dispatch("/tools enable web", ctx)
    assert port.created[1].toolsets == ["file", "terminal", "web"]
    ctx.lines.clear()
    dispatch("/tools enable nonsense", ctx)
    assert ctx.lines[0] == "Unknown toolset: nonsense."
    ctx.lines.clear()
    dispatch("/tools remove web", ctx)
    assert ctx.lines == ["Usage: /tools [list|enable|disable] [toolset...]"]


# -- information ----------------------------------------------------------------------------------


def test_config_is_redacted(scope: Scope) -> None:
    secret = "sk-" + "Zq8" * 14
    scope = with_config(scope, f"web:\n  extract_key: {secret}\n")
    ctx = make_ctx(scope)
    dispatch("/config", ctx)
    assert ctx.lines[0].startswith("Effective settings (")
    assert secret not in ctx.text
    assert "extract_key:" in ctx.text


def test_status_whoami_version_usage_context(scope: Scope, store: StoreHandle) -> None:
    seed(store, "s1", 2, title="demo")
    ctx = make_ctx(scope, store=store)
    dispatch("/status", ctx)
    assert ctx.lines[0] == "Session: s1 (demo), 4 messages"
    assert ctx.lines[1] == "Model: m1 (provider fake), reasoning medium"
    assert ctx.lines[2] == "Approvals: manual"
    ctx.lines.clear()
    dispatch("/whoami", ctx)
    assert ctx.lines == ["Profile: work", f"Home: {scope.home}", "Session: s1"]
    ctx.lines.clear()
    dispatch("/usage", ctx)
    assert ctx.lines[0] == "Requests: 0"
    assert ctx.lines[-1] == "Cost: $0.00 (no requests yet)"
    ctx.lines.clear()
    dispatch("/ctx", ctx)
    assert ctx.lines == ["Context window: 128,000 tokens (m1).", "No requests yet in this session."]
    ctx.lines.clear()
    dispatch("/compact", ctx)
    assert ctx.lines == ["Nothing to compress yet."]


def test_help_groups_and_filters(scope: Scope) -> None:
    scope = with_config(scope, "quick_commands:\n  gs: {type: exec, command: git status}\n")
    ctx = make_ctx(scope)
    dispatch("/help", ctx)
    headers = [line for line in ctx.lines if not line.startswith(" ")]
    assert headers == [
        "Session:",
        "Settings:",
        "Tools and skills:",
        "Information:",
        "Leaving:",
        "Quick commands:",
        "Also: !COMMAND runs a shell command here without asking the model.",
    ]
    assert "  /gs" in ctx.lines
    assert not any("/agents" in line for line in ctx.lines)  # not built yet: not offered
    assert any(line.startswith("  /mcp ") for line in ctx.lines)  # P13 wave 2
    assert any(line.startswith("  /goal ") for line in ctx.lines)  # P10 slice K
    ctx.lines.clear()
    dispatch("/help undo", ctx)
    assert ctx.lines[0] == "Session:"
    assert ctx.lines[1].startswith("  /undo [N]")
    ctx.lines.clear()
    dispatch("/help qwerty", ctx)
    assert ctx.lines == ["No command matches 'qwerty'."]


def test_help_line_shows_aliases(scope: Scope) -> None:
    ctx = make_ctx(scope)
    dispatch("/help quit", ctx)
    assert ctx.lines[1] == f"  {'/quit':<34} Leave Indus Agent (also /exit)"


# -- running work ---------------------------------------------------------------------------------


def test_stop_kills_jobs_and_interrupts(scope: Scope) -> None:
    port = FakePort(busy=True, jobs=2)
    ctx = make_ctx(scope, port=port)
    dispatch("/stop", ctx)
    assert port.names() == ["interrupt", "wait_idle", "interrupt", "stop_processes"]
    assert ("stop_processes", True) in port.calls
    assert ctx.lines == ["Done: stopped 2 background processes."]
    ctx.lines.clear()
    dispatch("/stop", ctx)
    assert ctx.lines == ["Nothing was running."]


def test_steer_needs_a_running_turn(scope: Scope) -> None:
    port = FakePort()
    ctx = make_ctx(scope, port=port)
    dispatch("/s check the logs", ctx)
    assert ctx.lines == ["Nothing is running to steer; send it as a normal message instead."]
    ctx.lines.clear()
    dispatch("/steer", ctx)
    assert ctx.lines == ["Usage: /steer TEXT"]


def test_queue_management(scope: Scope) -> None:
    port = FakePort(busy=True)
    ctx = make_ctx(scope, port=port)
    for text in ("one", "two", "three"):
        dispatch(f"/queue {text}", ctx)
    ctx.lines.clear()
    dispatch("/queue list", ctx)
    assert ctx.lines == ["Queued messages:", "  1. one", "  2. two", "  3. three"]
    dispatch("/queue rm 2", ctx)
    assert ctx.state.queue == ["one", "three"]
    dispatch("/queue rm 9", ctx)
    assert ctx.lines[-1] == "There is no queued message number 9."
    dispatch("/queue clear", ctx)
    assert ctx.state.queue == []
    dispatch("/queue add list the files", ctx)
    dispatch("/queue list every test", ctx)
    assert ctx.state.queue == ["list the files", "list every test"]
    ctx.state.queue.clear()
    port.busy = False
    assert dispatch("/queue right now", ctx).submit == "right now"


def test_quit_and_exit(scope: Scope) -> None:
    ctx = make_ctx(scope)
    assert dispatch("/quit", ctx).exit
    assert dispatch("/EXIT", ctx).exit


@pytest.mark.usefixtures("resolved")
def test_model_switch_survives_resume(scope: Scope, store: StoreHandle) -> None:
    from indus.cli import resume
    from indus.cli.session_select import LAUNCH_KEY, LaunchPolicy, check_frozen, stored_launch

    sid = "20260101_090000_mod001"
    launch = LaunchPolicy(model="m1", provider="fake", reasoning="low")
    sessions.create(store, sid, "cli", model_config={LAUNCH_KEY: launch.record()})
    ctx = make_ctx(scope, store=store, port=FakePort(), session_id=sid)
    dispatch("/model m2", ctx)
    row = sessions.get(store, sid)
    config = sessions.parse_model_config((row or {}).get("model_config"))
    assert config[resume.MODEL_OVERRIDE_KEY] == {"provider": "fake", "model": "m2"}
    got = resume.resume_session(scope, sid, store=store, restore=False, breadcrumb=False)
    assert got.policy is not None and got.policy.model == "m2" and got.policy.provider == "fake"
    assert got.policy.reasoning == "low"
    stored = resume.apply_switches(stored_launch(row), row)
    assert check_frozen(stored, {"model": "m2"}) == []
    assert check_frozen(stored, {"model": "m1"}) != []


# -- /compress (P08-05) and /usage cost (P08-08) ------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ("", (None, None, False)),
        ("the refactor", ("the refactor", None, False)),
        ("auth flow here 6 --preview", ("auth flow", 6, True)),
        ("--PREVIEW here 2", (None, 2, True)),
        ("here", None),
        ("here zero", None),
        ("here 0", None),
    ],
)
def test_compress_arguments(args: str, expected: Any) -> None:
    from indus.cli.slash.control_cmds import compress_args

    assert compress_args(args) == expected


def test_compress_goes_through_the_port(scope: Scope) -> None:
    from indus.cli.slash.context import CompressReport

    port = FakePort()
    ctx = make_ctx(scope, port=port)
    dispatch("/compress the refactor here 4", ctx)
    assert port.calls[-1] == ("compress", ("the refactor", 4, False))
    assert ctx.lines == ["Nothing to compress yet."]
    ctx.lines.clear()
    port.compress_answer = CompressReport(
        "preview", "s1", reason="12 message(s) would be condensed", before=90_000
    )
    dispatch("/compress --preview", ctx)
    assert ctx.lines == [
        "Preview: 12 message(s) would be condensed (about 90,000 tokens now); nothing changed."
    ]
    ctx.lines.clear()
    port.compress_answer = CompressReport("compressed", "s1", before=90_000, after=20_000)
    dispatch("/compress", ctx)
    assert ctx.lines == []  # the engine's own notice reports it
    port.compress_answer = CompressReport("compressed", "s1-child", mode="rotating")
    dispatch("/compress", ctx)
    assert ctx.lines == ["The conversation continues in session s1-child."]
    assert ctx.session_id == "s1-child"
    ctx.lines.clear()
    dispatch("/compress here x", ctx)
    assert ctx.lines[0].startswith("Usage: /compress")
    ctx.lines.clear()
    port.busy = True
    dispatch("/compress", ctx)
    assert ctx.lines[0].startswith("/compress has to wait until the reply finishes")
    assert port.calls[-1] == ("compress", (None, None, False))  # not called again


def test_usage_shows_cost_and_cache_from_the_store(scope: Scope, store: StoreHandle) -> None:
    from indus.state import usage as usage_rows
    from indus.state.usage import UsageDelta

    seed(store, "s1", 1)
    delta = UsageDelta(
        input_tokens=200,
        output_tokens=50,
        cache_read_tokens=600,
        cache_write_tokens=200,
        api_calls=2,
        estimated_cost_usd=0.0123,
        cost_status="estimated",
        model="claude-x",
    )
    usage_rows.record(store, "s1", delta)
    ctx = make_ctx(scope, store=store)
    dispatch("/usage", ctx)
    assert ctx.lines[0] == "Requests: 2"
    assert ctx.lines[2] == "Prompt cache: 60% of prompt tokens read from the cache"
    assert ctx.lines[-1] == "Cost: $0.01 (estimated from list prices)"
    usage_rows.record(store, "s1", UsageDelta(input_tokens=5, api_calls=1, cost_status="unknown"))
    ctx.lines.clear()
    dispatch("/usage", ctx)
    assert ctx.lines[-1] == "Cost: $0.01+ (some requests have no known price)"
