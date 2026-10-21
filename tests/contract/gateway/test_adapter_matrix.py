"""The adapter matrix (P16-36, the M5 criterion): every registered platform runs the shared
contract suite over its own wire fake, or is a named non-chat surface with its own contract tests.

``matrix_problems`` compares the registry's built-in platforms with the contract cases: which
platform each case drives (its adapter's ``platform``, so variant cases such as ``feishu_webhook``
count for ``feishu``), which wire fake serves it (its env's wire object must come from a
``tests/fakes/platform/<name>_wire.py`` file that exists), and which case modules failed to import
(``conftest.CASE_IMPORT_ERRORS``: a removed wire fake shows up here). Every problem names the
platform.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from indus.gateway.registry import PlatformRegistry
from tests.contract.gateway.conftest import (
    ADAPTER_CASES,
    CASE_IMPORT_ERRORS,
    CLOCK_START,
    AdapterCase,
)
from tests.contract.gateway.test_adapter_contract import CASES
from tests.fakes.clock import FakeClock

ROOT = Path(__file__).resolve().parents[3]
WIRE_DIR = ROOT / "tests" / "fakes" / "platform"
WIRE_PACKAGE = "tests.fakes.platform."

# Surfaces that cannot send a chat reply, so the shared chat suite does not fit them: each has its
# own contract tests (and, where it talks to a vendor, its own fake), listed here.
NON_CHAT: Mapping[str, tuple[str, tuple[str, ...]]] = {
    "api_server": (
        "an HTTP API for programs, not a chat",
        (
            "tests/contract/gateway/test_api_server_openai.py",
            "tests/contract/gateway/test_api_server_runs.py",
        ),
    ),
    "webhook": (
        "signed inbound events only; replies go to other platforms",
        ("tests/contract/gateway/test_webhook.py",),
    ),
    "msgraph": (
        "change notifications become internal events; it cannot send",
        (
            "tests/unit/gateway/adapters/msgraph/test_adapter.py",
            "tests/fakes/platform/msgraph_wire.py",
        ),
    ),
    "raft": (
        "a wake bridge: the agent answers through Raft's own CLI, replies here go nowhere",
        ("tests/unit/gateway/adapters/raft/test_adapter.py",),
    ),
    "a2a": (
        "a JSON-RPC task server for peer agents: answers are task results, not chat sends",
        ("tests/unit/gateway/adapters/a2a/test_adapter.py",),
    ),
}

# Platforms being built in the current wave, allowed without a case until they land.
# Empty since P16-25 (Photon) landed with its case; a wave that builds a platform in parallel adds
# it here with a reason, and the integrator removes it when the case lands.
PENDING: Mapping[str, str] = {}


@dataclass(frozen=True)
class CaseFacts:
    """What one contract case drives: its platform and the module of its wire fake (or None)."""

    case: str
    platform: str
    wire_module: str | None


def facts_of(case: AdapterCase) -> CaseFacts:
    env = case.make_env(FakeClock(CLOCK_START))
    adapter = case.build(env)
    wire = env.wire
    module = type(wire).__module__ if wire is not None else None
    return CaseFacts(case.name, str(type(adapter).platform), module)


def _registered_label(platform: str) -> str:
    return f"platform {platform}"


def matrix_problems(
    registered: Iterable[str],
    facts: Iterable[CaseFacts],
    *,
    import_errors: Mapping[str, str],
    wire_dir: Path = WIRE_DIR,
    root: Path = ROOT,
    non_chat: Mapping[str, tuple[str, tuple[str, ...]]] = NON_CHAT,
    pending: Mapping[str, str] = PENDING,
) -> list[str]:
    """Every gap between the registry and the contract runs, each naming its platform."""
    names = list(dict.fromkeys(registered))
    known = set(names)
    cases = list(facts)
    problems: list[str] = []
    for module, error in sorted(import_errors.items()):
        platform = module.removesuffix("_case")
        problems.append(
            f"{_registered_label(platform)}: its contract case module {module} did not import "
            f"({error}); is its wire fake missing?"
        )
    for fact in cases:
        label = _registered_label(fact.platform)
        if fact.platform not in known:
            problems.append(
                f"{label}: case {fact.case} drives a platform the registry does not know"
            )
        module = fact.wire_module
        if module is None or not module.startswith(WIRE_PACKAGE) or not module.endswith("_wire"):
            problems.append(f"{label}: case {fact.case} runs without a wire fake ({module})")
            continue
        file = wire_dir / f"{module.removeprefix(WIRE_PACKAGE)}.py"
        if not file.is_file():
            problems.append(f"{label}: case {fact.case} needs wire fake {file.name}, which is gone")
    covered = {fact.platform for fact in cases}
    for name in names:
        label = _registered_label(name)
        if name in non_chat:
            if name in covered:
                problems.append(f"{label} is listed as non-chat but also runs the chat suite")
            for path in non_chat[name][1]:
                found = root / path
                if not found.is_file():
                    problems.append(f"{label}: its own contract file {path} is missing")
                elif "/fakes/" not in path and "def test_" not in found.read_text("utf-8"):
                    problems.append(f"{label}: {path} holds no tests")
            continue
        if name in pending:
            if name in covered:
                problems.append(
                    f"{label} has its contract case now: remove it from PENDING in "
                    "test_adapter_matrix.py"
                )
            continue
        if name not in covered:
            why = import_errors.get(f"{name}_case")
            detail = f" ({name}_case did not import: {why})" if why else ""
            problems.append(
                f"{label} has no contract run over a wire fake: add "
                f"tests/fakes/platform/{name}_wire.py and tests/contract/gateway/{name}_case.py"
                f"{detail}"
            )
    for name in non_chat:
        if name not in known:
            problems.append(f"{_registered_label(name)} is listed as non-chat but not registered")
    return problems


def _builtins() -> list[str]:
    registry = PlatformRegistry()
    return [e.name for e in registry.entries() if registry.is_builtin(e.name)]


_FACTS: list[CaseFacts] | None = None


def real_facts() -> list[CaseFacts]:
    global _FACTS
    if _FACTS is None:
        _FACTS = [facts_of(case) for case in ADAPTER_CASES]
    return _FACTS


# -- the gate --------------------------------------------------------------------------------------


def test_every_registered_platform_runs_the_contract_suite() -> None:
    problems = matrix_problems(_builtins(), real_facts(), import_errors=dict(CASE_IMPORT_ERRORS))
    assert not problems, "adapter matrix:\n" + "\n".join(problems)


GATE = test_every_registered_platform_runs_the_contract_suite


def test_every_case_is_in_the_contract_parametrisation() -> None:
    ran = {case.name for case in CASES}
    assert {case.name for case in ADAPTER_CASES} <= ran
    assert len(ADAPTER_CASES) >= 25  # telegram, discord, slack and the P16 adapters


def test_non_chat_surfaces_are_really_not_chat_platforms() -> None:
    registry = PlatformRegistry()
    for name in NON_CHAT:
        entry = registry.get(name)
        assert entry is not None, name
        # A platform people chat on must run the shared suite: only trusted machine surfaces
        # or broadcast bridges (DMs not private) may stand aside.
        assert entry.trusted_inbound or not entry.dm_is_private, name


# -- the gate bites --------------------------------------------------------------------------------


def _without(platform: str) -> list[CaseFacts]:
    return [fact for fact in real_facts() if fact.platform != platform]


def test_a_removed_wire_fake_fails_with_the_platform_named(tmp_path: Path) -> None:
    wires = tmp_path / "platform"
    wires.mkdir()
    for path in WIRE_DIR.glob("*_wire.py"):
        if path.name != "line_wire.py":
            (wires / path.name).write_text("")
    # The case module still imported (cached) but its wire file is gone.
    problems = matrix_problems(_builtins(), real_facts(), import_errors={}, wire_dir=wires)
    assert problems == ["platform line: case line needs wire fake line_wire.py, which is gone"]
    # The usual shape: the case module cannot import its wire fake, so no case runs at all.
    error = "ModuleNotFoundError: No module named 'tests.fakes.platform.line_wire'"
    problems = matrix_problems(
        _builtins(), _without("line"), import_errors={"line_case": error}, wire_dir=wires
    )
    assert len(problems) == 2 and all(p.startswith("platform line") for p in problems)
    assert "line_case did not import" in problems[1]


def test_removing_a_wire_fake_fails_the_real_gate_with_the_platform_named() -> None:
    """End to end: a pytest run in which ``line_wire`` cannot be imported."""
    target = f"{Path(__file__).relative_to(ROOT)}::{GATE.__name__}"
    script = (
        "import sys, pytest\n"
        "sys.modules['tests.fakes.platform.line_wire'] = None\n"
        "raise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider', '-p', 'no:randomly', "
        f"{target!r}]))\n"
    )
    done = subprocess.run(  # noqa: S603 - our own interpreter and a fixed script
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 1, done.stdout + done.stderr
    assert "platform line: its contract case module line_case did not import" in done.stdout
    assert "platform line has no contract run" in done.stdout


def test_a_registered_platform_without_a_case_is_named() -> None:
    problems = matrix_problems([*_builtins(), "carrier_pigeon"], real_facts(), import_errors={})
    assert problems == [
        "platform carrier_pigeon has no contract run over a wire fake: add "
        "tests/fakes/platform/carrier_pigeon_wire.py and "
        "tests/contract/gateway/carrier_pigeon_case.py"
    ]


def test_a_case_without_a_wire_fake_or_platform_is_named() -> None:
    facts = [
        *real_facts(),
        CaseFacts("homemade", "telegram", "tests.unit.somewhere"),
        CaseFacts("ghost", "ghostchat", "tests.fakes.platform.telegram_wire"),
    ]
    problems = matrix_problems(_builtins(), facts, import_errors={})
    assert problems == [
        "platform telegram: case homemade runs without a wire fake (tests.unit.somewhere)",
        "platform ghostchat: case ghost drives a platform the registry does not know",
    ]


def test_pending_and_non_chat_entries_stay_honest(tmp_path: Path) -> None:
    pending = {"newchat": "built in parallel"}
    assert matrix_problems(_builtins(), real_facts(), import_errors={}) == []
    # a pending platform may run without a case ...
    waiting = matrix_problems(
        [*_builtins(), "newchat"], real_facts(), import_errors={}, pending=pending
    )
    assert waiting == []
    # ... and is named once its case is there
    facts = [*real_facts(), CaseFacts("newchat", "newchat", "tests.fakes.platform.telegram_wire")]
    stale = matrix_problems([*_builtins(), "newchat"], facts, import_errors={}, pending=pending)
    assert stale == [
        "platform newchat has its contract case now: remove it from PENDING in "
        "test_adapter_matrix.py"
    ]
    missing = matrix_problems(_builtins(), real_facts(), import_errors={}, root=tmp_path)
    assert all(
        p.startswith(
            (
                "platform api_server",
                "platform webhook",
                "platform msgraph",
                "platform raft",
                "platform a2a",
            )
        )
        for p in missing
    )
    assert len(missing) == sum(len(files) for _, files in NON_CHAT.values())


@pytest.mark.parametrize("platform", sorted(NON_CHAT))
def test_non_chat_surfaces_name_why(platform: str) -> None:
    reason, files = NON_CHAT[platform]
    assert reason and files
