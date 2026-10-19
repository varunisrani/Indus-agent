"""The auxiliary model client: one answer for a side task (P08-17, A42).

Side tasks — compression summaries, session titles, the smart approval judge, later vision,
session search and memory — ask ``AuxClient.call(task, messages, …)``. The route is picked in
this order, each candidate tried once:

1. the task's pin ``auxiliary.<task>.{provider, model, base_url, reasoning_effort}``;
   ``provider: auto`` (or nothing) means the main route and model — a pinned ``model`` alone
   swaps the model on the main route;
2. the task's own ``auxiliary.<task>.fallback`` list (``{provider, model}`` entries);
3. the main fallback chain ``fallback_providers`` (same shape, P08-13).

A candidate that cannot be built (unknown provider, no key, no model) is skipped; one whose call
ends in HTTP 402 (billing / credits) falls through to the next. Any other failure belongs to the
caller, who usually has a deterministic path (a fallback summary, a human prompt). The call is
non-streaming, has P03's classified retry (one retry by default) and a deadline per task on the
injected clock (``auxiliary.<task>.timeout_s``; compression and vision 120 s, titles and
approvals 30 s, others 60 s).

Auxiliary calls never go through the turn loop, so loop-level request hooks never see them; the
client has its own observers instead (P13 attaches plugin hooks there; their failures are
ignored). Usage of every answered call goes to the recorder with its task tag (B24).

``window_for`` tells the compressor the summariser's context window and warns once when it is
smaller than the main model's (summaries of a big context would fail or lose the middle).
``auto_title`` makes a session title after the first exchange (``llm`` provenance, P02 rules).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, cast

import httpx

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.foundation.errors import ProviderError
from indus.foundation.scope import Scope
from indus.providers import context_length
from indus.providers import route as route_mod
from indus.providers.http import client_for, timeouts_for
from indus.providers.retry import RetryFailed, retry_call
from indus.providers.route import RuntimeRoute
from indus.providers.settings import as_mapping, config_of, positive_number, text
from indus.providers.streaming import CallResult, StreamFlags, stream_call
from indus.providers.timeouts import TimeoutPolicy
from indus.providers.transports import get_transport
from indus.providers.types import ChatRequest, Message, NormalizedResponse, Usage
from indus.state import titles
from indus.state.registry import StoreHandle

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "TASK_TIMEOUTS",
    "TITLE_PROMPT",
    "AuxClient",
    "AuxResult",
    "AuxTimeout",
    "AuxUnavailable",
    "Candidate",
    "Observer",
    "UsageRecorder",
    "auto_title",
    "clean_title",
    "generate_title",
]

TASK_TIMEOUTS: Mapping[str, float] = {
    "compression": 120.0,
    "title": 30.0,
    "approval": 30.0,
    "vision": 120.0,  # P10-15: an image and a question to the text route
}
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_RETRIES = 1
AUTO = "auto"

type UsageRecorder = Callable[[str, RuntimeRoute, Usage | None, float], object]
type Observer = Callable[[str, str, Mapping[str, Any]], object]
type ClientFactory = Callable[[RuntimeRoute], httpx.AsyncClient]
type Resolver = Callable[..., RuntimeRoute]


class AuxUnavailable(ProviderError):
    """No candidate route could answer the side task."""

    default_code = "provider.aux_unavailable"

    def __init__(self, task: str, reasons: Sequence[str]) -> None:
        detail = "; ".join(reasons) or "no route configured"
        super().__init__(f"no model could run the {task} task ({detail})")
        self.task = task
        self.reasons = tuple(reasons)


class AuxTimeout(ProviderError):
    """The side task's call did not finish within its deadline."""

    default_code = "provider.aux_timeout"


@dataclass(frozen=True)
class Candidate:
    """One route to try. ``origin``: ``pin``, ``main``, ``task-fallback`` or ``main-fallback``."""

    origin: str
    provider: str = AUTO
    model: str = ""
    base_url: str = ""
    reasoning_effort: str = ""

    def key(self) -> tuple[str, str, str]:
        return (self.provider, self.model, self.base_url)


@dataclass(frozen=True)
class AuxResult:
    text: str
    response: NormalizedResponse
    route: RuntimeRoute
    latency_s: float
    task: str
    origin: str


