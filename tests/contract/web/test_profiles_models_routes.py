"""P18-16: the Profiles and Models REST routes (R04.25) and the device-code sign-in sessions."""

from __future__ import annotations

import json
import secrets
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from indus.foundation.config import loader as config_loader
from indus.foundation.profiles import is_tombstoned, read_active, tombstone_path
from indus.foundation.scope import Scope
from indus.foundation.secrets import SecretStore
from indus.providers.auth.device_code import DeviceFlowSpec
from indus.providers.auth.pool_model import secret_name
from indus.providers.auth.store import AuthStore
from indus.web.routes import models, profiles
from tests.contract.web.rest_app import (
    RestApp,
    build_rest_app,
    pause,
    route_table,
    unscoped_writes,
)

WRITE = {"profile": "work"}


def _code(answer: Any) -> str:
    return answer.json()["detail"]["code"]


@pytest.fixture
def rest(scope: Scope) -> Iterator[RestApp]:
    config_loader.reset()
    built = build_rest_app(scope)
    yield built
    built.close()
    config_loader.reset()


def _wait(rest: RestApp, name: str, params: dict[str, str]) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while True:
        got = rest.client.get(f"/api/actions/{name}/status", params=params).json()
        if got["state"] != "running" or time.monotonic() > deadline:
            return got
        pause(0.05)


def test_route_lists_are_pinned(rest: RestApp) -> None:
    assert route_table(rest.app, ("/api/profiles",)) == set(profiles.ROUTE_LIST)
    assert route_table(rest.app, ("/api/model", "/api/providers/login")) == set(models.ROUTE_LIST)
    assert unscoped_writes(rest.app, ("/api/model", "/api/providers/login")) == []
    expected = {f"{m} {p}" for m, p, _ in profiles.ROUTE_LIST if m != "GET"}
    assert set(unscoped_writes(rest.app, ("/api/profiles",))) == expected


# -- profiles -------------------------------------------------------------------------------------


def test_list_names_every_live_profile(rest: RestApp) -> None:
    got = rest.client.get("/api/profiles").json()
    assert [p["name"] for p in got["profiles"]] == ["default", "work"]
    work = got["profiles"][1]
    assert work["is_current"] and not work["is_default"] and got["current"] == "work"
    assert got["active"] == "default"


def test_the_list_carries_each_bot_s_forever_chat(rest: RestApp, scope: Scope) -> None:
    """P24-31 wiring (D-16.799): ``profile_rows`` on the real list — the row names the same chat
    ``bots.open`` opens, a profile without one says null, and listing creates no store."""
    from indus.foundation.profiles import profile_home
    from indus.service import bots_rpc
    from indus.state.connection import DB_NAME

    before = rest.client.get("/api/profiles").json()["profiles"]
    assert [p["canonical_session"] for p in before] == [None, None]
    spare = rest.client.post("/api/profiles", json={"name": "spare"})
    assert spare.status_code == 201, spare.text
    opened = bots_rpc.for_root(scope).call("bots.open", {"profile": "work"})
    assert opened["created"] is True
    rows = {p["name"]: p for p in rest.client.get("/api/profiles").json()["profiles"]}
    assert rows["work"]["canonical_session"] == {"session_id": opened["session_id"]}
    assert rows["spare"]["canonical_session"] is None
    assert not (profile_home(scope.root, "spare") / DB_NAME).exists()
    again = bots_rpc.for_root(scope).call("bots.open", {"profile": "work"})
    assert again == {"session_id": opened["session_id"], "created": False}


def test_a_description_that_looks_like_a_flag_stays_text(rest: RestApp) -> None:
    created = rest.client.post("/api/profiles", json={"name": "odd", "description": "--clone-all"})
    assert created.status_code == 201, created.text
    rows = {p["name"]: p for p in rest.client.get("/api/profiles").json()["profiles"]}
    assert rows["odd"]["description"] == "--clone-all"


def test_create_delete_and_a_deleted_profile_is_never_recreated(
    rest: RestApp, scope: Scope
) -> None:
    """R04.25: after the delete, reads and writes 404 and the folder stays gone."""
    created = rest.client.post("/api/profiles", json={"name": "Scratch", "description": "tmp"})
    assert created.status_code == 201, created.text
    assert created.json()["name"] == "scratch"
    home = scope.root / "profiles" / "scratch"
    assert home.is_dir()
    names = [p["name"] for p in rest.client.get("/api/profiles").json()["profiles"]]
    assert names == ["default", "scratch", "work"]
    again = rest.client.post("/api/profiles", json={"name": "scratch"})
    assert again.status_code == 409

    deleted = rest.client.delete("/api/profiles/scratch")
    assert deleted.status_code == 200, deleted.text
    assert not home.exists() and is_tombstoned(scope.root, "scratch")
    for method, path, body in [
        ("GET", "/api/profiles/scratch/persona", None),
        ("PUT", "/api/profiles/scratch/persona", {"content": "back?"}),
        ("PUT", "/api/profiles/scratch/description", {"description": "x"}),
        ("PUT", "/api/profiles/scratch/model", {"provider": "openai", "model": "m"}),
        ("POST", "/api/profiles/scratch/export", None),
        ("DELETE", "/api/profiles/scratch", None),
    ]:
        answer = rest.client.request(method, path, json=body)
        assert answer.status_code == 404, (method, path)
    reads = rest.client.get("/api/model/info", params={"profile": "scratch"})
    assert reads.status_code == 404
    assert not home.exists() and tombstone_path(scope.root, "scratch").is_file()
    names = [p["name"] for p in rest.client.get("/api/profiles").json()["profiles"]]
    assert "scratch" not in names


