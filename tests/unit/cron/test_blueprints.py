"""P24-46: blueprints — slot schemas, filling, skill blueprints, renderings, ``/blueprint``."""

from __future__ import annotations

import json
import shlex
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from indus.cli.args import CliState
from indus.cli.commands.blueprint import blueprint_group
from indus.cli.slash.cron_cmds import blueprint_cmd
from indus.cron import blueprints as bps
from indus.cron import ports
from indus.cron.grammar import validate_cron
from indus.foundation.scope import Scope
from tests.unit.cli.slash._fakes import make_ctx
from tests.unit.cron.world import RecordingHost


@pytest.fixture
def backend(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> Iterator[RecordingHost]:
    host = RecordingHost(scope)
    monkeypatch.setattr(ports, "BACKEND_FACTORY", lambda s: host)
    yield host


def by_key(key: str) -> bps.Blueprint:
    return next(bp for bp in bps.CATALOGUE if bp.key == key)


def skill(scope: Scope, name: str, frontmatter: str) -> Path:
    folder = scope.home / "skills" / "automation" / name
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "SKILL.md"
    path.write_text(f"---\nname: {name}\ndescription: d\n{frontmatter}---\n\n# {name}\n")
    return path


ROLLUP = """\
metadata:
  indus:
    blueprints:
      - key: rollup
        title: Release rollup
        description: Lists merged changes.
        schedule: "{minute} {hour} * * {dow}"
        prompt: "Summarise what merged into {repo} since yesterday."
        slots:
          - {name: time, kind: time, default: "09:15"}
          - {name: days, kind: weekdays, default: weekdays}
          - {name: repo, kind: text, label: Repository}
      - key: broken
        title: Broken one
        schedule: "{minute} {hour} {day} * *"
        prompt: p
        slots:
          - {name: time, kind: time, default: "09:15"}
"""


# -- slots --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("7:05", "07:05"), ("23:59", "23:59"), ("00:00", "00:00")],
)
def test_time_slot_accepts(raw: str, expected: str) -> None:
    assert bps.Slot("t", "time", "T").check(raw) == expected


@pytest.mark.parametrize("raw", ["25:00", "12:60", "9am", "noon", "1230"])
def test_time_slot_refuses(raw: str) -> None:
    with pytest.raises(bps.BlueprintError):
        bps.Slot("t", "time", "T").check(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("daily", "daily"),
        ("Weekdays", "weekdays"),
        ("weekends", "weekends"),
        ("mon,Wednesday fri", "mon,wed,fri"),
        ("0,6", "sun,sat"),
        ("sat, 1", "mon,sat"),
    ],
)
def test_weekday_slot_accepts(raw: str, expected: str) -> None:
    assert bps.Slot("d", "weekdays", "D").check(raw) == expected


@pytest.mark.parametrize("raw", ["8", "7", "mon,8", "someday", ","])
def test_weekday_slot_outside_schema_refused(raw: str) -> None:
    with pytest.raises(bps.BlueprintError):
        bps.Slot("d", "weekdays", "D").check(raw)


def test_choice_interval_and_text_slots() -> None:
    choice = bps.Slot("c", "choice", "C", options=("brief", "full"))
    assert choice.check("FULL") == "full"
    with pytest.raises(bps.BlueprintError, match="one of brief, full"):
        choice.check("medium")
    interval = bps.Slot("i", "interval", "I", minimum=15, maximum=120)
    assert interval.check("30") == "30" and interval.check("45m") == "45"
    for bad in ("10", "121", "often"):
        with pytest.raises(bps.BlueprintError):
            interval.check(bad)
    text = bps.Slot("x", "text", "X", maximum=10_000)
    assert text.maximum == bps.TEXT_MAX
    assert text.check("  the repo  ") == "the repo"
    for bad in ("a\nb", "x" * (bps.TEXT_MAX + 1), ""):
        with pytest.raises(bps.BlueprintError):
            text.check(bad)
    assert bps.Slot("o", "text", "O", optional=True).check("") == ""


# -- filling ------------------------------------------------------------------------------------


def test_fill_renders_the_fixed_recurrence() -> None:
    bp = by_key("morning-brief")
    spec = bps.fill(bp, {"time": "7:30", "days": "mon,wed", "topics": "the build"})
    assert spec["schedule"] == "30 7 * * 1,3"
    assert validate_cron(spec["schedule"]) == spec["schedule"]
    assert "Cover: the build." in spec["prompt"] and "{" not in spec["prompt"]
    assert spec["name"] == "Morning briefing" and spec["deliver"] == "origin"
    assert bps.fill(bp)["schedule"] == "0 8 * * 1-5"
    assert bps.fill(bp, {"days": "weekends"})["schedule"] == "0 8 * * 0,6"
    assert bps.fill(bp, {"days": "daily"})["schedule"] == "0 8 * * *"


