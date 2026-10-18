"""P04-08: registration rules, overlays, generation and the probe cache."""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

import pytest

from indus.foundation.errors import ERROR_CODES, ScopeUnresolved
from indus.foundation.scope import Scope, use_scope
from indus.tools.builtin import BUILTIN_MODULES, BuiltinModule, load_builtins
from indus.tools.registry import (
    PROBE_CACHE_MAX,
    ToolNameTaken,
    ToolRegistry,
    ToolSchemaInvalid,
    scope_key,
)
from tests.fakes.clock import FakeClock
from tests.unit.tools._helpers import CountingProbe, echo, other_scope, schema


@pytest.fixture
def reg(fake_clock: FakeClock) -> ToolRegistry:
    return ToolRegistry(clock=fake_clock)


# -- registration -------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [["not", "a", "dict"], "text", 7, None])
def test_rejects_schema_that_is_not_an_object(reg: ToolRegistry, bad: object) -> None:
    with pytest.raises(ToolSchemaInvalid) as caught:
        reg.register("lookup", "demo", bad, echo)  # type: ignore[arg-type]
    assert "lookup" in str(caught.value)
    assert caught.value.code == "tools.bad_schema"
    assert reg.names() == []


@pytest.mark.parametrize("bad", [["a"], "object", 3])
def test_rejects_parameters_that_are_not_an_object(reg: ToolRegistry, bad: object) -> None:
    with pytest.raises(ToolSchemaInvalid, match="parameters"):
        reg.register("lookup", "demo", {"description": "x", "parameters": bad}, echo)


def test_schema_without_parameters_is_fine(reg: ToolRegistry) -> None:
    entry = reg.register("ping", "demo", {"description": "no inputs"}, echo)
    assert entry.parameters is None


def test_error_codes_are_listed() -> None:
    for code in ("tools.error", "tools.bad_schema", "tools.name_taken"):
        assert code in ERROR_CODES


def test_registered_schema_is_a_private_copy(reg: ToolRegistry) -> None:
    source = schema(q={"type": "string"})
    reg.register("lookup", "demo", source, echo)
    source["parameters"]["properties"]["q"]["type"] = "integer"
    entry = reg.get("lookup")
    assert entry is not None
    assert entry.schema["parameters"]["properties"]["q"]["type"] == "string"


def test_is_async_detected(reg: ToolRegistry) -> None:
    async def handler(args: object) -> str:
        return ""

    assert reg.register("a", "demo", schema(), handler).is_async
    assert not reg.register("b", "demo", schema(), echo).is_async


def test_same_name_same_toolset_overwrites(reg: ToolRegistry) -> None:
    reg.register("lookup", "demo", schema("first"), echo)
    reg.register("lookup", "demo", schema("second"), echo)
    entry = reg.get("lookup")
    assert entry is not None and entry.schema["description"] == "second"
    assert reg.names() == ["lookup"]


def test_collision_across_toolsets_rejected(reg: ToolRegistry) -> None:
    reg.register("lookup", "alpha", schema("alpha"), echo)
    with pytest.raises(ToolNameTaken) as caught:
        reg.register("lookup", "beta", schema("beta"), echo)
    assert "alpha" in str(caught.value) and caught.value.code == "tools.name_taken"
    entry = reg.get("lookup")
    assert entry is not None and entry.toolset == "alpha"


def test_collision_across_toolsets_with_override_accepted(reg: ToolRegistry) -> None:
    reg.register("lookup", "alpha", schema("alpha"), echo)
    reg.register("lookup", "beta", schema("beta"), echo, override=True)
    entry = reg.get("lookup")
    assert entry is not None and entry.toolset == "beta"


def test_overlay_is_seen_only_by_its_profile(
    reg: ToolRegistry, scope: Scope, tmp_path: Path, fake_clock: FakeClock
) -> None:
    other = other_scope(tmp_path, "other", fake_clock)
    reg.register("plug", "plugin_x", schema(), echo, profile=scope_key(scope))
    assert reg.get("plug", scope) is not None
    assert reg.get("plug", other) is None
    assert reg.get("plug") is None  # no scope set: globals only
    with use_scope(scope):
        assert reg.get("plug") is not None
    assert reg.names_in_toolset("plugin_x", scope) == ["plug"]
    assert reg.toolset_names(other) == []


