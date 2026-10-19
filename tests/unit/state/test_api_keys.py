"""P15-30: the API key ring (values in the secret store, labels in the index, constant-time
check)."""

from __future__ import annotations

import json
import stat

from indus.foundation.scope import Scope
from indus.state.api_keys import INDEX_FILE, SECRET_PREFIX, ApiKeyRing, secret_name
from tests.fakes.secret_store import MemorySecretStore


def test_create_list_verify_revoke(scope: Scope) -> None:
    vault = MemorySecretStore()
    ring = ApiKeyRing(scope, vault)
    assert ring.list() == [] and not ring.has_keys()
    first, value = ring.create("  laptop   client ")
    assert first.label == "laptop client" and len(first.key_id) == 8
    assert vault.values[secret_name(first.key_id)] == value
    second, other = ring.create()
    assert [k.key_id for k in ring.list()] == [first.key_id, second.key_id]
    assert ring.verify(value) == first.key_id and ring.verify(other) == second.key_id
    assert ring.verify(value + "x") is None and ring.verify("") is None
    assert ring.verify("ünïcode") is None
    index = scope.home / INDEX_FILE
    assert value not in index.read_text() and other not in index.read_text()
    assert stat.S_IMODE(index.stat().st_mode) == 0o600
    assert ring.revoke(first.key_id.lower())
    assert ring.verify(value) is None and not ring.revoke(first.key_id)
    assert not ring.revoke("not-an-id")
    assert [k.key_id for k in ring.list()] == [second.key_id]


def test_a_key_removed_by_another_process_stops_verifying(scope: Scope) -> None:
    vault = MemorySecretStore()
    daemon = ApiKeyRing(scope, vault)
    info, value = ApiKeyRing(scope, vault).create("cli")
    assert daemon.verify(value) == info.key_id
    data = json.loads((scope.home / INDEX_FILE).read_text())
    data["keys"].clear()
    (scope.home / INDEX_FILE).write_text(json.dumps(data))
    assert daemon.verify(value) is None  # the index is read on every check


def test_an_index_row_without_a_stored_value_is_not_a_key(scope: Scope) -> None:
    vault = MemorySecretStore()
    ring = ApiKeyRing(scope, vault)
    info, _ = ring.create()
    vault.delete(SECRET_PREFIX + info.key_id)
    assert ring.list() == [] and not ring.has_keys()