def _entries(value: object) -> list[Candidate]:
    """``[{provider, model, base_url?}]`` entries; ones without both provider and model are
    ignored (a half-written entry must not silently mean "the main model")."""
    out: list[Candidate] = []
    if not isinstance(value, list):
        return out
    for item in cast(list[object], value):
        entry = as_mapping(item)
        provider, model = text(entry.get("provider")).lower(), text(entry.get("model"))
        if provider and model:
            out.append(Candidate("", provider, model, text(entry.get("base_url"))))
    return out


async def _until(awaitable: Awaitable[Any], clock: Clock, seconds: float) -> Any:
    """``awaitable``'s result, or ``AuxTimeout`` after ``seconds`` on ``clock``."""
    work = asyncio.ensure_future(awaitable)
    timer = asyncio.ensure_future(clock.sleep(seconds))
    try:
        done, _ = await asyncio.wait({work, timer}, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        work.cancel()
        timer.cancel()
        raise
    if work in done:
        timer.cancel()
        return work.result()
    work.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await work
    raise AuxTimeout(f"the call did not finish within {seconds:.0f} s")


def _is_billing(error: RetryFailed) -> bool:
    verdict = error.classified
    return verdict.status == 402 or verdict.reason == "billing"


class AuxClient:
    """Side-task model calls for one profile (see the module notes)."""

    def __init__(
        self,
        scope: Scope,
        main: RuntimeRoute | Callable[[], RuntimeRoute],
        *,
        clock: Clock | None = None,
        resolve: Resolver | None = None,
        client_factory: ClientFactory | None = None,
        record: UsageRecorder | None = None,
        observers: Sequence[Observer] = (),
        max_retries: int = DEFAULT_RETRIES,
    ) -> None:
        self.scope = scope
        self._main = main
        self.clock = clock or scope.clock
        self._resolve = resolve or route_mod.resolve
        self._client_factory = client_factory
        self.record = record
        self.observers: list[Observer] = list(observers)
        self.max_retries = max_retries
        self._warned: set[tuple[str, str, str]] = set()

    def bound(
        self, main: RuntimeRoute | Callable[[], RuntimeRoute], *, record: UsageRecorder | None
    ) -> AuxClient:
        """This client for one session: ``auto`` follows ``main`` (the session's own route, read
        at call time) and usage goes to ``record``; observers and the once-only window warnings
        are shared with this client."""
        twin = AuxClient(
            self.scope,
            main,
            clock=self.clock,
            resolve=self._resolve,
            client_factory=self._client_factory,
            record=record,
            max_retries=self.max_retries,
        )
        twin.observers = self.observers
        twin._warned = self._warned
        return twin

    # -- configuration -----------------------------------------------------------------------------

    @property
    def main(self) -> RuntimeRoute:
        return self._main() if callable(self._main) else self._main

    def task_config(self, task: str) -> Mapping[str, Any]:
        return as_mapping(config_of(self.scope).get(f"auxiliary.{task}"))

    def timeout_for(self, task: str) -> float:
        configured = positive_number(self.task_config(task).get("timeout_s"))
        return configured or TASK_TIMEOUTS.get(task, DEFAULT_TIMEOUT_S)

    def candidates(self, task: str) -> list[Candidate]:
        """The routes to try for ``task``, in order, without duplicates."""
        pin = self.task_config(task)
        provider = text(pin.get("provider")).lower() or AUTO
        first = Candidate(
            "pin" if provider != AUTO or text(pin.get("model")) else "main",
            provider,
            text(pin.get("model")),
            text(pin.get("base_url")),
            text(pin.get("reasoning_effort")),
        )
        listed = [first]
        listed += [replace(c, origin="task-fallback") for c in _entries(pin.get("fallback"))]
        chain = config_of(self.scope).get("fallback_providers")
        listed += [replace(c, origin="main-fallback") for c in _entries(chain)]
        seen: set[tuple[str, str, str]] = set()
        out: list[Candidate] = []
        main = self.main
        for candidate in listed:
            key = candidate.key()
            if candidate.provider in (AUTO, main.provider) and not candidate.base_url:
                key = (main.provider, candidate.model or main.model, "")
            if key in seen:
                continue
            seen.add(key)
            out.append(candidate)
        return out

    def route_for(self, candidate: Candidate) -> RuntimeRoute:
        """The route a candidate names; ``ProviderError`` when it cannot be used."""
        main = self.main
        if candidate.provider in (AUTO, main.provider) and not candidate.base_url:
            return replace(main, model=candidate.model) if candidate.model else main
        if not candidate.model:
            raise ProviderError(f"{candidate.provider}: no model named for this task")
        return self._resolve(
            self.scope,
            provider=candidate.provider,
            model=candidate.model,
            base_url=candidate.base_url or None,
        )

    def _client(self, route: RuntimeRoute) -> httpx.AsyncClient:
        if self._client_factory is not None:
            return self._client_factory(route)
        return client_for(route, timeouts=timeouts_for(route, self.scope))

    # -- calling -----------------------------------------------------------------------------------

    def _observe(self, phase: str, task: str, info: Mapping[str, Any]) -> None:
        for observer in list(self.observers):
            try:
                observer(phase, task, info)
            except Exception:
                log.debug("an auxiliary observer failed", exc_info=True)

    async def _record(
        self, recorder: UsageRecorder | None, task: str, route: RuntimeRoute, result: Any
    ) -> None:
        if recorder is None:
            return
        try:
            outcome = recorder(task, route, result.response.usage, result.latency_s)
            if inspect.isawaitable(outcome):
                await cast(Awaitable[object], outcome)
        except Exception:
            log.warning("usage of an auxiliary %s call was not recorded", task, exc_info=True)

    async def _once(
        self, task: str, route: RuntimeRoute, request: ChatRequest, timeout: float | None
    ) -> AuxResult:
        transport = get_transport(route.api_mode)
        client = self._client(route)
        flags = StreamFlags(streaming=False)

        async def attempt() -> Any:
            return await stream_call(
                client,
                transport,
                route,
                request,
                policy=TimeoutPolicy(),
                clock=self.clock,
                on_event=lambda _event: None,
                route_epoch=lambda: 0,
                flags=flags,
            )

        started = self.clock.monotonic()
        work = retry_call(attempt, max_retries=self.max_retries, clock=self.clock)
        outcome = await (work if timeout is None else _until(work, self.clock, timeout))
        if not isinstance(outcome, CallResult):
            raise ProviderError("the auxiliary call ended without an answer")
        response = outcome.response
        return AuxResult(
            text=(response.content or "").strip(),
            response=response,
            route=route,
            latency_s=max(0.0, self.clock.monotonic() - started),
            task=task,
            origin="",
        )

    async def call(
        self,
        task: str,
        messages: Sequence[Message],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout_s: float | None = None,
        reasoning_effort: str | None = None,
        extra_body: Mapping[str, Any] | None = None,
        record: UsageRecorder | None = None,
        tools: Sequence[Mapping[str, Any]] = (),
        tool_choice: str | Mapping[str, Any] | None = None,
    ) -> AuxResult:
        """One answer for ``task`` (see the module notes for the route order). ``tools`` (the
        provider-neutral tool array) lets a side task such as the curator call tools; the caller
        runs them and calls again."""
        timeout = timeout_s if timeout_s is not None else self.timeout_for(task)
        recorder = record if record is not None else self.record
        reasons: list[str] = []
        for candidate in self.candidates(task):
            try:
                route = self.route_for(candidate)
            except ProviderError as exc:
                reasons.append(f"{candidate.origin} {candidate.provider}: {exc.message}")
                log.info("auxiliary %s: skipping %s (%s)", task, candidate.provider, exc.message)
                continue
            effort = reasoning_effort or candidate.reasoning_effort or None
            request = ChatRequest(
                model=route.model,
                messages=list(messages),
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning_effort=effort,
                stream=False,
                extra_body=dict(extra_body or {}),
                tools=list(tools),
                tool_choice=tool_choice,
            )
            info = {"provider": route.provider, "model": route.model, "origin": candidate.origin}
            self._observe("before", task, info)
            try:
                result = await self._once(task, route, request, timeout)
            except RetryFailed as exc:
                if not _is_billing(exc):
                    self._observe("failed", task, {**info, "reason": exc.classified.reason})
                    raise
                reasons.append(f"{candidate.origin} {route.provider}: billing (402)")
                log.warning(
                    "auxiliary %s: %s refused for billing; trying the next route",
                    task,
                    route.provider,
                )
                continue
            result = replace(result, origin=candidate.origin)
            self._observe("after", task, {**info, "latency_s": result.latency_s})
            await self._record(recorder, task, route, result)
            return result
        raise AuxUnavailable(task, reasons)

    async def call_route(
        self,
        task: str,
        route: RuntimeRoute,
        messages: Sequence[Message],
        *,
        max_tokens: int | None = None,
        timeout_s: float | None = None,
        reasoning_effort: str | None = None,
        record: UsageRecorder | None = None,
    ) -> AuxResult:
        """One answer for ``task`` from the given ``route`` (no candidate walk): a side task
        that names its own model per call (P10-21 mixture-of-agents references). ``timeout_s``
        None means no deadline. Usage goes to the recorder with the task tag, like ``call``."""
        recorder = record if record is not None else self.record
        request = ChatRequest(
            model=route.model,
            messages=list(messages),
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort or None,
            stream=False,
        )
        info = {"provider": route.provider, "model": route.model, "origin": "explicit"}
        self._observe("before", task, info)
        try:
            result = await self._once(task, route, request, timeout_s)
        except RetryFailed as exc:
            self._observe("failed", task, {**info, "reason": exc.classified.reason})
            raise
        result = replace(result, origin="explicit")
        self._observe("after", task, {**info, "latency_s": result.latency_s})
        await self._record(recorder, task, route, result)
        return result

    # -- windows -----------------------------------------------------------------------------------

    async def window_for(self, task: str, main_window: int) -> int:
        """The context window of the task's first usable route, warning once when it is smaller
        than ``main_window`` (the compression trigger should then use the smaller one)."""
        for candidate in self.candidates(task):
            if candidate.origin == "main":
                return main_window  # ``auto``: the main route itself, whose window is known
            try:
                route = self.route_for(candidate)
            except ProviderError:
                continue
            window = (await context_length.find_window(route, scope=self.scope)).tokens
            if window < main_window:
                key = (task, route.provider, route.model)
                if key not in self._warned:
                    self._warned.add(key)
                    log.warning(
                        "the %s model %s has a %d-token window, smaller than the main model's "
                        "%d; compaction will work from the smaller window",
                        task,
                        route.model,
                        window,
                        main_window,
                    )
            return window
        return main_window


# -- titles ----------------------------------------------------------------------------------------

TITLE_PROMPT = (
    "Name this conversation for a list of past sessions. Reply with the name only: three to "
    "seven plain words, no quotes, no ending punctuation, in the language the user wrote in."
)
_TITLE_INPUT_CHARS = 2000
_TITLE_WORDS = 7
_LEAD_IN = re.compile(r"^(?:title|name)\s*[:\-]\s*", re.IGNORECASE)


def clean_title(raw: str) -> str | None:
    """The first line, without quotes, markdown marks, a ``Title:`` lead-in or end punctuation,
    cut to seven words; ``None`` when nothing usable is left."""
    for line in raw.splitlines():
        line = _LEAD_IN.sub("", line.strip().strip("#*`_ ").strip())
        line = line.strip("\"'“”‘’ ").rstrip(".!?;:,").strip()
        if line:
            words = line.split()
            return " ".join(words[:_TITLE_WORDS])
    return None


async def generate_title(
    aux: AuxClient,
    first_user: str,
    first_reply: str,
    *,
    record: UsageRecorder | None = None,
) -> str | None:
    """A title for a conversation from its first exchange (``None`` when the model gave none)."""
    exchange = (
        f"User: {first_user[:_TITLE_INPUT_CHARS]}\n\nAssistant: {first_reply[:_TITLE_INPUT_CHARS]}"
    )
    result = await aux.call(
        "title",
        [{"role": "system", "content": TITLE_PROMPT}, {"role": "user", "content": exchange}],
        max_tokens=32,
        temperature=0.2,
        record=record,
    )
    return clean_title(result.text)


async def auto_title(
    aux: AuxClient,
    store: StoreHandle,
    session_id: str,
    first_user: str,
    first_reply: str,
    *,
    record: UsageRecorder | None = None,
) -> str | None:
    """Generate and store a title (``llm`` provenance; a user title is never replaced). A title
    another session holds gets the next ``#N``. Returns the stored title, else ``None``."""
    base = await generate_title(aux, first_user, first_reply, record=record)
    if base is None:
        return None
    chosen: str = base

    def write() -> bool:
        return titles.set_title(store, session_id, chosen, source="llm")

    try:
        stored = await run_blocking(write, label="title")
    except titles.TitleTaken:
        chosen = await run_blocking(lambda: titles.next_lineage_title(store, base), label="title")
        stored = await run_blocking(write, label="title")
    return chosen if stored else None
