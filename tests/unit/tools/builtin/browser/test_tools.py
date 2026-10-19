"""The browser page tools over the fake driver (P10-13)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.safety import url_policy
from indus.tools.builtin.browser import TOOL_NAMES
from indus.tools.builtin.browser import tools as tools_mod
from indus.tools.builtin.browser.tools import js_url_literals, parse_ref
from indus.tools.calls import ToolCall, ToolContext
from indus.tools.dispatch import dispatch
from indus.tools.registry import ToolRegistry
from tests.fakes.aux_model import FakeAuxModel
from tests.fakes.browser import FakeSite, el
from tests.unit.tools.builtin.browser._rig import Rig, make_rig, make_scope_for

HOME_URL = "https://example.com/"
FORM_URL = "https://example.com/form"


@pytest.fixture(autouse=True)
def _fresh_blocklist() -> Any:
    url_policy.clear_blocklist_cache()
    yield
    url_policy.clear_blocklist_cache()


def sites() -> dict[str, FakeSite]:
    return {
        HOME_URL: FakeSite(
            title="Example home",
            elements=[
                el("heading", "Welcome"),
                el("link", "Form page", goes_to=FORM_URL, depth=1),
                el("link", "Admin", goes_to="http://10.0.0.5/admin", depth=1),
                el("text", "Some words about the site."),
            ],
            images=[
                {"src": "https://example.com/cat.png", "alt": "a cat", "width": 640, "height": 480},
                {"src": "data:image/png;base64,AAAA", "alt": "inline", "width": 1, "height": 1},
            ],
            console=[("log", "page ready"), ("error", "bad thing")],
            errors=["TypeError: x is undefined"],
        ),
        FORM_URL: FakeSite(
            title="A form",
            elements=[el("textbox", "Name"), el("button", "Send"), el("checkbox", "Agree")],
        ),
        "https://example.com/go": FakeSite(redirect_to="http://10.0.0.5/inside"),
        "http://10.0.0.5/inside": FakeSite(title="Router admin"),
        "https://example.com/wall": FakeSite(title="Just a moment..."),
        "https://example.com/alert": FakeSite(title="Alerts", dialog_on_load=("alert", "hello")),
    }


class Session:
    """One event loop for a run of calls (the fake page lives on it)."""

    def __init__(self, rig: Rig, scope: Scope, extras: dict[str, Any] | None = None) -> None:
        self.rig, self.scope = rig, scope
        self.registry = ToolRegistry()
        rig.tools.register(self.registry)
        self.loop = asyncio.new_event_loop()
        self.extras = extras or {}

    def __call__(self, name: str, task: str = "t", **args: Any) -> dict[str, Any]:
        ctx = ToolContext(scope=self.scope, task_id=task, extras=self.extras)
        result = self.loop.run_until_complete(
            dispatch(ToolCall(name, args), ctx, registry=self.registry)
        )
        assert isinstance(result.content, str), result.content
        return json.loads(result.content)

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
def run(rig: Rig, scope: Scope) -> Any:
    session = Session(rig, scope)
    yield session
    session.close()


def page(rig: Rig) -> Any:
    return rig.driver.handles[-1].page


# -- refs and small helpers -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("e5", "e5"), ("@e5", "e5"), ("[e5]", "e5"), ("ref=e12", "e12"), (" @e1 ", "e1")],
)
def test_parse_ref_accepts_the_usual_spellings(raw: str, expected: str) -> None:
    assert parse_ref(raw) == expected


@pytest.mark.parametrize("raw", ["", "5", "button", "e", "@x5", None, 5, "e5; drop"])
def test_parse_ref_refuses_other_text(raw: object) -> None:
    assert parse_ref(raw) is None


def test_url_literals_in_code() -> None:
    code = "fetch('http://10.0.0.1/x'); location.href = \"https://example.com/a?b=1\";"
    assert js_url_literals(code) == ["http://10.0.0.1/x", "https://example.com/a?b=1"]


# -- navigate -------------------------------------------------------------------------------------


def test_metadata_address_is_refused_before_any_browser_starts(run: Session, rig: Rig) -> None:
    body = run("browser_navigate", url="http://169.254.169.254/latest/meta-data/")
    assert body["success"] is False
    assert body["blocked"] == "metadata"
    assert rig.driver.launches == []


def test_url_with_a_token_is_refused_before_fetch(run: Session, rig: Rig) -> None:
    body = run("browser_navigate", url="https://example.com/cb?token=abcdef123456")
    assert body["success"] is False and body["blocked"] == "secret"
    assert rig.driver.launches == []


def test_private_address_is_refused_by_default(run: Session, rig: Rig) -> None:
    body = run("browser_navigate", url="http://10.0.0.5/admin")
    assert body["success"] is False and body["blocked"] == "private"


def test_redirect_to_a_private_address_is_blocked_after_the_redirect(
    run: Session, rig: Rig
) -> None:
    body = run("browser_navigate", url="https://example.com/go")
    assert body["success"] is False
    assert "redirected to" in body["error"] and "10.0.0.5" in body["error"]
    assert page(rig).url() == "about:blank"
    assert "snapshot" not in body


def test_navigate_returns_title_and_a_compact_outline(run: Session, rig: Rig) -> None:
    body = run("browser_navigate", url=HOME_URL)
    assert body["success"] is True
    assert body["url"] == HOME_URL and body["title"] == "Example home"
    assert body["element_count"] == 2
    assert '- heading "Welcome"' in body["snapshot"]
    assert '  - link "Form page" [ref=e1]' in body["snapshot"]
    assert "Some words" not in body["snapshot"]  # compact view
    goto = [c for c in page(rig).calls if c[0] == "goto"]
    assert goto[0][1][1] == 120  # first navigation of the session
    run("browser_navigate", url=FORM_URL)
    goto = [c for c in page(rig).calls if c[0] == "goto"]
    assert goto[1][1][1] == 60


def test_bot_wall_title_adds_a_warning(run: Session) -> None:
    body = run("browser_navigate", url="https://example.com/wall")
    assert body["success"] is True and "bot" in body["bot_detection_warning"]


def test_dialog_on_load_is_reported_not_a_dead_session(run: Session, rig: Rig) -> None:
    body = run("browser_navigate", url="https://example.com/alert")
    assert body["success"] is False
    assert body["pending_dialogs"][0]["type"] == "alert"
    assert body["pending_dialogs"][0]["message"] == "hello"
    assert rig.manager.get("t") is not None and rig.manager.get("t").suspect is None  # type: ignore[union-attr]


# -- snapshot, click, type -------------------------------------------------------------------


def test_full_snapshot_adds_text(run: Session) -> None:
    run("browser_navigate", url=HOME_URL)
    body = run("browser_snapshot", full=True)
    assert "- text: Some words about the site." in body["snapshot"]


def test_click_resolves_the_ref_to_the_session_attribute(run: Session, rig: Rig) -> None:
    run("browser_navigate", url=HOME_URL)
    body = run("browser_click", ref="@e1")
    assert body == {"success": True, "clicked": "@e1"}
    attr = rig.manager.get("t").ref_attr  # type: ignore[union-attr]
    assert ("click", f'[{attr}="e1"]') in page(rig).calls
    assert page(rig).url() == FORM_URL


def test_unknown_or_bad_ref_is_an_error(run: Session) -> None:
    run("browser_navigate", url=HOME_URL)
    missing = run("browser_click", ref="e9")
    assert missing["success"] is False and "new snapshot" in missing["error"]
    bad = run("browser_click", ref="the blue button")
    assert bad["success"] is False and "@e5" in bad["error"]


def test_click_onto_a_private_address_is_blocked_and_blanked(run: Session, rig: Rig) -> None:
    run("browser_navigate", url=HOME_URL)
    body = run("browser_click", ref="e2")
    assert body["success"] is False and "10.0.0.5" in body["error"]
    assert page(rig).url() == "about:blank"


def test_type_fills_the_field_and_redacts_what_it_shows(run: Session, rig: Rig) -> None:
    run("browser_navigate", url=FORM_URL)
    body = run("browser_type", ref="e1", text="Ada")
    assert body == {"success": True, "typed": "Ada", "element": "@e1"}
    secret = "sk-" + "a" * 32
    shown = run("browser_type", ref="e1", text=secret)
    assert secret not in json.dumps(shown)
    fills = [c for c in page(rig).calls if c[0] == "fill"]
    assert fills[-1][1][1] == secret  # the page got the real text
    outline = run("browser_snapshot")
    assert secret not in outline["snapshot"]


def test_scroll_back_and_press(run: Session, rig: Rig) -> None:
    run("browser_navigate", url=HOME_URL)
    assert run("browser_scroll", direction="down") == {"success": True, "scrolled": "down"}
    assert run("browser_scroll", direction="up")["success"] is True
    assert page(rig).scrolled == 0
    assert run("browser_scroll", direction="left")["success"] is False
    run("browser_navigate", url=FORM_URL)
    back = run("browser_back")
    assert back == {"success": True, "url": HOME_URL}
    assert run("browser_press", key="Enter") == {"success": True, "pressed": "Enter"}
    assert run("browser_press", key="")["success"] is False


def test_key_press_landing_on_a_private_address_is_blocked(run: Session, rig: Rig) -> None:
    rig.driver.key_targets["Enter"] = "http://192.168.1.1/"
    run("browser_navigate", url=HOME_URL)
    body = run("browser_press", key="Enter")
    assert body["success"] is False and "192.168.1.1" in body["error"]


def test_private_pages_are_fine_when_the_user_allows_them(tmp_path: Path) -> None:
    rig = make_rig(sites())
    scope = make_scope_for(tmp_path, rig.clock, {"security": {"allow_private_urls": True}})
    run = Session(rig, scope)
    try:
        body = run("browser_navigate", url="https://example.com/go")
        assert body["success"] is True and body["url"] == "http://10.0.0.5/inside"
        metadata = run("browser_navigate", url="http://169.254.169.254/")
        assert metadata["success"] is False  # the metadata floor stays
    finally:
        run.close()


# -- images and console ------------------------------------------------------------------------


def test_images_leave_out_inline_data(run: Session) -> None:
    run("browser_navigate", url=HOME_URL)
    body = run("browser_get_images")
    assert body["count"] == 1
    assert body["images"] == [
        {"src": "https://example.com/cat.png", "alt": "a cat", "width": 640, "height": 480}
    ]


def test_console_reads_and_clears(run: Session) -> None:
    run("browser_navigate", url=HOME_URL)
    body = run("browser_console")
    assert body["console_messages"] == [
        {"type": "log", "text": "page ready"},
        {"type": "error", "text": "bad thing"},
    ]
    assert body["js_errors"] == [{"message": "TypeError: x is undefined"}]
    assert body["total_messages"] == 2 and body["total_errors"] == 1
    run("browser_console", clear=True)
    empty = run("browser_console")
    assert empty["total_messages"] == 0 and empty["total_errors"] == 0


def test_console_expression_is_evaluated(run: Session, rig: Rig) -> None:
    rig.driver.eval_answers["document.title"] = "Example home"
    rig.driver.eval_answers["JSON.stringify({a: 1})"] = '{"a": 1}'
    run("browser_navigate", url=HOME_URL)
    body = run("browser_console", expression="document.title")
    assert body == {"success": True, "result": "Example home", "result_type": "str"}
    parsed = run("browser_console", expression="JSON.stringify({a: 1})")
    assert parsed["result"] == {"a": 1} and parsed["result_type"] == "dict"


def test_eval_policy_refuses_code_that_reaches_a_refused_address(run: Session, rig: Rig) -> None:
    run("browser_navigate", url=HOME_URL)
    body = run("browser_console", expression="fetch('http://169.254.169.254/latest')")
    assert body["success"] is False and "169.254.169.254" in body["error"]
    assert page(rig).evaluated == []


def test_eval_that_moves_the_page_to_a_private_address_is_withheld(run: Session, rig: Rig) -> None:
    async def move(p: Any) -> str:
        p.current = "http://10.1.2.3/"
        return "secret page text"

    expression = "location.assign(atob('aHR0cDovLzEwLjEuMi4zLw=='))"
    rig.driver.eval_answers[expression] = move
    run("browser_navigate", url=HOME_URL)
    body = run("browser_console", expression=expression)
    assert body["success"] is False and "secret page text" not in json.dumps(body)


# -- long outlines ---------------------------------------------------------------------------


def _big_site() -> FakeSite:
    return FakeSite(
        title="Big",
        elements=[el("link", f"Item number {i} with a fairly long label text") for i in range(400)],
    )


def test_big_snapshot_is_truncated_with_a_saved_path(tmp_path: Path) -> None:
    rig = make_rig({"https://example.com/big": _big_site()})
    scope = make_scope_for(tmp_path, rig.clock)
    run = Session(rig, scope)
    try:
        run("browser_navigate", url="https://example.com/big")
        body = run("browser_snapshot")
    finally:
        run.close()
    assert body["truncated"] is True
    assert len(body["snapshot"]) <= 15_000
    saved = Path(body["full_snapshot_path"])
    assert saved.is_file() and saved.parent == scope.home / "cache" / "web"
    assert len(saved.read_text()) > 20_000
    assert f'read_file path="{saved}"' in body["snapshot"]
    assert body["element_count"] == 400


def test_threshold_setting_and_floor(tmp_path: Path) -> None:
    rig = make_rig({"https://example.com/big": _big_site()})
    scope = make_scope_for(tmp_path, rig.clock, {"browser": {"snapshot_threshold": 10}})
    run = Session(rig, scope)
    try:
        run("browser_navigate", url="https://example.com/big")
        body = run("browser_snapshot")
    finally:
        run.close()
    assert len(body["snapshot"]) <= 1_000


def test_big_snapshot_can_be_summarised_by_the_auxiliary_model(tmp_path: Path) -> None:
    rig = make_rig({"https://example.com/big": _big_site()})
    scope = make_scope_for(tmp_path, rig.clock, {"browser": {"snapshot_summary": True}})
    aux = FakeAuxModel(default="- link list of 400 items [ref=e1] … [ref=e400]")
    run = Session(rig, scope, extras={"aux": aux})
    try:
        run("browser_navigate", url="https://example.com/big")
        body = run("browser_snapshot")
    finally:
        run.close()
    assert body["summarised"] is True
    assert body["snapshot"].startswith("- link list of 400 items")
    assert Path(body["full_snapshot_path"]).is_file()
    assert aux.calls[-1].task == "browser_snapshot"


# -- registration -----------------------------------------------------------------------------


def test_all_browser_tools_register_into_the_browser_toolset(rig: Rig) -> None:
    registry = ToolRegistry()
    rig.tools.register(registry)
    assert sorted(registry.names_in_toolset("browser")) == sorted(TOOL_NAMES)
    for name in TOOL_NAMES:
        entry = registry.get(name)
        assert entry is not None and not entry.traits.read_only  # barriers


def test_toolset_is_unavailable_without_playwright(
    rig: Rig, scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("indus.tools.builtin.browser.playwright_available", lambda: False)
    registry = ToolRegistry()
    rig.tools.register(registry)
    entry = registry.get("browser_navigate")
    assert entry is not None and registry.available(entry, scope) is False


def test_tools_need_a_scope(rig: Rig) -> None:
    registry = ToolRegistry()
    rig.tools.register(registry)
    result = asyncio.run(
        dispatch(ToolCall("browser_snapshot", {}), ToolContext(task_id="t"), registry=registry)
    )
    assert "profile scope" in str(result.content)


def test_failure_text_is_redacted(run: Session, rig: Rig) -> None:
    secret = "sk-" + "b" * 30

    async def boom(_p: Any) -> None:
        raise RuntimeError(f"failed with {secret}")

    rig.driver.eval_answers["boom()"] = boom
    run("browser_navigate", url=HOME_URL)
    body = run("browser_console", expression="boom()")
    assert body["success"] is False and secret not in body["error"]


def test_scroll_step_constant() -> None:
    assert tools_mod.SCROLL_STEP_PX == 500