def test_refusals(rest: RestApp) -> None:
    assert rest.client.post("/api/profiles", json={"name": "Not Valid!"}).status_code == 400
    assert rest.client.post("/api/profiles", json={"name": "default"}).status_code == 400
    assert rest.client.delete("/api/profiles/default").status_code == 400
    current = rest.client.delete("/api/profiles/work")
    assert current.status_code == 409 and _code(current) == "profile_in_use"
    assert rest.client.get("/api/profiles/ghost/persona").status_code == 404
    assert rest.client.get("/api/profiles/BAD NAME!/persona").status_code == 400


def test_persona_description_model_and_sticky(rest: RestApp, scope: Scope) -> None:
    empty = rest.client.get("/api/profiles/work/persona").json()
    assert empty == {"content": "", "exists": False}
    put = rest.client.put("/api/profiles/work/persona", json={"content": "You are terse."})
    assert put.status_code == 200
    assert (scope.home / "PERSONA.md").read_text(encoding="utf-8") == "You are terse."
    assert rest.client.get("/api/profiles/work/persona").json()["exists"] is True
    described = rest.client.put("/api/profiles/work/description", json={"description": " Ops "})
    assert described.json()["description"] == "Ops"
    model = rest.client.put(
        "/api/profiles/default/model", json={"provider": "openai", "model": "some-model"}
    )
    assert model.status_code == 200
    rows = {p["name"]: p for p in rest.client.get("/api/profiles").json()["profiles"]}
    assert rows["work"]["description"] == "Ops" and rows["work"]["has_persona"]
    assert rows["default"]["model"] == "some-model" and rows["default"]["provider"] == "openai"
    sticky = rest.client.post("/api/profiles/active", json={"name": "work"})
    assert sticky.status_code == 200 and read_active(scope.root) == "work"
    assert rest.client.get("/api/profiles/active").json() == {"active": "work", "current": "work"}
    assert rest.client.post("/api/profiles/active", json={"name": "ghost"}).status_code == 404


def test_export_then_import_as_a_new_profile(rest: RestApp, scope: Scope) -> None:
    (scope.home / "PERSONA.md").write_text("Persona text", encoding="utf-8")
    (scope.home / ".env").write_text("SOME_KEY=value\n", encoding="utf-8")
    (scope.home / "logs").mkdir(exist_ok=True)
    (scope.home / "logs" / "agent.log").write_text("log", encoding="utf-8")
    cast(SecretStore, scope.secrets).set("SECRET_NAME", "secret-" + secrets.token_hex(8))
    started = rest.client.post("/api/profiles/work/export")
    assert started.status_code == 202
    archive = Path(started.json()["archive"])
    status = _wait(rest, "profile-export", WRITE)
    assert status["state"] == "done", status
    with zipfile.ZipFile(archive) as made:
        names = set(made.namelist())
    assert {"PERSONA.md", "profile.yaml", "indus-profile.json"} <= names
    assert not names & {".env", "secrets.json", "secrets.index.json", "logs/agent.log"}

    imported = rest.client.post(
        "/api/profiles/import", json={"archive": str(archive), "name": "copy"}
    )
    assert imported.status_code == 201, imported.text
    copy = scope.root / "profiles" / "copy"
    assert (copy / "PERSONA.md").read_text(encoding="utf-8") == "Persona text"
    clash = rest.client.post("/api/profiles/import", json={"archive": str(archive), "name": "copy"})
    assert clash.status_code == 409


def test_import_refuses_unsafe_members(rest: RestApp, tmp_path: Path, scope: Scope) -> None:
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as made:
        made.writestr("../escape.txt", "x")
        made.writestr("profile.yaml", "")
    answer = rest.client.post("/api/profiles/import", json={"archive": str(bad), "name": "evil"})
    assert answer.status_code == 400 and _code(answer) == "bad_archive"
    assert not (scope.root / "profiles" / "evil").exists()
    assert not (scope.root / "profiles" / "escape.txt").exists()


# -- models ---------------------------------------------------------------------------------------


