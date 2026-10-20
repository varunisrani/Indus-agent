"""Protocol models: JSON round trips, strictness, envelope rules and params validation."""

from __future__ import annotations

import inspect
import json
from enum import Enum
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from indus.protocol import attach, bridge, envelope, events, methods, server_requests
from indus.protocol.base import ProtocolModel
from indus.protocol.envelope import (
    ErrorResponse,
    FrameError,
    Notification,
    Request,
    SuccessResponse,
    parse_frame,
)
from indus.protocol.errors import INVALID_PARAMS, METHOD_NOT_FOUND, TEXT_EMPTY, UNKNOWN_PARAM_KEY
from indus.protocol.events import EVENTS, Event, ExitReason, TurnDone
from indus.protocol.methods import (
    METHODS,
    PromptSubmitParams,
    ProtocolError,
    ServerRequestAnswerParams,
    negotiate_features,
    validate_params,
)
from indus.protocol.server_requests import (
    SERVER_REQUEST_KINDS,
    ApprovalChoice,
    ApprovalRequest,
    ClarifyRequest,
    SecretRequest,
    ServerRequest,
)
from tests.unit.protocol._samples import SAMPLES

MODULES = (envelope, events, methods, server_requests, bridge, attach)
ABSTRACT = {events.EventBase, server_requests.ServerRequestBase}


def _concrete_models() -> set[type[ProtocolModel]]:
    found: set[type[ProtocolModel]] = set()
    for module in MODULES:
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, ProtocolModel) and obj is not ProtocolModel and obj not in ABSTRACT:
                found.add(obj)
    return found


def _ids(models: list[ProtocolModel]) -> list[str]:
    return [f"{type(m).__name__}-{i}" for i, m in enumerate(models)]


def test_every_model_has_a_sample() -> None:
    assert _concrete_models() <= {type(m) for m in SAMPLES}


@pytest.mark.parametrize("sample", SAMPLES, ids=_ids(SAMPLES))
def test_round_trip_through_json(sample: ProtocolModel) -> None:
    text = json.dumps(sample.to_wire())
    again = type(sample).model_validate_json(text)
    assert again == sample
    assert json.dumps(again.to_wire()) == text


@pytest.mark.parametrize("sample", SAMPLES, ids=_ids(SAMPLES))
def test_unknown_field_is_refused(sample: ProtocolModel) -> None:
    with pytest.raises(ValidationError, match="extra_forbidden|Extra inputs"):
        type(sample).model_validate({**sample.to_wire(), "surprise": 1})


def test_unknown_nested_field_is_refused() -> None:
    wire: dict[str, Any] = {
        "request_id": "r",
        "session_id": "s",
        "questions": [{"id": "q", "text": "?", "colour": "red"}],
    }
    with pytest.raises(ValidationError, match="questions.0.colour"):
        ClarifyRequest.model_validate(wire)
    answer = {"session_id": "s", "request_id": "r", "answer": {"kind": "sudo", "pin": "1"}}
    with pytest.raises(ValidationError):
        ServerRequestAnswerParams.model_validate(answer)


def test_every_model_enum_and_field_is_documented() -> None:
    for model in _concrete_models() | ABSTRACT:
        assert model.__doc__ and model.__doc__.strip(), model.__name__
        assert model.model_json_schema().get("description"), model.__name__
        for name, field in model.model_fields.items():
            assert field.description, f"{model.__name__}.{name}"
    for module in MODULES:
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, Enum) and obj.__module__ == module.__name__:
                assert obj.__doc__ and obj.__doc__.strip(), obj.__name__


# --- envelope ------------------------------------------------------------------------------------


@pytest.mark.parametrize("good", [0, 42, "abc"])
def test_request_id_accepts_strings_and_integers(good: object) -> None:
    assert Request.model_validate({"jsonrpc": "2.0", "id": good, "method": "m"}).id == good


@pytest.mark.parametrize("bad", [True, 1.5, None, [1]])
def test_request_id_types(bad: object) -> None:
    with pytest.raises(ValidationError):
        Request.model_validate({"jsonrpc": "2.0", "id": bad, "method": "m"})


def test_jsonrpc_version_must_be_2_0() -> None:
    with pytest.raises(ValidationError):
        Request.model_validate({"jsonrpc": "1.0", "id": 1, "method": "m"})
    with pytest.raises(FrameError, match="2.0"):
        parse_frame({"id": 1, "method": "m"})
    assert Request(id=1, method="m").to_wire()["jsonrpc"] == "2.0"


