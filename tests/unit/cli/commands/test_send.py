"""P15-20: ``indus send`` (one-shot adapters, the P14 target grammar, the router)."""

from __future__ import annotations

import io
import json
import os
from collections.abc import Iterator
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.gateway.adapters.base import ChannelEntry
from indus.gateway.config import PlatformSettings
from indus.gateway.delivery import send_command
from indus.gateway.delivery.directory import ChannelDirectory
from indus.state import gateway_routes as routes
from indus.state import registry as store_registry
from indus.state import session_meta
from tests.unit.cli.commands.cmdrun import Ran, run_cli, write_settings
from tests.unit.gateway.outbound.recording import RecordingAdapter, fail, recording_adapter

SETTINGS = """
platforms:
  telegram:
    home_channel:
      chat_id: "-100"
"""


@pytest.fixture
def built(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[RecordingAdapter]]:
    """Telegram enabled (a fake token in the secret store) and adapters from the fake wire."""
    secrets: Any = scope.secrets
    secrets.set("TELEGRAM_BOT_TOKEN", "1234:" + "fake" * 4)
    write_settings(scope, SETTINGS)
    made: list[RecordingAdapter] = []

    def build(_scope: Scope, name: str, settings: PlatformSettings, *_: Any) -> RecordingAdapter:
        assert settings.enabled and settings.name == name
        adapter = recording_adapter(name=name, markup="plain")
        made.append(adapter)
        return adapter

    monkeypatch.setattr(send_command, "ADAPTER_BUILDER", build)
    yield made


def _run(capsys: pytest.CaptureFixture[str], scope: Scope, *args: str) -> Ran:
    return run_cli(capsys, scope, "send", *args)


def test_no_target_is_a_usage_error(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    ran = _run(capsys, scope)
    assert ran.code == 2 and "a target is required" in ran.err


def test_send_to_a_chat(
    capsys: pytest.CaptureFixture[str], scope: Scope, built: list[RecordingAdapter]
) -> None:
    ran = _run(capsys, scope, "telegram:42", "deploy done")
    assert ran.code == 0 and ran.out.strip() == "Sent to telegram:42."
    (adapter,) = built
    assert [(op.chat_id, op.text) for op in adapter.ops] == [("42", "deploy done")]
    assert not adapter.connected  # disconnected after the send


def test_send_to_the_home_channel_with_subject_and_json(
    capsys: pytest.CaptureFixture[str], scope: Scope, built: list[RecordingAdapter]
) -> None:
    ran = _run(capsys, scope, "telegram", "all green", "--subject", "[CI]", "--json")
    assert ran.code == 0
    data = json.loads(ran.out)
    assert data["ok"] is True and data["target"] == "telegram" and data["message_id"] == "m1"
    assert built[0].ops[0].chat_id == "-100" and built[0].ops[0].text == "[CI]\n\nall green"


def test_body_from_stdin(
    capsys: pytest.CaptureFixture[str],
    scope: Scope,
    built: list[RecordingAdapter],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("RAM at 92%\n"))
    ran = _run(capsys, scope, "telegram:42", "-q")
    assert ran.code == 0 and ran.out == ""
    assert built[0].ops[0].text == "RAM at 92%"


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("myspace:1", "not a platform Indus can deliver to"),
        ("discord:1", "discord is not enabled"),
        ("origin", "origin is not a chat"),
        ("local", "local is not a chat"),
        ("telegram:1,slack:2", "exactly one target"),
    ],
)
def test_bad_targets_never_reach_a_platform(
    capsys: pytest.CaptureFixture[str],
    scope: Scope,
    built: list[RecordingAdapter],
    target: str,
    message: str,
) -> None:
    ran = _run(capsys, scope, target, "x")
    assert ran.code == 2 and message in ran.err
    assert built == []


def test_no_text_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
    scope: Scope,
    built: list[RecordingAdapter],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    ran = _run(capsys, scope, "telegram:42")
    assert ran.code == 2 and "no message" in ran.err and built == []