def test_overlay_cannot_shadow_global_without_override(reg: ToolRegistry, scope: Scope) -> None:
    reg.register("lookup", "alpha", schema("global"), echo)
    with pytest.raises(ToolNameTaken):
        reg.register("lookup", "alpha", schema("plugin"), echo, profile=scope_key(scope))
    entry = reg.get("lookup", scope)
    assert entry is not None and entry.schema["description"] == "global"
    reg.register("lookup", "alpha", schema("plugin"), echo, profile=scope_key(scope), override=True)
    entry = reg.get("lookup", scope)
    assert entry is not None and entry.schema["description"] == "plugin"
    assert reg.get("lookup") is not None and reg.get("lookup").schema["description"] == "global"  # type: ignore[union-attr]


def test_generation_bumps_on_every_change(reg: ToolRegistry, scope: Scope) -> None:
    seen = [reg.generation]
    reg.register("a", "demo", schema(), echo)
    seen.append(reg.generation)
    reg.register("a", "demo", schema("again"), echo)
    seen.append(reg.generation)
    reg.register("b", "demo", schema(), echo, profile=scope_key(scope))
    seen.append(reg.generation)
    assert reg.deregister("a")
    seen.append(reg.generation)
    assert seen == sorted(set(seen)) and len(seen) == 5


def test_generation_unchanged_by_rejected_or_missing(reg: ToolRegistry) -> None:
    reg.register("a", "alpha", schema(), echo)
    before = reg.generation
    with pytest.raises(ToolNameTaken):
        reg.register("a", "beta", schema(), echo)
    assert not reg.deregister("missing")
    assert reg.generation == before


# -- probes -------------------------------------------------------------------------------------


def _probed(reg: ToolRegistry, probe: CountingProbe, name: str = "gated") -> None:
    reg.register(name, "demo", schema(), echo, probe=probe)


def _available(reg: ToolRegistry, scope: Scope | None, name: str = "gated") -> bool:
    entry = reg.get(name, scope)
    assert entry is not None
    return reg.available(entry, scope)


def test_no_probe_is_available(reg: ToolRegistry) -> None:
    reg.register("free", "demo", schema(), echo)
    assert _available(reg, None, "free")


def test_probe_cached_for_thirty_seconds(
    reg: ToolRegistry, scope: Scope, fake_clock: FakeClock
) -> None:
    probe = CountingProbe(True)
    _probed(reg, probe)
    assert _available(reg, scope) and _available(reg, scope)
    assert probe.calls == 1
    fake_clock.advance(29.9)
    assert _available(reg, scope) and probe.calls == 1
    fake_clock.advance(0.2)
    assert _available(reg, scope) and probe.calls == 2


