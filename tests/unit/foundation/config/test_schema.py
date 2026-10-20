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
        "restart_drain_timeout": 0.0,  # P15-25
        "restart_after_turn_timeout": 1800.0,
        "cron_drain_timeout": 30.0,  # P15-21
        "clarify_timeout": 3600,
        "local_stream_stale_timeout": 900,
        "turn_liveness_s": 600.0,  # P08-16
        "run_budget_s": 0.0,  # P08-16: off
        "fast_mode": "normal",  # P08-15
        "fast_window_s": 60.0,  # P08-15
        "reconnect_attention_after": 1800.0,  # P15-29
    }
    # Container backend rows (``docker_*``, ``container_*``) are pinned by the P11 docker tests,
    # the Singularity rows by tests/unit/backends/test_singularity.py (P21-04).
    singularity = ("singularity_", "scratch_dir", "disk_warning_gb")
    core_terminal = {
        k: v
        for k, v in d["terminal"].items()
        if not k.startswith(("docker_", "container_", *singularity))
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
        "modal_mode": "auto",  # P21-05: direct only (D-16.16)
        "modal_image": "",
        "daytona_image": "",  # P21-07
        "vercel_runtime": "",  # P21-08
        "sync_back_max_bytes": 2 * 1024 * 1024 * 1024,  # P21-10
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
    assert d["auxiliary"] == {
        "compression": aux_task,
        "title": aux_task,
        "approval": aux_task,
        "curator": aux_task,  # P09-19
        "review": aux_task,  # P09-15
        "goal_judge": aux_task,  # P10-24
        "side_question": aux_task,  # P10-23
        "vision": aux_task,  # P10-15
    }
    # P09 slice F: the curator
    assert d["curator"] == {
        "enabled": True,
        "interval_hours": 168,  # D-16.24
        "min_idle_hours": 2.0,
        "stale_after_days": 14,
        "archive_after_days": 30,
        "consolidate": False,
        "prune_builtins": False,
        "archive_ttl_days": 0,
        "backup": {"enabled": True, "keep": 2},
    }
    # P10 slice K: mixture of agents, goals, loops
    assert d["moa"] == {
        "presets": {},
        "default_preset": "",
        "reference_timeout": None,
        "privacy_filter": "",
    }
    assert d["goals"] == {"max_turns": 20}
    assert d["loops"] == {"min_interval_seconds": 30, "max_ticks": 100}
    # P13-14/15/17: plugins
    assert d["plugins"] == {
        "enabled": [],
        "disabled": [],
        "project": False,
        "load_timeout_seconds": 10.0,
        "hook_callback_timeout": 30.0,
        "entries": {},
        "clone_timeout_seconds": 300.0,  # P13-21
        "auto_update_check_hours": 24.0,
        "auto_apply": False,
    }
    # P13-19: shell hooks
    assert d["hooks"] == {}
    assert d["hooks_auto_accept"] is False
    # P13-01: the MCP client
    assert d["mcp_servers"] == {}
    assert d["mcp"] == {
        "auto_reload_on_config_change": True,
        "discovery_concurrency": 4,
        "osv_preflight": True,
    }
    # P14 (C1 catch-up, C2 ticker/preflight/scripts/runs/output, C3 delivery and failures)
    assert d["cron"] == {
        "catch_up_missed": True,
        "max_parallel_jobs": 0,
        "preflight": True,
        "script_timeout_seconds": 3600,
        "toolsets": None,
        "allow_agent_scheduling": False,
        "model": "",
        "provider": "",
        "inactivity_timeout_seconds": 600,
        "output_retention": 50,
        "cleanup_timeout_seconds": 10.0,
        "wrap_response": True,
        "local_notify": False,
        "failure_nudge_threshold": 3,
        "retry_unreachable": True,
        "failure_repeat_alert_hours": 6.0,
    }
    # P22-08: research batch runs
    assert d["research"] == {
        "runs_root": "./research-runs",
        "save_trajectories": False,  # P22-10
        "sample_datasets": [],  # P22-15
    }
    # P10-20: desktop control
    assert d["computer_use"] == {"driver": "stdio", "driver_cmd": "", "action_timeout_s": 30.0}
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
    # P09 slice A: memory stores and session search
    assert d["memory"] == {
        "notes_enabled": True,
        "profile_enabled": True,
        "notes_char_limit": 2200,
        "profile_char_limit": 1375,
        "write_approval": False,
        "review_interval": 10,  # P09-14
        "provider": "",  # P13-24
        "provider_recall_spill": False,  # P13-24
    }
    # P09 slice E: the background review
    assert d["skills"]["review_interval"] == 10
    assert d["learning"] == {
        "review": {
            "enabled": True,
            "extra_tools": [],
            "max_input_tokens": None,
            "defer": "auto",
            "defer_max_age_s": 1800.0,
        }
    }
    assert d["session_search"] == {"summarize": False}
    # P09 slice B (+ the skill write gate of P09-03, added at integration)
    assert d["skills"] == {
        "create_dir": "",
        "external_dirs": [],
        "disabled": [],
        "platform_disabled": {},
        "auto_load": [],
        "template_vars": True,
        "inline_shell": False,
        "config": {},
        "scan_agent_created": False,
        "ledger_max_bytes": 5 * 1024 * 1024,
        "write_approval": False,
        "review_interval": 10,  # P09-14
        # P13-25..28: the skills hub and project skills
        "hub": {"search_timeout": 30.0, "well_known": [], "advisory_scanner": ""},
        "trusted_project_dirs": [],
        "project_discovery": True,
    }
    # P10 slice J: media tools
    assert d["vision"] == {"download_timeout": 30.0, "embed_target_kb": 256}
    assert d["image_gen"] == {
        "provider": "",
        "model": "",
        "base_url": "",
        "key_name": "OPENAI_API_KEY",
        "max_parallel_requests": 4,
    }
    assert d["media"] == {"video_provider": "", "video_model": ""}
    assert d["tts"] == {
        "provider": "piper",  # D-16.71
        "voice": "",
        "speed": 1.0,
        "model": "",
        "base_url": "",
        "key_name": "OPENAI_API_KEY",
        "piper_binary": "piper",
        "piper_model": "",
    }
    assert d["stt"] == {
        "provider": "",
        "model": "",
        "base_url": "",
        "language": "",
        "local_model": "base",
        "local_device": "auto",
        "max_file_mb": 25.0,
    }
    assert d["voice"] == {  # P10-19
        "record_key": "ctrl+space",
        "silence_threshold": 300.0,
        "silence_seconds": 2.0,
        "wait_seconds": 10.0,
        "max_seconds": 120.0,
        "read_aloud": False,
    }
    assert d["context"] == {"engine": "builtin"}
    assert d["fallback"] == {"min_switch_reset_s": 0.0}  # P08-13: always switch
    assert d["approvals"] == {
        "mode": "manual",
        "timeout": 300,
        "deny": [],
        "cron_mode": "deny",
        "single_query_mode": "deny",
        "unattended_mode": "deny",
        "denial_breaker_threshold": 3,  # P08-14
        "smart_policy": "",  # P08-14
        "mcp_reload_confirm": True,  # P13-08
        "destructive_slash_confirm": False,  # P11 /rollback, P15-13 chat commands
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
    assert d["browser"] == {
        "inactivity_timeout": 120,
        "command_timeout": 30,
        "snapshot_threshold": 15000,
        "snapshot_summary": False,
        "cdp_url": "",
        "executable_path": "",
        "env_passthrough": [],
        "dialog_policy": "must_respond",
        "dialog_timeout_s": 300,
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
        "learning_notices": "on",  # P09-16
        "busy_ack": True,  # P15-02 (read by P15-12)
        "busy_text_quiet_s": 0.35,
        "busy_text_cap_s": 1.0,
        "ephemeral_system_ttl": 0.0,  # P15-17: notices stay unless set
        "tool_progress_grouping": "accumulate",  # P15-16
        "heartbeat_seconds": 180.0,
    }
    platform = {  # P15-02: enabled unset = follow credentials
        "enabled": None,
        "home_channel": None,
        "reply_to_mode": "first",
        "restart_notice": True,
        "typing_indicator": True,
        "streaming": None,  # P15-16: unset = the platform default
        "tool_progress": None,
        "channel_overrides": {},
        "extra": {},
    }
    assert d["platforms"] == {
        "telegram": platform,
        "discord": platform,
        "slack": platform,
        "signal": platform,  # P16-05
        "matrix": platform,  # P16-06
        "mattermost": platform,  # P16-08
        "email": platform,  # P16-09
        "whatsapp": platform,  # P16-03
        "whatsapp_cloud": platform,  # P16-04
        "sms": platform,  # P16-10
        "teams": platform,  # P16-11
        "msgraph": platform,  # P16-12
        "google_chat": platform,  # P16-13
        "qqbot": platform,  # P16-20
        "dingtalk": platform,  # P16-16
        "wecom": platform,  # P16-17
        "wecom_callback": platform,  # P16-18
        "feishu": platform,  # P16-14
        "bluebubbles": platform,  # P16-24
        "simplex": platform,  # P16-28
        "buzz": platform,  # P16-29
        "weixin": platform,  # P16-19
        "yuanbao": platform,  # P16-21
        "raft": platform,  # P16-30
    }
    assert d["gateway"] == {
        "message_timestamps": {"enabled": False},
        "trust_env": True,  # P15-06
        "group_sessions_per_user": True,  # P15-07
        "thread_sessions_per_user": False,
        "unauthorized_dm_behavior": "pair",  # P15-08
        "bot_loop_guard": {  # P15-10
            "enabled": True,
            "max_messages": 20,
            "window_seconds": 300.0,
            "cooldown_seconds": 600.0,
        },
        "max_concurrent_sessions": None,
        "media_strict": False,  # P15-18
        "media_allow_dirs": [],
        "media_trust_recent": False,  # lead decision: off (no birth time on Linux)
        "media_trust_recent_seconds": 600.0,
        "connect_timeout_seconds": 30.0,  # P15-24
        "systemd_watchdog_seconds": 0.0,
        "idle_exit_seconds": 600.0,  # P17-23
        "delivery_ledger": True,  # P15-19
        "stt_enabled": True,  # P15-23
        "api_server": {  # P15-30, P15-31
            "enabled": False,
            "host": "127.0.0.1",
            "port": 8642,
            "cors_origins": [],
            "max_concurrent_runs": 10,
            "model_name": "indus-agent",
            "idle_close_seconds": 900.0,
        },
        "webhook": {  # P15-32
            "enabled": False,
            "host": "127.0.0.1",
            "port": 8644,
            "rate_limit": 30,
            "max_body_bytes": 1_048_576,
            "routes": {},
        },
    }
    assert d["tui"] == {  # P12-31
        "theme": "auto",
        "indicator": "spinner",
        "details": "collapsed",
        "statusbar": True,
        "mouse": False,
        "tool_progress": "all",
        "confirm_models": [],
    }
    assert d["onboarding"] == {"seen": {"busy_input": False}, "setup_completed": False}
    assert d["fallback_providers"] == []
    assert d["credential_pool_strategies"] == {}
    assert d["platform_toolsets"] == {}
    assert d["doctor"] == {"live_probe_timeout_s": 10.0}
    assert d["delegation"] == {  # P10-01, D-16.24
        "max_concurrent_children": 10,
        "max_depth": 1,
        "max_iterations": 250,
        "max_async_children": 3,
        "summary_max_chars": 24000,
        "oneshot_max_children": 2,
        "child_toolsets": ["terminal", "file", "web"],
        "model": "",
        "inherit_mcp_toolsets": True,
        "worktree": False,
    }
    assert d["session"] == {"terminal_continue": True}
    assert d["code_execution"] == {  # P10-08, P10-10
        "timeout": 300,
        "max_tool_calls": 50,
        "mode": "project",
        "kernel": "auto",
        "kernel_idle_timeout": 1800,
        "max_session_kernels": 4,
    }
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
        ("platforms.telegram.enabled", bool),
        ("platforms.slack.reply_to_mode", str),
        ("platforms.discord.channel_overrides", dict),
        ("gateway.message_timestamps.enabled", bool),
        ("gateway.bot_loop_guard.max_messages", int),
        ("gateway.max_concurrent_sessions", int),
        ("gateway.unauthorized_dm_behavior", str),
        ("gateway.media_allow_dirs", list),
        ("gateway.media_trust_recent", bool),
        ("agent.restart_drain_timeout", float),
        ("gateway.systemd_watchdog_seconds", float),
        ("gateway.idle_exit_seconds", float),
        ("gateway.delivery_ledger", bool),
        ("gateway.api_server.port", int),
        ("gateway.api_server.cors_origins", list),
        ("gateway.webhook.routes", dict),
        ("platforms.telegram.streaming", bool),
        ("platforms.discord.tool_progress", str),
        ("display.ephemeral_system_ttl", float),
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


def test_gateway_rows_validate() -> None:  # P15 wave 1
    ok = {"gateway": {"max_concurrent_sessions": 3, "media_allow_dirs": ["~/out"]}}
    assert schema.validate(ok) == []
    assert schema.validate({"platforms": {"slack": {"tool_progress": "log"}}}) == []
    assert schema.validate({"gateway": {"max_concurrent_sessions": 0}}) != []
    assert schema.validate({"gateway": {"unauthorized_dm_behavior": "shout"}}) != []
    assert schema.validate({"gateway": {"bot_loop_guard": {"window_seconds": 0}}}) != []
    assert schema.validate({"display": {"ephemeral_system_ttl": -1}}) != []
    assert schema.validate({"gateway": {"stt_enabled": False}}) == []  # P15-23
    assert schema.validate({"gateway": {"stt_enabled": "sometimes"}}) != []
    assert schema.validate({"agent": {"reconnect_attention_after": 0}}) == []  # P15-29
    assert schema.validate({"agent": {"reconnect_attention_after": -1}}) != []


def test_gateway_daemon_rows_validate() -> None:  # P15-24, P15-25
    ok = {"agent": {"restart_drain_timeout": 5, "restart_after_turn_timeout": 60}}
    assert schema.validate(ok) == []
    assert schema.validate({"gateway": {"systemd_watchdog_seconds": 30}}) == []
    assert schema.validate({"gateway": {"connect_timeout_seconds": 0}}) != []
    assert schema.validate({"agent": {"restart_drain_timeout": -1}}) != []
    assert schema.validate({"agent": {"cron_drain_timeout": -1}}) != []  # P15-21


def test_idle_exit_row_validates() -> None:  # P17-23
    assert schema.validate({"gateway": {"idle_exit_seconds": 0}}) == []
    assert schema.validate({"gateway": {"idle_exit_seconds": 120.5}}) == []
    assert schema.validate({"gateway": {"idle_exit_seconds": -1}}) != []


def test_http_surface_rows_validate() -> None:  # P15-30..32
    ok = {"gateway": {"api_server": {"enabled": True, "port": 0, "cors_origins": ["https://a"]}}}
    assert schema.validate(ok) == []
    hook = {"gateway": {"webhook": {"routes": {"gh": {"secret": "INDUS_WEBHOOK_GH"}}}}}
    assert schema.validate(hook) == []
    assert schema.validate({"gateway": {"api_server": {"port": 70000}}}) != []
    assert schema.validate({"gateway": {"api_server": {"max_concurrent_runs": -1}}}) != []
    assert schema.validate({"gateway": {"webhook": {"rate_limit": 0}}}) != []
    assert schema.validate({"gateway": {"webhook": {"max_body_bytes": 0}}}) != []
