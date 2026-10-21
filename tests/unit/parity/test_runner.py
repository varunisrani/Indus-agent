"""P25-05: the parity runner (product side), its transcript driver and the Bot API stand-in."""

from __future__ import annotations

import json
import shutil
import threading
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from scripts.parity import runner, scenarios
from scripts.parity.scripted_model_server import SCRIPTS
from scripts.parity.telegram_fake import FakeBotApi
from scripts.parity.terminal_driver import Screen, TranscriptProfile, key_bytes, structure

pytestmark = pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded:DeprecationWarning"
)


# -- the table and --list ------------------------------------------------------------------------


def test_list_prints_every_flow(capsys: pytest.CaptureFixture[str]) -> None:
    assert runner.main(["--list"]) == 0
    out = capsys.readouterr().out
    for setup in runner.FLOWS:
        assert setup.flow in out
        assert f"{setup.flow}: {setup.note}" in out
    assert "model script" in out and "needs" in out


def test_every_flow_names_a_known_script_and_has_a_scenario() -> None:
    names = [f.flow for f in runner.FLOWS]
    assert len(names) == len(set(names)) == 12
    for setup in runner.FLOWS:
        assert setup.script in SCRIPTS
        assert runner.scenario_path(setup.flow).is_file()
        assert setup.driver in ("terminal", "stdio", "browser", "platform")


def test_unknown_flow_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert runner.main(["no-such-flow"]) == 2
    assert "unknown flow" in capsys.readouterr().err
    assert runner.main([]) == 2


# -- running flows -------------------------------------------------------------------------------


