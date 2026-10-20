"""P18-10: the profile scope of each dashboard request (R09.20, R04.25)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from indus.foundation.profiles import tombstone
from indus.foundation.scope import EnvView, Scope, make_scope
from indus.foundation.secrets import open_secret_store
from indus.web import scope as web_scope
from indus.web.scope import (
    ProfileRequired,
    ProfileScopes,
    ProfileUnknown,
    install,
    read_scope,
    unscoped_write_routes,
    write_scope,
)
from tests.fakes.clock import FakeClock

SRC_WEB = Path(web_scope.__file__).resolve().parent


def _profile(root: Path, name: str, marker: str = "profile.yaml") -> Path:
    home = root / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    (home / marker).write_text("", encoding="utf-8")
    return home


@pytest.fixture
def root(isolated_home: Path) -> Path:
    return isolated_home


@pytest.fixture
def base(root: Path, fake_clock: FakeClock) -> Scope:
    """The backend's own scope: the root (``default``) profile."""
    env = EnvView.from_process().with_overlay({"INDUS_HOME": str(root)})
    scope = make_scope(root, env=env, clock=fake_clock)
    return scope.with_secrets(open_secret_store(root, scope.profile_id, mode="file"))


def _app(scopes: ProfileScopes) -> FastAPI:
    app = FastAPI()
    install(app, scopes)

    @app.get("/api/thing")
    def get_thing(scope: Scope = Depends(read_scope)) -> dict[str, str]:  # noqa: B008
        return {"profile": scope.profile_id, "home": str(scope.home)}

    @app.put("/api/thing")
    def put_thing(scope: Scope = Depends(write_scope)) -> dict[str, str]:  # noqa: B008
        return {"profile": scope.profile_id, "home": str(scope.home)}

    return app


def test_one_profile_host_writes_without_naming_it(base: Scope) -> None:
    client = TestClient(_app(ProfileScopes(base)))
    assert client.put("/api/thing").json()["profile"] == "default"
    assert client.get("/api/thing").json()["profile"] == "default"


def test_two_profiles_write_needs_a_profile(base: Scope, root: Path) -> None:
    work = _profile(root, "work")
    client = TestClient(_app(ProfileScopes(base)))
    refused = client.put("/api/thing")
    assert refused.status_code == 400
    assert refused.json()["detail"]["error"] == "profile_required"
    assert refused.json()["detail"]["code"] == "profile_required"  # what the SPA client reads
    assert client.get("/api/thing").json()["profile"] == "default"  # reads: the backend's own
    named = client.put("/api/thing", params={"profile": "work"}).json()
    assert named == {"profile": "work", "home": str(work)}
    assert client.put("/api/thing", params={"profile": "default"}).json()["profile"] == "default"
    assert client.get("/api/thing", params={"profile": "Work"}).json()["profile"] == "work"


def test_unknown_invalid_and_deleted_profiles_are_404(base: Scope, root: Path) -> None:
    _profile(root, "work")
    gone = _profile(root, "gone")
    tombstone(root, "gone")
    client = TestClient(_app(ProfileScopes(base)))
    unknown = client.get("/api/thing", params={"profile": "nope"})
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == {
        "error": "profile_not_found",
        "code": "profile_not_found",
        "message": "No such profile.",
        "profile": "nope",
    }
    for bad in ("../work", "a b", "<script>", "x" * 80):
        answer = client.put("/api/thing", params={"profile": bad})
        assert answer.status_code == 404
        assert "profile" not in answer.json()["detail"]  # the raw value is never echoed
    assert client.get("/api/thing", params={"profile": "gone"}).status_code == 404
    assert not (root / "profiles" / "nope").exists()  # a lookup creates nothing
    assert gone.exists()


def test_a_half_created_profile_is_not_served(base: Scope, root: Path) -> None:
    (root / "profiles" / "empty").mkdir(parents=True)
    with pytest.raises(ProfileUnknown):
        ProfileScopes(base).resolve("empty")


def test_another_profile_gets_its_own_home_env_config_and_secrets(base: Scope, root: Path) -> None:
    work = _profile(root, "work", marker="config.yaml")
    (work / "config.yaml").write_text("display:\n  interface: tui\n", encoding="utf-8")
    (work / ".env").write_text("WORK_ONLY_SETTING=1\n", encoding="utf-8")
    scopes = ProfileScopes(base)
    scope = scopes.resolve("work")
    assert scope.home == work and scope.root == root and scope.profile_id == "work"
    assert scope.routed
    assert scope.env["INDUS_HOME"] == str(work)
    assert scope.env.get("WORK_ONLY_SETTING") == "1"
    assert base.env.get("WORK_ONLY_SETTING") is None
    assert scope.config is not None and scope.config.get("display.interface") == "tui"
    assert scope.secrets is not None
    assert scopes.resolve(None) is base
    assert scopes.resolve("  ") is base
    assert scopes.resolve("default") is base


