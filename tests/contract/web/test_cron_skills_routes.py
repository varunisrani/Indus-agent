"""P18-14: the Cron and Skills REST routes, the public fire webhook and its signed token."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.cron import blueprints as bps
from indus.cron.ports import CronBackend
from indus.foundation.config import loader as config_loader
from indus.foundation.scope import Scope
from indus.learning.skills.hub.lock import HubLock
from indus.learning.skills.resolver import clear_scan_cache
from indus.service import cron_host
from indus.web.routes import cron, skills
from indus.web.routes.cron import FireClaims, mint_fire_token, read_fire_token
from tests.contract.web.rest_app import (
    SESSION_HEADER,
    RestApp,
    build_rest_app,
    route_table,
    unscoped_writes,
)
from tests.fakes.clock import FakeClock
from tests.unit.learning.skills.hub._web import FakeWeb

WRITE = {"profile": "work"}
# Built at runtime so no scanner-bait text sits in the repo as one line.
DANGEROUS_LINE = "rm -rf " + "/"
HUB = "https://skills.example.com"


class Runs:
    """The ``cron.runner`` seam: records the runs a route started; ``busy`` ids read as running."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.busy: set[str] = set()

    def __call__(self, _backend: CronBackend, job_id: str) -> None:
        self.started.append(job_id)


class BusyAware:
    """The real backend, except that ``is_running`` answers from ``Runs.busy``."""

    def __init__(self, inner: CronBackend, runs: Runs) -> None:
        self._inner = inner
        self._runs = runs

    def is_running(self, job_id: str) -> bool:
        return job_id in self._runs.busy

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.fixture
def runs() -> Runs:
    return Runs()


@pytest.fixture
def web() -> FakeWeb:
    return FakeWeb()


@pytest.fixture
def rest(scope: Scope, runs: Runs, web: FakeWeb) -> Iterator[RestApp]:
    config_loader.reset()
    clear_scan_cache()

    def backend(used: Scope) -> Any:
        cron_host.install()
        return BusyAware(cron_host.CronHost(used), runs)

    built = build_rest_app(
        scope,
        extras={
            "cron.backend": backend,
            "cron.runner": runs,
            "skills.hub_sources": web.sources,
        },
    )
    built.client.params = WRITE  # the test root holds two profiles: writes must name one
    yield built
    built.close()
    config_loader.reset()
    clear_scan_cache()


def _code(answer: Any) -> str:
    return answer.json()["detail"]["code"]


def _new_job(rest: RestApp, **fields: Any) -> dict[str, Any]:
    body = {"name": "digest", "prompt": "Summarise the news.", "schedule": "every 30m", **fields}
    answer = rest.client.post("/api/cron/jobs", json=body)
    assert answer.status_code == 201, answer.text
    return answer.json()


# -- cron jobs -------------------------------------------------------------------------------------


def test_cron_crud_pause_resume(rest: RestApp) -> None:
    job = _new_job(rest)
    job_id = job["id"]
    assert job["schedule"] and job["deliver"] == "local" and job["enabled"] is True
    listed = rest.client.get("/api/cron/jobs").json()["jobs"]
    assert [j["id"] for j in listed] == [job_id]
    assert rest.client.get(f"/api/cron/jobs/{job_id}").json()["name"] == "digest"

    changed = rest.client.put(f"/api/cron/jobs/{job_id}", json={"schedule": "every 2h"})
    assert changed.status_code == 200, changed.text
    assert "2" in changed.json()["schedule"]

    paused = rest.client.post(f"/api/cron/jobs/{job_id}/pause").json()
    assert paused["state"] == "paused" and paused["paused_reason"]
    resumed = rest.client.post(f"/api/cron/jobs/{job_id}/resume").json()
    assert resumed["state"] == "scheduled" and resumed["enabled"] is True

    assert rest.client.delete(f"/api/cron/jobs/{job_id}").json() == {"ok": True}
    gone = rest.client.get(f"/api/cron/jobs/{job_id}")
    assert gone.status_code == 404 and _code(gone) == "job_not_found"


