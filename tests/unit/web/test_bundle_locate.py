"""P18-29: finding the built pages, publishing them into the package, serving them last."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
from starlette.routing import Route

import indus
from indus.foundation.scope import Scope
from indus.web import bundle as bundle_module
from indus.web.app import AppOptions, create_app
from indus.web.bundle import (
    BundleLocation,
    PublishError,
    dev_bundle_dir,
    locate,
    publish,
    runtime_bundle_dir,
    source_checkout,
)
from indus.web.command import Launch, _maybe_build  # pyright: ignore[reportPrivateUsage]
from tests.unit.web.conftest import INDEX_HTML, Builder, write_bundle

REPO = Path(__file__).resolve().parents[3]


def _installed(tmp_path: Path) -> Path:
    """A package dir the way a wheel lays it out (site-packages/indus)."""
    package = tmp_path / "venv" / "lib" / "python3.13" / "site-packages" / "indus"
    (package / "web").mkdir(parents=True)
    return package


def _checkout(tmp_path: Path) -> tuple[Path, Path]:
    """A source checkout: <repo>/src/indus beside <repo>/apps/web/package.json."""
    repo = tmp_path / "repo"
    package = repo / "src" / "indus"
    (package / "web").mkdir(parents=True)
    (repo / "apps" / "web").mkdir(parents=True)
    (repo / "apps" / "web" / "package.json").write_text("{}", encoding="utf-8")
    return repo, package


def test_installed_tree_finds_its_runtime_bundle(tmp_path: Path) -> None:
    package = _installed(tmp_path)
    assert source_checkout(package) is None
    assert locate(package) == BundleLocation(runtime_bundle_dir(package), "missing")
    write_bundle(runtime_bundle_dir(package))
    found = locate(package)
    assert found.kind == "installed" and found.found
    assert found.directory == package / "web" / "static"


def test_source_checkout_prefers_the_dev_build(tmp_path: Path) -> None:
    repo, package = _checkout(tmp_path)
    assert source_checkout(package) == repo
    # Nothing built: the answer is where the build will land, so the server picks it up live.
    nothing = locate(package)
    assert nothing == BundleLocation(dev_bundle_dir(repo), "missing") and not nothing.found
    write_bundle(runtime_bundle_dir(package))
    assert locate(package).kind == "installed"
    write_bundle(dev_bundle_dir(repo))
    assert locate(package) == BundleLocation(repo / "apps" / "web" / "dist", "source")


def test_a_half_copy_does_not_count(tmp_path: Path) -> None:
    package = _installed(tmp_path)
    static = runtime_bundle_dir(package)
    (static / "assets").mkdir(parents=True)
    (static / "assets" / "index-abc123.js").write_text("x", encoding="utf-8")
    assert not locate(package).found


def test_a_package_outside_src_is_not_a_checkout(tmp_path: Path) -> None:
    repo, _ = _checkout(tmp_path)
    elsewhere = repo / "lib" / "indus"
    elsewhere.mkdir(parents=True)
    assert source_checkout(elsewhere) is None


def test_this_repository_is_a_source_checkout() -> None:
    package = Path(indus.__file__).resolve().parent
    assert source_checkout(package) == REPO
    assert locate().directory in (dev_bundle_dir(REPO), runtime_bundle_dir(package))


# -- Wave 47 (f): pages stamped with another release are stale ------------------------------------


def _on_release(repo: Path, version: str | None) -> None:
    """A checkout whose HEAD is a commit, tagged ``v<version>`` (no tag when None), from ``.git``
    files only (the probes ``locate`` reads)."""
    head = "ab" * 20
    (repo / ".git" / "refs" / "tags").mkdir(parents=True, exist_ok=True)
    (repo / ".git" / "HEAD").write_text(head + "\n", encoding="utf-8")
    if version is not None:
        (repo / ".git" / "refs" / "tags" / f"v{version}").write_text(head + "\n", encoding="utf-8")


def _stamp(directory: Path, version: str) -> None:
    write_bundle(directory)
    (directory / bundle_module.RELEASE_STAMP).write_text(f"{version}\n", encoding="utf-8")


def test_fetched_pages_of_another_release_are_stale_in_a_checkout(tmp_path: Path) -> None:
    repo, package = _checkout(tmp_path)
    _on_release(repo, "0.3.0")
    _stamp(runtime_bundle_dir(package), "0.2.0")
    stale = locate(package)
    assert stale.kind == "stale" and not stale.found
    # Never served: the answer is where a build lands, as when nothing is built.
    assert stale.directory == dev_bundle_dir(repo)
    assert stale.stamped == "0.2.0"
    _stamp(runtime_bundle_dir(package), "0.3.0")
    assert locate(package) == BundleLocation(runtime_bundle_dir(package), "installed")


def test_fetched_pages_on_a_head_off_any_release_are_stale(tmp_path: Path) -> None:
    repo, package = _checkout(tmp_path)
    _on_release(repo, None)
    _stamp(runtime_bundle_dir(package), "0.2.0")
    assert locate(package).kind == "stale"
    # A developer's own build still wins over everything.
    write_bundle(dev_bundle_dir(repo))
    assert locate(package).kind == "source"


def test_unstamped_runtime_pages_are_never_stale(tmp_path: Path) -> None:
    repo, package = _checkout(tmp_path)
    _on_release(repo, "0.3.0")
    write_bundle(runtime_bundle_dir(package))  # published by a build, not fetched
    assert locate(package).kind == "installed"


def test_an_installed_tree_compares_the_stamp_with_its_package_version(tmp_path: Path) -> None:
    package = _installed(tmp_path)
    _stamp(runtime_bundle_dir(package), "0.2.0")
    assert locate(package, release=lambda _pkg: "0.2.0").kind == "installed"
    stale = locate(package, release=lambda _pkg: "0.3.0")
    assert stale == BundleLocation(runtime_bundle_dir(package), "stale", stamped="0.2.0")
    assert locate(package, release=lambda _pkg: None).kind == "installed", "unknown: not judged"


def test_first_launch_names_stale_pages(
    scope: Scope,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from indus.web import spa

    stale = BundleLocation(tmp_path / "dist", "stale", stamped="0.2.0")
    monkeypatch.setattr(bundle_module, "locate", lambda: stale)
    monkeypatch.setattr(bundle_module, "source_checkout", lambda: tmp_path)
    monkeypatch.setattr(spa, "build_plan", lambda repo, env: None)
    _maybe_build(Launch(scope=scope, mode="dashboard", host="127.0.0.1", port=0))
    said = capsys.readouterr().err
    assert "0.2.0" in said and "indus.web.bundle fetch" in said


def test_publish_copies_and_replaces(tmp_path: Path) -> None:
    source = write_bundle(tmp_path / "dist")
    target = tmp_path / "pkg" / "web" / "static"
    assert publish(source, target) == target
    assert (target / "index.html").read_text(encoding="utf-8") == INDEX_HTML
    assert (target / "assets" / "index-abc123.js").is_file()

    (target / "stale.txt").write_text("old build", encoding="utf-8")
    (source / "index.html").write_text("<html>new</html>", encoding="utf-8")
    publish(source, target)
    assert (target / "index.html").read_text(encoding="utf-8") == "<html>new</html>"
    assert not (target / "stale.txt").exists(), "the old copy is replaced, not merged"
    assert sorted(p.name for p in target.parent.iterdir()) == ["static"]


def test_publish_refuses_an_unfinished_build(tmp_path: Path) -> None:
    source = tmp_path / "dist"
    source.mkdir()
    target = write_bundle(tmp_path / "static")
    with pytest.raises(PublishError):
        publish(source, target)
    assert (target / "index.html").is_file(), "the old pages stay"


def test_publish_clears_leftovers_of_an_interrupted_run(tmp_path: Path) -> None:
    source = write_bundle(tmp_path / "dist")
    target = write_bundle(tmp_path / "pkg" / "static")
    (target.parent / "static.new").mkdir()
    (target.parent / "static.new" / "junk").write_text("x", encoding="utf-8")
    (target.parent / "static.old").write_text("x", encoding="utf-8")
    publish(source, target)
    assert sorted(p.name for p in target.parent.iterdir()) == ["static"]


def test_a_failed_copy_keeps_the_old_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = write_bundle(tmp_path / "dist")
    target = write_bundle(tmp_path / "pkg" / "static")
    (target / "index.html").write_text("<html>old</html>", encoding="utf-8")

    def broken(*args: Any, **kwargs: Any) -> None:
        stage = Path(args[1])
        stage.mkdir()
        (stage / "partial").write_text("x", encoding="utf-8")
        raise OSError("disk full")

    monkeypatch.setattr(bundle_module.shutil, "copytree", broken)
    with pytest.raises(OSError, match="disk full"):
        publish(source, target)
    assert (target / "index.html").read_text(encoding="utf-8") == "<html>old</html>"
    assert sorted(p.name for p in target.parent.iterdir()) == ["static"]


def test_publish_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = write_bundle(tmp_path / "dist")
    target = tmp_path / "static"
    assert bundle_module.main(["publish", "--source", str(source), "--target", str(target)]) == 0
    assert (target / "index.html").is_file()
    assert bundle_module.main(["publish", "--source", str(tmp_path / "nope"), "--target", "x"]) == 1
    assert "Could not publish" in capsys.readouterr().err


def test_the_app_serves_the_located_bundle(
    scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette.testclient import TestClient

    located = write_bundle(tmp_path / "located")
    monkeypatch.setattr("indus.web.app.locate_bundle", lambda: BundleLocation(located, "installed"))
    app = create_app(scope, "dashboard", options=AppOptions(config={}))
    with TestClient(app, base_url="http://127.0.0.1:9119") as client:
        page = client.get("/sessions")
        assert page.status_code == 200 and "index-abc123.js" in page.text
        assert client.get("/assets/index-abc123.js").text == "console.log(1)"


def test_catch_alls_come_after_every_api_route(build: Builder) -> None:
    from fastapi import APIRouter

    extra = APIRouter()

    @extra.get("/api/zz-extra")
    async def zz() -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        return {"extra": True}

    built = build(routers=[extra])
    catch_alls = {"/api", "/api/{rest:path}", "/assets/{name:path}", "/{rest:path}"}
    routes = built.app.routes
    first = next(i for i, route in enumerate(routes) if getattr(route, "path", "") in catch_alls)
    # Everything after the first catch-all is a catch-all (plain routes, no included router).
    tail = routes[first:]
    assert all(isinstance(route, Route) and route.path in catch_alls for route in tail)
    assert {route.path for route in tail if isinstance(route, Route)} == catch_alls
    answer = built.client.get("/api/zz-extra", headers=built.auth())
    assert answer.json() == {"extra": True}
    missing = built.client.get("/api/zz-missing", headers=built.auth())
    assert missing.status_code == 404 and missing.json()["error"] == "not_found"


def test_serve_mode_never_serves_the_pages(build: Builder) -> None:
    built = build(mode="serve")
    for path in ("/", "/sessions", "/assets/index-abc123.js", "/index.html"):
        answer = built.client.get(path, headers=built.auth())
        assert answer.status_code == 404
        assert answer.headers["content-type"].startswith("application/json")


def test_first_launch_build_is_skipped_when_pages_exist(
    scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from indus.web import spa

    planned: list[Path] = []
    monkeypatch.setattr(
        bundle_module, "locate", lambda: BundleLocation(write_bundle(tmp_path / "b"), "source")
    )
    monkeypatch.setattr(spa, "build_plan", lambda repo, env: planned.append(repo))
    _maybe_build(Launch(scope=scope, mode="dashboard", host="127.0.0.1", port=0))
    assert planned == []


def test_first_launch_build_runs_only_in_a_checkout(
    scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from indus.web import spa

    planned: list[Path] = []
    missing = BundleLocation(tmp_path / "none", "missing")
    monkeypatch.setattr(bundle_module, "locate", lambda: missing)
    monkeypatch.setattr(spa, "build_plan", lambda repo, env: planned.append(repo))
    monkeypatch.setattr(bundle_module, "source_checkout", lambda: None)
    _maybe_build(Launch(scope=scope, mode="dashboard", host="127.0.0.1", port=0))
    assert planned == [], "an installed tree never builds"

    monkeypatch.setattr(bundle_module, "source_checkout", lambda: tmp_path)
    _maybe_build(Launch(scope=scope, mode="dashboard", host="127.0.0.1", port=0))
    assert planned == [tmp_path]


def test_the_wheel_ships_the_published_pages() -> None:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = data["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert "src/indus/web/static/" in wheel["artifacts"]
    assert wheel["packages"] == ["src/indus"]
    ignored = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "src/indus/web/static/" in ignored
