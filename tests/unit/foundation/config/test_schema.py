"""Config schema, defaults and the commented example (P01-09)."""

from __future__ import annotations

from typing import Any

import pytest

from indus.foundation import yamlio
from indus.foundation.config import schema


def test_decided_defaults() -> None:
    d = schema.DEFAULTS
    assert d["approvals"]["mode"] == "manual"  # D-16.53
    assert d["agent"]["max_turns"] == 150  # D-16.62
    assert d["_config_version"] == 1 == schema.CURRENT_VERSION  # D-16.3
    assert d["agent"] == {
        "max_turns": 150,
        "api_max_retries": 3,
        "gateway_timeout": 1800,
        "clarify_timeout": 3600,
        "local_stream_stale_timeout": 900,
        "turn_liveness_s": 600.0,  # P08-16
        "run_budget_s": 0.0,  # P08-16: off
    }
    # Container backend rows (``docker_*``, ``container_*``) are pinned by the P11 docker tests.
    core_terminal = {
        k: v for k, v in d["terminal"].items() if not k.startswith(("docker_", "container_"))
    }
    assert core_terminal == {
        "backend": "local",
        "cwd": ".",
        "timeout": 180,
        "env_passthrough": [],
        "persistent_shell": True,
        "temp_dir": "",
        "lifetime_s": 300,
        "degraded_mode": "warn",
        "daemon_term_grace_s": 2.0,
        "max_foreground_timeout": 600,
        "process_wait_auto_notify": True,
        "ssh_host": "",
        "ssh_user": "",
        "ssh_port": 22,
        "ssh_key": "",
        "ssh_persistent_shell": True,
        "credential_files": [],
    }
    assert d["tool_output"] == {"max_chars": 50000}
    # P08 (context and cost): the union of the three wave-1 slices
    aux_task = {"provider": "auto", "model": "", "base_url": "", "reasoning_effort": "",
                "timeout_s": None, "fallback": []}  # fmt: skip
    assert d["auxiliary"] == {"compression": aux_task, "title": aux_task, "approval": aux_task}
    assert d["prompt_caching"] == {"cache_ttl": "5m"}
    assert d["compression"] == {
        "enabled": True,
        "threshold": 0.5,
        "threshold_tokens": None,
        "model_thresholds": {},
        "max_attempts": 3,
        "timeout_s": 120.0,
        "tail_mode": "lean",
        "target_ratio": 0.2,
        "min_tail_user_messages": 1,
        "in_place": True,
        "abort_on_summary_failure": False,
        "idle_after_s": 0,
    }
    assert d["context"] == {"engine": "builtin"}
    assert d["approvals"] == {
        "mode": "manual",
        "timeout": 300,
        "deny": [],
        "cron_mode": "deny",
        "single_query_mode": "deny",
        "unattended_mode": "deny",
    }
    assert d["command_allowlist"] == []
    assert d["file_read_max_chars"] == 100000
    assert d["terminal"]["env_passthrough"] == []
    assert d["database"]["journal_mode"] == "wal"
    assert d["sessions"] == {
        "retention_days": 90,
        "auto_prune": False,
        "resume_message_limit": 20000,
        "search_slow_ms": 1000,
    }
    assert d["logging"] == {"level": "INFO", "max_size_mb": 5, "backup_count": 3}
    assert d["security"] == {
        "redact_secrets": True,
        "secret_store": "auto",
        "allow_private_urls": False,
        "fake_ip_ranges": [],
        "website_blocklist": {"enabled": False, "domains": [], "shared_files": []},
        "protected_instruction_files": [],
        "allow_data_training_tiers_noninteractive": False,
        "acked_advisories": [],
    }
    assert d["web"] == {
        "backend": "",
        "search_backend": "",
        "extract_backend": "",
        "keyless_rescue": True,
        "searxng_url": "",
    }
    assert d["display"] == {
        "language": "en",
        "interface": "cli",
        "busy_input_mode": "interrupt",
        "final_response_markdown": "strip",
        "resume_display": True,
        "resume_exchanges": 10,
        "resume_max_user_chars": 300,
        "resume_max_assistant_chars": 200,
        "resume_max_assistant_lines": 3,
        "cli_refresh_interval": 1.0,
        "cli_multiline_shortcuts": True,
        "show_reasoning": False,
        "bell_on_prompt": True,
    }
    assert d["onboarding"] == {"seen": {"busy_input": False}, "setup_completed": False}
    assert d["fallback_providers"] == []
    assert d["credential_pool_strategies"] == {}
    assert d["platform_toolsets"] == {}
    assert d["doctor"] == {"live_probe_timeout_s": 10.0}
    assert d["session"] == {"terminal_continue": True}
    assert d["quick_commands"] == {}
    assert d["network"] == {"force_ipv4": False}
    assert d["timezone"] == ""
    assert d["model"]["default"] == "" and d["model"]["context_length"] is None
    assert d["providers"] == {}
    assert d["prompt"] == {
        "task_completion_guidance": True,
        "parallel_tool_guidance": True,
        "tool_use_enforcement": "auto",
        "execution_guidance": "auto",
        "context_file_max_chars": None,
        "context_file_read_timeout": 5.0,
        "platform_hints": {},
    }