def test_probe_flake_inside_grace_returns_true_uncached(
    reg: ToolRegistry, scope: Scope, fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    probe = CountingProbe(True, False, False, False)
    _probed(reg, probe)
    assert _available(reg, scope)
    fake_clock.advance(31)  # verdict expired, last success 31 s ago
    with caplog.at_level(logging.WARNING):
        assert _available(reg, scope)
    assert "flake" in caplog.text
    assert _available(reg, scope)  # not cached: the probe runs again
    assert probe.calls == 3


def test_probe_failure_after_grace_is_honoured(
    reg: ToolRegistry, scope: Scope, fake_clock: FakeClock
) -> None:
    probe = CountingProbe(True, False)
    _probed(reg, probe)
    assert _available(reg, scope)
    fake_clock.advance(61)
    assert not _available(reg, scope)
    calls = probe.calls
    assert not _available(reg, scope)  # the "no" is cached now
    assert probe.calls == calls


def test_probe_never_true_is_unavailable(reg: ToolRegistry, scope: Scope) -> None:
    _probed(reg, CountingProbe(False))
    assert not _available(reg, scope)


def test_raising_probe_unavailable_and_warns_once(
    reg: ToolRegistry, scope: Scope, fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    probe = CountingProbe(RuntimeError("socket gone"))
    _probed(reg, probe)
    with caplog.at_level(logging.WARNING, logger="indus.tools.registry"):
        assert not _available(reg, scope)
        fake_clock.advance(31)
        assert not _available(reg, scope)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "RuntimeError" in warnings[0].getMessage()
    assert probe.calls == 2


def test_two_profiles_cache_separately(
    reg: ToolRegistry, scope: Scope, tmp_path: Path, fake_clock: FakeClock
) -> None:
    other = other_scope(tmp_path, "other", fake_clock)
    seen: list[str] = []

    def probe(s: Scope) -> bool:
        seen.append(s.profile_id)
        return s.profile_id == "work"

    reg.register("gated", "demo", schema(), echo, probe=probe)
    assert _available(reg, scope)
    assert not _available(reg, other)
    assert _available(reg, scope) and not _available(reg, other)
    assert seen == ["work", "other"]


def test_probe_reads_secrets_through_the_scope(reg: ToolRegistry, scope: Scope) -> None:
    def probe(s: Scope) -> bool:
        return s.secrets is not None and s.secrets.get("DEMO_KEY") is not None

    reg.register("gated", "demo", schema(), echo, probe=probe)
    assert not _available(reg, scope)
    reg.invalidate_probes()
    assert scope.secrets is not None
    scope.secrets.set("DEMO_KEY", "value-" + "1234")  # type: ignore[attr-defined]
    assert _available(reg, scope)


def test_unscoped_probe_unavailable_and_not_cached(
    reg: ToolRegistry, scope: Scope, caplog: pytest.LogCaptureFixture
) -> None:
    probe = CountingProbe(True)
    _probed(reg, probe)
    with caplog.at_level(logging.DEBUG, logger="indus.tools.registry"):
        assert not _available(reg, None)
    assert probe.calls == 0
    assert len(reg.probes) == 0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert _available(reg, scope)  # a scoped call right after is not poisoned


def test_probe_that_loses_its_scope_is_not_cached(reg: ToolRegistry, scope: Scope) -> None:
    probe = CountingProbe(ScopeUnresolved("lost"), True)
    _probed(reg, probe)
    assert not _available(reg, scope)
    assert len(reg.probes) == 0
    assert _available(reg, scope)


def test_one_probe_run_per_pass_with_memo(reg: ToolRegistry, scope: Scope) -> None:
    probe = CountingProbe(True)
    reg.register("one", "demo", schema(), echo, probe=probe)
    reg.register("two", "demo", schema(), echo, probe=probe)
    reg.invalidate_probes()
    memo: dict[object, bool] = {}
    for entry in reg.entries(scope):
        assert reg.available(entry, scope, memo)  # type: ignore[arg-type]
    assert probe.calls == 1


def test_probe_cache_is_bounded(reg: ToolRegistry, scope: Scope) -> None:
    probes = [CountingProbe(True) for _ in range(PROBE_CACHE_MAX + 20)]
    for probe in probes:
        assert reg.probes.check(probe, scope)
    assert len(reg.probes) <= PROBE_CACHE_MAX


# -- built-in list ------------------------------------------------------------------------------


def _fake_module(monkeypatch: pytest.MonkeyPatch, name: str, body: str | None) -> None:
    module = types.ModuleType(name)
    if body is not None:

        def register_tools(registry: ToolRegistry) -> None:
            registry.register(body, "demo", schema(), echo)

        module.register_tools = register_tools  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, module)


def test_builtin_list_is_explicit() -> None:
    assert all(isinstance(m, BuiltinModule) for m in BUILTIN_MODULES)


def test_builtins_load_and_optional_failure_skipped(
    reg: ToolRegistry, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _fake_module(monkeypatch, "fake_tools_good", "good_tool")
    _fake_module(monkeypatch, "fake_tools_no_hook", None)
    modules = (
        BuiltinModule("fake_tools_good"),
        BuiltinModule("fake_tools_missing_entirely"),
        BuiltinModule("fake_tools_no_hook"),
    )
    with caplog.at_level(logging.WARNING, logger="indus.tools.builtin"):
        loaded = load_builtins(reg, modules)
    assert loaded == ["fake_tools_good"]
    assert reg.names() == ["good_tool"]
    assert "fake_tools_missing_entirely" in caplog.text
    assert "fake_tools_no_hook" in caplog.text


def test_required_builtin_failure_raises(reg: ToolRegistry) -> None:
    with pytest.raises(ModuleNotFoundError):
        load_builtins(reg, (BuiltinModule("fake_tools_required_missing", optional=False),))
