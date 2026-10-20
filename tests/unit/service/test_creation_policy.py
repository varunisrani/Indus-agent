"""P17-09: the frozen creation policy (freeze, resume checks, bypass rule, secrets, env)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import pytest

from indus.foundation.errors import ERROR_CODES
from indus.foundation.scope import Scope
from indus.service.creation_policy import (
    FROZEN_FIELDS,
    LAUNCH_ENV,
    LAUNCH_KEY,
    CreationPolicy,
    PolicyConflict,
    PolicyRefused,
    check_launch,
    check_resume,
    launch_flags_from_env,
    policy_patch,
    require_same,
    scrub_launch_env,
    stored_policy,
)
from indus.state import registry, sessions
from indus.state.registry import StoreHandle

CWD = os.path.abspath(os.sep + os.path.join("work", "repo"))


def launch(**values: Any) -> CreationPolicy:
    return CreationPolicy.from_launch(values)


def fake_key() -> str:
    return "sk-" + "q" * 30  # built at runtime: no credential literal in the tree


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()


# -- resume checks --------------------------------------------------------------------------------


def test_resume_with_same_values_is_accepted() -> None:
    frozen = launch(cwd=CWD, model="m1", provider="p1", toolsets="web,files", max_turns=0)
    assert check_resume(frozen, launch(model="m1")) is None
    assert check_resume(frozen, launch(toolsets=["files", "web"], max_turns=0)) is None
    assert check_resume(frozen, launch(cwd=CWD + os.sep + "." + os.sep)) is None
    assert check_resume(frozen, CreationPolicy()) is None  # naming nothing is always fine
    require_same(frozen, None)


def test_resume_with_a_different_model_is_a_policy_conflict() -> None:
    frozen = launch(model="m1", provider="p1")
    conflict = check_resume(frozen, launch(model="m2"))
    assert isinstance(conflict, PolicyConflict)
    assert conflict.field == "model" and conflict.fields == ("model",)
    assert conflict.code == "runtime.policy_conflict" and conflict.code in ERROR_CODES
    assert conflict.to_data() == {
        "reason": "policy_conflict",
        "field": "model",
        "fields": ["model"],
    }
    with pytest.raises(PolicyConflict) as info:
        require_same(frozen, launch(model="m2", provider="p2", yolo=True))
    assert info.value.fields == ("model", "provider", "yolo")


def test_every_frozen_field_conflicts() -> None:
    frozen = CreationPolicy()
    other: dict[str, Any] = {
        "cwd": CWD,
        "model": "m",
        "provider": "p",
        "reasoning": "high",
        "toolsets": ["web"],
        "max_turns": 3,
        "skills": ["plan"],
        "checkpoints": True,
        "yolo": True,
        "accept_hooks": True,
        "ignore_rules": True,
        "base_url": "https://models.example.test/v1",
        "key_ref": "EXAMPLE_API_KEY",
        "safe_mode": True,
        "ignore_user_config": True,
        "pass_session_id": True,
    }
    assert set(other) == set(FROZEN_FIELDS)
    for name, value in other.items():
        conflict = check_resume(frozen, launch(**{name: value}))
        assert conflict is not None and conflict.fields == (name,), name


def test_nothing_frozen_means_defaults_so_an_explicit_value_differs() -> None:
    with pytest.raises(PolicyConflict):
        require_same(None, launch(model="m1"))
    require_same(None, CreationPolicy())


def test_an_empty_toolset_selection_is_a_value() -> None:
    frozen = launch(toolsets=[])
    assert frozen.toolsets == ()
    assert check_resume(frozen, launch(toolsets="")) is None
    conflict = check_resume(launch(toolsets=["web"]), launch(toolsets=[]))
    assert conflict is not None and conflict.field == "toolsets"


def test_lists_are_split_stripped_and_deduplicated() -> None:
    policy = launch(toolsets=" web , files,web,", skills=["a,b", " a ", ""])
    assert policy.toolsets == ("web", "files")
    assert policy.skills == ("a", "b")


# -- launch validation ----------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["model", "provider"])
@pytest.mark.parametrize("switch", ["safe_mode", "ignore_user_config"])
def test_a_bypass_launch_must_name_model_and_provider(missing: str, switch: str) -> None:
    values: dict[str, Any] = {"model": "m1", "provider": "p1", switch: True}
    del values[missing]
    policy = launch(**values)
    assert policy.bypass
    with pytest.raises(PolicyRefused) as info:
        check_launch(policy)
    assert info.value.reason == "bypass_needs_route" and info.value.field == missing
    assert info.value.code == "runtime.policy_refused" and info.value.code in ERROR_CODES
    check_launch(launch(model="m1", provider="p1", **{switch: True}))
    check_launch(launch())  # an ordinary launch reads the profile's defaults


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("max_turns", -1),
        ("max_turns", True),
        ("yolo", "yes"),
        ("model", ""),
        ("model", 3),
        ("cwd", "relative/dir"),
        ("base_url", "ftp://models.example.test"),
        ("toolsets", [1, 2]),
    ],
)
def test_malformed_values_are_refused(name: str, value: object) -> None:
    with pytest.raises(PolicyRefused) as info:
        launch(**{name: value})
    assert info.value.field == name and info.value.reason == "invalid_value"


def test_an_unknown_launch_field_is_refused_but_a_stored_one_is_ignored() -> None:
    with pytest.raises(PolicyRefused) as info:
        launch(modle="m1")
    assert info.value.reason == "invalid_field" and info.value.field == "modle"
    stored = CreationPolicy.from_record({"model": "m1", "worktree": "/x", "max_turns": "bad"})
    assert stored == CreationPolicy(model="m1")


def test_max_turns_zero_is_a_value() -> None:
    policy = launch(max_turns=0)
    assert policy.max_turns == 0 and policy.given() == {"max_turns": 0}
    conflict = check_resume(policy, launch(max_turns=5))
    assert conflict is not None and conflict.field == "max_turns"


# -- secrets --------------------------------------------------------------------------------------


def test_api_key_is_never_stored() -> None:
    key = fake_key()
    policy = launch(model="m1", api_key=key, key_ref="EXAMPLE_API_KEY")
    record = policy.record()
    assert "api_key" not in record and key not in json.dumps(record)
    assert record["key_ref"] == "EXAMPLE_API_KEY"
    # A repeated key on resume is not a policy change (it is never compared).
    assert check_resume(policy, launch(api_key=fake_key() + "x")) is None


def test_a_credential_shaped_key_reference_is_refused_without_echoing_it() -> None:
    key = fake_key()
    with pytest.raises(PolicyRefused) as info:
        launch(key_ref=key)
    assert info.value.reason == "secret_in_policy" and info.value.field == "key_ref"
    assert key not in str(info.value) and key not in repr(info.value)


def test_a_base_url_with_user_info_is_refused() -> None:
    secret = "pw" + "-" + "x" * 12
    with pytest.raises(PolicyRefused) as info:
        launch(base_url=f"https://user:{secret}@models.example.test/v1")
    assert info.value.reason == "secret_in_policy" and secret not in str(info.value)
    assert launch(base_url="https://models.example.test/v1").base_url is not None


# -- environment ----------------------------------------------------------------------------------


def test_launch_env_vars_become_creation_flags() -> None:
    env = {
        "INDUS_YOLO_MODE": "1",
        "INDUS_SAFE_MODE": "true",
        "INDUS_IGNORE_RULES": "0",
        "INDUS_ACCEPT_HOOKS": "",
        "PATH": "/bin",
    }
    assert launch_flags_from_env(env) == {"yolo": True, "safe_mode": True}
    assert set(LAUNCH_ENV.values()) <= set(FROZEN_FIELDS)


def test_scrub_removes_only_launch_vars() -> None:
    env = {name: "1" for name in LAUNCH_ENV} | {"PATH": "/bin", "INDUS_HOME": "/h"}
    scrubbed = scrub_launch_env(env)
    assert scrubbed == {"PATH": "/bin", "INDUS_HOME": "/h"}
    assert len(env) == len(LAUNCH_ENV) + 2  # the input is not changed


def test_scrub_also_drops_the_tui_launchers_run_values() -> None:
    env = {
        "INDUS_MODEL": "m1",
        "INDUS_PROVIDER": "p1",
        "INDUS_LAUNCH_CWD": "/w",
        "INDUS_TUI_QUERY": "hello",
        "INDUS_TUI_GRANT_COMMAND": "indus",
        "INDUS_HOME": "/h",
        "INDUS_GATEWAY_LOCK_DIR": "/l",
    }
    assert scrub_launch_env(env) == {"INDUS_HOME": "/h", "INDUS_GATEWAY_LOCK_DIR": "/l"}


# -- storage --------------------------------------------------------------------------------------


def test_record_round_trips_through_the_session_row(store: StoreHandle) -> None:
    policy = launch(
        cwd=CWD, model="m1", provider="p1", toolsets=["web"], skills=["plan"], max_turns=0
    )
    store.write(
        lambda conn: sessions.create_in(
            conn, "s1", "cli", now=1.0, model_config=policy_patch(policy)
        )
    )
    row = sessions.get(store, "s1")
    assert stored_policy(row) == policy
    assert stored_policy(None) is None
    config = sessions.parse_model_config(row["model_config"] if row else None)
    assert set(config) == {LAUNCH_KEY}
    store.write(lambda conn: sessions.create_in(conn, "s2", "cli", now=1.0))
    assert stored_policy(sessions.get(store, "s2")) is None


def test_the_cli_launch_record_reads_as_a_policy() -> None:
    """The in-process CLI stores its record under the same key (extra fields are ignored)."""
    cli_record = {
        "cwd": CWD,
        "model": "m1",
        "provider": None,
        "reasoning": None,
        "toolsets": None,
        "max_turns": None,
        "skills": [],
        "yolo": True,
        "pass_session_id": False,
        "ignore_rules": False,
        "ignore_user_config": False,
        "safe_mode": False,
        "worktree": None,
    }
    policy = CreationPolicy.from_record(cli_record)
    assert policy == CreationPolicy(cwd=CWD, model="m1", yolo=True)
    assert CreationPolicy.from_record({**cli_record, "pass_session_id": True}).pass_session_id