def test_media_tags_in_the_text_are_sent_as_text(
    capsys: pytest.CaptureFixture[str], scope: Scope, built: list[RecordingAdapter]
) -> None:
    image = scope.home / "cache" / "images" / "chart.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"png")
    ran = _run(capsys, scope, "telegram:42", f"look MEDIA:{image}")
    assert ran.code == 0
    assert [op.op for op in built[0].ops] == ["send"] and "MEDIA:" in built[0].ops[0].text


def test_file_must_pass_the_media_policy(
    capsys: pytest.CaptureFixture[str], scope: Scope, built: list[RecordingAdapter]
) -> None:
    home = scope.env.get("HOME") or os.path.expanduser("~")
    secret = os.path.join(home, ".ssh", "id_ed25519")
    refused = _run(capsys, scope, "telegram:42", "key", "--file", secret)
    assert refused.code == 2 and "cannot attach" in refused.err and built == []
    outside = scope.home.parent / "report.pdf"
    outside.write_bytes(b"%PDF")
    refused = _run(capsys, scope, "telegram:42", "r", "--file", str(outside))
    assert refused.code == 2 and "media_allow_dirs" in refused.err and built == []
    doc = scope.home / "cache" / "documents" / "report.pdf"
    doc.parent.mkdir(parents=True)
    doc.write_bytes(b"%PDF")
    ran = _run(capsys, scope, "telegram:42", "the report", "--file", str(doc))
    assert ran.code == 0, ran.err
    assert [(op.op, op.text) for op in built[0].ops] == [
        ("send", "the report"),
        ("document", os.path.realpath(doc)),
    ]


def test_a_refusal_exits_1(
    capsys: pytest.CaptureFixture[str],
    scope: Scope,
    built: list[RecordingAdapter],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing(_scope: Scope, name: str, _settings: PlatformSettings, *_: Any) -> RecordingAdapter:
        adapter = recording_adapter(name=name)
        adapter.send_results.append(fail("forbidden", "Forbidden: bot was kicked"))
        built.append(adapter)
        return adapter

    monkeypatch.setattr(send_command, "ADAPTER_BUILDER", failing)
    ran = _run(capsys, scope, "telegram:42", "hello")
    assert ran.code == 1 and "not sent to telegram:42" in ran.err and "kicked" in ran.err


def test_hash_names_and_list_use_the_saved_directory(
    capsys: pytest.CaptureFixture[str], scope: Scope, built: list[RecordingAdapter]
) -> None:
    directory = ChannelDirectory(scope)
    adapter = recording_adapter(name="telegram")

    async def listed() -> list[ChannelEntry]:
        return [ChannelEntry("-200", "ops room", type="group")]

    adapter.list_channels = listed  # type: ignore[method-assign]
    import asyncio

    asyncio.run(directory.build({"telegram": adapter}))
    ran = _run(capsys, scope, "telegram:#ops room", "hi")
    assert ran.code == 0 and built[0].ops[0].chat_id == "-200"
    listed_out = _run(capsys, scope, "--list")
    assert listed_out.code == 0 and "telegram:ops room  [-200] (group)" in listed_out.out
    as_json = json.loads(_run(capsys, scope, "--list", "telegram", "--json").out)
    assert as_json["platforms"]["telegram"][0]["id"] == "-200"
    assert _run(capsys, scope, "--list", "slack").code == 1


def test_a_send_into_a_routed_chat_leaves_a_note_for_its_conversation(
    capsys: pytest.CaptureFixture[str], scope: Scope, built: list[RecordingAdapter]
) -> None:
    from tests.unit.state._helpers_d import add_session

    store = store_registry.open_unshared(scope)
    try:
        add_session(store, "s-42")
        origin: dict[str, Any] = {"platform": "telegram", "chat_id": "42", "chat_type": "dm"}
        routes.get_or_create(
            store,
            "@default:telegram:dm:42",
            new_session_id=lambda: "s-42",
            platform="telegram",
            chat_type="dm",
            origin=origin,
        )
        assert _run(capsys, scope, "telegram:42", "deploy finished").code == 0
        (note,) = session_meta.list_entries(store, "s-42", "gateway.mirror").values()
        assert "indus send" in note["text"] and "deploy finished" in note["text"]
    finally:
        store.close()
