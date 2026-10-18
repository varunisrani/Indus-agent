"""Long-session soak (P08-18, the M2 demo): a real ``Agent`` through 300 turns and two hours.

The fake model follows ``tests/fixtures/model_scripts/long_session.yaml``: every user turn it
calls one tool that returns a large log, then answers in a line; it reports usage as the real size
of what it was sent (4 characters per token), writes compaction summaries that carry the goal it
can see, and answers the last turn with the goal. A 429 storm at one turn runs through the real
credential pool (P08-12: two keys, the second 429 rotates) and the real fallback chain (P08-13:
``fallback_providers`` names the backup host), and the next turn is back on the primary's first key.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from indus.core.agent import Agent, AgentOptions
from indus.core.compression.engine import BuiltinEngine, GateVerdict
from indus.core.compression.threshold import trigger_tokens
from indus.core.exit_reasons import ExitReason
from indus.core.persist import MemoryStore
from indus.foundation.config import load
from indus.foundation.scope import EnvView, Scope, make_scope
from indus.protocol.events import EventBase, Notice
from indus.providers.auth.pool_model import Pool, PoolEntry, edit_pool, secret_name
from indus.providers.auth.store import AuthStore
from indus.providers.model_list import ModelInfo
from indus.providers.pricing import load_table
from indus.providers.profile import ProviderProfile
from indus.providers.route import RuntimeRoute, Secret
from indus.tools.registry import ToolRegistry, ToolTraits
from tests.fakes.agent import run_driven
from tests.fakes.clock import FakeClock
from tests.fakes.model import (
    SCRIPTS_DIR,
    Fail,
    FakeModelServer,
    Finish,
    Script,
    Text,
    ToolCall,
    Turn,
    Usage,
    fake_route,
)
from tests.fakes.model.mini_yaml import loads
from tests.fakes.model.server import CHAT_BASE_URL, FAKE_HOST
from tests.fakes.turn import StubBuilder, check_alternation

SPEC: Mapping[str, Any] = loads((SCRIPTS_DIR / "long_session.yaml").read_text(encoding="utf-8"))
TASK: str = SPEC["task"]
WINDOW: int = SPEC["window"]
GOAL = re.compile(r"TASK: [^\"\\\n]*")
# Read at import: the autouse fixture drops every INDUS_* variable before a test runs.
LIVE = os.environ.get("INDUS_LIVE") == "1"
LIVE_PROVIDER = os.environ.get("INDUS_LIVE_PROVIDER", "")
LIVE_MODEL = os.environ.get("INDUS_LIVE_MODEL", "")


def user_text(number: int, last: int) -> str:
    if number == 1:
        return f"{TASK}. Start with the data model."
    if number == last:
        return "Final step: tell me what we set out to build and whether it is done."
    return f"Step {number}: carry on with the next part."


# -- the fake model -------------------------------------------------------------------------------


@dataclass
class SoakServer(FakeModelServer):
    """A fake server whose next turn is decided from the request it answers (see the YAML)."""

    spec: Mapping[str, Any] = field(default_factory=dict)
    storming: bool = False
    answered: list[tuple[str, int, int]] = field(default_factory=list)
    refused: int = 0
    calls: int = 0

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        raw = await request.aread()
        if request.method == "POST":
            turn = self._decide(json.loads(raw), raw.decode("utf-8"))
            self.script = replace(self.script, turns=(*self.script.turns, turn))
        return await super()._handle(request)

    def _decide(self, body: Mapping[str, Any], text: str) -> Turn:
        if self.storming:
            self.refused += 1
            storm = self.spec["storm"]
            return Turn(
                (Fail(storm["status"], storm["body"], {"Retry-After": storm["retry_after"]}),)
            )
        prompt = max(1, len(text) // 4)
        completion: int = self.spec["completion_tokens"]
        found = GOAL.search(text)
        goal = found.group(0) if found else "(goal not in view)"
        messages: Sequence[Mapping[str, Any]] = body["messages"]
        if "tools" not in body:  # the compaction summary request
            self.answered.append(("summary", prompt, completion))
            notes = f"{self.spec['summary_goal_heading']}\n{goal}\n\n## Next steps\nKeep going."
            return Turn((Text((notes,)), Usage(prompt, completion), Finish("stop")))
        self.answered.append(("main", prompt, completion))
        last = messages[-1]
        if last.get("role") == "user":
            self.calls += 1
            call = ToolCall(self.spec["tool"], {"part": self.calls}, id=f"call_{self.calls}")
            return Turn((call, Usage(prompt, completion), Finish("tool_calls")))
        asked = next((m for m in reversed(messages) if m.get("role") == "user"), {"content": ""})
        if "Final step" in json.dumps(asked.get("content")):
            answer = f"We set out to do this: {goal}. It is done and the tests are green."
        else:
            answer = "Done; the checks pass."
        return Turn((Text((answer,)), Usage(prompt, completion), Finish("stop")))

    def main_totals(self) -> tuple[int, int, int]:
        main = [a for a in self.answered if a[0] == "main"]
        return len(main), sum(a[1] for a in main), sum(a[2] for a in main)

    def summaries(self) -> int:
        return sum(1 for a in self.answered if a[0] == "summary")


def soak_server(clock: FakeClock, name: str) -> SoakServer:
    return SoakServer(Script((), name=name), clock=clock, spec=SPEC)


async def window_list(route: RuntimeRoute) -> list[ModelInfo]:
    return [ModelInfo(route.model, WINDOW)]


def tool_registry(clock: Any) -> ToolRegistry:
    registry = ToolRegistry(clock=clock)
    size: int = SPEC["tool_output_chars"]

    async def run_checks(args: Mapping[str, Any]) -> str:
        part = args.get("part", 0)
        line = f"check {part}: ledger ok, items ok, latency fine\n"
        return (line * (size // len(line) + 1))[:size]

    registry.register(
        SPEC["tool"],
        "soak",
        {"description": "run the project checks", "parameters": {"type": "object"}},
        run_checks,
        traits=ToolTraits(read_only=True),
    )
    return registry


def price_table() -> Any:
    prices = SPEC["prices"]
    fallback = SPEC["fallback"]
    return load_table(
        "version: 1\nvendors:\n"
        "  soak:\n    hosts: [fake.invalid, backup.invalid]\n    models:\n"
        f'      fake-model-1: {{input: "{prices["input"]}", output: "{prices["output"]}"}}\n'
        f'      {fallback["model"]}: {{input: "{prices["input"]}", output: "{prices["output"]}"}}\n'
    )


# -- the pool and the chain -----------------------------------------------------------------------

POOL_PROVIDER = "soakvendor"


def pooled_scope(scope: Scope, keys: Sequence[str]) -> tuple[Scope, ProviderProfile]:
    """``scope`` with the primary's keys in its credential pool (P07 rows + secrets) and the
    backup in ``fallback_providers`` (config.yaml, loaded over the defaults)."""
    profile = ProviderProfile(
        name=POOL_PROVIDER,
        base_url=CHAT_BASE_URL,
        hostname=FAKE_HOST,
        env_vars=("SOAKVENDOR_API_KEY",),
        context_length_hint=WINDOW,  # a vendor profile: its window is not read from /models
    )
    assert scope.secrets is not None
    idents = [f"key{n}" for n in range(len(keys))]
    for ident, value in zip(idents, keys, strict=True):
        scope.secrets.set(secret_name(POOL_PROVIDER, ident), value)  # type: ignore[attr-defined]

    def seed(pool: Pool, _doc: dict[str, Any]) -> None:
        for ident in idents:
            pool.add(PoolEntry(id=ident, label=ident))

    edit_pool(AuthStore(scope.home, env=scope.env), POOL_PROVIDER, seed)
    backup = SPEC["fallback"]
    (scope.home / "config.yaml").write_text(
        "fallback_providers:\n"
        f"  - provider: {backup['provider']}\n"
        f"    model: {backup['model']}\n"
        f"    base_url: {backup['base_url']}\n",
        encoding="utf-8",
    )
    env = scope.env.with_overlay({"OPENAI_API_KEY": None, "OPENAI_BASE_URL": None})
    scoped = scope.with_env(env)
    return scoped.with_config(load(scoped)), profile


def fallback_notices(events: Sequence[EventBase]) -> list[str]:
    return [e.text for e in events if isinstance(e, Notice) and e.key == "fallback"]


# -- the run --------------------------------------------------------------------------------------


@dataclass
class Run:
    agent: Agent
    primary: SoakServer
    backup: SoakServer
    notices: list[str]
    verdicts: list[tuple[str, GateVerdict]]
    compactions_by_turn: list[int]
    final_text: str
    rows: list[Any]
    builder: StubBuilder
    hosts_by_turn: dict[int, list[str]]


def run_soak(scope: Scope, clock: FakeClock, turns: int) -> Run:
    primary = soak_server(clock, "long_session")
    backup = soak_server(clock, "long_session_backup")
    keys: list[str] = SPEC["keys"]
    scope, profile = pooled_scope(scope, keys)
    route = replace(
        fake_route(primary), provider=POOL_PROVIDER, profile=profile, api_key=Secret(keys[0])
    )
    events: list[EventBase] = []

    def client_for(target: RuntimeRoute) -> httpx.AsyncClient:
        server = backup if "backup.invalid" in target.base_url else primary
        return server.client()

    builder = StubBuilder()
    store = MemoryStore()
    options = AgentOptions(
        session_id="s1",
        store=store,
        builder=builder,
        registry=tool_registry(clock),
        client_factory=client_for,
        list_models=window_list,
    )
    verdicts: list[tuple[str, GateVerdict]] = []
    compactions_by_turn: list[int] = []
    hosts_by_turn: dict[int, list[str]] = {}
    storm_turn: int = SPEC["storm"]["turn"]
    pause: float = SPEC["seconds_between_turns"]

    async def main() -> tuple[Agent, str]:
        agent = await Agent.start(scope, route, ["soak"], options, events.append)
        agent.usage.table = price_table()
        engine = agent.context_engine
        assert isinstance(engine, BuiltinEngine)
        judge = engine.should_compress

        async def watched(rows: Any, *, gate: Any) -> GateVerdict:
            verdict = await judge(rows, gate=gate)
            verdicts.append((str(gate), verdict))
            return verdict

        engine.should_compress = watched  # type: ignore[method-assign]
        final = ""
        for number in range(1, turns + 1):
            before = primary.summaries() + backup.summaries()
            seen = len(primary.model_requests) + len(backup.model_requests)
            primary.storming = number == storm_turn
            result = await agent.run_turn(user_text(number, turns))
            primary.storming = False
            assert result.exit_reason is ExitReason.COMPLETED, (number, result.exit_reason)
            after = primary.summaries() + backup.summaries()
            compactions_by_turn.extend([number] * (after - before))
            requests = [*primary.model_requests, *backup.model_requests]
            requests.sort(key=lambda r: r.at or 0.0)
            hosts_by_turn[number] = [
                f"{r.headers.get('host')}|{r.headers.get('authorization', '')}"
                for r in requests[seen:]
            ]
            final = result.final_text or ""
            await clock.sleep(pause)
        return agent, final

    agent, final = asyncio.run(run_driven(main(), clock))
    rows = asyncio.run(store.load(agent.session_id))
    asyncio.run(agent.close())
    return Run(
        agent,
        primary,
        backup,
        fallback_notices(events),
        verdicts,
        compactions_by_turn,
        final,
        rows,
        builder,
        hosts_by_turn,
    )


@pytest.mark.slow
def test_three_hundred_turns_over_two_hours(scope: Scope, fake_clock: FakeClock) -> None:
    turns: int = SPEC["turns"]
    started = fake_clock.monotonic()
    run = run_soak(scope, fake_clock, turns)
    agent = run.agent
    engine = agent.context_engine
    assert isinstance(engine, BuiltinEngine)

    # two simulated hours
    assert fake_clock.monotonic() - started >= turns * SPEC["seconds_between_turns"]

    # compression fires at the computed trigger, never below it
    trigger = trigger_tokens(WINDOW, engine.max_output, 0.5)
    assert engine.trigger == trigger
    compress = [v for _, v in run.verdicts if v.action == "compress"]
    assert len(run.compactions_by_turn) >= 3
    assert len(compress) == len(run.compactions_by_turn)
    assert all(v.tokens >= trigger and v.anchored for v in compress)
    assert all(v.action in ("ok", "wait") for _, v in run.verdicts if v.tokens < trigger)

    # the task from turn 1 survives every compaction
    assert TASK in run.final_text

    # the stored transcript alternates
    check_alternation(run.rows)

    # the prompt is built once, then only after a compaction (the next turn rebuilds it)
    rebuilt_after = {n for n in run.compactions_by_turn if n < turns}
    assert run.builder.calls == 1 + len(rebuilt_after)

    # usage and cost equal what the fake model reported
    snap = agent.usage.snapshot()
    calls = run.primary.main_totals()
    backup_calls = run.backup.main_totals()
    api_calls = calls[0] + backup_calls[0]
    prompt = calls[1] + backup_calls[1]
    completion = calls[2] + backup_calls[2]
    assert snap.api_calls == api_calls == 2 * turns
    assert snap.usage.prompt_tokens == prompt and snap.usage.output_tokens == completion
    assert snap.side_calls == {"compression": run.primary.summaries() + run.backup.summaries()}
    prices = SPEC["prices"]
    expected = (
        Decimal(prompt) * Decimal(prices["input"]) + Decimal(completion) * Decimal(prices["output"])
    ) / Decimal(1_000_000)
    assert snap.cost_usd == expected and snap.unpriced_calls == 0

    # the 429 storm: the key rotates, then the backup route answers; the next turn is back
    storm: int = SPEC["storm"]["turn"]
    keys: list[str] = SPEC["keys"]
    during = run.hosts_by_turn[storm]
    assert during[0] == f"fake.invalid|Bearer {keys[0]}"
    assert f"fake.invalid|Bearer {keys[1]}" in during
    assert during[-1].startswith("backup.invalid|")
    assert run.primary.refused >= 2
    assert all(h.startswith("fake.invalid|") for h in run.hosts_by_turn[storm + 1])
    assert run.hosts_by_turn[storm + 1][0] == f"fake.invalid|Bearer {keys[0]}"
    assert len(run.notices) == 2
    assert run.notices[0].startswith("Model fallback:")
    assert run.notices[1].startswith("Main model restored")
    assert all(h.startswith("fake.invalid|") for h in run.hosts_by_turn[storm - 1])


# -- the live lane --------------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.live
def test_short_live_session(tmp_path: Path) -> None:
    if not LIVE:
        pytest.skip("live lane only: set INDUS_LIVE=1 with a real key")
    if not LIVE_PROVIDER or not LIVE_MODEL:
        pytest.skip("set INDUS_LIVE_PROVIDER and INDUS_LIVE_MODEL (and the provider's key)")
    from indus.providers import route as routes

    home = tmp_path / "live" / "profiles" / "work"
    home.mkdir(parents=True)
    (home / "profile.yaml").touch()
    scope = make_scope(home, env=EnvView.from_process().with_overlay({"INDUS_HOME": str(home)}))
    route = routes.resolve(scope, provider=LIVE_PROVIDER, model=LIVE_MODEL)
    turns: int = SPEC["live"]["turns"]

    async def main() -> tuple[str, int]:
        agent = await Agent.start(scope, route, [], AgentOptions(session_id="live1"))
        try:
            final = ""
            for number in range(1, turns + 1):
                result = await agent.run_turn(
                    user_text(number, turns) + " Answer in one short sentence."
                )
                assert result.exit_reason is ExitReason.COMPLETED
                final = result.final_text or ""
            return final, agent.usage.snapshot().api_calls
        finally:
            await agent.close()

    final, calls = asyncio.run(main())
    assert calls >= turns
    assert "inventory" in final.lower()