def test_absent_params_are_omitted_not_null() -> None:
    assert "params" not in Request(id=1, method="m").to_wire()
    assert "params" not in Notification(method="m").to_wire()


def test_success_response_keeps_null_result() -> None:
    assert SuccessResponse(id=1, result=None).to_wire() == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": None,
    }


def test_error_response_keeps_null_id_and_drops_absent_data() -> None:
    wire = ErrorResponse.model_validate(
        {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "x"}}
    ).to_wire()
    assert wire == {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "x"}}


def test_response_needs_exactly_one_of_result_or_error() -> None:
    both = {"jsonrpc": "2.0", "id": 1, "result": 1, "error": {"code": 1, "message": "m"}}
    with pytest.raises(FrameError):
        parse_frame(both)
    with pytest.raises(FrameError):
        parse_frame({"jsonrpc": "2.0", "id": 1})


@pytest.mark.parametrize(
    ("frame", "kind"),
    [
        ({"jsonrpc": "2.0", "id": 1, "method": "m", "params": {}}, Request),
        ({"jsonrpc": "2.0", "method": "turn.done", "params": {"a": 1}}, Notification),
        ({"jsonrpc": "2.0", "id": 1, "result": [1, 2]}, SuccessResponse),
        ({"jsonrpc": "2.0", "id": "x", "error": {"code": 1, "message": "m"}}, ErrorResponse),
    ],
)
def test_parse_frame_classifies(frame: dict[str, Any], kind: type[BaseModel]) -> None:
    assert type(parse_frame(frame)) is kind


@pytest.mark.parametrize(
    "bad",
    [
        [],
        "text",
        {"jsonrpc": "2.0", "id": 1, "method": "m", "params": [1, 2]},
        {"jsonrpc": "2.0", "id": 1, "method": "", "params": {}},
        {"jsonrpc": "2.0", "id": 1, "method": "m", "extra": True},
    ],
)
def test_parse_frame_refuses_invalid_frames(bad: object) -> None:
    with pytest.raises(FrameError):
        parse_frame(bad)


# --- methods -------------------------------------------------------------------------------------


def test_catalogue_names() -> None:
    assert list(METHODS) == [
        "session.create",
        "session.resume",
        "prompt.submit",
        "session.interrupt",
        "session.steer",
        "session.redirect",
        "server_request.answer",
        "client.capabilities",
        "session.list",
        "session.most_recent",
        "session.close",
        "session.history",
        "session.title",
        "session.undo",
        "session.compress",
        "session.branch",
        "session.usage",
        "prompt.background",
        "clarify.lock",
        "server_request.decline",
        "gateway.ping",
        "complete.slash",
        "complete.path",
        "commands.catalog",
        "slash.exec",
        "command.dispatch",
        "shell.exec",
        "config.get",
        "config.set",
        "model.options",
        "model.save_key",
        "model.disconnect",
        "reload.mcp",
        "paste.collapse",
        "image.attach",
        "input.detect_drop",
        "session.subscribe",
        "session.unsubscribe",
        "sessions.watch",
        "events.since",
        "admission.discard",
        "admission.receipt",
        "runtime.describe",
        "maintenance.run",
    ]
    for spec in METHODS.values():
        assert spec.summary


def test_validate_params_returns_the_model() -> None:
    model = validate_params("prompt.submit", {"session_id": "s", "text": "hi"})
    assert model == PromptSubmitParams(session_id="s", text="hi")
    assert validate_params("client.capabilities", None).to_wire() == {"features": []}


def test_validate_params_unknown_method() -> None:
    with pytest.raises(ProtocolError) as info:
        validate_params("session.explode", {})
    assert info.value.error is METHOD_NOT_FOUND


def test_validate_params_unknown_key_names_its_path() -> None:
    with pytest.raises(ProtocolError) as info:
        validate_params("session.create", {"titel": "typo"})
    assert info.value.error is UNKNOWN_PARAM_KEY
    assert info.value.path == "titel"
    with pytest.raises(ProtocolError) as info:
        validate_params("session.list", {"limit": "many", "colour": 1})
    assert info.value.error is UNKNOWN_PARAM_KEY, "an unknown key wins over other errors"
    assert info.value.path == "colour"
    with pytest.raises(ProtocolError) as info:
        validate_params(
            "server_request.answer",
            {
                "session_id": "s",
                "request_id": "r",
                "answer": {"kind": "approval", "choice": "maybe"},
            },
        )
    assert info.value.error is INVALID_PARAMS
    assert info.value.path is not None and info.value.path.startswith("answer.approval.choice")


