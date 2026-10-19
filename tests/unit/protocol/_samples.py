"""One valid instance of every protocol model, for round-trip and strictness tests."""

from __future__ import annotations

from datetime import UTC, datetime

from indus.protocol.base import ProtocolModel
from indus.protocol.envelope import (
    ErrorDetail,
    ErrorObject,
    ErrorResponse,
    Notification,
    Request,
    SuccessResponse,
)
from indus.protocol.events import (
    CancelReason,
    ExitReason,
    GoalUpdate,
    HeartbeatFired,
    LearningReviewDone,
    LoopUpdate,
    MessageDelta,
    Notice,
    NoticeLevel,
    ReasoningDelta,
    ServerRequestCancelled,
    Status,
    StatusKind,
    ToolCompleted,
    ToolProgress,
    ToolStarted,
    TurnDone,
    TurnFailure,
    TurnStarted,
)
from indus.protocol.methods import (
    ClientCapabilitiesParams,
    ClientCapabilitiesResult,
    PromptSubmitParams,
    PromptSubmitResult,
    RedirectOutcome,
    ServerRequestAnswerParams,
    ServerRequestAnswerResult,
    SessionCreateParams,
    SessionCreateResult,
    SessionInterruptParams,
    SessionInterruptResult,
    SessionRedirectParams,
    SessionRedirectResult,
    SessionResumeParams,
    SessionResumeResult,
    SessionSteerParams,
    SessionSteerResult,
)
from indus.protocol.server_requests import (
    ApprovalAnswer,
    ApprovalChoice,
    ApprovalRequest,
    ClarifyAnswer,
    ClarifyQuestion,
    ClarifyRequest,
    SecretAnswer,
    SecretRequest,
    SudoAnswer,
    SudoRequest,
)
from tests.unit.protocol._bridge_samples import BRIDGE_SAMPLES

SID = "sess-1"
TID = "turn-1"

APPROVAL = ApprovalRequest(
    request_id="rq-1",
    session_id=SID,
    turn_id=TID,
    deadline_s=60.0,
    command="rm -r build/",
    description="deletes a folder",
)
CLARIFY = ClarifyRequest(
    request_id="rq-2",
    session_id=SID,
    questions=[
        ClarifyQuestion(id="q1", text="Which branch?", choices=["main", "dev"]),
        ClarifyQuestion(id="q2", text="Anything else?"),
    ],
)
SUDO = SudoRequest(request_id="rq-3", session_id=SID, reason="install a package")
SECRET = SecretRequest(request_id="rq-4", session_id=SID, name="DEMO_TOKEN", prompt="paste it")

SAMPLES: list[ProtocolModel] = [
    # envelope
    Request(id=7, method="prompt.submit", params={"session_id": SID, "text": "hi"}),
    Request(id="a-1", method="client.capabilities"),
    Notification(method="message.delta", params={"text": "x", "nested": {"k": None}}),
    SuccessResponse(id=7, result={"turn_id": TID, "generation": 1}),
    SuccessResponse(id="a-1", result=None),
    ErrorResponse(id=None, error=ErrorObject(code=-32700, message="bad json")),
    ErrorResponse(id=3, error=ErrorObject(code=2001, message="gone", data={"name": "X"})),
    ErrorDetail(
        name="INVALID_PARAMS", path="params.text", indus_code="store.locked", retryable=True
    ),
    ErrorObject(code=-32603, message="crash", data=[1, "two", None]),
    # methods
    SessionCreateParams(),
    SessionCreateParams(title="t", cwd="/w", model="m", toolsets=["core"], idempotency_key="k1"),
    SessionCreateResult(session_id=SID, created=True),
    SessionResumeParams(session_id=SID),
    SessionResumeResult(
        session_id=SID, message_count=4, last_seq=9, pending_requests=[APPROVAL, CLARIFY]
    ),
    PromptSubmitParams(session_id=SID, text="hello"),
    PromptSubmitResult(turn_id=TID, generation=2),
    SessionInterruptParams(session_id=SID, generation=2, hard=True),
    SessionInterruptResult(interrupted=False),
    SessionSteerParams(session_id=SID, text="also check tests"),
    SessionSteerResult(queued=True, ticket="st-1"),
    SessionRedirectParams(session_id=SID, text="no, the other file"),
    SessionRedirectResult(outcome=RedirectOutcome.STEERED),
    ServerRequestAnswerParams(
        session_id=SID, request_id="rq-1", answer=ApprovalAnswer(choice=ApprovalChoice.ONCE)
    ),
    ServerRequestAnswerParams(
        session_id=SID, request_id="rq-2", answer=ClarifyAnswer(answers={"q1": ["dev"], "q2": []})
    ),
    ServerRequestAnswerResult(accepted=True),
    ClientCapabilitiesParams(features=["future.thing"]),
    ClientCapabilitiesResult(features=[]),
    # events
    TurnStarted(session_id=SID, seq=0, turn_id=TID, generation=1),
    MessageDelta(session_id=SID, seq=1, turn_id=TID, text="Hel"),
    ReasoningDelta(session_id=SID, seq=2, turn_id=TID, text="think"),
    ToolStarted(session_id=SID, seq=3, turn_id=TID, call_id="c1", name="read", arguments={"p": 1}),
    ToolProgress(session_id=SID, seq=4, turn_id=TID, call_id="c1", message="50%", fraction=0.5),
    ToolCompleted(
        session_id=SID,
        seq=5,
        turn_id=TID,
        call_id="c1",
        name="read",
        ok=True,
        duration_ms=12,
        result="text",
    ),
    Status(session_id=SID, seq=6, kind=StatusKind.RETRYING, text="attempt 2"),
    Notice(session_id=SID, seq=7, level=NoticeLevel.WARNING, text="low disk", key="disk"),
    ServerRequestCancelled(session_id=SID, seq=8, request_id="rq-1", reason=CancelReason.TIMED_OUT),
    TurnDone(session_id=SID, seq=9, turn_id=TID, reason=ExitReason.COMPLETED),
    TurnDone(
        session_id=SID,
        seq=10,
        turn_id=TID,
        reason=ExitReason.PROVIDER_FAILED,
        failure=TurnFailure(
            reason="rate limited", retryable=True, resets_at=datetime(2026, 1, 1, tzinfo=UTC)
        ),
    ),
    TurnFailure(reason="store write failed", retryable=False),
    LearningReviewDone(
        session_id=SID, seq=11, kinds=["memory", "skills"], summary_lines=["Memory updated"]
    ),
    GoalUpdate(
        session_id=SID,
        seq=12,
        status="active",
        goal="make the tests pass",
        turns_used=2,
        max_turns=20,
        reason="two tests still fail",
    ),
    LoopUpdate(
        session_id=SID,
        seq=13,
        loop_id="l1",
        status="active",
        prompt="check the build",
        ticks=3,
        next_due_at=datetime(2026, 1, 1, 9, 5, tzinfo=UTC),
    ),
    HeartbeatFired(
        session_id=SID, seq=14, heartbeat_id="h1", prompt="look at the inbox", fire_count=4
    ),
    # server requests and answers
    APPROVAL,
    CLARIFY,
    SUDO,
    SECRET,
    ApprovalAnswer(choice=ApprovalChoice.DENY),
    ClarifyAnswer(answers={"q1": ["main"]}),
    SudoAnswer(password="pw-" + "example"),
    SudoAnswer(),
    SecretAnswer(value="v-" + "example"),
    SecretAnswer(),
    ClarifyQuestion(id="q", text="?", choices=["a", "b"], multi_select=True),
]

SAMPLES += BRIDGE_SAMPLES
