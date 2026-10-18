"""Slash dispatch and the session commands (P06-09)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from indus import __version__
from indus.cli.slash import dispatch as dispatch_mod
from indus.cli.slash.context import Extensions, SlashContext, SlashResult
from indus.cli.slash.dispatch import after_turn, dispatch, is_slash, take_resume_pick
from indus.foundation.scope import Scope
from indus.state import messages, sessions, titles
from indus.state.registry import StoreHandle
from tests.unit.cli.slash._fakes import FakePort, live_users, make_ctx, seed, with_config

# -- recognising and routing ----------------------------------------------------------------------


def test_is_slash_rules() -> None:
    assert is_slash("/help")
    assert is_slash("  /new now")
    assert not is_slash("/usr/bin/env python")
    assert not is_slash("what does /help do?")
    assert not is_slash("/")


def test_arguments_keep_their_case(scope: Scope, store: StoreHandle) -> None:
    seed(store, "s1", 1)
    ctx = make_ctx(scope, store=store)
    dispatch("/TITLE My Big Title", ctx)
    assert titles.get_title(store, "s1") == "My Big Title"
    assert "Title set: My Big Title" in ctx.text


def test_precedence_order(scope: Scope) -> None:
    scope = with_config(
        scope,
        "quick_commands:\n"
        "  help: {type: alias, target: version}\n"
        "  both: {type: alias, target: whoami}\n",
    )
    seen: list[str] = []

    def plugin(args: str, ctx: SlashContext) -> SlashResult | None:
        seen.append(f"plugin:{args}")
        return None

    def skill(args: str, ctx: SlashContext) -> SlashResult | None:
        seen.append(f"skill:{args}")
        return SlashResult(submit=f"skill says {args}")

    ext = Extensions(
        plugin_commands={"both": plugin, "plug": plugin, "dup": plugin},
        skill_commands={"both": skill, "dup": skill, "deploy-notes": skill},
    )
    ctx = make_ctx(scope, extensions=ext)
    # 1. a built-in beats a quick command of the same name
    dispatch("/help", ctx)
    assert "Session:" in ctx.text and f"Indus Agent {__version__}" not in ctx.text
    # 2. a quick command beats plugin and skill commands
    ctx.lines.clear()
    dispatch("/both", ctx)
    assert "Profile:" in ctx.text and seen == []
    # 3. a plugin command beats a skill of the same name
    dispatch("/DUP x", ctx)
    assert seen == ["plugin:x"]
    # 4. a skill runs when nothing above has the name
    result = dispatch("/deploy-notes Fix It", ctx)
    assert result.submit == "skill says Fix It"
    # 5. unique-prefix expansion reaches every level
    assert dispatch("/deploy now", ctx).submit == "skill says now"
    ctx.lines.clear()
    dispatch("/vers", ctx)
    assert ctx.lines == [f"Indus Agent {__version__}"]


def test_observer_fires_first_and_cannot_block(scope: Scope) -> None:
    order: list[str] = []

    def observer(command: str, args: str, ctx: SlashContext) -> object:
        order.append(f"observe {command} {args}")
        raise RuntimeError("observer broke")

    def watch(command: str, args: str, ctx: SlashContext) -> object:
        order.append("second")
        return False  # a return value never blocks

    ctx = make_ctx(scope, extensions=Extensions(observers=[observer, watch]))
    dispatch("/V", ctx)
    assert order == ["observe version ", "second"]
    assert ctx.lines == [f"Indus Agent {__version__}"]


def test_prefix_ambiguity_lists(scope: Scope) -> None:
    ctx = make_ctx(scope)
    result = dispatch("/re", ctx)
    assert result == SlashResult()
    assert ctx.lines[0] == ("/re could be several commands: /reasoning, /reset, /resume, /retry.")


def test_unknown_command_suggests(scope: Scope) -> None:
    ctx = make_ctx(scope)
    dispatch("/hepl", ctx)
    assert ctx.lines == ["/hepl is not a command.", "Did you mean /help? /help lists all."]
    ctx.lines.clear()
    dispatch("/zzzz", ctx)
    assert ctx.lines == ["/zzzz is not a command.", "Type /help to see the commands."]


def test_planned_and_other_surface_commands_say_so(scope: Scope) -> None:
    ctx = make_ctx(scope)
    dispatch("/goal ship it", ctx)
    assert ctx.lines == ["/goal is not built yet; it arrives with P10."]
    ctx.lines.clear()
    dispatch("/approve", ctx)
    assert ctx.lines == ["/approve is not available here; it works in: gateway."]


def test_a_failing_handler_is_reported(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(ctx: SlashContext, args: str) -> SlashResult:
        raise ValueError("boom")

    monkeypatch.setitem(dispatch_mod.HANDLERS, "version", broken)
    ctx = make_ctx(scope)
    assert dispatch("/version", ctx) == SlashResult()
    assert ctx.lines == ["/version failed: ValueError: boom"]


# -- busy policies --------------------------------------------------------------------------------


def test_new_while_busy_interrupts_then_dispatches(scope: Scope) -> None:
    port = FakePort(busy=True)
    ctx = make_ctx(scope, port=port)
    result = dispatch("/new now", ctx)
    assert port.names()[:3] == ["interrupt", "wait_idle", "create"]
    assert ("close", "s1") in port.calls
    assert result.switch_to == "new1" and ctx.session_id == "new1"


def test_busy_reject_policy(scope: Scope) -> None:
    port = FakePort(busy=True)
    ctx = make_ctx(scope, port=port)
    dispatch("/model other", ctx)
    assert ctx.lines == [
        "/model has to wait until the reply finishes (or press Ctrl+C to stop it)."
    ]
    assert port.calls == []


def test_dispatch_policy_runs_while_busy(scope: Scope) -> None:
    port = FakePort(busy=True)
    ctx = make_ctx(scope, port=port)
    dispatch("/steer look at the tests", ctx)
    assert port.steers == ["look at the tests"]
    assert "interrupt" not in port.names()


def test_interrupt_that_does_not_settle_skips_the_command(scope: Scope) -> None:
    class Stuck(FakePort):
        def wait_idle(self, session_id: str, timeout: float) -> bool:
            return False

    port = Stuck(busy=True)
    ctx = make_ctx(scope, port=port)
    dispatch("/new now", ctx)
    assert port.created == []
    assert ctx.lines == ["The reply did not stop in time, so /new did not run."]


# -- /new, /clear ---------------------------------------------------------------------------------


def test_new_asks_first_when_there_is_history(scope: Scope, store: StoreHandle) -> None:
    seed(store, "s1", 1)
    asked: list[str] = []

    def no(question: str) -> bool:
        asked.append(question)
        return False

    port = FakePort()
    ctx = make_ctx(scope, store=store, port=port, confirm=no)
    assert dispatch("/reset", ctx) == SlashResult()
    assert asked and port.created == []
    assert ctx.lines == ["Kept the current session."]
    result = dispatch("/new -y Experiment One", ctx)
    assert result.switch_to == "new1"
    assert port.created[0].title == "Experiment One"
    assert ctx.state.pending_title == "Experiment One"
    assert ("stop_processes", False) in port.calls


def test_new_keeps_the_creation_flags(scope: Scope) -> None:
    from indus.protocol.methods import SessionCreateParams

    port = FakePort()
    ctx = make_ctx(
        scope,
        port=port,
        create_params=SessionCreateParams(cwd="/work", model="m9", idempotency_key="k1"),
    )
    dispatch("/new now", ctx)
    made = port.created[0]
    assert (made.cwd, made.model, made.idempotency_key) == ("/work", "m9", None)


def test_clear_starts_fresh_and_clears_the_screen(scope: Scope) -> None:
    ctx = make_ctx(scope)
    ctx.state.queue.append("later")
    result = dispatch("/clear", ctx)
    assert result.clear_screen and result.switch_to == "new1"
    assert ctx.state.queue == []


# -- /history, /title -----------------------------------------------------------------------------


def test_history_lists_the_transcript(scope: Scope, store: StoreHandle) -> None:
    sessions.create(store, "s1", "cli")
    messages.append(store, "s1", {"role": "user", "content": "hello"})
    calls = [{"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]
    messages.append(store, "s1", {"role": "assistant", "content": "", "tool_calls": calls})
    messages.append(
        store,
        "s1",
        {"role": "tool", "content": "ok", "tool_call_id": "c1", "tool_name": "terminal"},
    )
    messages.append(store, "s1", {"role": "assistant", "content": "done"})
    ctx = make_ctx(scope, store=store)
    dispatch("/history", ctx)
    assert ctx.lines == [
        "You: hello",
        "  (used terminal)",
        "  [terminal: 2 characters of output]",
        "Agent: done",
    ]


def test_title_waits_for_the_row(scope: Scope, store: StoreHandle) -> None:
    ctx = make_ctx(scope, store=store)
    dispatch("/title Night Shift", ctx)
    assert ctx.lines == ["Title set: Night Shift (saved with the first reply)."]
    assert after_turn(ctx) is None
    assert ctx.state.pending_title == "Night Shift"  # still no row
    seed(store, "s1", 1)
    after_turn(ctx)
    assert titles.get_title(store, "s1") == "Night Shift"
    assert ctx.state.pending_title is None


def test_title_taken_is_reported(scope: Scope, store: StoreHandle) -> None:
    seed(store, "s2", 1, title="taken")
    seed(store, "s1", 1)
    ctx = make_ctx(scope, store=store)
    dispatch("/title taken", ctx)
    assert ctx.lines[0].startswith("The title could not be set:")
    ctx.lines.clear()
    dispatch("/title", ctx)
    assert ctx.lines == ["This session has no title yet."]


# -- /resume, /sessions ---------------------------------------------------------------------------


def test_resume_picker_disarmed_by_other_command(scope: Scope, store: StoreHandle) -> None:
    seed(store, "s1", 1, title="first")
    seed(store, "s2", 2, title="second")
    port = FakePort()
    ctx = make_ctx(scope, store=store, port=port)
    dispatch("/resume", ctx)
    assert ctx.state.resume_choices is not None and len(ctx.state.resume_choices) == 2
    assert "Type a number to resume" in ctx.text
    dispatch("/version", ctx)
    assert ctx.state.resume_choices is None
    assert take_resume_pick("1", ctx) is None  # disarmed: plain input goes to the model


def test_resume_picker_number_switches(scope: Scope, store: StoreHandle) -> None:
    seed(store, "s1", 1, title="first")
    seed(store, "s2", 2, title="second")
    port = FakePort()
    ctx = make_ctx(scope, store=store, port=port)
    dispatch("/sessions", ctx)
    choices = ctx.state.resume_choices
    assert choices is not None
    target = next(sid for sid in choices if sid != "s1")
    number = choices.index(target) + 1
    result = take_resume_pick(f" {number} ", ctx)
    assert result is not None and result.switch_to == target and result.resumed
    assert port.calls[-2:] == [("resume", target), ("close", "s1")]
    assert ctx.session_id == target
    assert ctx.state.resume_choices is None


def test_plain_text_disarms_the_picker(scope: Scope, store: StoreHandle) -> None:
    seed(store, "s1", 1)
    ctx = make_ctx(scope, store=store)
    dispatch("/resume", ctx)
    assert take_resume_pick("hello there", ctx) is None
    assert ctx.state.resume_choices is None


def test_resume_by_title_and_id(scope: Scope, store: StoreHandle) -> None:
    seed(store, "s1", 1)
    seed(store, "20260101_090000_abcdef", 1, title="Research")
    port = FakePort()
    ctx = make_ctx(scope, store=store, port=port)
    assert dispatch("/resume research", ctx).switch_to is None  # titles match exactly
    assert "No session found matching 'research'." in ctx.text
    assert dispatch("/resume Research", ctx).switch_to == "20260101_090000_abcdef"
    ctx.session_id = "s1"
    assert dispatch("/resume <20260101_0900>", ctx).switch_to == "20260101_090000_abcdef"
    ctx.lines.clear()
    dispatch("/resume s1", ctx)
    assert ctx.lines == ["Resumed s1."]
    ctx.lines.clear()
    dispatch("/resume s1", ctx)
    assert ctx.lines == ["You are already in that session."]


# -- /retry, /undo --------------------------------------------------------------------------------


def test_undo_two_hides_two_turns(scope: Scope, store: StoreHandle) -> None:
    seed(store, "s1", 3)
    ctx = make_ctx(scope, store=store)
    result = dispatch("/undo 2", ctx)
    assert live_users(store, "s1") == ["q1"]
    assert result.prefill == "q2"
    assert ctx.lines == ["Took back your last 2 messages and the replies to them."]
    stored = messages.get(store, "s1", include_inactive=True)
    assert len(stored) == 6  # soft delete: rows stay for audit


def test_undo_edge_cases(scope: Scope, store: StoreHandle) -> None:
    ctx = make_ctx(scope, store=store)
    dispatch("/undo", ctx)
    assert ctx.lines == ["There is nothing to undo yet."]
    ctx.lines.clear()
    dispatch("/undo zero", ctx)
    assert ctx.lines[0].startswith("Usage: /undo")
    seed(store, "s1", 2)
    ctx.lines.clear()
    dispatch("/undo 9", ctx)
    assert live_users(store, "s1") == []
    assert ctx.lines == ["Took back your last 2 messages and the replies to them."]


def test_retry_resubmits(scope: Scope, store: StoreHandle) -> None:
    seed(store, "s1", 2)
    ctx = make_ctx(scope, store=store)
    result = dispatch("/retry", ctx)
    assert result.submit == "q2"
    assert live_users(store, "s1") == ["q1"]


def test_session_commands_without_a_store(scope: Scope) -> None:
    ctx = make_ctx(scope)
    dispatch("/history", ctx)
    assert ctx.lines == [
        "Sessions are not being saved in this run, so this command cannot work here."
    ]


# -- /save, /branch -------------------------------------------------------------------------------


def test_save_writes_three_formats(
    scope: Scope, store: StoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(store, "s1", 2, title="Plan <b>")
    monkeypatch.chdir(tmp_path)
    ctx = make_ctx(scope, store=store)
    dispatch("/save json", ctx)
    lines = (tmp_path / "indus-session-s1.jsonl").read_text(encoding="utf-8").splitlines()
    kinds = [json.loads(line)["kind"] for line in lines]
    assert kinds == ["session", "message", "message", "message", "message"]
    dispatch("/save md notes.md", ctx)
    md = (tmp_path / "notes.md").read_text(encoding="utf-8")
    assert md.startswith("# Plan <b>\n") and "## You\n\nq1" in md and "## Indus Agent\n\na2" in md
    dispatch("/save HTML page.html", ctx)
    page = (tmp_path / "page.html").read_text(encoding="utf-8")
    assert "<title>Plan &lt;b&gt;</title>" in page and "<pre>q2</pre>" in page
    ctx.lines.clear()
    dispatch("/save md notes.md", ctx)
    assert ctx.lines == [f"{tmp_path / 'notes.md'} already exists; give another file name."]
    ctx.lines.clear()
    dispatch("/save pdf", ctx)
    assert ctx.lines == ["Usage: /save json|md|html [file]"]


def test_branch_switches_to_a_copy(scope: Scope, store: StoreHandle) -> None:
    seed(store, "s1", 2, title="idea")
    port = FakePort()
    ctx = make_ctx(scope, store=store, port=port)
    result = dispatch("/fork", ctx)
    child = result.switch_to
    assert child is not None and child != "s1" and result.resumed
    assert live_users(store, child) == ["q1", "q2"]
    assert titles.get_title(store, child) == "idea #2"
    assert port.calls == [("resume", child), ("close", "s1")]
    assert live_users(store, "s1") == ["q1", "q2"]


def test_branch_needs_history(scope: Scope, store: StoreHandle) -> None:
    ctx = make_ctx(scope, store=store)
    dispatch("/branch", ctx)
    assert ctx.lines == ["There is nothing to branch yet; send a message first."]


# -- after_turn and the queue ---------------------------------------------------------------------


def test_queue_runs_after_the_turn(scope: Scope) -> None:
    port = FakePort(busy=True)
    ctx = make_ctx(scope, port=port)
    dispatch("/queue then run the tests", ctx)
    dispatch("/q and lint", ctx)
    assert ctx.state.queue == ["then run the tests", "and lint"]
    port.busy = False
    assert after_turn(ctx) == "then run the tests"
    assert after_turn(ctx) == "and lint"
    assert after_turn(ctx) is None