def test_the_backend_s_own_named_profile_is_its_scope(root: Path, fake_clock: FakeClock) -> None:
    work = _profile(root, "work")
    own = make_scope(work, env={"INDUS_HOME": str(work)}, clock=fake_clock)
    scopes = ProfileScopes(own)
    assert scopes.resolve("work") is own
    assert scopes.resolve(None) is own
    assert scopes.is_multi()
    with pytest.raises(ProfileRequired):
        scopes.resolve_for_write(None)
    assert scopes.resolve_for_write("work") is own


def test_a_profile_created_while_running_counts_at_once(base: Scope, root: Path) -> None:
    scopes = ProfileScopes(base)
    assert not scopes.is_multi()
    _profile(root, "later")
    assert scopes.is_multi() and scopes.profiles() == ["default", "later"]


def test_every_write_route_depends_on_the_write_scope(base: Scope) -> None:
    app = _app(ProfileScopes(base))
    assert unscoped_write_routes(app) == []

    def nested(scope: Scope = Depends(write_scope)) -> Scope:  # noqa: B008
        return scope

    @app.delete("/api/nested")
    def delete_nested(scope: Scope = Depends(nested)) -> None:  # noqa: B008
        return None

    @app.post("/api/leaky")
    def leaky() -> None:
        return None

    @app.patch("/api/reads-only")
    def patch_with_read(scope: Scope = Depends(read_scope)) -> None:  # noqa: B008
        return None

    assert unscoped_write_routes(app) == ["POST /api/leaky", "PATCH /api/reads-only"]


def test_included_routers_are_checked_too(base: Scope) -> None:
    app = _app(ProfileScopes(base))
    inner = APIRouter()

    @inner.post("/leaky")
    def leaky() -> None:
        return None

    @inner.put("/scoped")
    def scoped(scope: Scope = Depends(write_scope)) -> None:  # noqa: B008
        return None

    outer = APIRouter()
    outer.include_router(inner, prefix="/inner")
    app.include_router(outer, prefix="/api")
    guarded = APIRouter()

    @guarded.delete("/whole-router")
    def whole() -> None:
        return None

    app.include_router(guarded, prefix="/api", dependencies=[Depends(write_scope)])
    assert unscoped_write_routes(app) == ["POST /api/inner/leaky"]


def test_the_real_app_installs_the_resolver(scope: Scope) -> None:
    from indus.web.app import AppOptions, create_app
    from indus.web.scope import scopes_of

    app = create_app(scope, "serve", options=AppOptions(config={}))
    found = scopes_of(_Conn(app))  # pyright: ignore[reportArgumentType]
    assert found.base is scope  # P18-11.. REST routes resolve through it


class _Conn:
    def __init__(self, app: FastAPI) -> None:
        self.app = app


def test_routes_without_a_resolver_fail_loudly() -> None:
    app = FastAPI()

    @app.get("/x")
    def x(scope: Scope = Depends(read_scope)) -> None:  # noqa: B008
        return None

    with pytest.raises(RuntimeError, match="no profile resolver"):
        TestClient(app).get("/x")


_FORBIDDEN_CALLS = {("os", "getenv"), ("Path", "home"), ("os", "putenv")}
_FORBIDDEN_ATTRS = {"environ", "expanduser"}


def _process_reads(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute):
            if node.attr in _FORBIDDEN_ATTRS:
                found.append(node.attr)
            base = node.value
            if isinstance(base, ast.Name) and (base.id, node.attr) in _FORBIDDEN_CALLS:
                found.append(f"{base.id}.{node.attr}")
        if isinstance(node, ast.Constant) and node.value == "INDUS_HOME":
            found.append("INDUS_HOME")
    return found


def test_route_modules_never_read_the_home_or_the_process_env() -> None:
    # Architecture rule 4: routes take their scope from the dependency (or the resolver) only.
    checked = 0
    for folder in ("routes", "ws"):
        for path in sorted((SRC_WEB / folder).glob("*.py")):
            checked += 1
            assert _process_reads(path.read_text(encoding="utf-8")) == [], path.name
    assert checked >= 3
    assert _process_reads("import os\nos.environ['X']\nPath.home()\n") == [
        "environ",
        "Path.home",
    ]