def test_model_set_info_and_options(rest: RestApp) -> None:
    set_main = rest.client.post(
        "/api/model/set",
        params=WRITE,
        json={"scope": "main", "provider": "openai", "model": "gpt-test-model"},
    )
    assert set_main.status_code == 200
    info = rest.anonymous().get("/api/model/info", params=WRITE)
    assert info.status_code == 200
    assert info.json()["model"] == "gpt-test-model" and info.json()["provider"] == "openai"
    options = rest.client.get("/api/model/options", params=WRITE).json()
    names = {p["name"] for p in options["providers"]}
    assert {"openai", "openrouter"} <= names
    assert options["current"] == {"provider": "openai", "model": "gpt-test-model"}
    unknown = rest.client.post(
        "/api/model/set", params=WRITE, json={"scope": "main", "provider": "nope", "model": "m"}
    )
    assert unknown.status_code == 400


def test_auxiliary_tasks(rest: RestApp) -> None:
    put = rest.client.post(
        "/api/model/set",
        params=WRITE,
        json={"scope": "auxiliary", "task": "title", "provider": "openrouter", "model": "small"},
    )
    assert put.status_code == 200
    got = rest.client.get("/api/model/auxiliary", params=WRITE).json()
    rows = {t["task"]: t for t in got["tasks"]}
    assert rows["title"]["provider"] == "openrouter" and rows["title"]["model"] == "small"
    assert rows["compression"]["provider"] == "auto"
    bad = rest.client.post(
        "/api/model/set", params=WRITE, json={"scope": "auxiliary", "task": "nope", "model": "m"}
    )
    assert bad.status_code == 400 and _code(bad) == "unknown_task"


# -- device-code sign-in --------------------------------------------------------------------------


def test_login_without_a_documented_device_flow_is_refused(rest: RestApp) -> None:
    answer = rest.client.post(
        "/api/providers/login/start", params=WRITE, json={"provider": "openai"}
    )
    assert answer.status_code == 400 and _code(answer) == "no_device_flow"
    unknown = rest.client.post("/api/providers/login/start", params=WRITE, json={"provider": "x"})
    assert unknown.status_code == 404


SPEC = DeviceFlowSpec(
    provider="openrouter",
    device_authorization_url="https://idp.example.test/device",
    token_url="https://idp.example.test/token",
    client_id="dashboard-test",
)


def _fake_client(token_answer: dict[str, Any], status: int = 200) -> Any:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/device":
            return httpx.Response(
                200,
                json={
                    "device_code": "dc-" + "1" * 20,
                    "user_code": "WXYZ-1234",
                    "verification_uri": "https://idp.example.test/activate",
                    "expires_in": 600,
                    "interval": 5,
                },
            )
        return httpx.Response(status, json=token_answer)

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle))


def _poll(rest: RestApp, session_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while True:
        got = rest.client.get(f"/api/providers/login/{session_id}", params=WRITE).json()
        if got["status"] != "pending" or time.monotonic() > deadline:
            return got
        pause(0.05)


def test_device_login_approved_lands_in_the_pool(rest: RestApp, scope: Scope) -> None:
    access = "at-" + secrets.token_hex(16)
    rest.state.extras["providers.device_flows"] = {"openrouter": SPEC}
    rest.state.extras["providers.http_client"] = _fake_client(
        {"access_token": access, "token_type": "Bearer", "expires_in": 3600}
    )
    started = rest.client.post(
        "/api/providers/login/start", params=WRITE, json={"provider": "openrouter"}
    )
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["user_code"] == "WXYZ-1234" and body["status"] == "pending"
    assert "dc-" not in started.text
    done = _poll(rest, body["session_id"])
    assert done["status"] == "approved", done
    rows, _ = AuthStore(scope.home, env=scope.env).pool_rows("openrouter")
    assert rows[0]["auth_type"] == "oauth" and rows[0]["source"] == "device_code"
    assert scope.secrets is not None
    stored = scope.secrets.get(secret_name("openrouter", rows[0]["id"]))
    assert stored is not None and json.loads(stored)["access_token"] == access
    listing = rest.client.get("/api/credentials/pool", params=WRITE)
    assert access not in listing.text and "(signed in)" in listing.text
    other = rest.client.get(
        f"/api/providers/login/{body['session_id']}", params={"profile": "default"}
    )
    assert other.status_code == 404


def test_device_login_denied_and_cancelled(rest: RestApp) -> None:
    rest.state.extras["providers.device_flows"] = {"openrouter": SPEC}
    rest.state.extras["providers.http_client"] = _fake_client({"error": "access_denied"}, 400)
    denied = rest.client.post(
        "/api/providers/login/start", params=WRITE, json={"provider": "openrouter"}
    ).json()
    assert _poll(rest, denied["session_id"])["status"] == "denied"

    rest.state.extras["providers.http_client"] = _fake_client(
        {"error": "authorization_pending"}, 400
    )
    pending = rest.client.post(
        "/api/providers/login/start", params=WRITE, json={"provider": "openrouter"}
    ).json()
    cancelled = rest.client.delete(f"/api/providers/login/{pending['session_id']}", params=WRITE)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert _poll(rest, pending["session_id"])["status"] == "cancelled"
