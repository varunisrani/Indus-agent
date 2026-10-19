"""browser_cdp, the supervisor, browser_vision and /browser (P10-14)."""

from __future__ import annotations

import asyncio
import base64
import functools
import json
import threading
from collections.abc import Iterator, Mapping
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from indus.cli.slash import browser_cmds
from indus.foundation.scope import Scope
from indus.safety import url_policy
from indus.tools import toolsets
from indus.tools.builtin import browser as browser_pkg
from indus.tools.builtin.browser import cdp as cdp_mod
from indus.tools.builtin.browser.cdp import (
    MAX_CDP_TIMEOUT_S,
    MAX_RECONNECT_FAILURES,
    Supervisor,
    clamp_cdp_timeout,
    endpoint_live,
    normalise_endpoint,
    redact_cdp,
)
from indus.tools.builtin.browser.session import Choice
from indus.tools.calls import ToolCall, ToolContext
from indus.tools.definitions import definitions
from indus.tools.dispatch import dispatch
from indus.tools.registry import ToolRegistry
from tests.fakes.aux_model import FakeAuxModel
from tests.fakes.browser import PNG_BYTES, FakeDialog, FakeSite, el
from tests.fakes.clock import checkpoint
from tests.unit.cli.slash._fakes import make_ctx
from tests.unit.tools.builtin.browser._rig import Rig, make_clock, make_rig, make_scope_for

PAGE = "https://example.com/"
SECRETISH = "sk-" + "c" * 30


@pytest.fixture(autouse=True)
def _fresh_blocklist() -> Iterator[None]:
    url_policy.clear_blocklist_cache()
    yield
    url_policy.clear_blocklist_cache()


def sites() -> dict[str, FakeSite]:
    return {
        PAGE: FakeSite(
            title="Dialogs",
            elements=[
                el("button", "Alert me", opens_dialog=("alert", "Saved!")),
                el("button", "Ask me", opens_dialog=("prompt", "Your word?")),
                el("textbox", "Name"),
            ],
            frames=3,
        )
    }


class Run:
    def __init__(self, rig: Rig, scope: Scope, extras: Mapping[str, Any] | None = None) -> None:
        self.rig, self.scope = rig, scope
        self.registry = ToolRegistry()
        rig.tools.register(self.registry)
        self.loop = asyncio.new_event_loop()
        self.extras = dict(extras or {})

    def raw(self, name: str, task: str = "t", **args: Any) -> Any:
        ctx = ToolContext(scope=self.scope, task_id=task, extras=self.extras)
        return self.loop.run_until_complete(
            dispatch(ToolCall(name, args), ctx, registry=self.registry)
        ).content

    def __call__(self, name: str, task: str = "t", **args: Any) -> dict[str, Any]:
        content = self.raw(name, task, **args)
        assert isinstance(content, str), content
        return json.loads(content)

    def spin(self, times: int = 10) -> None:
        for _ in range(times):
            self.loop.run_until_complete(checkpoint())

    def close(self) -> None:
        self.loop.run_until_complete(self.rig.manager.shutdown())
        self.loop.close()


@pytest.fixture
def rig() -> Rig:
    return make_rig(sites())


@pytest.fixture
def scope(tmp_path: Path, rig: Rig) -> Scope:
    return make_scope_for(tmp_path, rig.clock)


@pytest.fixture
def run(rig: Rig, scope: Scope) -> Iterator[Run]:
    made = Run(rig, scope)
    yield made
    made.close()


# -- a DevTools discovery endpoint on 127.0.0.1 ------------------------------------------------


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib name
        pass


@pytest.fixture
def devtools(tmp_path: Path) -> Iterator[str]:
    """A folder served on 127.0.0.1 whose ``json/version`` looks like a DevTools answer."""
    root = tmp_path / "devtools"
    (root / "json").mkdir(parents=True)
    answer = {"Browser": "Chrome/141", "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/browser/x"}
    (root / "json" / "version").write_text(json.dumps(answer))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(root)))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def tool_names(rig: Rig, scope: Scope) -> set[str]:
    registry = ToolRegistry(clock=rig.clock)
    rig.tools.register(registry)
    selection = toolsets.select(["browser"], registry=registry, scope=scope)
    available = {
        name
        for name in selection.tools
        if (entry := registry.get(name, scope)) is not None and registry.available(entry, scope)
    }
    return {d["function"]["name"] for d in definitions(registry, available, scope=scope)}


# -- availability ---------------------------------------------------------------------------------


@pytest.fixture
def browser_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """Availability is judged as if the browser extra were installed (CI installs no extras)."""
    monkeypatch.setattr("indus.tools.builtin.browser.playwright_available", lambda: True)


