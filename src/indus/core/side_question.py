"""Side questions: ``/btw <question>`` answered without changing the conversation (P10-23).

Three paths, picked in this order:

1. **Routed** — ``auxiliary.side_question`` names its own provider or model: one auxiliary call
   with the transcript rendered as text (the compact form below) and the question.
2. **Fork** — a live agent whose system prompt is known: a detached copy of its runtime replays
   that prompt, the stored transcript (cut back to the last finished assistant answer) and the very
   same tool array, so the provider's prefix cache is warm; the question is a new user message.
   The copy has no store (nothing is written to the session), no context engine, no fallback chain
   and no event sink; every tool is refused at dispatch by name; at most ``FORK_MAX_ITERATIONS``
   model calls and no grace call after them. Usage goes to the session ledger under
   ``side_question``. It runs beside a turn that is in flight and never touches its control.
3. **Transcript** — no live agent (the process restarted, a host evicted it) or the fork failed:
   the stored
   transcript rendered as text, each message at most ``MESSAGE_CHARS`` and ``TRANSCRIPT_CHARS`` in
   all (newest kept), in one auxiliary call.

The texts that frame the question are our own.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, Literal

from indus.core.agent import Agent
from indus.core.exit_reasons import ExitReason
from indus.core.loop import run_turn
from indus.core.persist import StateStore, TranscriptStore
from indus.core.prompt.builder import BuildContext, PromptParts
from indus.core.prompt.lifecycle import PromptLifecycle, SessionPrompts
from indus.core.transcript import TranscriptRow, call_name
from indus.core.turn_state import SessionRuntime, TurnState, new_turn_state
from indus.core.usage import RecordingCaller
from indus.foundation.errors import IndusError
from indus.providers.auxiliary import AuxClient
from indus.tools.calls import ToolCall, ToolResult

__all__ = [
    "FORK_MAX_ITERATIONS",
    "MESSAGE_CHARS",
    "TASK",
    "TRANSCRIPT_CHARS",
    "SideAnswer",
    "SideQuestionUnavailable",
    "answer_side_question",
    "ask_fork",
    "ask_transcript",
    "render_transcript",
    "trim_to_answer",
]

log = logging.getLogger(__name__)

TASK: Final = "side_question"
FORK_MAX_ITERATIONS: Final = 3
MESSAGE_CHARS: Final = 2_000
TRANSCRIPT_CHARS: Final = 24_000

FORK_INSTRUCTION: Final = (
    "[Side question] The person is asking something on the side while this conversation goes "
    "on. Answer only that question, from the conversation above; do not carry on with the main "
    "task. Tools are switched off for this answer. If the conversation does not tell you, say so "
    "instead of guessing. Keep it short.\n\nQuestion: {question}"
)
TRANSCRIPT_INSTRUCTION: Final = (
    "You are the assistant of the conversation shown below as a transcript (it may leave out "
    "the latest steps). The person asks a side question about it. Answer only that question, "
    "from the transcript; do not continue the work. If the transcript does not tell you, say so "
    "instead of guessing. Keep it short."
)
TOOL_REFUSED: Final = (
    "Tools are switched off while answering a side question; answer from the conversation."
)
NO_ANSWER: Final = "No answer came back for that side question."
OLDER_LEFT_OUT: Final = "[… older messages left out …]"
_LABELS: Final = {"user": "User", "assistant": "Assistant", "tool": "Tool result"}

type Path = Literal["fork", "routed", "transcript"]


class SideQuestionUnavailable(IndusError):
    """No live agent and no auxiliary client: the question cannot be answered here."""

    default_code = "provider.aux_unavailable"


@dataclass(frozen=True)
class SideAnswer:
    text: str
    path: Path


# -- transcripts -----------------------------------------------------------------------------------


def trim_to_answer(rows: Sequence[TranscriptRow]) -> list[TranscriptRow]:
    """``rows`` cut back so the last one is a finished assistant answer (no open tool calls, no
    turn in flight), so the replayed history alternates on every provider."""
    kept = [r for r in rows if not r.scaffold and r.role != "system"]
    while kept and not (kept[-1].role == "assistant" and not kept[-1].tool_calls):
        kept.pop()
    return kept


def _row_text(row: TranscriptRow) -> str:
    content = row.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:  # pyright: ignore[reportUnknownVariableType]
            if isinstance(part, Mapping):
                value = part.get("text")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                if isinstance(value, str):
                    texts.append(value)
        return "\n".join(texts)
    return ""


def render_transcript(
    rows: Sequence[TranscriptRow],
    *,
    message_chars: int = MESSAGE_CHARS,
    budget: int = TRANSCRIPT_CHARS,
) -> str:
    """The transcript as plain lines, newest kept when it does not fit ``budget``."""
    lines: list[str] = []
    for row in rows:
        if row.scaffold or row.role not in _LABELS:
            continue
        if row.role == "assistant" and row.tool_calls:
            names = ", ".join(sorted({str(n) for n in _call_names(row.tool_calls)}))
            lines.append(f"Assistant used tools: {names}")
        text = _row_text(row).strip()
        if text:
            if len(text) > message_chars:
                text = text[:message_chars] + " […]"
            lines.append(f"{_LABELS[row.role]}: {text}")
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        if kept and used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    if not kept:
        return "(the conversation is empty so far)"
    kept.reverse()
    if len(kept) < len(lines):
        kept.insert(0, OLDER_LEFT_OUT)
    return "\n".join(kept)


def _call_names(calls: Sequence[Any]) -> list[str]:
    return [call_name(call) or "tool" for call in calls]


# -- paths -----------------------------------------------------------------------------------------


class _FixedPrompt:
    """A builder that gives back the parent's prompt text unchanged."""

    def __init__(self, text: str) -> None:
        self.text = text

    async def build(self, ctx: BuildContext) -> PromptParts:
        return PromptParts(stable=self.text, context="", volatile="", text=self.text)