def test_bad_schedule_deliver_and_body_are_refused(rest: RestApp) -> None:
    bad = rest.client.post("/api/cron/jobs", json={"prompt": "x", "schedule": "whenever you like"})
    assert bad.status_code == 400 and _code(bad) == "invalid_job"
    target = rest.client.post(
        "/api/cron/jobs", json={"prompt": "x", "schedule": "every 1h", "deliver": "pigeon:42"}
    )
    assert target.status_code == 400 and _code(target) == "invalid_deliver"
    assert rest.client.post("/api/cron/jobs", json={"schedule": "every 1h"}).status_code == 422
    assert rest.client.put("/api/cron/jobs/nope", json={"name": "x"}).status_code == 404


def test_trigger_runs_in_the_background_and_refuses_a_second(rest: RestApp, runs: Runs) -> None:
    job_id = _new_job(rest)["id"]
    first = rest.client.post(f"/api/cron/jobs/{job_id}/trigger")
    assert first.status_code == 202 and first.json()["id"] == job_id
    assert runs.started == [job_id]
    runs.busy.add(job_id)
    second = rest.client.post(f"/api/cron/jobs/{job_id}/trigger")
    assert second.status_code == 409 and _code(second) == "job_running"
    assert runs.started == [job_id]


def test_delivery_targets_list_local_and_configured_platforms(rest: RestApp, scope: Scope) -> None:
    (scope.home / "config.yaml").write_text(
        "platforms:\n"
        "  telegram:\n"
        "    home_channel:\n"
        "      chat_id: '12345'\n"
        "  discord:\n"
        "    enabled: false\n"
        "    home_channel:\n"
        "      chat_id: '678'\n"
        "  email:\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    rows = rest.client.get("/api/cron/delivery-targets").json()["targets"]
    assert rows[0] == {"id": "local", "label": "Save locally", "kind": "local", "available": True}
    by_id = {row["id"]: row for row in rows[1:]}
    assert by_id["telegram"]["available"] is True and by_id["telegram"]["kind"] == "platform"
    assert by_id["discord"]["available"] is False
    assert by_id["email"] == {"id": "email", "label": "Email", "kind": "email", "available": False}
    assert "slack" not in by_id  # never set up


def test_blueprints_list_the_starter_catalogue_then_the_skills(rest: RestApp, scope: Scope) -> None:
    _skill(
        scope,
        "morning-brief",
        "Daily brief.",
        extra="metadata:\n  indus:\n    blueprint:\n      schedule: daily at 8am\n"
        "      prompt: Write my brief.\n",
    )
    _skill(scope, "plain", "No blueprint.")
    rows = rest.client.get("/api/cron/blueprints").json()["blueprints"]
    starters = rows[: len(bps.CATALOGUE)]
    assert [r["key"] for r in starters] == [bp.key for bp in bps.CATALOGUE]
    assert {r["source"] for r in starters} == {"catalogue"}
    morning = starters[0]
    assert morning["title"] == "Morning briefing" and morning["skill"] is None
    assert morning["schedule"] == "0 8 * * 1-5" and "{" not in morning["prompt"]
    assert [f["name"] for f in morning["fields"]] == ["time", "days", "topics"]
    assert rows[len(bps.CATALOGUE) :] == [
        {
            "key": "morning-brief:blueprint",
            "title": "morning-brief",
            "source": "skill",
            "skill": "morning-brief",
            "description": "Daily brief.",
            "schedule": "daily at 8am",
            "prompt": "Write my brief.",
            "fields": [],
        }
    ]
    assert len({r["key"] for r in rows}) == len(rows)


def test_a_skills_slotted_blueprints_replace_its_plain_block(rest: RestApp, scope: Scope) -> None:
    _skill(
        scope,
        "releases",
        "Release notes.",
        extra="blueprint:\n  key: rollup\n  title: Release rollup\n"
        "  schedule: '{minute} {hour} * * 1-5'\n  prompt: Summarise {repo}.\n"
        "  slots:\n    - {name: time, kind: time, default: '09:15'}\n"
        "    - {name: repo, kind: text}\n"
        "metadata:\n  indus:\n    blueprint:\n      schedule: daily\n      prompt: Old.\n",
    )
    rows = rest.client.get("/api/cron/blueprints").json()["blueprints"]
    skill_rows = [r for r in rows if r["source"] == "skill"]
    assert [r["key"] for r in skill_rows] == ["releases:rollup"]
    assert skill_rows[0]["skill"] == "releases" and skill_rows[0]["title"] == "Release rollup"
    assert skill_rows[0]["schedule"] == "15 9 * * 1-5"
    assert skill_rows[0]["prompt"] == "Summarise {repo}."
    off = rest.client.post("/api/skills/toggle", json={"name": "releases", "enabled": False})
    assert off.status_code == 200, off.text
    rows = rest.client.get("/api/cron/blueprints").json()["blueprints"]
    assert [r["key"] for r in rows] == [bp.key for bp in bps.CATALOGUE]  # a disabled skill: none


# -- the fire webhook ------------------------------------------------------------------------------


def _token(rest: RestApp, job_id: str, **body: Any) -> str:
    answer = rest.client.post(f"/api/cron/jobs/{job_id}/fire-token", json=body or None)
    assert answer.status_code == 200, answer.text
    assert answer.json()["path"] == "/api/cron/fire"
    assert answer.headers["cache-control"] == "no-store"
    return answer.json()["token"]


def _fire(rest: RestApp, token: str | None, body: Any = None) -> Any:
    headers = {"authorization": f"Bearer {token}"} if token is not None else {}
    return rest.anonymous().post("/api/cron/fire", json=body, headers=headers)


def test_fire_with_a_good_token_starts_the_job_without_the_session_token(
    rest: RestApp, runs: Runs, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    job_id = _new_job(rest)["id"]
    token = _token(rest, job_id)
    answer = _fire(rest, token, {"job_id": job_id})
    assert answer.status_code == 202
    assert answer.json() == {"status": "accepted", "job_id": job_id}
    assert runs.started == [job_id]
    assert token not in caplog.text and token.split(".")[1] not in caplog.text


def test_fire_token_expires(rest: RestApp, runs: Runs, fake_clock: FakeClock) -> None:
    job_id = _new_job(rest)["id"]
    token = _token(rest, job_id, ttl_s=60)
    fake_clock.advance(59)
    assert _fire(rest, token).status_code == 202
    fake_clock.advance(1)  # exactly at the expiry: no longer valid
    late = _fire(rest, token)
    assert late.status_code == 401 and _code(late) == "invalid_fire_token"
    assert runs.started == [job_id]


def test_fire_refuses_bad_tokens_with_one_answer(rest: RestApp, runs: Runs, scope: Scope) -> None:
    job_id = _new_job(rest)["id"]
    other_id = _new_job(rest, name="other")["id"]
    token = _token(rest, job_id)
    payload, _, signature = token.partition(".")
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    foreign = mint_fire_token(
        b"k" * 32, FireClaims(profile="work", job_id=job_id, expires_at=4_000_000_000)
    )
    answers = [
        _fire(rest, None),
        _fire(rest, ""),
        _fire(rest, "not-a-token"),
        _fire(rest, f"{payload}.{flipped}"),
        _fire(rest, foreign),
        _fire(rest, token, {"job_id": other_id}),
    ]
    bodies = {a.text for a in answers}
    assert [a.status_code for a in answers] == [401] * 6 and len(bodies) == 1
    assert runs.started == []
    assert (
        rest.client.post(f"/api/cron/jobs/{job_id}/fire-token", json={"ttl_s": 7200}).status_code
        == 422
    )


def test_fire_for_a_deleted_job_is_gone(rest: RestApp, runs: Runs) -> None:
    job_id = _new_job(rest)["id"]
    token = _token(rest, job_id)
    rest.client.delete(f"/api/cron/jobs/{job_id}")
    answer = _fire(rest, token)
    assert answer.status_code == 200 and answer.json() == {"status": "gone", "job_id": job_id}
    assert runs.started == []


def test_fire_token_helpers_round_trip() -> None:
    key = b"s" * 32
    claims = FireClaims(profile="work", job_id="abc", expires_at=100.0)
    token = mint_fire_token(key, claims)
    assert read_fire_token(key, token, 99.9) == claims
    assert read_fire_token(key, token, 100.0) is None
    assert read_fire_token(b"t" * 32, token, 1.0) is None


# -- skills ----------------------------------------------------------------------------------------


def _skill(scope: Scope, name: str, description: str, *, extra: str = "", body: str = "") -> Path:
    folder = scope.home / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    text = f"---\nname: {name}\ndescription: {description}\n{extra}---\n\n# {name}\n\n{body}\n"
    (folder / "SKILL.md").write_text(text, encoding="utf-8")
    return folder


def test_skill_toggle_round_trips_through_config(rest: RestApp, scope: Scope) -> None:
    _skill(scope, "alpha", "First.")
    _skill(scope, "beta", "Second.")
    rows = {r["name"]: r for r in rest.client.get("/api/skills").json()["skills"]}
    assert rows["alpha"]["enabled"] is True and rows["alpha"]["source"] == "local"
    off = rest.client.post("/api/skills/toggle", json={"name": "alpha", "enabled": False})
    assert off.json() == {"ok": True, "name": "alpha", "enabled": False}
    assert "alpha" in (scope.home / "config.yaml").read_text(encoding="utf-8")
    rows = {r["name"]: r for r in rest.client.get("/api/skills").json()["skills"]}
    assert rows["alpha"]["enabled"] is False and rows["beta"]["enabled"] is True
    rest.client.post("/api/skills/toggle", json={"name": "alpha", "enabled": True})
    rows = {r["name"]: r for r in rest.client.get("/api/skills").json()["skills"]}
    assert rows["alpha"]["enabled"] is True
    missing = rest.client.post("/api/skills/toggle", json={"name": "nope", "enabled": False})
    assert missing.status_code == 404 and _code(missing) == "skill_not_found"


SKILL_TEXT = (
    "---\nname: release-notes\ndescription: Draft release notes.\n---\n\n# Notes\n\nStep.\n"
)


def test_skill_create_saves_through_the_skill_tool(rest: RestApp, scope: Scope) -> None:
    made = rest.client.post(
        "/api/skills/create",
        json={"name": "release-notes", "content": SKILL_TEXT, "category": "writing"},
    )
    assert made.status_code == 200, made.text
    assert made.json()["ok"] is True and made.json()["staged"] is False
    path = scope.home / "skills" / "writing" / "release-notes" / "SKILL.md"
    assert path.read_text(encoding="utf-8") == SKILL_TEXT
    rows = {r["name"]: r for r in rest.client.get("/api/skills").json()["skills"]}
    assert rows["release-notes"]["category"] == "writing"
    again = rest.client.post(
        "/api/skills/create", json={"name": "release-notes", "content": SKILL_TEXT}
    )
    assert again.status_code == 400 and _code(again) == "skill_refused"
    assert "already exists" in again.json()["detail"]["message"]


def test_skill_create_refuses_bad_names_and_text_and_writes_nothing(
    rest: RestApp, scope: Scope
) -> None:
    bad_name = rest.client.post("/api/skills/create", json={"name": "../up", "content": SKILL_TEXT})
    assert bad_name.status_code == 400 and _code(bad_name) == "skill_refused"
    no_front = rest.client.post("/api/skills/create", json={"name": "plain", "content": "# hi\n"})
    assert no_front.status_code == 400 and _code(no_front) == "skill_refused"
    assert rest.client.post("/api/skills/create", json={"name": "x"}).status_code == 422
    assert not (scope.home / "skills" / "plain").exists()
    assert not (scope.home / "up").exists()


def test_skill_create_is_held_when_write_approval_is_on(rest: RestApp, scope: Scope) -> None:
    (scope.home / "config.yaml").write_text("skills:\n  write_approval: true\n", encoding="utf-8")
    held = rest.client.post(
        "/api/skills/create", json={"name": "release-notes", "content": SKILL_TEXT}
    )
    assert held.status_code == 200 and held.json()["staged"] is True
    assert not (scope.home / "skills" / "release-notes").exists()


def test_skill_create_needs_the_dashboard_session(rest: RestApp, scope: Scope) -> None:
    anonymous = rest.anonymous()
    refused = anonymous.post(
        "/api/skills/create",
        params=WRITE,
        json={"name": "release-notes", "content": SKILL_TEXT},
    )
    assert refused.status_code == 401
    assert not (scope.home / "skills" / "release-notes").exists()


def test_toolsets_list_plain_toolsets_with_tools(rest: RestApp) -> None:
    rows = rest.client.get("/api/tools/toolsets").json()["toolsets"]
    assert rows and all(
        set(r) == {"name", "label", "description", "enabled", "tools"} for r in rows
    )
    assert not any(r["name"].startswith("indus-") or r["name"] == "core" for r in rows)
    assert any(r["enabled"] for r in rows)


def _hub_skill(web: FakeWeb, name: str, body: str) -> str:
    text = f"---\nname: {name}\ndescription: The {name} skill.\n---\n\n# {name}\n\n{body}\n"
    url = f"{HUB}/{name}/SKILL.md"
    web.add(url, text)
    return url


def test_hub_install_of_a_dangerous_skill_is_409_with_the_verdict(
    rest: RestApp, scope: Scope, web: FakeWeb
) -> None:
    evil = _hub_skill(web, "evil", f"    {DANGEROUS_LINE}\n")
    for force in (False, True):
        answer = rest.client.post(
            "/api/skills/hub/install", json={"identifier": evil, "force": force}
        )
        assert answer.status_code == 409
        assert answer.json()["detail"]["code"] == "scan_blocked"
        assert answer.json()["detail"]["verdict"] == "dangerous"
    assert not (scope.home / "skills" / "evil").exists()
    assert HubLock(scope.home / "skills").get("evil") is None


def test_hub_install_inspect_and_uninstall_a_safe_skill(
    rest: RestApp, scope: Scope, web: FakeWeb
) -> None:
    plain = _hub_skill(web, "plain", "Do the thing.")
    seen = rest.client.get("/api/skills/hub/inspect", params={"identifier": plain}).json()
    assert seen["verdict"] == "safe" and seen["allowed"] is True
    assert not (scope.home / "skills" / "plain").exists()
    done = rest.client.post("/api/skills/hub/install", json={"identifier": plain})
    assert done.status_code == 200 and done.json() == {
        "ok": True,
        "name": "plain",
        "verdict": "safe",
    }
    again = rest.client.post("/api/skills/hub/install", json={"identifier": plain})
    assert again.status_code == 409 and _code(again) == "already_installed"
    rows = {r["name"]: r for r in rest.client.get("/api/skills").json()["skills"]}
    assert rows["plain"]["source"] == "hub"
    gone = rest.client.post("/api/skills/hub/uninstall", json={"name": "plain"})
    assert gone.json()["ok"] is True and not (scope.home / "skills" / "plain").exists()
    missing = rest.client.post("/api/skills/hub/uninstall", json={"name": "plain"})
    assert missing.status_code == 404 and _code(missing) == "not_a_hub_skill"


def test_hub_search_marks_no_verdict(rest: RestApp) -> None:
    assert rest.client.get("/api/skills/hub/search", params={"q": " "}).json() == {"results": []}


# -- scope rules and the route list ----------------------------------------------------------------


def test_writes_need_a_profile_on_a_multi_profile_host(rest: RestApp, scope: Scope) -> None:
    unnamed = rest.anonymous()
    unnamed.headers[SESSION_HEADER] = rest.state.auth.token.value
    refused = unnamed.post("/api/cron/jobs", json={"prompt": "x", "schedule": "every 1h"})
    assert refused.status_code == 400
    named = rest.client.post("/api/cron/jobs", json={"prompt": "x", "schedule": "every 1h"})
    assert named.status_code == 201
    toggle = unnamed.post("/api/skills/toggle", json={"name": "x", "enabled": True})
    assert toggle.status_code == 400
    assert unnamed.get("/api/cron/jobs").status_code == 200  # reads default to the backend's


def test_every_write_depends_on_the_write_scope_except_the_fire_webhook(rest: RestApp) -> None:
    prefixes = ("/api/cron", "/api/skills", "/api/tools")
    assert unscoped_writes(rest.app, prefixes) == ["POST /api/cron/fire"]


def test_route_lists_match_the_app(rest: RestApp) -> None:
    assert route_table(rest.app, ("/api/cron",)) == set(cron.ROUTE_LIST)
    assert route_table(rest.app, ("/api/skills", "/api/tools")) == set(skills.ROUTE_LIST)