@pytest.mark.slow
def test_dry_run_completes_the_cli_flow_and_writes_a_capture(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    assert runner.main(["cli-chat-approve", "--dry-run", "--cache", str(cache)]) == 0
    capture = json.loads((cache / "indus" / "cli-chat-approve.capture.json").read_text())
    assert capture["format"] == runner.CAPTURE_FORMAT
    assert capture["status"] == "passed", capture["reason"]
    assert capture["side"] == "indus" and capture["model_requests"] >= 4
    statuses = {s["step_id"]: s["status"] for s in capture["steps"]}
    assert set(statuses.values()) == {"completed"}
    approval = next(s for s in capture["steps"] if s["step_id"] == "wait-approval-1")
    assert [c["kind"] for c in approval["observed"]["cards"]] == ["approval"]


@pytest.mark.slow
def test_a_missing_step_id_fails_that_flow_not_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario_dir = tmp_path / "scenarios"
    shutil.copytree(runner.SCENARIO_DIR, scenario_dir, ignore=shutil.ignore_patterns("ledger"))
    broken = scenario_dir / "cron-schedule-run.indus.json"
    data = json.loads(broken.read_text())
    del data["actions"][2]["step_id"]
    broken.write_text(json.dumps(data))
    code = runner.main(
        [
            "cron-schedule-run",
            "acp-session",
            "--dry-run",
            "--scenarios",
            str(scenario_dir),
            "--cache",
            str(tmp_path / "cache"),
        ]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "failed   cron-schedule-run" in out and "scenario invalid" in out
    assert "passed   acp-session" in out  # the next flow still ran
    assert (tmp_path / "cache" / "indus" / "acp-session.capture.json").is_file()


def test_a_failing_step_marks_the_rest_not_run(tmp_path: Path) -> None:
    scenario_dir = tmp_path / "scenarios"
    scenario_dir.mkdir()
    data = {
        "format": scenarios.TERMINAL_FORMAT,
        "driver": "terminal",
        "terminal": {"columns": 110, "rows": 40},
        "environment": {"locale": "en-US", "timezone": "UTC"},
        "actions": [
            {
                "step_id": "launch",
                "action": "launch",
                "argv": ["indus", "--version"],
                "timeout_ms": 5000,
            },
            {
                "step_id": "wait-nothing",
                "action": "wait_for",
                "locator": {"kind": "text", "pattern": "this never prints"},
                "timeout_ms": 3000,
            },
            {"step_id": "wait-exit", "action": "wait_exit", "timeout_ms": 5000},
        ],
    }
    (scenario_dir / "cron-schedule-run.indus.json").write_text(json.dumps(data))
    options = runner.RunOptions(cache=tmp_path / "cache", scenarios=scenario_dir, dry_run=True)
    result = runner.run_flow("cron-schedule-run", options)
    assert result.status == "failed" and result.reason.startswith("wait-nothing: timed out")
    assert [s["status"] for s in result.steps] == ["completed", "failed", "not_run"]
    assert "Indus Agent" in "\n".join(result.steps[1]["observed"]["screen"])


def test_browser_flows_prepare_reas_input_in_a_dry_run(tmp_path: Path) -> None:
    options = runner.RunOptions(
        cache=tmp_path / "cache", port=24123, browser="/opt/chromium/chrome", dry_run=True
    )
    result = runner.run_flow("dashboard-sessions", options)
    assert result.status in ("not_run", "blocked")
    prepared = json.loads(
        (tmp_path / "cache" / "indus" / "dashboard-sessions.rea-input.json").read_text()
    )
    assert prepared["start_url"]["url"] == "http://127.0.0.1:24123/"
    assert prepared["browser"] == {
        "mode": "launch",
        "executable_path": "/opt/chromium/chrome",
        "headless": True,
    }
    assert runner.rea_browser_problems(prepared) == []
    assert runner.CANONICAL_ORIGIN not in json.dumps(prepared)


def test_rewrite_keeps_a_connect_browser() -> None:
    data = runner.load_scenario("desktop-onboarding")
    prepared = runner.rewrite_origin(data, "http://127.0.0.1:1", "/bin/chrome")
    assert prepared["browser"]["mode"] == "connect"
    assert prepared["start_url"]["url"] == "http://127.0.0.1:1/"


def test_import_turns_a_rea_result_into_a_capture(tmp_path: Path) -> None:
    result = {
        "normalized_result": {
            "duration_ms": 50,
            "steps": [
                {"step_id": "scenario_start", "action": "goto_start", "status": "completed"},
                {
                    "step_id": "wait-shell",
                    "action": "wait_for",
                    "status": "completed",
                    "error": None,
                },
                {
                    "step_id": "open-sessions",
                    "action": "click",
                    "status": "failed",
                    "error": "boom",
                },
            ],
            "events": {"items": [{"kind": "page-error", "message": "x is undefined"}]},
        }
    }
    source = tmp_path / "rea.json"
    source.write_text(json.dumps(result))
    options = runner.RunOptions(cache=tmp_path / "cache")
    imported = runner.import_capture("dashboard-sessions", source, options)
    assert imported.status == "failed" and imported.reason == "boom"
    capture = json.loads(
        (tmp_path / "cache" / "indus" / "dashboard-sessions.capture.json").read_text()
    )
    assert [s["step_id"] for s in capture["steps"]] == ["wait-shell", "open-sessions"]
    assert capture["page_errors"] == ["x is undefined"]


def test_clean_environment_carries_nothing_from_the_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOME_PROVIDER_API_KEY", "not-for-children")
    setup = runner.FLOW_BY_NAME["cli-chat-approve"]

    class _Server:
        base_url = "http://127.0.0.1:9/v1"

    ctx = runner.FlowContext(setup, tmp_path, _Server(), runner.RunOptions())  # type: ignore[arg-type]
    env = ctx.env()
    assert "SOME_PROVIDER_API_KEY" not in env
    assert env["INDUS_HOME"] == str(tmp_path / "indus-home") and env["TZ"] == "UTC"
    assert env["INDUS_TERMINAL_ID"] == "parity-cli-chat-approve"
    assert ctx.expand(["${model_base_url}", "${work}"]) == [
        "http://127.0.0.1:9/v1",
        str(tmp_path / "project"),
    ]


# -- the screen model ----------------------------------------------------------------------------


def test_screen_follows_cursor_moves_erases_and_wraps() -> None:
    screen = Screen(rows=5, cols=10)
    screen.feed("hello\r\nworld")
    screen.feed("\x1b[1;3HXY")  # absolute move, overwrite
    assert screen.lines()[:2] == ["heXYo", "world"]
    screen.feed("\x1b[2;1H\x1b[K")  # erase the second line
    assert screen.lines()[1] == ""
    screen.feed("\x1b[3;1H0123456789AB")  # wraps after ten columns
    assert screen.lines()[2:4] == ["0123456789", "AB"]
    screen.feed("\x1b[2J")
    assert screen.lines() == [""] * 5


def test_screen_keeps_scrolled_rows_and_restores_after_the_alternate_screen() -> None:
    screen = Screen(rows=2, cols=20)
    screen.feed("one\r\ntwo\r\nthree")
    assert screen.lines() == ["two", "three"]
    assert screen.transcript() == ["one", "two", "three"]
    screen.feed("\x1b[?1049h\x1b[Hfull screen")
    assert screen.lines()[0] == "full screen"
    screen.feed("\x1b[?1049l")
    assert screen.lines() == ["two", "three"]


def test_split_escape_sequences_and_wide_characters() -> None:
    screen = Screen(rows=2, cols=10)
    screen.feed("a\x1b[")
    screen.feed("2Cb界c")
    assert screen.lines()[0] == "a  b界c"


def test_screen_answers_a_cursor_position_request() -> None:
    screen = Screen(rows=5, cols=10)
    screen.feed("\x1b[3;4H\x1b[6n")
    assert screen.replies == ["\x1b[3;4R"]


def test_structure_finds_regions_cards_and_prompts() -> None:
    profile = TranscriptProfile(cards={"approval": r"Approval needed"}, prompts=(r"^›",))
    rows = ["intro line", "", "⚠ Approval needed: risky", "  1  allow once", "", "›"]
    found = structure(rows, profile)
    assert [r["top"] for r in found["regions"]] == [0, 2, 5]
    assert found["cards"] == [{"kind": "approval", "row": 2, "title": "⚠ Approval needed: risky"}]
    assert found["prompts"] == [{"row": 5, "text": "›"}]


def test_key_names() -> None:
    assert key_bytes("Enter") == "\r" and key_bytes("Ctrl+C") == "\x03"
    assert key_bytes("Ctrl+O") == "\x0f" and key_bytes("4") == "4"
    with pytest.raises(ValueError, match="unknown key"):
        key_bytes("Hyper+Q")


# -- the Bot API stand-in ------------------------------------------------------------------------


def _call(fake: FakeBotApi, method: str, body: dict[str, Any]) -> Any:
    request = urllib.request.Request(
        f"{fake.base_url}/bot0/{method}",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as answer:  # noqa: S310 - loopback
        return json.loads(answer.read())


def test_bot_api_fake_round_trip() -> None:
    fake = FakeBotApi(user_id=200)
    fake.start()
    try:
        assert _call(fake, "getMe", {})["result"]["is_bot"] is True
        assert _call(fake, "getUpdates", {"offset": 0, "timeout": 0})["result"] == []
        fake.say("hello bot")
        updates = _call(fake, "getUpdates", {"offset": 0})["result"]
        assert updates[0]["message"]["text"] == "hello bot"
        assert updates[0]["message"]["chat"]["id"] == 200
        markup = {"inline_keyboard": [[{"text": "Run once", "callback_data": "a:1"}]]}
        sent = _call(fake, "sendMessage", {"chat_id": 200, "text": "ok?", "reply_markup": markup})
        assert sent["result"]["message_id"] >= 1
        assert fake.snapshot()[-1]["buttons"] == [{"text": "Run once", "data": "a:1"}]
        fake.press(fake.sent[-1], "a:1")
        newer = _call(fake, "getUpdates", {"offset": updates[0]["update_id"] + 1})["result"]
        assert newer[0]["callback_query"]["data"] == "a:1"
        assert _call(fake, "setMyCommands", {})["result"] is True
    finally:
        fake.stop()


def test_bot_api_long_poll_wakes_on_a_new_message() -> None:
    fake = FakeBotApi()
    fake.start()
    try:
        timer = threading.Timer(0.1, fake.say, args=("late",))
        timer.start()
        got = _call(fake, "getUpdates", {"offset": 0, "timeout": 30})["result"]
        timer.join()
        assert [u["message"]["text"] for u in got] == ["late"]
    finally:
        fake.stop()
