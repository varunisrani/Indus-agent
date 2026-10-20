"""P18-12: the Config and Keys REST routes (R04.12, R04.15)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.config import loader as config_loader
from indus.foundation.envfile import read_env_file
from indus.foundation.envload import env_file_path
from indus.foundation.scope import Scope
from indus.foundation.secrets import SecretStore
from indus.web.routes import config_env
from indus.web.routes.config_view import PLACEHOLDER
from tests.contract.web.rest_app import RestApp, build_rest_app, route_table, unscoped_writes

WRITE = {"profile": "work"}
SECRET = "kv-" + "".join(["q7Zr"] * 8)
"""A fake credential built at runtime (no vendor shape)."""
OTHER_SECRET = "kv-" + "".join(["m3Xa"] * 8)

COMMENTED = """\
# my own settings
agent:
  max_turns: 40  # keep turns short
model:
  default: fake-model
"""


@pytest.fixture
def env_scope(scope: Scope) -> Scope:
    return scope.with_env(scope.env.with_overlay({"MY_ENDPOINT_KEY": SECRET}))


@pytest.fixture
def rest(env_scope: Scope) -> Iterator[RestApp]:
    config_loader.reset()
    built = build_rest_app(env_scope)
    yield built
    built.close()
    config_loader.reset()


def _config_path(scope: Scope) -> Path:
    return scope.home / "config.yaml"


def _code(answer: Any) -> str:
    return answer.json()["detail"]["code"]


# -- config ---------------------------------------------------------------------------------------


def test_put_round_trips_comments_and_writes_only_changes(rest: RestApp, scope: Scope) -> None:
    path = _config_path(scope)
    path.write_text(COMMENTED, encoding="utf-8")
    form = rest.client.get("/api/config").json()
    assert form["agent"]["max_turns"] == 40 and form["model"]["default"] == "fake-model"
    assert "_config_version" not in form
    form["agent"]["max_turns"] = 50
    answer = rest.client.put("/api/config", params=WRITE, json={"config": form})
    assert answer.status_code == 200 and answer.json()["ok"] is True
    text = path.read_text(encoding="utf-8")
    assert "# my own settings" in text and "# keep turns short" in text
    assert "max_turns: 50" in text
    assert "file_read_max_chars" not in text  # defaults sent back are not written
    assert rest.client.get("/api/config").json()["agent"]["max_turns"] == 50


def test_references_stay_references_and_credentials_stay_hidden(
    rest: RestApp, scope: Scope
) -> None:
    path = _config_path(scope)
    path.write_text(
        "providers:\n"
        "  mine:\n"
        "    base_url: http://127.0.0.1:8000/v1\n"
        "    api_key: ${MY_ENDPOINT_KEY}\n"
        "  other:\n"
        "    base_url: http://127.0.0.1:8001/v1\n"
        f"    api_key: {OTHER_SECRET}\n",
        encoding="utf-8",
    )
    got = rest.client.get("/api/config")
    assert SECRET not in got.text and OTHER_SECRET not in got.text
    providers = got.json()["providers"]
    assert providers["mine"]["api_key"] == "${MY_ENDPOINT_KEY}"
    assert providers["other"]["api_key"] == PLACEHOLDER
    form = got.json()
    form["providers"]["other"]["base_url"] = "http://127.0.0.1:9000/v1"
    saved = rest.client.put("/api/config", params=WRITE, json={"config": form})
    assert saved.status_code == 200
    text = path.read_text(encoding="utf-8")
    assert f"api_key: {OTHER_SECRET}" in text and "${MY_ENDPOINT_KEY}" in text
    assert PLACEHOLDER not in text and "9000" in text

    effective = rest.client.get("/api/config/effective")
    assert SECRET not in effective.text and OTHER_SECRET not in effective.text
    entries = {e["key"]: e for e in effective.json()["entries"]}
    assert entries["providers.mine.api_key"]["source"] == "env"
    assert entries["providers.mine.api_key"]["value"] == "${MY_ENDPOINT_KEY}"
    assert entries["providers.other.base_url"]["source"] == "file"
    assert entries["agent.max_turns"] == {
        "key": "agent.max_turns",
        "value": 150,
        "source": "default",
    }


def test_unreadable_config_is_500_and_nothing_is_written(rest: RestApp, scope: Scope) -> None:
    path = _config_path(scope)
    path.write_text("agent: [unclosed\n", encoding="utf-8")
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    get = rest.client.get("/api/config")
    assert get.status_code == 500 and _code(get) == "config_unreadable"
    put = rest.client.put("/api/config", params=WRITE, json={"config": {"agent": {"max_turns": 3}}})
    assert put.status_code == 500 and _code(put) == "config_unreadable"
    assert rest.client.get("/api/config/effective").status_code == 500
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_put_needs_an_object(rest: RestApp) -> None:
    assert rest.client.put("/api/config", params=WRITE, json={"config": [1]}).status_code == 422
    assert rest.client.put("/api/config", params=WRITE, json={}).status_code == 422


def test_managed_install_refuses_writes(scope: Scope) -> None:
    managed = scope.with_env(scope.env.with_overlay({"INDUS_MANAGED": "1"}))
    rest = build_rest_app(managed)
    try:
        answer = rest.client.put("/api/config", params=WRITE, json={"config": {"agent": {}}})
        assert answer.status_code == 403 and _code(answer) == "config_managed"
        key = rest.client.put("/api/env", params=WRITE, json={"key": "X_TOKEN", "value": SECRET})
        assert key.status_code == 403
    finally:
        rest.close()


def test_schema_and_defaults_are_public(rest: RestApp) -> None:
    del rest.client.headers["X-Indus-Session-Token"]
    schema = rest.client.get("/api/config/schema")
    assert schema.status_code == 200
    fields = schema.json()["fields"]
    assert fields["agent.max_turns"]["type"] == "integer"
    assert fields["agent.max_turns"]["category"] == "agent"
    assert fields["agent.max_turns"]["description"]
    assert fields["agent.fast_mode"]["type"] == "enum"
    assert fields["agent.fast_mode"]["options"] == ["normal", "fast", "auto"]
    assert fields["model.default"]["type"] == "string"
    assert fields["agent.restart_drain_timeout"]["type"] == "number"
    assert fields["command_allowlist"]["type"] == "list"
    assert any(f["type"] == "boolean" for f in fields.values())
    order = schema.json()["category_order"]
    assert order[0] == "model" and "agent" in order and "dashboard" in order
    assert set(order) == {f["category"] for f in fields.values()}
    defaults = rest.client.get("/api/config/defaults")
    assert defaults.status_code == 200
    assert defaults.json()["agent"]["max_turns"] == 150 and "_config_version" not in defaults.json()
    assert rest.client.get("/api/config").status_code == 401


# -- keys -----------------------------------------------------------------------------------------


def test_denied_and_malformed_names_are_refused(rest: RestApp, scope: Scope) -> None:
    for name in ("LD_PRELOAD", "PATH", "PYTHONPATH", "DYLD_INSERT_LIBRARIES"):
        answer = rest.client.put("/api/env", params=WRITE, json={"key": name, "value": "/tmp/x"})
        assert answer.status_code == 400, name
        assert _code(answer) == "env_name_denied"
    lower = rest.client.put("/api/env", params=WRITE, json={"key": "ld_preload", "value": "x"})
    assert lower.status_code == 400 and _code(lower) == "invalid_key"
    dotted = rest.client.put("/api/env", params=WRITE, json={"key": "a.b", "value": "x"})
    assert _code(dotted) == "invalid_key"
    assert "LD_PRELOAD" not in read_env_file(env_file_path(scope.home))


def test_credential_goes_to_the_secret_store_and_is_never_echoed(
    rest: RestApp, scope: Scope
) -> None:
    env_path = env_file_path(scope.home)
    env_path.write_text(f"OPENAI_API_KEY={OTHER_SECRET}\n", encoding="utf-8")
    bodies: list[str] = []
    put = rest.client.put(
        "/api/env", params=WRITE, json={"key": "OPENAI_API_KEY", "value": f"  {SECRET}  "}
    )
    bodies.append(put.text)
    assert put.status_code == 200
    assert put.json() == {
        "key": "OPENAI_API_KEY",
        "is_set": True,
        "redacted_value": f"{SECRET[:4]}…{SECRET[-4:]}",
        "stored_in": "file",
    }
    assert isinstance(scope.secrets, SecretStore)
    assert scope.secrets.get("OPENAI_API_KEY") == SECRET
    assert "OPENAI_API_KEY" not in read_env_file(env_path)  # the older plain copy is gone

    listed = rest.client.get("/api/env")
    bodies.append(listed.text)
    row = listed.json()["OPENAI_API_KEY"]
    assert row["is_set"] is True and row["is_password"] is True and row["category"] == "provider"
    assert row["redacted_value"] == put.json()["redacted_value"]
    assert row["description"]

    blank = rest.client.put("/api/env", params=WRITE, json={"key": "OPENAI_API_KEY", "value": " "})
    bodies.append(blank.text)
    assert blank.status_code == 200 and blank.json()["is_set"] is True
    assert scope.secrets.get("OPENAI_API_KEY") == SECRET  # blank keeps the stored value
    preview = rest.client.put(
        "/api/env",
        params=WRITE,
        json={"key": "OPENAI_API_KEY", "value": row["redacted_value"]},
    )
    bodies.append(preview.text)
    assert preview.status_code == 400 and _code(preview) == "redacted_value"
    for path in ("/api/config", "/api/config/effective", "/api/config/defaults"):
        bodies.append(rest.client.get(path).text)
    for body in bodies:
        assert SECRET not in body and OTHER_SECRET not in body


def test_plain_settings_go_to_the_env_file(rest: RestApp, scope: Scope) -> None:
    url = "http://127.0.0.1:8080/v1"
    answer = rest.client.put(
        "/api/env", params=WRITE, json={"key": "OPENAI_BASE_URL", "value": url}
    )
    assert answer.status_code == 200 and answer.json()["stored_in"] == "env_file"
    assert read_env_file(env_file_path(scope.home))["OPENAI_BASE_URL"] == url
    row = rest.client.get("/api/env").json()["OPENAI_BASE_URL"]
    assert row["is_set"] is True and row["advanced"] is True and row["is_password"] is False
    custom = rest.client.put(
        "/api/env", params=WRITE, json={"key": "MY_SERVICE_TOKEN", "value": SECRET}
    )
    assert custom.json()["stored_in"] == "file"
    listed = rest.client.get("/api/env").json()
    assert listed["MY_SERVICE_TOKEN"]["category"] == "custom"
    assert listed["MY_SERVICE_TOKEN"]["is_password"] is True


def test_file_mode_keeps_credentials_in_the_env_file(scope: Scope) -> None:
    (scope.home / "config.yaml").write_text("security:\n  secret_store: file\n", encoding="utf-8")
    config_loader.reset()
    rest = build_rest_app(scope)
    try:
        answer = rest.client.put(
            "/api/env", params=WRITE, json={"key": "OPENAI_API_KEY", "value": SECRET}
        )
        assert answer.json()["stored_in"] == "env_file"
        assert read_env_file(env_file_path(scope.home))["OPENAI_API_KEY"] == SECRET
        assert SECRET not in answer.text
    finally:
        rest.close()
        config_loader.reset()


def test_delete_removes_from_store_and_env_file(rest: RestApp, scope: Scope) -> None:
    env_path = env_file_path(scope.home)
    rest.client.put("/api/env", params=WRITE, json={"key": "OPENAI_API_KEY", "value": SECRET})
    with env_path.open("a", encoding="utf-8") as handle:
        handle.write(f"OPENAI_API_KEY={OTHER_SECRET}{os.linesep}")
    gone = rest.client.request("DELETE", "/api/env", params=WRITE, json={"key": "OPENAI_API_KEY"})
    assert gone.status_code == 200 and gone.json() == {
        "ok": True,
        "key": "OPENAI_API_KEY",
        "removed": True,
    }
    assert isinstance(scope.secrets, SecretStore) and scope.secrets.get("OPENAI_API_KEY") is None
    assert "OPENAI_API_KEY" not in read_env_file(env_path)
    assert rest.client.get("/api/env").json()["OPENAI_API_KEY"]["is_set"] is False
    again = rest.client.request("DELETE", "/api/env", params=WRITE, json={"key": "OPENAI_API_KEY"})
    assert again.status_code == 200 and again.json()["removed"] is False


# -- wiring ---------------------------------------------------------------------------------------


def test_writes_on_a_multi_profile_host_need_a_profile(rest: RestApp) -> None:
    assert rest.client.put("/api/config", json={"config": {}}).status_code == 400
    assert rest.client.put("/api/env", json={"key": "A_TOKEN", "value": "x"}).status_code == 400


def test_every_write_route_is_scoped(rest: RestApp) -> None:
    assert unscoped_writes(rest.app, ("/api/config", "/api/env")) == []


def test_route_list_matches_the_app(rest: RestApp) -> None:
    table = route_table(rest.app, ("/api/config", "/api/env"))
    assert table == set(config_env.ROUTE_LIST)
