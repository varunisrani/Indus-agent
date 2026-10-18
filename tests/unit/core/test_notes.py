"""The one-shot note channel (P04-07): notes ride the user message, once."""

from __future__ import annotations

from indus.core.notes import NoteChannel, attach_notes


def test_pop_order_and_one_shot() -> None:
    channel = NoteChannel()
    channel.stage_skills_delta("skills: old")
    channel.stage_surface_switch("surface: tui")
    channel.stage_must_deliver("deliver one")
    channel.stage_must_deliver("   ")
    channel.stage_must_deliver("deliver two")
    channel.stage_skills_delta("skills: new")  # replaces the older delta
    assert channel.pending
    assert channel.pop() == "deliver one\n\ndeliver two\n\nsurface: tui\n\nskills: new"
    assert not channel.pending
    assert channel.pop() == ""


def test_attach_to_text_message_sets_api_content_only() -> None:
    message = {"role": "user", "content": "what changed?"}
    out = attach_notes(message, "note A")
    assert out == {
        "role": "user",
        "content": "what changed?",
        "api_content": "what changed?\n\nnote A",
    }
    assert message == {"role": "user", "content": "what changed?"}  # input untouched


def test_attach_extends_an_existing_sidecar() -> None:
    message = {"role": "user", "content": "hi", "api_content": "hi\n\n[plugin context]"}
    assert attach_notes(message, "note")["api_content"] == "hi\n\n[plugin context]\n\nnote"


def test_attach_to_multimodal_adds_a_text_part() -> None:
    parts = [{"type": "image_url", "image_url": {"url": "data:,"}}]
    out = attach_notes({"role": "user", "content": parts}, "note")
    assert out["content"] == [*parts, {"type": "text", "text": "note"}]
    assert "api_content" not in out and len(parts) == 1


def test_empty_notes_change_nothing() -> None:
    message = {"role": "user", "content": "hi"}
    assert attach_notes(message, "  ") == message