@pytest.mark.parametrize("method", ["prompt.submit", "session.steer", "session.redirect"])
@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_blank_text_is_its_own_error(method: str, text: str) -> None:
    with pytest.raises(ProtocolError) as info:
        validate_params(method, {"session_id": "s", "text": text})
    assert info.value.error is TEXT_EMPTY
    assert info.value.path == "text"


def test_capabilities_drop_unknown_features() -> None:
    assert negotiate_features(["anything", "else"]) == []
    wanted = ["server_requests.secret", "server_requests", "nope"]
    assert negotiate_features(wanted) == ["server_requests", "server_requests.secret"]


# --- events --------------------------------------------------------------------------------------


def test_exit_reason_is_the_closed_set() -> None:
    assert {r.value for r in ExitReason} == {
        "completed",
        "interrupted_by_user",
        "interrupted_by_system",
        "budget_exhausted",
        "max_iterations",
        "invalid_tool_strikes",
        "persistence_failed",
        "provider_failed",
        "compression_exhausted",
        "guardrail_halt",
    }


@pytest.mark.parametrize("reason", list(ExitReason))
def test_turn_done_reason_is_exit_reason(reason: ExitReason) -> None:
    done = TurnDone.model_validate(
        {"session_id": "s", "seq": 1, "turn_id": "t", "reason": reason.value}
    )
    assert done.reason is reason


def test_turn_done_refuses_other_reasons() -> None:
    with pytest.raises(ValidationError):
        TurnDone.model_validate({"session_id": "s", "seq": 1, "turn_id": "t", "reason": "bored"})


def test_event_union_dispatches_on_type() -> None:
    adapter: TypeAdapter[Event] = TypeAdapter(Event)
    for name, model in EVENTS.items():
        sample = next(s for s in SAMPLES if type(s) is model)
        parsed = adapter.validate_python(sample.to_wire())
        assert type(parsed) is model
        assert parsed.type == name
    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "turn.paused", "session_id": "s", "seq": 0})


def test_event_seq_is_never_negative() -> None:
    with pytest.raises(ValidationError):
        TurnDone(session_id="s", seq=-1, turn_id="t", reason=ExitReason.COMPLETED)


# --- server requests -----------------------------------------------------------------------------


def test_server_request_union_and_kind_table() -> None:
    adapter: TypeAdapter[ServerRequest] = TypeAdapter(ServerRequest)
    assert set(SERVER_REQUEST_KINDS) == {"approval", "clarify", "sudo", "secret"}
    for kind, (request, answer) in SERVER_REQUEST_KINDS.items():
        sample = next(s for s in SAMPLES if type(s) is request)
        assert type(adapter.validate_python(sample.to_wire())) is request
        assert request.model_fields["kind"].default == kind
        assert answer.model_fields["kind"].default == kind


def test_approval_must_offer_deny_once() -> None:
    base = {"request_id": "r", "session_id": "s", "command": "c", "description": "d"}
    assert ApprovalRequest.model_validate(base).choices == list(ApprovalChoice)
    with pytest.raises(ValidationError, match="deny"):
        ApprovalRequest.model_validate({**base, "choices": ["once"]})
    with pytest.raises(ValidationError, match="repeat"):
        ApprovalRequest.model_validate({**base, "choices": ["deny", "deny"]})


def test_clarify_question_ids_are_unique() -> None:
    questions = [{"id": "q", "text": "a"}, {"id": "q", "text": "b"}]
    with pytest.raises(ValidationError, match="unique"):
        ClarifyRequest.model_validate(
            {"request_id": "r", "session_id": "s", "questions": questions}
        )


@pytest.mark.parametrize("name", ["lower", "1ABC", "A-B", ""])
def test_secret_name_is_an_env_var_name(name: str) -> None:
    with pytest.raises(ValidationError):
        SecretRequest(request_id="r", session_id="s", name=name, prompt="p")


def test_models_are_immutable() -> None:
    model = PromptSubmitParams(session_id="s", text="hi")
    with pytest.raises(ValidationError):
        model.text = "changed"  # type: ignore[misc]
