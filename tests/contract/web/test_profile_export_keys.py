"""P24-27: a profile export archive carries no key material (fixture check over the real route)."""

from __future__ import annotations

import secrets
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from indus.foundation.config import loader as config_loader
from indus.foundation.scope import Scope
from indus.foundation.secrets import SecretStore
from tests.contract.web.rest_app import RestApp, build_rest_app, pause

WRITE = {"profile": "work"}


@pytest.fixture
def rest(scope: Scope) -> Iterator[RestApp]:
    config_loader.reset()
    built = build_rest_app(scope)
    yield built
    built.close()
    config_loader.reset()


def _wait(rest: RestApp, name: str) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while True:
        got = rest.client.get(f"/api/actions/{name}/status", params=WRITE).json()
        if got["state"] != "running" or time.monotonic() > deadline:
            return got
        pause(0.05)


def test_profile_export_archive_contains_no_key_material(rest: RestApp, scope: Scope) -> None:
    env_key = "kv-" + secrets.token_hex(12)
    vault_key = "kv-" + secrets.token_hex(12)
    oauth_key = "kv-" + secrets.token_hex(12)
    home = scope.home
    (home / "PERSONA.md").write_text("A careful helper.", encoding="utf-8")
    (home / ".env").write_text(f"PROVIDER_API_KEY={env_key}\n", encoding="utf-8")
    (home / ".env.local").write_text(f"OTHER_KEY={env_key}\n", encoding="utf-8")
    (home / "auth.json").write_text(f'{{"access": "{oauth_key}"}}', encoding="utf-8")
    cast(SecretStore, scope.secrets).set("PROVIDER_API_KEY", vault_key)
    started = rest.client.post("/api/profiles/work/export")
    assert started.status_code == 202
    archive = Path(started.json()["archive"])
    assert _wait(rest, "profile-export")["state"] == "done"
    with zipfile.ZipFile(archive) as made:
        names = made.namelist()
        blob = b"".join(made.read(name) for name in names)
    assert "PERSONA.md" in names
    assert not {".env", ".env.local", "auth.json", "secrets.json"} & set(names)
    for key in (env_key, vault_key, oauth_key):
        assert key.encode() not in blob