def test_no_cdp_endpoint_means_no_browser_cdp_in_the_tool_array(
    rig: Rig, scope: Scope, browser_extra: None
) -> None:
    names = tool_names(rig, scope)
    assert "browser_cdp" not in names
    assert {"browser_navigate", "browser_dialog", "browser_vision"} <= names


def test_a_live_endpoint_brings_browser_cdp(
    rig: Rig, tmp_path: Path, devtools: str, browser_extra: None
) -> None:
    scope = make_scope_for(tmp_path, rig.clock, {"browser": {"cdp_url": devtools}}, name="cdp")
    assert endpoint_live(devtools)
    assert "browser_cdp" in tool_names(rig, scope)


def test_a_dead_endpoint_keeps_browser_cdp_out(rig: Rig, tmp_path: Path) -> None:
    scope = make_scope_for(
        tmp_path, rig.clock, {"browser": {"cdp_url": "http://127.0.0.1:9"}}, name="dead"
    )
    assert not endpoint_live("http://127.0.0.1:9")
    assert "browser_cdp" not in tool_names(rig, scope)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("127.0.0.1:9222", "http://127.0.0.1:9222"),
        ("http://localhost:9222/json/version?x=1", "http://localhost:9222"),
        ("ws://127.0.0.1:9222/devtools/browser/abc", "ws://127.0.0.1:9222/devtools/browser/abc"),
        ("ftp://host:1", None),
        ("http://:9222", None),
        ("http://host:99999", None),
        ("", None),
    ],
)
def test_endpoint_normalising(raw: str, expected: str | None) -> None:
    assert normalise_endpoint(raw) == expected


# -- browser_cdp ----------------------------------------------------------------------------------


def test_clamp_cdp_timeout() -> None:
    assert clamp_cdp_timeout(None) == 30
    assert clamp_cdp_timeout(1000) == MAX_CDP_TIMEOUT_S == 300
    assert clamp_cdp_timeout(0) == 1
    assert clamp_cdp_timeout("12") == 12
    assert clamp_cdp_timeout("soon") == 30
    assert clamp_cdp_timeout(float("nan")) == 30


def test_cdp_round_trip_and_levels(run: Run, rig: Rig) -> None:
    rig.driver.cdp_answers["Network.getCookies"] = {"cookies": [{"name": "a", "value": "b"}]}
    rig.driver.cdp_answers["Browser.getVersion"] = {"product": "Chrome/141"}
    body = run("browser_cdp", method="Network.getCookies", params={"urls": [PAGE]})
    assert body == {
        "success": True,
        "method": "Network.getCookies",
        "result": {"cookies": [{"name": "a", "value": "b"}]},
    }
    run("browser_cdp", method="Browser.getVersion")
    assert rig.driver.cdp_calls == [
        ("Network.getCookies", {"urls": [PAGE]}, False),
        ("Browser.getVersion", {}, True),
    ]


