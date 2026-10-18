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
    }
    assert d["terminal"] == {"backend": "local", "cwd": ".", "timeout": 180}
    assert d["approvals"] == {"mode": "manual", "timeout": 300, "deny": []}
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
    }
    assert d["web"] == {
        "backend": "",
        "search_backend": "",
        "extract_backend": "",
        "keyless_rescue": True,
        "searxng_url": "",
    }
    assert d["display"] == {"language": "en"}
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