def test_defaults_are_read_only_and_tree_is_a_copy() -> None:
    with pytest.raises(TypeError):
        schema.DEFAULTS["agent"] = {}  # type: ignore[index]
    tree = schema.defaults_tree()
    tree["approvals"]["deny"].append("rm")
    assert schema.DEFAULTS["approvals"]["deny"] == []
    assert schema.defaults_tree()["approvals"]["deny"] == []


def test_every_default_validates() -> None:
    assert schema.validate(schema.defaults_tree()) == []
    for name, section in schema.DEFAULTS.items():
        assert schema.validate({name: section}) == []


def test_unknown_section_survives_validate_and_dump() -> None:
    data: dict[str, Any] = schema.defaults_tree()
    data["kanban"] = {"lanes": ["todo", "done"], "wip": 3}
    assert schema.validate(data) == []
    reread = yamlio.load_safe(yamlio.dump_plain(data))
    assert reread["kanban"] == {"lanes": ["todo", "done"], "wip": 3}
    assert schema.ConfigModel.model_validate(data).model_dump(by_alias=True)["kanban"] == {
        "lanes": ["todo", "done"],
        "wip": 3,
    }


def test_unknown_key_in_known_section_is_warned_and_kept() -> None:
    data = {"approvals": {"mode": "manual", "colour": "red"}}
    warnings = schema.validate(data)
    assert warnings == ["approvals.colour: unknown setting (kept)"]
    dumped = schema.ConfigModel.model_validate(data).model_dump(by_alias=True)
    assert dumped["approvals"]["colour"] == "red"


def test_type_errors_are_warnings() -> None:
    warnings = schema.validate({"agent": {"max_turns": "lots"}, "approvals": {"mode": "maybe"}})
    assert any(w.startswith("agent.max_turns") for w in warnings)
    assert any(w.startswith("approvals.mode") for w in warnings)


def test_model_shorthand_validates() -> None:
    assert schema.validate({"model": "test-model"}) == []
    assert schema.validate({"model": {"default": "x", "provider": "y"}}) == []


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("approvals.mode", str),
        ("approvals.deny", list),
        ("approvals.timeout", int),
        ("approvals", dict),
        ("providers", dict),
        ("providers.local.base_url", None),
        ("agent.max_turns", int),
        ("sessions.auto_prune", bool),
        ("security.secret_store", str),
        ("database.pragmas", dict),
        ("model", str),
        ("model.default", str),
        ("model.context_length", int),
        ("timezone", str),
        ("_config_version", int),
        ("nonsense", None),
        ("agent.nonsense", None),
    ],
)
def test_schema_for(key: str, expected: type | None) -> None:
    assert schema.schema_for(key) is expected


def test_default_for() -> None:
    assert schema.default_for("approvals.mode") == "manual"
    assert schema.default_for("approvals.nope", "fallback") == "fallback"
    deny = schema.default_for("approvals.deny")
    deny.append("x")
    assert schema.DEFAULTS["approvals"]["deny"] == []


def _uncomment(example: str) -> str:
    body: list[str] = []
    started = False
    for line in example.splitlines():
        if not started:
            started = line == ""
            continue
        if line == "" or line.startswith("# --"):
            continue
        assert line.startswith("#"), line
        body.append(line[2:] if line.startswith("# ") else "")
    return "\n".join(body) + "\n"


def test_example_parses_to_nothing_and_uncomments_to_defaults() -> None:
    example = schema.render_example()
    assert yamlio.load_safe(example) is None  # no uncommented leaf at all
    expected = schema.defaults_tree()
    del expected["_config_version"]
    assert yamlio.load_safe(_uncomment(example)) == expected
    order = [k for k in expected]
    positions = [example.index(f"# {k}:") for k in order]
    assert positions == sorted(positions)