async def _refuse(call: ToolCall, state: TurnState) -> ToolResult:
    log.info("side question: refused a %s call", call.name)
    return ToolResult.error(TOOL_REFUSED, error_type="denied")


async def _known_prompt(runtime: SessionRuntime) -> str | None:
    """The system prompt the session's requests carry: held in memory, else the stored one."""
    held = runtime.prompts.prompt
    if held:
        return held
    store = runtime.store
    if isinstance(store, StateStore):
        try:
            stored = await SessionPrompts(store.handle).load(runtime.session_id)
        except Exception:
            log.debug("could not read the stored system prompt", exc_info=True)
            return None
        if stored.state == "present" and stored.text:
            return stored.text
    return None


async def ask_fork(agent: Agent, question: str, *, prompt: str | None = None) -> str:
    """The fork path (see the module notes); ``NO_ANSWER`` when the model gave none."""
    runtime = agent.runtime
    text = prompt if prompt is not None else await _known_prompt(runtime)
    if not text:
        raise SideQuestionUnavailable("the session's system prompt is not known yet")
    store = runtime.store
    rows = await store.load(runtime.session_id) if store is not None else []
    history = trim_to_answer(rows)
    inner = runtime.caller.inner if isinstance(runtime.caller, RecordingCaller) else runtime.caller
    binding = agent.binding
    fork = SessionRuntime(
        session_id=runtime.session_id,
        caller=RecordingCaller(inner, agent.usage, lambda: binding.state.route, task=TASK),
        clock=runtime.clock,
        prompts=PromptLifecycle(_FixedPrompt(text)),
        prompt_context=runtime.prompt_context,
        store=None,
        registry=runtime.registry,
        tools=runtime.tools,
        scope=runtime.scope,
        route=runtime.route,
        options=replace(runtime.options, max_iterations=FORK_MAX_ITERATIONS, grace=False),
        inline={name: _refuse for name in runtime.tool_names()},
        source=runtime.source,
        profile=runtime.profile,
        hydrated=True,
    )
    state = new_turn_state(
        fork,
        message=FORK_INSTRUCTION.format(question=question),
        turn_id=f"btw_{secrets.token_hex(4)}",
        history=history,
    )
    result = await agent.spawn(run_turn(state), name="side question")
    if result.exit_reason is not ExitReason.COMPLETED or not (result.final_text or "").strip():
        log.info("side question fork ended %s without an answer", result.exit_reason)
        return NO_ANSWER
    return (result.final_text or "").strip()


async def ask_transcript(aux: AuxClient, question: str, rows: Sequence[TranscriptRow]) -> str:
    """One auxiliary call over the rendered transcript."""
    transcript = render_transcript(rows)
    reply = await aux.call(
        TASK,
        [
            {"role": "system", "content": TRANSCRIPT_INSTRUCTION},
            {
                "role": "user",
                "content": f"Transcript:\n{transcript}\n\nSide question: {question}",
            },
        ],
    )
    return reply.text.strip() or NO_ANSWER


def _routed(aux: AuxClient | None) -> bool:
    if aux is None:
        return False
    task_config = getattr(aux, "task_config", None)
    if task_config is None:
        return False
    pin: Mapping[str, Any] = task_config(TASK)
    provider = str(pin.get("provider") or "").strip().lower()
    return (provider not in ("", "auto")) or bool(str(pin.get("model") or "").strip())


async def answer_side_question(
    question: str,
    *,
    session_id: str,
    agent: Agent | None = None,
    store: TranscriptStore | None = None,
    aux: AuxClient | None = None,
) -> SideAnswer:
    """Answer ``question`` by the first path that applies (see the module notes)."""
    question = question.strip()
    if not question:
        raise ValueError("a side question needs some text")
    live = agent is not None and not agent.closed
    aux = aux if aux is not None else (agent.aux if agent is not None else None)
    source = store if store is not None else (agent.runtime.store if live and agent else None)
    if _routed(aux) and aux is not None:
        rows = await source.load(session_id) if source is not None else []
        return SideAnswer(await ask_transcript(aux, question, rows), "routed")
    if live and agent is not None:
        prompt = await _known_prompt(agent.runtime)
        if prompt:
            try:
                return SideAnswer(await ask_fork(agent, question, prompt=prompt), "fork")
            except Exception:
                if aux is None:
                    raise
                log.warning(
                    "side question fork failed; answering from the transcript", exc_info=True
                )
    if aux is None:
        raise SideQuestionUnavailable(
            "no model is available for side questions in this session",
            hint="start a turn first, or set auxiliary.side_question",
        )
    rows = await source.load(session_id) if source is not None else []
    return SideAnswer(await ask_transcript(aux, question, rows), "transcript")
