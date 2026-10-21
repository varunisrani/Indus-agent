"""P25-07: the product scenarios — one file per flow, checked before anything runs them."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.parity import runner, scenarios

FLOWS = [f.flow for f in runner.FLOWS]
BROWSER = [f.flow for f in runner.FLOWS if f.driver == "browser"]


def test_one_file_per_flow_and_nothing_else() -> None:
    files = sorted(p.name for p in scenarios.SCENARIO_DIR.glob(f"*{scenarios.SCENARIO_SUFFIX}"))
    assert files == sorted(f"{flow}{scenarios.SCENARIO_SUFFIX}" for flow in FLOWS)
    assert len(files) == 12


@pytest.mark.parametrize("flow", FLOWS)
def test_scenario_is_valid(flow: str) -> None:
    data = scenarios.load_scenario(flow)
    assert runner.scenario_problems(flow, data) == []


@pytest.mark.parametrize("flow", FLOWS)
def test_step_ids_are_unique_and_every_step_has_a_timeout(flow: str) -> None:
    data = scenarios.load_scenario(flow)
    ids = scenarios.step_ids(data)
    assert len(ids) == len(set(ids))
    for step in scenarios.steps_of(data):
        assert isinstance(step["timeout_ms"], int) and step["timeout_ms"] > 0, step["step_id"]
        assert scenarios.STEP_ID.match(step["step_id"])


@pytest.mark.parametrize("flow", BROWSER)
def test_browser_locators_use_our_hooks_and_resolve(flow: str) -> None:
    data = scenarios.load_scenario(flow)
    surface = runner.FLOW_BY_NAME[flow].surface
    for step in scenarios.steps_of(data):
        if "locator" in step:
            assert scenarios.hook_of(step["locator"]) is not None, step["step_id"]
    assert scenarios.hook_problems(surface, data) == []


def test_owed_hooks_are_still_owed_and_used() -> None:
    used: set[str] = set()
    for flow in BROWSER:
        for step in scenarios.steps_of(scenarios.load_scenario(flow)):
            hook = scenarios.hook_of(step.get("locator") or {})
            if hook is not None:
                used.add(hook.value)
    assert set(scenarios.OWED_HOOKS) <= used  # nothing listed that no scenario needs
    for value, reason in scenarios.OWED_HOOKS.items():
        assert reason.strip()
        hook = scenarios.Hook("data-ui", value, False)
        roots = [scenarios.ROOT / p for p in scenarios.APP_SOURCES["desktop"]]
        assert not scenarios.hook_in_source(hook, roots), f"{value} exists: drop it from OWED_HOOKS"


def test_browser_files_share_one_environment_and_capture_block() -> None:
    blocks = {
        flow: (
            scenarios.load_scenario(flow)["environment"],
            scenarios.load_scenario(flow)["capture"],
        )
        for flow in BROWSER
    }
    first = next(iter(blocks.values()))
    assert all(b == first for b in blocks.values())
    environment, capture = first
    assert environment == runner.SHARED_ENVIRONMENT
    assert environment["locale"] == "en-US" and environment["timezone"] == "UTC"
    assert environment["reduced_motion"] == "reduce"
    assert {"url", "accessibility", "screenshot"} <= set(capture["after_each_step"])
    assert {"console", "page-errors", "network"} <= set(capture["events"])


@pytest.mark.parametrize("flow", BROWSER)
def test_browser_files_name_the_canonical_origin(flow: str) -> None:
    data = scenarios.load_scenario(flow)
    assert data["start_url"]["url"].startswith(runner.CANONICAL_ORIGIN + "/")


# -- the checks catch what they should -----------------------------------------------------------


def _browser() -> dict[str, Any]:
    return copy.deepcopy(scenarios.load_scenario("dashboard-sessions"))


def _terminal() -> dict[str, Any]:
    return copy.deepcopy(scenarios.load_scenario("cli-chat-approve"))


def test_duplicate_ids_and_missing_timeouts_are_refused() -> None:
    data = _terminal()
    data["actions"][1]["step_id"] = data["actions"][0]["step_id"]
    del data["actions"][2]["timeout_ms"]
    problems = runner.scenario_problems("cli-chat-approve", data)
    assert any("duplicate step_id" in p for p in problems)
    assert any("timeout_ms" in p for p in problems)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda d: d.update(allowed_origins=["x"]), "unknown key 'allowed_origins'"),
        (lambda d: d["actions"][0].update(action="hover"), "unknown action 'hover'"),
        (lambda d: d["actions"][0].update(state="shown"), "state must be"),
        (lambda d: d["actions"][0]["locator"].update(kind="xpath"), "locator kind"),
        (lambda d: d["actions"][0].update(step_id="1-bad"), "step_id must match"),
        (lambda d: d["start_url"].update(url="file:///etc/passwd"), "http(s) URL"),
        (lambda d: d["capture"]["events"].append("video"), "unknown entry 'video'"),
        (lambda d: d.pop("browser"), "missing 'browser'"),
    ],
)
def test_rea_contract_violations_are_refused(change: Any, expected: str) -> None:
    data = _browser()
    change(data)
    problems = scenarios.rea_browser_problems(data)
    assert any(expected in p for p in problems), problems


def test_browser_steps_wait_for_states_not_time_and_use_hooks() -> None:
    data = _browser()
    data["actions"].append({"step_id": "pause", "action": "wait_for_timeout", "duration_ms": 500})
    data["actions"][0]["locator"] = {"kind": "role", "role": "button", "name": "Send"}
    problems = runner.scenario_problems("dashboard-sessions", data)
    assert any("states, not time" in p for p in problems)
    assert any("does not use a" in p for p in problems)


def test_an_unknown_hook_fails_until_it_exists_or_is_owed() -> None:
    data = _browser()
    data["actions"][0]["locator"] = {"kind": "css", "selector": '[data-ui="no-such-hook"]'}
    problems = scenarios.hook_problems("dashboard", data)
    assert problems == ["wait-shell: hook data-ui='no-such-hook' is not in the dashboard source"]


def test_secrets_are_declared_by_id() -> None:
    data = _browser()
    data["actions"].append(
        {
            "step_id": "fill-code",
            "action": "fill",
            "locator": {"kind": "css", "selector": '[data-testid="sidebar"] input'},
            "value": {"source": "secret", "secret_id": "pairing-code"},
            "timeout_ms": 1000,
        }
    )
    assert scenarios.secret_problems(data) == ["secret 'pairing-code' is used but not declared"]
    data["secrets"] = [{"secret_id": "pairing-code", "environment_variable": "PARITY_CODE"}]
    assert scenarios.secret_problems(data) == []
    assert scenarios.rea_browser_problems(data) == []


def test_terminal_steps_use_known_actions_keys_and_locators() -> None:
    data = _terminal()
    data["actions"][0]["argv"] = ["python", "-c", "print()"]
    data["actions"][1]["locator"] = {"kind": "css", "selector": "x"}
    data["actions"][3]["key"] = "Hyper+Q"
    data["actions"].append(
        {"step_id": "inbound", "action": "inbound", "timeout_ms": 1, "message": {}}
    )
    problems = scenarios.terminal_problems(data)
    assert any("starts with 'indus'" in p for p in problems)
    assert any("locator kind must be one of" in p for p in problems)
    assert any("unknown key" in p for p in problems)
    assert any("unknown action 'inbound' for the terminal driver" in p for p in problems)


def test_files_parse_as_plain_json(tmp_path: Path) -> None:
    for flow in FLOWS:
        text = scenarios.scenario_path(flow).read_text(encoding="utf-8")
        assert json.loads(text)
        assert "${model_base_url}" not in text or flow == "first-run-setup"
