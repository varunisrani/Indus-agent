"""P24-25: the desktop-only preview tools route to `preview.act` / `preview.read`."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from indus.core.untrusted import is_untrusted
from indus.foundation.scope import Scope
from indus.tools.builtin import preview
from indus.tools.builtin.preview import (
    PreviewDeclined,
    PreviewUndelivered,
    annotate_preview,
    drive_preview,
    register_tools,
)
from indus.tools.registry import ToolRegistry


class FakePort:
    def __init__(self, answer: Mapping[str, Any] | BaseException | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any], float]] = []
        self.answer = answer if answer is not None else {"ok": True}

    async def request(
        self, kind: str, payload: Mapping[str, Any], *, timeout: float
    ) -> Mapping[str, Any]:
        self.calls.append((kind, dict(payload), timeout))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def run(coro: Any) -> str:
    return asyncio.run(coro)


@pytest.mark.parametrize("surface", [None, "cli", "tui", "web", "acp", "messaging"])
def test_tools_absent_outside_desktop_sessions(surface: str | None) -> None:
    registry = ToolRegistry()
    assert register_tools(registry, surface=surface) is False
    assert registry.get("drive_preview") is None and registry.get("annotate_preview") is None


def test_tools_present_in_a_desktop_session() -> None:
    registry = ToolRegistry()
    assert register_tools(registry, surface="gui")
    assert sorted(registry.names_in_toolset(preview.PREVIEW_TOOLSET)) == [
        "annotate_preview",
        "drive_preview",
    ]


def test_actions_route_to_act_and_snapshot_to_read() -> None:
    port = FakePort({"ok": True, "detail": "done"})
    out = run(drive_preview({"action": "click", "selector": "button#go"}, preview=port))
    assert json.loads(out) == {"ok": True, "detail": "done"}
    run(
        drive_preview(
            {"action": "type", "selector": "#q", "text": "tea", "submit": True}, preview=port
        )
    )
    run(drive_preview({"action": "scroll", "dy": 400}, preview=port))
    run(drive_preview({"action": "navigate", "url": "http://localhost:5173/"}, preview=port))
    run(drive_preview({"action": "snapshot"}, preview=port))
    kinds = [(k, p) for k, p, _t in port.calls]
    assert kinds == [
        ("preview.act", {"action": "click", "selector": "button#go"}),
        ("preview.act", {"action": "type", "selector": "#q", "text": "tea", "submit": True}),
        ("preview.act", {"action": "scroll", "dx": 0, "dy": 400}),
        ("preview.act", {"action": "navigate", "url": "http://localhost:5173/"}),
        ("preview.read", {"what": "snapshot"}),
    ]
    assert all(t == preview.PREVIEW_TIMEOUT_S for _k, _p, t in port.calls)


def test_not_shown_and_undelivered_are_clear_errors() -> None:
    out = run(drive_preview({"action": "snapshot"}, preview=FakePort(PreviewDeclined())))
    assert "not open in a desktop window" in out
    out = run(drive_preview({"action": "snapshot"}, preview=FakePort(PreviewUndelivered())))
    assert "No desktop window is attached" in out
    out = run(drive_preview({"action": "snapshot"}, preview=FakePort(TimeoutError())))
    assert "did not answer within 30 s" in out
    assert "no desktop preview" in run(drive_preview({"action": "snapshot"}))


def test_arguments_are_checked_before_anything_is_sent() -> None:
    port = FakePort()
    bad = [
        {"action": "explode"},
        {"action": "navigate", "url": "javascript:alert(1)"},
        {"action": "navigate", "url": "data:text/html,hi"},
        {"action": "click"},
        {"action": "type", "selector": "#a", "text": "x" * 5_000},
        {"action": "scroll", "dy": "far"},
    ]
    for args in bad:
        assert "error" in json.loads(run(drive_preview(args, preview=port)))
    assert port.calls == []


def test_a_page_refusal_is_an_error() -> None:
    refusing = FakePort({"ok": False, "error": "no element matches #x"})
    out = run(drive_preview({"action": "click", "selector": "#x"}, preview=refusing))
    assert "no element matches #x" in out


def test_annotate_sends_pins() -> None:
    port = FakePort()
    run(annotate_preview({"pins": [{"selector": "h1", "note": "typo here"}]}, preview=port))
    assert port.calls[0][:2] == (
        "preview.act",
        {"action": "annotate", "pins": [{"selector": "h1", "note": "typo here"}]},
    )
    for args in ({"pins": []}, {"pins": [{"selector": "h1"}]}, {"pins": ["h1"]}):
        assert "error" in run(annotate_preview(args, preview=port))
    too_many = {"pins": [{"selector": "p", "note": "n"}] * 21}
    assert "at most 20" in run(annotate_preview(too_many, preview=port))
    assert len(port.calls) == 1


def test_local_files_follow_the_read_guards(scope: Scope, tmp_path: Path) -> None:
    """B-PREVIEW-1 (SEC-13): ``file:///`` addresses pass the file tools' read guards."""
    store = scope.home / "auth.json"
    store.write_text("{}", encoding="utf-8")
    page = tmp_path / "index.html"
    page.write_text("<p>ok</p>", encoding="utf-8")
    port = FakePort({"ok": True})
    refused = [store.as_uri(), "file:///dev/random", "file://localhost/etc/hosts"]
    for url in refused:
        out = run(drive_preview({"action": "navigate", "url": url}, preview=port, scope=scope))
        assert "error" in json.loads(out), url
    assert port.calls == []
    ok = run(drive_preview({"action": "navigate", "url": page.as_uri()}, preview=port, scope=scope))
    assert "error" not in json.loads(ok)
    assert preview.file_refusal("http://localhost:5173/.env", scope) is None
    shows = FakePort({"ok": True, "url": store.as_uri(), "text": "{}"})
    out = json.loads(run(drive_preview({"action": "snapshot"}, preview=shows, scope=scope)))
    assert "error" in out and "text" not in out


def test_what_the_page_says_is_outside_text() -> None:
    """SEC-12: the page's text reaches the model fenced (``core.untrusted``)."""
    assert is_untrusted("drive_preview")
