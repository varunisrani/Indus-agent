"""Bot-to-bot messaging in the gateway: receiving routes, local delivery, `self-chat` (P24-07).

Three pieces, all around each profile's canonical chat (``indus.state.bot_chat``):

- ``bots_router(ctx)``: routes for the API server (P15-30), so another gateway (a *peer*, see
  ``indus.service.peers``) can reach this profile's bot. ``POST /v1/bots/chat`` resolves or creates
  the canonical chat and answers its session id; ``POST /v1/bots/dm {message, author?}`` runs one
  unattended turn there and answers its reply (empty when the bot chose silence). A relayed turn
  gets ``RELAY_CAP_S`` (10 minutes); past that the answer is 504 ``delivery_timeout`` with
  ``accepted: true`` (the turn goes on in the chat; the sender must not resend). Failures carry a
  typed ``reason`` in the error body. The API server's bearer gate guards these routes like every
  other.
- ``LocalBotTransport``: how ``message_agent`` delivers. A bare name that is a live local profile
  (other than the sender) is local: the target's canonical chat is resolved in its own store and
  one turn runs there through a one-shot ``indus -p <profile> -z`` child (attached to that
  profile's daemon by default, so a running turn queues it), with the environment from the one
  child-env builder (R02.22) and the same 10-minute cap (the child is killed past it). Otherwise a
  registered peer of that name, or ``peer/profile``, goes through ``PeerClient.dm``.
- ``deliver_self_chat``: the ``self-chat`` cron delivery target (H11): the job's output is added
  to the job's own profile's canonical chat as an incoming (user-role) message; no turn runs.
  ``install_self_chat`` registers it in cron's deliverer table (the delivery router does this when
  it is built; the CLI scheduler too). Both live in ``indus.service.self_chat`` (the CLI may not
  import the gateway) and are re-exported here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import signal
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from indus.foundation.blocking import run_blocking
from indus.foundation.profiles import is_live, is_valid_id, mkdir_in_profile, profile_home
from indus.foundation.redact import redact_text
from indus.foundation.scope import Scope, make_scope
from indus.gateway.http.serving import BadRequest, read_json
from indus.gateway.outbound.filters import is_silence
from indus.safety.child_env import child_env
from indus.service.peers import RELAY_CAP_S, PeerBook, PeerClient, PeerError, PeerTarget
from indus.service.self_chat import SELF_CHAT, deliver_self_chat, install_self_chat
from indus.state import registry
from indus.state.bot_chat import CanonicalChat, FailureReason, classify_failure
from indus.state.bot_chat import ensure_canonical as _ensure_canonical
from indus.tools.builtin.message_agent import BotDelivery, DeliveryOutcome

if TYPE_CHECKING:
    from indus.gateway.http.api_server import ApiContext

__all__ = [
    "CHAT_PATH",
    "DM_PATH",
    "MAX_DM_BYTES",
    "SELF_CHAT",
    "LocalBotTransport",
    "OneShotResult",
    "bots_router",
    "deliver_self_chat",
    "install_self_chat",
    "run_oneshot",
]

log = logging.getLogger(__name__)

CHAT_PATH: Final = "/v1/bots/chat"
DM_PATH: Final = "/v1/bots/dm"
MAX_DM_BYTES: Final = 256 * 1024


# -- receiving side: the API server routes ---------------------------------------------------------


def _failure(
    status: int, reason: FailureReason, message: str, *, accepted: bool = False
) -> JSONResponse:
    body: dict[str, Any] = {
        "message": redact_text(message)[:1_000],
        "type": "invalid_request_error" if status < 500 else "server_error",
        "code": reason,
        "reason": reason,
    }
    if accepted:
        body["accepted"] = True
    return JSONResponse({"error": body}, status_code=status)


def _reason_of(exc: BadRequest) -> FailureReason:
    if exc.status in (409, 423, 429):
        return "target_busy"
    if exc.status == 404:
        return "missing_config"
    return classify_failure(exc.message)


def bots_router(ctx: ApiContext) -> APIRouter:
    """The ``/v1/bots/*`` routes (mounted by ``build_api_app`` beside the runs routes)."""
    from indus.gateway.http.api_server import failure_text, next_or_tick, open_turn

    api = APIRouter()

    def store() -> Any:
        if ctx.store is None:
            raise BadRequest(
                "the session store is not available", status=503, code="store_unavailable"
            )
        return ctx.store

    async def canonical() -> CanonicalChat:
        return await run_blocking(_ensure_canonical, store(), label="bot chat")

    async def chat(request: Request) -> Response:
        found = await canonical()
        return JSONResponse(
            {"object": "indus.bot_chat", "session_id": found.session_id, "created": found.created}
        )

    async def dm(request: Request) -> Response:
        body = await read_json(request, MAX_DM_BYTES)
        if not isinstance(body, dict):
            raise BadRequest("the body must be a JSON object", code="invalid_body")
        fields = cast(dict[str, Any], body)
        unknown = set(fields) - {"message", "author"}
        if unknown:
            raise BadRequest(f"unknown fields: {', '.join(sorted(unknown))}", code="invalid_body")
        message = fields.get("message")
        if not isinstance(message, str) or not message.strip():
            raise BadRequest("message must be non-empty text", code="invalid_body", param="message")
        author = fields.get("author")
        if author is not None and (not isinstance(author, str) or len(author) > 128):
            raise BadRequest("author must be a short name", code="invalid_body", param="author")
        found = await canonical()
        if not ctx.gate.enter():
            return _failure(429, "target_busy", "too many turns are running here; try later")
        try:
            turn = await open_turn(ctx, found.session_id, message, run_context="unattended")
        except BadRequest as exc:
            ctx.gate.leave()
            return _failure(exc.status, _reason_of(exc), exc.message)
        except BaseException:
            ctx.gate.leave()
            raise
        pending = asyncio.ensure_future(turn.outcome())
        done, outcome = await next_or_tick(pending, ctx.clock, RELAY_CAP_S)
        if not done:
            pending.add_done_callback(lambda _f: ctx.gate.leave())
            return _failure(
                504,
                "delivery_timeout",
                f"the turn is still running after {int(RELAY_CAP_S)} s; the message is in the "
                "bot's chat and will be answered there: do not resend",
                accepted=True,
            )
        ctx.gate.leave()
        problem = failure_text(outcome)
        text = outcome.text or ""
        if problem and not text.strip():
            return _failure(502, classify_failure(problem), problem)
        return JSONResponse(
            {
                "object": "indus.bot_dm",
                "session_id": outcome.session_id,
                "reply": "" if is_silence(text) else text,
            }
        )

    api.add_api_route(CHAT_PATH, chat, methods=["POST"])
    api.add_api_route(DM_PATH, dm, methods=["POST"])
    return api


# -- sending side: local delivery ---------------------------------------------------------------


@dataclass(frozen=True)
class OneShotResult:
    """A finished one-shot child: its exit code (None: stopped at the cap), stdout and its
    ``--usage-file`` report."""

    code: int | None
    stdout: str
    report: Mapping[str, Any] = field(default_factory=lambda: dict[str, Any]())


type OneShotRunner = Callable[[list[str], dict[str, str], Path, float], Awaitable[OneShotResult]]


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)  # the group of the child we started, by its own pid
    with contextlib.suppress(Exception):
        await proc.wait()


def _read_report(path: Path) -> dict[str, Any]:
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return cast(dict[str, Any], data) if isinstance(data, dict) else {}


def _cap_runner(scope: Scope) -> OneShotRunner:
    async def run(argv: list[str], env: dict[str, str], report: Path, cap: float) -> OneShotResult:
        return await run_oneshot(argv, env, report, cap, sleep=scope.clock.sleep)

    return run


async def run_oneshot(
    argv: list[str],
    env: dict[str, str],
    report: Path,
    cap: float,
    *,
    sleep: Callable[[float], Awaitable[None]],
) -> OneShotResult:
    """Start ``argv`` in its own process group; stop the whole group past ``cap`` seconds."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    talk = asyncio.ensure_future(proc.communicate())
    tick = asyncio.ensure_future(sleep(cap))
    try:
        await asyncio.wait({talk, tick}, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        talk.cancel()
        await _kill_group(proc)
        raise
    finally:
        tick.cancel()
    if not talk.done():
        talk.cancel()
        await _kill_group(proc)
        return OneShotResult(None, "", _read_report(report))
    out, _ = talk.result()
    return OneShotResult(
        proc.returncode, (out or b"").decode("utf-8", "replace"), _read_report(report)
    )


def _outcome_of(result: OneShotResult, session_id: str) -> DeliveryOutcome:
    failure = result.report.get("failure")
    said = failure if isinstance(failure, str) and failure.strip() else None
    if result.code is None:
        return DeliveryOutcome.failed(
            "delivery_timeout",
            f"the bot's turn did not finish within {int(RELAY_CAP_S)} s and was stopped",
        )
    if result.code == 0:
        reply = result.stdout.strip()
        return DeliveryOutcome.delivered("" if is_silence(reply) else reply, session_id)
    if result.code == 1 and result.report.get("completed"):
        return DeliveryOutcome.delivered("", session_id)  # the bot answered without text
    if result.code == 3:
        return DeliveryOutcome.failed(
            "target_busy", "the bot's chat is waiting behind a turn its daemon lost"
        )
    if result.code == 130:
        return DeliveryOutcome.failed("unknown", "the bot's turn was interrupted")
    if result.code == 1 and said is None:
        return DeliveryOutcome.failed("runtime_offline", "the bot's runtime could not start")
    text = redact_text(said or f"the bot's turn failed (exit {result.code})")[:500]
    return DeliveryOutcome.failed(classify_failure(said), text)


class LocalBotTransport:
    """``message_agent``'s delivery path for one sending profile (see the module notes)."""

    def __init__(
        self,
        scope: Scope,
        *,
        peers: PeerClient | None = None,
        runner: OneShotRunner | None = None,
        python: str | None = None,
    ) -> None:
        self.scope = scope
        self.peers = peers if peers is not None else PeerClient(scope)
        self.runner = runner if runner is not None else _cap_runner(scope)
        self.python = python or sys.executable

    async def deliver(self, delivery: BotDelivery) -> DeliveryOutcome:
        target = delivery.target
        if target.profile is not None:
            return await self._peer(PeerTarget(target.name, target.profile), delivery)
        name = target.name
        if name == self.scope.profile_id:
            return DeliveryOutcome.failed("missing_config", "a bot cannot message itself")
        if is_valid_id(name) and is_live(self.scope.root, name):
            return await self._local(name, delivery)
        if PeerBook(self.scope).get(name) is not None:
            return await self._peer(PeerTarget(name), delivery)
        return DeliveryOutcome.failed(
            "missing_config", f"no local profile and no registered peer is named {name!r}"
        )

    async def _peer(self, target: PeerTarget, delivery: BotDelivery) -> DeliveryOutcome:
        try:
            got = await self.peers.dm(target, delivery.message, author=delivery.sender)
        except PeerError as exc:
            return DeliveryOutcome.failed(exc.reason, exc.message)
        return DeliveryOutcome.delivered("" if is_silence(got.reply) else got.reply, got.session_id)

    def _target_scope(self, name: str) -> Scope:
        home = profile_home(self.scope.root, name)
        return make_scope(home, env=self.scope.env, clock=self.scope.clock)

    @staticmethod
    def _ensure_in(target: Scope) -> CanonicalChat:
        with registry.shared(target) as store:
            return _ensure_canonical(store)

    async def _local(self, name: str, delivery: BotDelivery) -> DeliveryOutcome:
        target = self._target_scope(name)
        chat = await run_blocking(self._ensure_in, target, label="bot chat")
        folder = mkdir_in_profile(self.scope.home, Path("cache") / "bots")
        report = folder / f"delivery-{secrets.token_hex(8)}.json"
        argv = [
            self.python,
            "-m",
            "indus",
            "-p",
            name,
            "-z",
            delivery.message,
            "--resume",
            chat.session_id,
            "--usage-file",
            str(report),
        ]
        try:
            result = await self.runner(argv, child_env(target), report, RELAY_CAP_S)
        finally:
            with contextlib.suppress(OSError):
                report.unlink()
        return _outcome_of(result, chat.session_id)
