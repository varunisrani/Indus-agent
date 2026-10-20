"""The editor MCP record and spec checks of the daemon (P19-13, D-16.602)."""

from __future__ import annotations

import json

import pytest

from indus.foundation.scope import Scope
from indus.service.editor_mcp import (
    ACP_BUNDLE,
    EditorRecord,
    SpecError,
    acp_selection,
    server_config,
    spec_fingerprint,
    valid_specs,
)

SPEC = {
    "name": "docs",
    "command": "docs-server",
    "args": ["--serve"],
    "env": [{"name": "DOCS_TOKEN", "value": "v" + "-1"}],
}


def test_the_fingerprint_hides_values_and_follows_every_part_of_the_spec() -> None:
    base = spec_fingerprint(SPEC)
    assert base == spec_fingerprint(dict(SPEC))
    assert "v-1" not in base
    for change in (
        {"args": ["--other"]},
        {"command": "other"},
        {"env": [{"name": "DOCS_TOKEN", "value": "v-2"}]},
        {"type": "http", "url": "http://127.0.0.1:1/mcp"},
    ):
        assert spec_fingerprint({**SPEC, **change}) != base, change
    # env given as a mapping or as ACP pairs is the same spec
    assert spec_fingerprint({**SPEC, "env": {"DOCS_TOKEN": "v-1"}}) == base


def test_the_record_refuses_changed_servers_admits_new_ones_and_freezes_listings() -> None:
    record = EditorRecord()
    assert record.admit([SPEC]) is True
    assert record.admit([SPEC]) is False
    assert record.refused([SPEC]) == []
    assert record.refused([{**SPEC, "args": []}]) == ["docs"]
    assert record.refused([{**SPEC, "name": "new"}]) == []
    assert record.freeze("docs", {"read": {"name": "read"}}) == (True, False)
    assert record.freeze("docs", {"read": {"name": "read"}}) == (False, False)
    assert record.freeze("docs", {"write": {"name": "write"}}) == (False, True)
    assert record.schemas["docs"] == {"read": {"name": "read"}}  # the frozen copy stays
    stored = json.loads(json.dumps(record.to_record()))
    assert "v-1" not in json.dumps(stored)
    again = EditorRecord.from_record(stored)
    assert again == record
    assert EditorRecord.from_record("junk") == EditorRecord()


def test_server_lists_are_checked_without_echoing_a_value() -> None:
    assert valid_specs(None) == []
    kept = valid_specs([SPEC, {**SPEC, "args": ["--later"]}])
    assert len(kept) == 1 and kept[0]["args"] == ["--later"]  # a repeated name keeps the later
    for bad in (
        "nope",
        [1],
        [{"command": "secret-arg"}],
        [{"name": "x"}],
        [{"name": "x", "type": "http"}],
        [{"name": "x", "type": "ws", "url": "secret-arg"}],
        [{**SPEC, "name": f"s{n}"} for n in range(17)],
    ):
        with pytest.raises(SpecError) as refused:
            valid_specs(bad)
        assert "secret-arg" not in str(refused.value)


def test_specs_become_p13_server_configs() -> None:
    stdio = server_config(SPEC, "/work/repo")
    assert stdio.command == "docs-server" and stdio.args == ["--serve"]
    assert stdio.env == {"DOCS_TOKEN": "v-1"} and stdio.cwd == "/work/repo"
    http = server_config(
        {"name": "web", "type": "sse", "url": "http://127.0.0.1:9/mcp", "headers": []}, None
    )
    assert http.url == "http://127.0.0.1:9/mcp" and http.transport == "sse"


def test_the_acp_selection_is_the_saved_list_or_the_bundle(scope: Scope) -> None:
    assert acp_selection(scope) == [ACP_BUNDLE]