def test_cdp_timeout_is_clamped_to_300(run: Run, rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []
    original = rig.manager.run

    async def spy(*args: Any, **kw: Any) -> Any:
        seen.append(kw["timeout_s"])
        return await original(*args, **kw)

    monkeypatch.setattr(rig.manager, "run", spy)
    run("browser_cdp", method="Page.reload", timeout=5000)
    assert seen == [300]


def test_cdp_timeout_reports_and_keeps_the_session(run: Run, rig: Rig) -> None:
    rig.driver.hang_on.add("cdp")
    body = run("browser_cdp", method="Page.reload", timeout=1)
    assert body["success"] is False and "No answer within 1 s" in body["error"]
    session = rig.manager.get("t")
    assert session is not None and session.suspect is None


def test_cdp_argument_and_protocol_errors(run: Run, rig: Rig) -> None:
    assert "method" in run("browser_cdp", method="not a method")["error"]
    assert "object" in run("browser_cdp", method="Page.reload", params=[1, 2])["error"]
    rig.driver.cdp_answers["Page.bogus"] = RuntimeError("Protocol error: 'Page.bogus' wasn't found")
    body = run("browser_cdp", method="Page.bogus")
    assert body["success"] is False and "wasn't found" in body["error"]
    assert body["cdp_docs"].startswith("https://chromedevtools.github.io/")


def test_cdp_results_are_redacted_but_binary_fields_kept() -> None:
    blob = base64.b64encode(b"x" * 10 + SECRETISH.encode()).decode()
    shot = redact_cdp("Page.captureScreenshot", {"data": SECRETISH})
    assert shot["data"] == SECRETISH  # protocol binary: untouched
    text = redact_cdp("Runtime.evaluate", {"result": {"value": SECRETISH}})
    assert SECRETISH not in json.dumps(text)
    body = redact_cdp("Network.getResponseBody", {"body": SECRETISH, "base64Encoded": True})
    assert body["body"] == SECRETISH
    plain = redact_cdp("Network.getResponseBody", {"body": SECRETISH, "base64Encoded": False})
    assert plain["body"] != SECRETISH
    assert blob  # the blob helper stays valid base64 text


def test_cdp_guards_refused_addresses(run: Run, rig: Rig) -> None:
    nav = run("browser_cdp", method="Page.navigate", params={"url": "http://169.254.169.254/"})
    assert nav["success"] is False and "Blocked" in nav["error"]
    code = run(
        "browser_cdp",
        method="Runtime.evaluate",
        params={"expression": "fetch('http://10.0.0.9/admin')"},
    )
    assert code["success"] is False and "10.0.0.9" in code["error"]
    assert rig.driver.cdp_calls == []


# -- dialogs --------------------------------------------------------------------------------------


def test_dialog_is_captured_shown_and_accepted(run: Run, rig: Rig) -> None:
    run("browser_navigate", url=PAGE)
    clicked = run("browser_click", ref="e1")
    assert clicked["success"] is False
    assert clicked["pending_dialogs"] == [{"id": "d1", "type": "alert", "message": "Saved!"}]
    shown = run("browser_snapshot")
    assert shown["pending_dialogs"][0]["id"] == "d1"
    blocked_type = run("browser_type", ref="e3", text="x")
    assert blocked_type["success"] is False and "dialog" in blocked_type["error"]
    answer = run("browser_dialog", action="accept")
    assert answer == {
        "success": True,
        "action": "accept",
        "dialog": {"id": "d1", "type": "alert", "message": "Saved!"},
    }
    assert rig.driver.dialogs[0].answer == "accept"
    after = run("browser_snapshot")
    assert "pending_dialogs" not in after
    assert after["recent_dialogs"][0]["answer"] == "accept"


def test_prompt_dialog_gets_the_text(run: Run, rig: Rig) -> None:
    run("browser_navigate", url=PAGE)
    run("browser_click", ref="e2")
    shown = run("browser_snapshot")["pending_dialogs"][0]
    assert shown["type"] == "prompt" and shown["default_prompt"] == "default"
    assert run("browser_dialog", action="dismiss_all")["success"] is False
    assert run("browser_dialog", action="accept", prompt_text="banana")["success"] is True
    dialog = rig.driver.dialogs[0]
    assert (dialog.answer, dialog.prompt_text) == ("accept", "banana")


def test_dialog_tool_without_a_page_or_dialog(run: Run) -> None:
    assert "No browser page" in run("browser_dialog", action="accept")["error"]
    run("browser_navigate", url=PAGE)
    assert "No dialog" in run("browser_dialog", action="accept")["error"]


def test_frames_show_in_snapshots(run: Run) -> None:
    run("browser_navigate", url=PAGE)
    frames = run("browser_snapshot")["frames"]
    assert [f["frame_id"] for f in frames] == ["f1", "f2", "f3"]
    assert frames[1]["parent_id"] == "f1"


def _supervisor(policy: str = "must_respond", timeout: float = 300.0) -> Supervisor:
    return Supervisor(policy=policy, dialog_timeout_s=timeout, clock=make_clock())


def test_two_open_dialogs_need_an_id() -> None:
    async def go() -> None:
        sup = _supervisor()
        first, second = FakeDialog("confirm", "One?"), FakeDialog("confirm", "Two?")
        sup.on_dialog(first)
        sup.on_dialog(second)
        assert "pass dialog_id" in (await sup.respond("accept"))["error"]
        assert "No open dialog 'd9'" in (await sup.respond("accept", dialog_id="d9"))["error"]
        assert (await sup.respond("dismiss", dialog_id="d2"))["success"]
        assert (second.answer, first.answer) == ("dismiss", None)
        assert (await sup.respond("accept"))["dialog"]["id"] == "d1"
        await sup.stop()

    asyncio.run(go())


@pytest.mark.parametrize(
    ("policy", "answer"), [("auto_dismiss", "dismiss"), ("auto_accept", "accept")]
)
def test_auto_policies_answer_at_once(policy: str, answer: str) -> None:
    async def go() -> None:
        sup = _supervisor(policy)
        dialog = FakeDialog("confirm", "Leave?")
        sup.on_dialog(dialog)
        await checkpoint()
        await checkpoint()
        assert dialog.answer == answer and not sup.pending
        assert sup.view()["recent_dialogs"][0]["by"] == "policy"
        await sup.stop()

    asyncio.run(go())


def test_unanswered_dialog_is_dismissed_after_the_safety_timeout() -> None:
    async def go() -> None:
        sup = _supervisor(timeout=300)
        clock = sup.clock
        dialog = FakeDialog("alert", "Hi")
        sup.on_dialog(dialog)
        await checkpoint()
        clock.advance(299)  # type: ignore[attr-defined]
        await checkpoint()
        assert dialog.answer is None
        clock.advance(2)  # type: ignore[attr-defined]
        for _ in range(5):
            await checkpoint()
        assert dialog.answer == "dismiss" and not sup.pending
        assert sup.recent[-1]["by"] == "timeout"
        await sup.stop()

    asyncio.run(go())


def test_dialog_text_is_redacted() -> None:
    sup = _supervisor()
    sup.on_dialog(FakeDialog("alert", f"your key is {SECRETISH}"))
    assert SECRETISH not in json.dumps(sup.pending_dialogs())


# -- reconnects -----------------------------------------------------------------------------------


def _attached(rig: Rig, scope: Scope, run: Run) -> Any:
    rig.manager.choices.set(scope, Choice("cdp", "http://127.0.0.1:9222", "connect"))
    run("browser_snapshot")
    session = rig.manager.get("t")
    assert session is not None and session.choice.mode == "cdp"
    return session


def test_reconnect_gives_up_after_five_failures(run: Run, rig: Rig, scope: Scope) -> None:
    session = _attached(rig, scope, run)
    rig.driver.attach_failures = 100
    run.loop.call_soon(rig.driver.handles[-1].drop_connection)
    for _ in range(MAX_RECONNECT_FAILURES + 2):
        run.spin()
        rig.clock.advance(10)
    run.spin()
    assert session.supervisor.gave_up is True
    assert session.supervisor.reconnect_failures == MAX_RECONNECT_FAILURES == 5
    assert len(rig.driver.attaches) == 1 + 5
    rig.driver.attach_failures = 0
    run("browser_snapshot")
    assert rig.manager.get("t") is not session  # replaced on next use


def test_reconnect_success_resets_the_count(run: Run, rig: Rig, scope: Scope) -> None:
    session = _attached(rig, scope, run)
    rig.driver.attach_failures = 2
    run.loop.call_soon(rig.driver.handles[-1].drop_connection)
    for _ in range(4):
        run.spin()
        rig.clock.advance(10)
    run.spin()
    sup = session.supervisor
    assert sup.connected is True and sup.gave_up is False and sup.reconnect_failures == 0
    assert session.handle is rig.driver.handles[-1]
    assert rig.manager.get("t") is session


def test_a_launched_session_that_dies_is_replaced(run: Run, rig: Rig) -> None:
    run("browser_snapshot")
    first = rig.manager.get("t")
    run.loop.call_soon(rig.driver.handles[-1].drop_connection)
    run("browser_snapshot")
    assert rig.manager.get("t") is not first


# -- vision ---------------------------------------------------------------------------------------


def test_vision_on_a_non_vision_route_asks_the_auxiliary_model(rig: Rig, scope: Scope) -> None:
    aux = FakeAuxModel(f"A form with a Name field. Token on screen: {SECRETISH}")
    run = Run(rig, scope, extras={"aux": aux, "native_vision": False})
    try:
        run("browser_navigate", url=PAGE)
        body = run("browser_vision", question="What is on the page?")
    finally:
        run.close()
    assert body["success"] is True
    assert body["analysis"].startswith("A form with a Name field.")
    assert SECRETISH not in body["analysis"]
    path = Path(body["screenshot_path"])
    assert path.read_bytes() == PNG_BYTES and path.parent == scope.home / "cache" / "screenshots"
    call = aux.calls[-1]
    assert call.task == "vision"
    parts = call.messages[0]["content"]
    assert "What is on the page?" in parts[0]["text"]
    assert (
        parts[1]["image_url"]["url"]
        == "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()
    )


def test_vision_on_a_vision_route_attaches_the_image(rig: Rig, scope: Scope) -> None:
    run = Run(rig, scope, extras={"native_vision": True})
    try:
        run("browser_navigate", url=PAGE)
        result = run.raw("browser_vision", question="Where is the button?", annotate=True)
    finally:
        run.close()
    assert isinstance(result, Mapping) and result["multimodal"] is True
    text, image = result["content"]
    assert text["type"] == "text" and "MEDIA:" in text["text"]
    assert "[1] = @e1" in result["text_summary"]
    assert image["image_url"]["url"].startswith("data:image/png;base64,")
    shot_calls = [c for c in rig.driver.handles[-1].page.calls if c[0] == "screenshot"]
    assert shot_calls[-1][1] is not None  # labels asked for


def test_vision_without_any_vision_model_keeps_the_path(rig: Rig, scope: Scope) -> None:
    run = Run(rig, scope)
    try:
        run("browser_navigate", url=PAGE)
        body = run("browser_vision", question="Anything?")
    finally:
        run.close()
    assert body["success"] is False and Path(body["screenshot_path"]).is_file()


def test_vision_failure_keeps_the_path(rig: Rig, scope: Scope) -> None:
    aux = FakeAuxModel(RuntimeError("model down"))
    run = Run(rig, scope, extras={"aux": aux})
    try:
        run("browser_navigate", url=PAGE)
        body = run("browser_vision", question="Anything?")
    finally:
        run.close()
    assert body["success"] is False and "model down" in body["error"]
    assert Path(body["screenshot_path"]).is_file() and "MEDIA" in body["note"]


def test_vision_needs_a_question(run: Run) -> None:
    assert run("browser_vision", question=" ")["success"] is False


# -- /browser -------------------------------------------------------------------------------------


@pytest.fixture
def slash(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> Rig:
    monkeypatch.setattr(browser_cmds, "_tools", lambda: rig.tools)
    return rig


def test_browser_connect_switches_the_sessions_driver(
    slash: Rig, scope: Scope, devtools: str
) -> None:
    run = Run(slash, scope)
    try:
        run("browser_snapshot")
        local = slash.manager.get("t")
        assert local is not None and local.choice.mode == "local"
        ctx = make_ctx(scope)
        browser_cmds.cmd_browser(ctx, f"connect {devtools}/json/version")
        assert "now drive the browser at" in ctx.text
        run("browser_snapshot")
        attached = slash.manager.get("t")
        assert attached is not None and attached is not local
        assert attached.choice == Choice("cdp", devtools, "connect")
        assert slash.driver.attaches == [devtools]
        status = make_ctx(scope)
        browser_cmds.cmd_browser(status, "")
        assert "attached over CDP" in status.text and "it answers" in status.text
        assert "sessions for this profile: 1" in status.text
        off = make_ctx(scope)
        browser_cmds.cmd_browser(off, "disconnect")
        assert "local headless Chromium again" in off.text
        run("browser_snapshot")
        assert slash.manager.get("t").choice.mode == "local"  # type: ignore[union-attr]
    finally:
        run.close()


def test_browser_connect_to_nothing_changes_nothing(slash: Rig, scope: Scope) -> None:
    ctx = make_ctx(scope)
    browser_cmds.cmd_browser(ctx, "connect 127.0.0.1:9")
    assert "Nothing answers" in ctx.text
    assert slash.manager.choices.get(scope) is None


def test_browser_use_local_overrides_the_config(slash: Rig, tmp_path: Path) -> None:
    scope = make_scope_for(
        tmp_path, slash.clock, {"browser": {"cdp_url": "http://127.0.0.1:9333"}}, name="cfg"
    )
    assert slash.manager.choice_for(scope).mode == "cdp"
    ctx = make_ctx(scope)
    browser_cmds.cmd_browser(ctx, "use local")
    assert slash.manager.choice_for(scope) == Choice("local", None, "use-local")
    browser_cmds.cmd_browser(ctx, "disconnect")
    assert "configured endpoint applies again" in ctx.text
    assert slash.manager.choice_for(scope).mode == "cdp"


def test_browser_usage_and_nothing_to_disconnect(slash: Rig, scope: Scope) -> None:
    ctx = make_ctx(scope)
    browser_cmds.cmd_browser(ctx, "launch now")
    browser_cmds.cmd_browser(ctx, "disconnect")
    browser_cmds.cmd_browser(ctx, "connect ftp://x:1")
    assert ctx.lines[0] == browser_cmds.USAGE
    assert "nothing to undo" in ctx.lines[1]
    assert "Not a CDP address" in ctx.lines[2]


def test_registry_entry_lists_the_subcommands() -> None:
    from indus.service.commands.registry import REGISTRY

    entry = REGISTRY.resolve("browser")
    assert entry is not None and entry.subcommands == ("connect", "disconnect", "status", "use")
    assert browser_pkg.TOOLSET == "browser"
    assert cdp_mod.DIALOG_POLICIES == ("must_respond", "auto_dismiss", "auto_accept")