def test_fill_refuses_out_of_schema_unknown_and_missing() -> None:
    with pytest.raises(bps.BlueprintError, match="out of range"):
        bps.fill(by_key("morning-brief"), {"days": "8"})
    with pytest.raises(bps.BlueprintError, match="no slot color"):
        bps.fill(by_key("morning-brief"), {"color": "red"})
    check_in = by_key("check-in")
    assert not check_in.fills_from_defaults()
    with pytest.raises(bps.BlueprintError, match="needs a value"):
        bps.fill(check_in)
    spec = bps.fill(check_in, {"target": "the status page", "interval": "90"})
    assert spec["schedule"] == "every 90m" and "Check the status page." in spec["prompt"]
    with pytest.raises(bps.BlueprintError):
        bps.fill(check_in, {"target": "x", "interval": "5"})  # this slot's minimum is 15


def test_every_catalogue_entry_is_valid() -> None:
    keys = [bp.key for bp in bps.CATALOGUE]
    assert len(keys) == len(set(keys))
    for bp in bps.CATALOGUE:
        assert bp.source == "catalogue" and bp.slots
        if bp.fills_from_defaults():
            bps.fill(bp)


def test_bad_templates_refused() -> None:
    def make(schedule: str, *slots: dict[str, Any]) -> bps.Blueprint:
        raw = {"key": "k", "title": "T", "prompt": "p", "schedule": schedule, "slots": list(slots)}
        return bps.blueprint_from_mapping(raw)

    time_slot = {"name": "time", "kind": "time", "default": "09:00"}
    assert make("{minute} {hour} * * *", time_slot).schedule == "{minute} {hour} * * *"
    with pytest.raises(bps.BlueprintError, match="not one of"):
        make("{minute} {hour} {day} * *", time_slot)
    with pytest.raises(bps.BlueprintError, match="no weekdays slot"):
        make("{minute} {hour} * * {dow}", time_slot)
    with pytest.raises(bps.BlueprintError, match="kind"):
        make("0 9 * * *", {"name": "x", "kind": "colour"})
    with pytest.raises(bps.BlueprintError, match="no options"):
        make("0 9 * * *", {"name": "x", "kind": "choice"})
    with pytest.raises(bps.BlueprintError):
        make("0 9 * * *", {"name": "d", "kind": "weekdays", "default": "8"})
    with pytest.raises(bps.BlueprintError, match="must recur"):
        bps.fill(make("in 2h"))


# -- skill blueprints ---------------------------------------------------------------------------


def test_skill_blueprints_load_and_bad_ones_are_skipped(
    scope: Scope, caplog: pytest.LogCaptureFixture
) -> None:
    skill(scope, "releases", ROLLUP)
    skill(scope, "single", 'blueprint: {key: ping, title: Ping, schedule: "every {interval}m", '
          'prompt: "ping", slots: [{name: every, kind: interval, default: 30}]}\n')  # fmt: skip
    hidden = scope.home / "skills" / ".hub" / "quarantine" / "x"
    hidden.mkdir(parents=True)
    (hidden / "SKILL.md").write_text("---\nname: x\nblueprint: {key: q, title: Q}\n---\n")
    found = {bp.key: bp for bp in bps.skill_blueprints(scope)}
    assert set(found) == {"releases:rollup", "single:ping"}
    assert "blueprint skipped" in caplog.text and "broken" in caplog.text
    rollup = found["releases:rollup"]
    assert rollup.skill == "releases" and rollup.source == "skill"
    assert not rollup.fills_from_defaults()
    assert bps.fill(rollup, {"repo": "core"})["schedule"] == "15 9 * * 1-5"
    assert found["single:ping"].fills_from_defaults()
    keys = [bp.key for bp in bps.list_blueprints(scope)]
    assert keys[: len(bps.CATALOGUE)] == [bp.key for bp in bps.CATALOGUE]
    assert keys[len(bps.CATALOGUE) :] == ["releases:rollup", "single:ping"]


def test_match_rules(scope: Scope) -> None:
    skill(scope, "releases", ROLLUP)
    all_bps = bps.list_blueprints(scope)
    assert bps.match_blueprint(all_bps, "morning-brief")[0] is by_key("morning-brief")
    assert bps.match_blueprint(all_bps, "rollup")[0].key == "releases:rollup"  # type: ignore[union-attr]
    assert bps.match_blueprint(all_bps, "Weekly")[0] is by_key("weekly-review")
    found, several = bps.match_blueprint(all_bps, "w")
    assert found is None and len(several) > 1
    assert bps.match_blueprint(all_bps, "merged")[0].key == "releases:rollup"  # type: ignore[union-attr]
    assert bps.match_blueprint(all_bps, "zzz") == (None, [])


