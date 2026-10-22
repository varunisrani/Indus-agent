"""P18-01: SPA serving — catch-all, JSON 404s, traversal, cache headers, bootstrap script."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from indus.web.spa import (
    BUILD_COMMAND,
    IMMUTABLE,
    SpaBundle,
    build_plan,
    cgroup_memory_bytes,
    heap_mib,
    run_build,
)
from tests.unit.web.conftest import Builder, basic_config, write_bundle


def test_unknown_api_path_is_a_json_404_for_every_method(build: Builder) -> None:
    built = build()
    for method in ("GET", "POST", "DELETE", "PUT"):
        response = built.client.request(method, "/api/nope", headers=built.auth())
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")
        assert "/api/nope" in response.json()["detail"]
    bare = built.client.get("/api", headers=built.auth())
    assert bare.status_code == 404 and bare.json()["error"] == "not_found"


def test_client_routes_get_the_index_with_no_store(build: Builder) -> None:
    built = build()
    for path in ("/", "/sessions", "/chat/deep/link", "/index.html"):
        response = built.client.get(path)
        assert response.status_code == 200, path
        assert response.headers["cache-control"] == "no-store"
        assert "<div id=root>" in response.text


def test_hashed_assets_are_immutable(build: Builder) -> None:
    built = build()
    js = built.client.get("/assets/index-abc123.js")
    css = built.client.get("/assets/index-abc123.css")
    assert js.status_code == css.status_code == 200
    assert js.headers["cache-control"] == IMMUTABLE == css.headers["cache-control"]
    assert built.client.get("/assets/missing.js").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/x/..%2f..%2fetc/passwd",
        "/..%2f..%2fetc/passwd",
        "/assets/..%2findex.html",
        "/assets/%2e%2e/%2e%2e/secret.txt",
        "/x%5c..%5c..%5csecret.txt",
    ],
)
def test_traversal_is_refused(build: Builder, bundle_dir: Path, path: str) -> None:
    (bundle_dir.parent / "secret.txt").write_text("top secret", encoding="utf-8")
    built = build()
    response = built.client.get(path)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert "top secret" not in response.text


def test_files_outside_the_bundle_through_a_link_are_refused(
    build: Builder, bundle_dir: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("not for you", encoding="utf-8")
    (bundle_dir / "link.txt").symlink_to(outside)
    response = build().client.get("/link.txt")
    assert "not for you" not in response.text


def test_bootstrap_globals_are_escaped(build: Builder) -> None:
    built = build(initial_profile="</script><script>alert(1)</script>")
    page = built.client.get("/").text
    assert "</script><script>alert(1)" not in page
    assert "\\u003c/script\\u003e" in page
    scripts = re.findall(r"<script>(.*?)</script>", page, flags=re.S)
    assert len(scripts) == 1
    body = scripts[0]
    for name in (
        "__INDUS_SESSION_TOKEN__",
        "__INDUS_BASE_PATH__",
        "__INDUS_AUTH_REQUIRED__",
        "__INDUS_DASHBOARD_EMBEDDED_CHAT__",
        "__INDUS_DASHBOARD_CONSOLE__",
        "__INDUS_INITIAL_PROFILE__",
        "__INDUS_DASHBOARD_PROFILE__",
    ):
        assert f"window.{name}=" in body
    assert built.token in body
    assert page.index('id="indus-theme-bootstrap"') < page.index("<script>window.")
    assert page.index("<script>window.") < page.index("</head>")


def test_gated_index_carries_no_token(build: Builder) -> None:
    built = build(host="0.0.0.0", config=basic_config())
    page = built.client.get("/login")
    assert page.status_code == 200
    # the SPA shell needs a session in gated mode; the token is never on any page
    assert built.token not in page.text
    logged = built.client.post(
        "/auth/password-login", json={"username": "admin", "password": "correct horse"}
    )
    assert logged.status_code == 200
    index = built.client.get("/")
    assert index.status_code == 200
    assert built.token not in index.text
    assert "__INDUS_SESSION_TOKEN__" not in index.text
    assert "window.__INDUS_AUTH_REQUIRED__=true" in index.text


def test_no_bundle_answer_may_be_framed(build: Builder) -> None:
    """SEC2-1 (P25-03): the loopback index holds the session token; a framing site must get
    nothing it can drive, so every answer from the bundle refuses framing and sniffing."""
    built = build()
    for path in ("/", "/sessions", "/index.html", "/favicon.ico", "/assets/index-abc123.js"):
        response = built.client.get(path)
        assert response.status_code == 200, path
        assert response.headers["x-frame-options"] == "DENY", path
        csp = response.headers["content-security-policy"]
        if path in ("/", "/sessions", "/index.html"):  # SEC-18: the index adds its script policy
            assert csp.startswith("frame-ancestors 'none'; script-src 'self' 'sha256-"), path
        else:
            assert csp == "frame-ancestors 'none'", path
        assert response.headers["x-content-type-options"] == "nosniff", path
    css = built.client.get("/assets/index-abc123.css", headers={"X-Forwarded-Prefix": "/x"})
    assert css.headers["x-frame-options"] == "DENY"
    gated = build(host="0.0.0.0", config=basic_config())
    gated.client.post(
        "/auth/password-login", json={"username": "admin", "password": "correct horse"}
    )
    index = gated.client.get("/")
    assert index.status_code == 200
    assert index.headers["x-frame-options"] == "DENY"


def test_missing_bundle_then_built_bundle_without_restart(build: Builder, tmp_path: Path) -> None:
    later = tmp_path / "later-dist"
    built = build(bundle=later)
    first = built.client.get("/sessions")
    assert first.status_code == 404
    assert first.json()["build"] == BUILD_COMMAND
    assert built.client.get("/assets/index-abc123.js").status_code == 404
    write_bundle(later)
    assert built.client.get("/sessions").status_code == 200
    assert built.client.get("/assets/index-abc123.js").status_code == 200
    (later / "index.html").unlink()
    assert built.client.get("/").status_code == 404
    shutil.rmtree(later)
    assert built.client.get("/").json()["error"] == "not_built"


def test_theme_setting_picks_first_paint_css(build: Builder) -> None:
    dark = build(config={"dashboard": {"theme": "dark"}}).client.get("/").text
    assert "color-scheme:dark" in dark
    system = build().client.get("/").text
    assert "prefers-color-scheme: dark" in system


def test_bundle_inside_rules(tmp_path: Path) -> None:
    bundle = SpaBundle(write_bundle(tmp_path / "b"))
    assert bundle.inside("assets/index-abc123.js") is not None
    for bad in ("", "../x", "assets/../../x", "a\\b", "a\x00b", "assets"):
        assert bundle.inside(bad) is None


def test_build_plan_caps_heap_and_threads(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "apps" / "web").mkdir(parents=True)
    assert build_plan(repo, {}, which=lambda name: "/bin/pnpm") is None
    (repo / "apps" / "web" / "package.json").write_text("{}", encoding="utf-8")
    assert build_plan(repo, {}, which=lambda name: None) is None
    plan = build_plan(
        repo,
        {"NODE_OPTIONS": "--trace-warnings"},
        which=lambda name: "/opt/pnpm",
        memory_limit=lambda: 3 * 1024**3,
        cpu_count=lambda: 8,
    )
    assert plan is not None
    assert plan.argv == ("/opt/pnpm", "--filter", "@indus/web", "build")
    assert plan.heap_mib == 1536 and plan.threads == 4
    assert plan.env["NODE_OPTIONS"] == "--trace-warnings --max-old-space-size=1536"
    assert plan.env["UV_THREADPOOL_SIZE"] == "4"
    assert heap_mib(None) == 4096 and heap_mib(256 * 1024**2) == 512
    calls: list[list[str]] = []

    class Done:
        returncode = 0

    def runner(argv: list[str], **kwargs: object) -> Done:
        calls.append(argv)
        return Done()

    assert run_build(plan, runner=runner)
    assert calls == [list(plan.argv)]


def test_cgroup_limits_are_read(tmp_path: Path) -> None:
    (tmp_path / "memory.max").write_text("max\n", encoding="ascii")
    assert cgroup_memory_bytes(tmp_path) is None
    (tmp_path / "memory.max").write_text("2147483648\n", encoding="ascii")
    assert cgroup_memory_bytes(tmp_path) == 2147483648
    (tmp_path / "memory.max").unlink()
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "memory.limit_in_bytes").write_text("1073741824", encoding="ascii")
    assert cgroup_memory_bytes(tmp_path) == 1073741824