# -- renderings ---------------------------------------------------------------------------------


def test_form_cron_line_and_seed() -> None:
    bp = by_key("weekly-review")
    shown = bps.form(bp)
    assert shown["key"] == "weekly-review" and shown["source"] == "catalogue"
    fields = {f["name"]: f for f in shown["fields"]}
    assert fields["style"]["options"] == ["brief", "full"]
    assert fields["time"]["default"] == "16:30" and fields["days"]["kind"] == "weekdays"
    json.dumps(shown)
    line = bps.cron_line(bp, {"style": "full"})
    words = shlex.split(line)
    assert words[:2] == ["/cron", "add"]
    assert "30 16 * * 5" in words and words[-1].endswith("Style: full.")
    text = bps.seed(bp)
    assert "weekly-review" in text and "cronjob_manage" in text
    assert "Level of detail (style, one of brief, full), default brief" in text
    assert bp.schedule in text and "wait for my yes" in text


# -- /blueprint and the CLI ---------------------------------------------------------------------


def test_handle_blueprint_flow(scope: Scope, backend: RecordingHost) -> None:
    get = lambda: backend  # noqa: E731
    listing = bps.handle_blueprint(scope, "", backend=get)
    assert "morning-brief" in listing.text and listing.seed is None
    asked = bps.handle_blueprint(scope, "morning", backend=get)
    assert asked.seed is not None and "Morning briefing" in asked.seed
    assert backend.jobs == {}
    refused = bps.handle_blueprint(scope, "morning-brief days=8", backend=get)
    assert "Can't set up" in refused.text and backend.jobs == {}
    made = bps.handle_blueprint(
        scope, "morning-brief time=06:45 'topics=the deploy queue'", backend=get, surface="gateway"
    )
    assert made.job_id is not None and "Ask me any time" in made.text
    assert len(backend.jobs) == 1
    job = backend.jobs[made.job_id]
    assert job.schedule_display == "45 6 * * 1-5" and "the deploy queue" in (job.prompt or "")
    several = bps.handle_blueprint(scope, "w x=1", backend=get)
    assert "matches several" in several.text


def test_slash_blueprint_submits_the_seed(scope: Scope, backend: RecordingHost) -> None:
    ctx = make_ctx(scope)
    result = blueprint_cmd(ctx, "weekly")
    assert result.submit is not None and "weekly-review" in result.submit
    assert backend.jobs == {}
    result = blueprint_cmd(ctx, "weekly days=mon")
    assert result.submit is None and len(backend.jobs) == 1


def run_cli(scope: Scope, *argv: str) -> Any:
    state = CliState(scope=scope, stdin_isatty=lambda: False, stdout_isatty=lambda: False)
    result = CliRunner().invoke(blueprint_group, list(argv), obj=state)
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result


def test_cli_verbs(scope: Scope, backend: RecordingHost) -> None:
    listed = run_cli(scope, "list")
    assert listed.exit_code == 0 and "check-in: Regular check-in" in listed.stdout
    shown = run_cli(scope, "show", "weekly-review")
    assert shown.exit_code == 0 and "/cron add" in shown.stdout
    as_json = run_cli(scope, "show", "weekly-review", "--json")
    assert json.loads(as_json.stdout)["key"] == "weekly-review"
    assert run_cli(scope, "show", "nothing-like-it").exit_code == 1
    dry = run_cli(scope, "use", "morning-brief", "time=07:00", "--dry-run")
    assert dry.exit_code == 0 and json.loads(dry.stdout)["schedule"] == "0 7 * * 1-5"
    assert backend.jobs == {}
    bad = run_cli(scope, "use", "morning-brief", "days=8")
    assert bad.exit_code == 1 and "out of range" in bad.stderr and backend.jobs == {}
    made = run_cli(scope, "use", "check-in", "target=the site")
    assert made.exit_code == 0 and "created job" in made.stdout
    assert [j.schedule_display for j in backend.jobs.values()] == ["every 1h"]


def test_close_spellings_and_did_you_mean(scope: Scope, backend: RecordingHost) -> None:
    all_bps = bps.list_blueprints(scope)
    assert bps.match_blueprint(all_bps, "mornig-brief")[0] is by_key("morning-brief")
    missing = bps.handle_blueprint(scope, "ev-wrqqqqq", backend=lambda: backend)
    assert "Did you mean evening-wrap" in missing.text
    assert "Did you mean" not in bps.handle_blueprint(scope, "zzz", backend=lambda: backend).text
