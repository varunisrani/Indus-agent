"""P17-26: the dual-host matrix — surface suites run against the in-process and the attached host.

Every test here takes ``session_host`` (``conftest_hosts.py``) and runs once per host kind: the
agent in the surface's own process tree (``--local`` CLI, the TUI's ``tui-bridge --stdio`` child)
and the agent in a real gateway daemon subprocess (the CLI attached by default, the TUI protocol
over the daemon's WebSocket). Each check goes through ``HostRig.same(case, facet, got, want)``:
the expected value is what a person sees today in process; a different value on either host fails
unless ``tests/fixtures/service/dual_host_allowlist.yaml`` lists it with a D-id, and a listed
difference that is gone fails as stale.

Suites mirrored here (their cases, written once for both hosts):

- **P06 CLI chat** (``tests/integration/cli/test_oneshot_exit_codes.py``): ``-z`` and finite
  ``chat -q`` stdout/stderr contracts, exit codes, the usage file, Ctrl-C, approvals under ``-z``
  and ``-q``, resume chains, ``-c`` misses;
- **P12 bridge** (``tests/integration/tui_bridge/test_bridge_child.py``): the Demo 2 turn over the
  TUI protocol (stream, two tools, an approval answered "once", the final message), a client lost
  mid-turn and a new one resuming the session, the settings the TUI reads at start, and the turn's
  event shape (start, stream, end, final message, usage, title);
- **P15 Demo 3** (``tests/e2e/gateway/test_demo3.py``): run under ``messaging_host``, where
  ``inprocess`` runs chat turns directly on the daemon's service (P15) and ``attached`` admits them
  into the shared session host every attached surface uses (P17-22).

``test_a_seeded_difference_fails_the_matrix`` drops the attached host's usage event and checks the
matrix catches it.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.fakes.daemon import pause, until
from tests.fakes.turn import check_alternation
from tests.integration.service.conftest_hosts import (
    ALLOWLIST,
    HOSTS,
    HostKind,
    HostRig,
    MatrixDifference,
    Peer,
    StdioPeer,
    WsPeer,
    host_rig,
    load_allowlist,
    session_host,
    settle,
    steps_script,
)

__all__ = ["session_host"]

pytestmark = [
    pytest.mark.skipif(os.name != "posix", reason="POSIX sockets and signals"),
    pytest.mark.slow,
]

TIMEOUT = 90.0


def model_bodies(rig: HostRig) -> list[dict[str, Any]]:
    """The request bodies the scripted model answered (title and model-list calls excluded)."""
    return [r.body for r in rig.model.model.requests if r.turn is not None and r.method == "POST"]


def tool_call(name: str, command: str, call_id: str) -> dict[str, Any]:
    return {"tool_call": {"name": name, "arguments": {"command": command}, "id": call_id}}


# == P06: the classic CLI's finite runs ===========================================================


def test_z_prints_only_the_reply(session_host: HostRig) -> None:
    rig, case = session_host, "cli.z.completed"
    rig.use(steps_script([{"text": "hello"}]))
    out = rig.indus("-z", "say hello")
    rig.same(case, "exit", out.returncode, 0)
    rig.same(case, "stdout", out.stdout, "hello\n")
    rig.same(case, "stderr", out.stderr, "")


def test_z_empty_answers_are_a_failure(session_host: HostRig) -> None:
    rig, case = session_host, "cli.z.empty"
    empty = [{"usage": {"prompt": 10, "completion": 0}}, {"finish": "stop"}]
    rig.use(steps_script(*[empty] * 8))
    out = rig.indus("-z", "say nothing")
    rig.same(case, "exit", out.returncode, 2)


def test_z_failure_exits_2_and_writes_the_usage_file(session_host: HostRig) -> None:
    rig, case = session_host, "cli.z.failed"
    rig.use(steps_script([{"fail": {"status": 400, "body": '{"error": "bad request"}'}}]))
    usage = rig.root / "usage" / "report.json"
    out = rig.indus("-z", "anything", "--usage-file", str(usage))
    rig.same(case, "exit", out.returncode, 2)
    rig.same(case, "stderr prefix", out.stderr.startswith("indus -z: "), True)
    report = json.loads(usage.read_text(encoding="utf-8")) if usage.exists() else {}
    rig.same(
        case,
        "usage file",
        {k: report.get(k) for k in ("failed", "completed", "cost_status", "exit_reason")},
        {
            "failed": True,
            "completed": False,
            "cost_status": "unknown",
            "exit_reason": "provider_failed",
        },
    )


def test_z_text_then_failure_is_partial(session_host: HostRig) -> None:
    rig, case = session_host, "cli.z.partial"
    cut = [{"text": ["half an ans"]}, {"mid_stream_error": {"error": {"message": "boom"}}}]
    rig.use(steps_script(*[cut] * 4))
    usage = rig.root / "u.json"
    out = rig.indus("-z", "go", "--usage-file", str(usage))
    rig.same(case, "exit", out.returncode, 2)
    report = json.loads(usage.read_text(encoding="utf-8")) if usage.exists() else {}
    rig.same(case, "usage completed", report.get("completed"), False)
    rig.same(case, "stdout", out.stdout, "")  # only a model reply ever goes to stdout
    rig.same(case, "stderr says why", bool(out.stderr.strip()), True)


def test_usage_errors_exit_2(session_host: HostRig) -> None:
    rig, case = session_host, "cli.usage_errors"
    rig.use(steps_script([{"text": "never"}]))
    out = rig.indus("-z", "x", "--provider", "openai")
    rig.same(case, "provider without model", out.returncode, 2)
    rig.same(case, "provider message", "--provider needs --model" in out.stderr, True)
    rig.same(case, "reasoning", rig.indus("-z", "x", "--reasoning", "loud").returncode, 2)
    rig.same(case, "bogus flag", rig.indus("--bogus-flag").returncode, 2)
    rig.same(case, "model turns", len(model_bodies(rig)), 0)


def test_ctrl_c_on_z_exits_130(session_host: HostRig) -> None:
    rig, case = session_host, "cli.z.interrupted"
    rig.use(steps_script([{"text": ["thinking"]}, {"stall": 120}]))
    proc = rig.spawn("-z", "think long")
    try:
        until(lambda: model_bodies(rig) or proc.poll() is not None, "the model asked", TIMEOUT)
        pause(1.0)  # the first chunk is on its way to the reply
        proc.send_signal(signal.SIGINT)
        out, _err = proc.communicate(timeout=TIMEOUT)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
    rig.same(case, "exit", proc.returncode, 130)
    rig.same(case, "stdout", out in ("", "thinking\n"), True)  # at most the sealed reply


def test_z_refuses_hardline_and_auto_approves_the_rest(session_host: HostRig) -> None:
    rig, case = session_host, "cli.z.approvals"
    work = rig.profile.work
    (work / "victim").mkdir()
    rig.use(
        steps_script(
            [tool_call("terminal", "touch HARD_RAN; grep 'unterminated", "call_hard")],
            [tool_call("terminal", "find victim -delete", "call_risky")],
            [{"text": "done"}],
        )
    )
    out = rig.indus("-z", "clean up")
    bodies = model_bodies(rig)
    hard = [
        m
        for body in bodies[1:2]
        for m in body["messages"]
        if m.get("role") == "tool" and m.get("tool_call_id") == "call_hard"
    ]
    rig.same(case, "hardline blocked", bool(hard) and "BLOCKED" in json.dumps(hard), True)
    rig.same(case, "hardline did not run", (work / "HARD_RAN").exists(), False)
    # -z approves the dangerous command for the run: it runs and the reply is printed.
    outcome = (out.returncode, out.stdout, (work / "victim").exists())
    rig.same(case, "outcome", outcome, (0, "done\n", False))


def test_finite_chat_query_denies_risky_commands(session_host: HostRig) -> None:
    rig, case = session_host, "cli.q.denies"
    work = rig.profile.work
    (work / "victim").mkdir()
    rig.use(
        steps_script([tool_call("terminal", "find victim -delete", "call_risky")], [{"text": "no"}])
    )
    out = rig.indus("chat", "-q", "clean up")
    # Nobody can answer in a finite run: the command is denied, the reply printed, then the
    # session line.
    outcome = (
        out.returncode,
        out.stdout,
        (work / "victim").exists(),
        out.stderr.startswith("session_id: "),
    )
    rig.same(case, "outcome", outcome, (0, "no\n", True, True))


def test_finite_chat_codes(session_host: HostRig) -> None:
    rig, case = session_host, "cli.q.codes"
    rig.use(steps_script([{"fail": {"status": 400, "body": "{}"}}]))
    rig.same(case, "failed", rig.indus("chat", "-Q", "-q", "x").returncode, 1)
    rig.use(steps_script([{"text": "hello"}]))
    out = rig.indus("chat", "--query-file", "-", stdin="from stdin")
    rig.same(case, "query file", (out.returncode, out.stdout), (0, "hello\n"))


def test_resume_chain_with_z(session_host: HostRig) -> None:
    rig, case = session_host, "cli.z.resume"
    rig.use(steps_script([{"text": "one"}]))
    rig.same(case, "first", rig.indus("-z", "first").returncode, 0)
    rig.use(steps_script([{"text": "again"}]))
    second = rig.indus("--resume", "latest", "-z", "second")
    rig.same(case, "second", (second.returncode, second.stdout), (0, "again\n"))
    sent = model_bodies(rig)[0]["messages"] if model_bodies(rig) else []
    users = [m["content"] for m in sent if m["role"] == "user"]
    rig.same(case, "history sent", users, ["first", "second"])


@pytest.mark.parametrize("flag", ["-c", "--continue"])
def test_continue_miss_exits_1(session_host: HostRig, flag: str) -> None:
    rig, case = session_host, "cli.continue_miss"
    rig.use(steps_script([{"text": "never"}]))
    out = rig.indus(flag, "nothing", "here", "-z", "x")
    rig.same(case, "exit", out.returncode, 1)
    rig.same(case, "message", "No session found matching 'nothing here'." in out.stderr, True)


# == P12: the TUI protocol =========================================================================


def streamed(peer: Peer, turn_id: str) -> str:
    return "".join(
        str(e.get("text") or "") for e in peer.events("message.delta") if e["turn_id"] == turn_id
    )


def started_after(peer: Peer, count: int) -> dict[str, Any]:
    """The ``count``-th ``turn.started`` (1-based)."""
    frame = peer.wait_for(lambda f: len(peer.events("turn.started")) >= count, "a turn start")
    del frame
    return peer.events("turn.started")[count - 1]


def stored_rows(rig: HostRig, session_id: str) -> list[Any]:
    from indus.core.persist import StateStore
    from indus.foundation.scope import make_scope
    from indus.state import registry

    with registry.open_unshared(make_scope(rig.profile.home)) as handle:
        return list(asyncio.run(StateStore(handle).load(session_id)))


def user_texts(rig: HostRig, session_id: str) -> list[str]:
    return [str(r.content) for r in stored_rows(rig, session_id) if r.role == "user"]


def stored_session(rig: HostRig, session_id: str) -> dict[str, Any]:
    import sqlite3

    uri = f"file:{rig.profile.home / 'state.db'}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else {}


def test_bridge_demo_turn(session_host: HostRig) -> None:
    """Stream, two tool calls, an approval answered "once", the final message (P12-33)."""
    rig, case = session_host, "bridge.demo_turn"
    rig.use("tui_demo2")
    peer = rig.peer()
    sid = peer.call("session.create", title="demo two")["session_id"]
    peer.call("prompt.submit", session_id=sid, text="make a counter script and run it")
    ask = peer.wait_for(
        lambda f: f.get("method") == "server_request" and f["params"]["kind"] == "approval",
        "the approval request",
    )["params"]
    rig.same(case, "approval command", ask["command"], "chmod +x count.sh && ./count.sh")
    rig.same(case, "not run before the answer", (rig.profile.work / "runs.log").exists(), False)
    rig.same(
        case,
        "answer",
        peer.answer(sid, ask, {"kind": "approval", "choice": "once"}),
        {"accepted": True},
    )
    done = peer.wait_for(lambda f: f.get("method") == "message.complete", "the final message")
    rig.same(
        case,
        "final message",
        (done["params"]["text"], done["params"]["reason"]),
        ("All done: count.sh printed **counted**.", "completed"),
    )
    turn = started_after(peer, 1)["turn_id"]
    rig.same(
        case,
        "stream",
        streamed(peer, turn).startswith("Writing count.sh for you.Running it now."),
        True,
    )
    tools = [(e["name"], e["ok"]) for e in peer.events("tool.completed")]
    rig.same(case, "tools", tools, [("terminal", True), ("terminal", True)])
    rig.same(
        case, "ran once", (rig.profile.work / "runs.log").read_text(encoding="utf-8"), "once\n"
    )
    rig.same(case, "model turns", rig.model.model_turns, 3)
    rows = stored_rows(rig, sid)
    check_alternation(rows)
    rig.same(case, "stored prompts", user_texts(rig, sid), ["make a counter script and run it"])


# -- the event shape of a turn ---------------------------------------------------------------------

TURN_EVENTS = (
    "turn.started",
    "message.delta",
    "reasoning.delta",
    "tool.started",
    "tool.completed",
    "server_request",
    "turn.done",
    "message.complete",
    "session.usage",
)
"""Events a turn's lines on screen are built from (presence, settle notices, transport aside)."""
SESSION_EVENTS = ("session.info", "session.title")
"""Events the session header is built from (model, folder, tools, title)."""


def shown(peer: Peer, kinds: tuple[str, ...]) -> list[str]:
    """The event methods of ``kinds`` in order, consecutive stream pieces counted once (the
    daemon joins pieces that arrive within 33 ms, D-16.367)."""
    out: list[str] = []
    for method in peer.methods():
        if method not in kinds:
            continue
        if method.endswith(".delta") and out and out[-1] == method:
            continue
        out.append(method)
    return out


TURN_SHAPE = [
    "turn.started",
    "message.delta",
    "turn.done",
    "message.complete",
    "session.usage",
    "turn.started",
    "tool.started",
    "server_request",
    "tool.completed",
    "message.delta",
    "turn.done",
    "message.complete",
    "session.usage",
]


def event_shape_case(rig: HostRig, *, drop: Callable[[dict[str, Any]], bool] | None = None) -> None:
    case = "bridge.event_shape"
    (rig.profile.work / "victim").mkdir(exist_ok=True)
    rig.use(
        steps_script(
            [{"text": ["Hello ", "there"]}, {"usage": {"prompt": 10, "completion": 3}}],
            [tool_call("terminal", "find victim -delete", "c1")],
            [{"text": "done"}],
        )
    )
    peer = rig.peer()
    if drop is not None:
        peer.dropped = drop
    sid = peer.call("session.create")["session_id"]
    peer.call("prompt.submit", session_id=sid, text="hi")
    peer.wait_for(lambda f: f.get("method") == "turn.done", "the first turn's end")
    settle(0.5)
    peer.call("prompt.submit", session_id=sid, text="clean")
    ask = peer.wait_for(lambda f: f.get("method") == "server_request", "the approval")["params"]
    peer.answer(sid, ask, {"kind": "approval", "choice": "once"})
    until(lambda: len(peer.events("turn.done")) >= 2, "the second turn's end")
    until(lambda: rig_title(rig, sid), "the stored title", 20)
    settle(1.5)  # the turn-end events and the title may still be on their way
    rig.same(case, "turn events", shown(peer, TURN_EVENTS), TURN_SHAPE)
    rig.same(case, "session events", shown(peer, SESSION_EVENTS), ["session.info", "session.title"])
    finals = [(e["text"], e["reason"]) for e in peer.events("message.complete")]
    rig.same(case, "finals", finals, [("Hello there", "completed"), ("done", "completed")])
    usage = peer.events("session.usage")
    rig.same(
        case,
        "usage",
        [
            {k: u["usage"].get(k) for k in ("api_calls", "input_tokens", "output_tokens")}
            for u in usage
        ],
        [
            {"api_calls": 1, "input_tokens": 10, "output_tokens": 3},
            {"api_calls": 3, "input_tokens": 10, "output_tokens": 3},
        ],
    )
    rig.same(case, "stored title", rig_title(rig, sid), "Chat: hi")
    rig.same(case, "stored source", stored_session(rig, sid).get("source"), "tui")


def rig_title(rig: HostRig, session_id: str) -> str | None:
    found = stored_session(rig, session_id).get("title")
    return str(found) if found else None


def test_bridge_event_shape(session_host: HostRig) -> None:
    event_shape_case(session_host)


def test_a_seeded_difference_fails_the_matrix(tmp_path: Path) -> None:
    """The attached host with its usage event dropped no longer matches: the matrix fails."""
    dropped = MatrixDifference
    with (
        host_rig("attached", tmp_path / "seeded") as rig,
        pytest.raises(dropped, match=r"bridge\.event_shape/turn events differs on the attached"),
    ):
        event_shape_case(rig, drop=lambda frame: frame.get("method") == "session.usage")


# -- a client lost mid-turn ---------------------------------------------------------------------


def test_bridge_client_lost_mid_turn_then_resumed(session_host: HostRig) -> None:
    """The TUI's connection dies mid-turn; a new one resumes the session; nothing is sent again."""
    rig, case = session_host, "bridge.lost_mid_turn"
    rig.use(
        steps_script(
            [{"text": ["Long ", "partial"]}, {"stall": 4}, {"text": " end"}],
            [{"text": "After the restart."}],
        )
    )
    first = rig.peer()
    sid = first.call("session.create")["session_id"]
    first.call("prompt.submit", session_id=sid, text="start a long one")
    first.wait_for(
        lambda f: f.get("method") == "message.delta" and "partial" in str(f["params"].get("text")),
        "the partial reply",
    )
    if isinstance(first, StdioPeer):
        first.proc.send_signal(signal.SIGKILL)
        first.proc.wait(10)
    elif isinstance(first, WsPeer):
        first.drop()
    second = rig.peer()
    resumed = second.call("session.resume", session_id=sid)
    rig.same(case, "same session", resumed["session_id"], sid)
    pause(6.0)  # longer than the stall: a turn still running would finish now
    rig.same(case, "model turns after the loss", rig.model.model_turns, 1)  # never resent
    finished = [str(r.content) for r in stored_rows(rig, sid) if r.role == "assistant"]
    rig.same(case, "lost turn finished", finished, [])
    rig.same(case, "stored prompts", user_texts(rig, sid), ["start a long one"])
    seen = {e["turn_id"] for e in second.events("message.complete")}
    second.call("prompt.submit", session_id=sid, text="and now?")
    done = second.wait_for(
        lambda f: f.get("method") == "message.complete" and f["params"]["turn_id"] not in seen,
        "the reply",
    )
    rig.same(case, "reply", done["params"]["text"], "After the restart.")
    rig.same(case, "model turns", rig.model.model_turns, 2)
    rig.same(case, "prompts", user_texts(rig, sid), ["start a long one", "and now?"])


# -- steering a running reply ---------------------------------------------------------------------


def test_bridge_steer_reaches_the_running_turn(session_host: HostRig) -> None:
    """``session.steer`` naming the running turn's generation (as the TUI sends it) is taken and
    reaches the model with the turn's next request."""
    rig, case = session_host, "bridge.steer"
    rig.use(
        steps_script(
            [{"text": ["Working"]}, {"stall": 3}, tool_call("terminal", "echo ok", "c1")],
            [{"text": "Steered done."}],
        )
    )
    peer = rig.peer()
    sid = peer.call("session.create")["session_id"]
    peer.call("prompt.submit", session_id=sid, text="start")
    started = started_after(peer, 1)
    peer.wait_for(lambda f: f.get("method") == "message.delta", "the reply streaming")
    steered = peer.call(
        "session.steer", session_id=sid, text="use python", generation=started["generation"]
    )
    rig.same(case, "queued", steered.get("queued"), True)
    done = peer.wait_for(lambda f: f.get("method") == "message.complete", "the reply's end")
    rig.same(case, "reply", done["params"]["text"], "Steered done.")
    later = json.dumps(model_bodies(rig)[1:])
    rig.same(case, "steer reached the model", "use python" in later, True)


# -- what the TUI reads at start -------------------------------------------------------------------


def outcome_of(peer: Peer, method: str, **params: Any) -> Any:
    answer = peer.answer_of(peer.send(method, **params), method)
    if "error" in answer:
        return {"error": answer["error"]["code"]}
    return answer["result"]


def test_bridge_settings_the_tui_reads_at_start(session_host: HostRig) -> None:
    rig, case = session_host, "bridge.settings"
    rig.use("tui_demo2")
    peer = rig.peer()
    options = outcome_of(peer, "model.options")
    providers = options.get("providers", []) if isinstance(options, dict) else []
    custom = next((p for p in providers if p.get("slug") == "custom"), {})
    before = outcome_of(peer, "config.get", key="full")
    outcome_of(peer, "config.set", key="busy", value="steer")
    after = outcome_of(peer, "config.get", key="full")
    seen = {
        "custom provider usable": (custom.get("authenticated"), custom.get("current")),
        "busy before": before.get("value", {}).get("busy", "missing"),
        "busy after": after.get("value", {}).get("busy", "missing"),
    }
    want = {"custom provider usable": (True, True), "busy before": None, "busy after": "steer"}
    rig.same(case, "settings", seen, want)
    rig.same(case, "model turns", rig.model.model_turns, 0)


# == P15 Demo 3 under both hosts ===================================================================


@pytest.fixture(params=HOSTS)
def messaging_host(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[HostKind]:
    """``inprocess``: chat turns run on the daemon's service directly (P15). ``attached``: they are
    admitted into the shared session host attached surfaces use (P17-22)."""
    from tests.integration.gateway import three_platforms

    shared = request.param == "attached"
    plain = three_platforms.quick

    def quick(**changes: Any) -> Any:
        return plain(**{"shared_host": shared, **changes})

    monkeypatch.setattr(three_platforms, "quick", quick)
    assert three_platforms.quick().shared_host is shared  # what three_daemon builds with
    yield request.param


def test_demo3_one_agent_on_three_platforms(messaging_host: HostKind, tmp_path: Path) -> None:
    from tests.e2e.gateway import test_demo3 as demo3

    demo3.test_the_same_agent_answers_on_telegram_discord_and_slack(tmp_path)


def test_demo3_approval_by_a_telegram_button(messaging_host: HostKind, tmp_path: Path) -> None:
    from tests.e2e.gateway import test_demo3 as demo3

    demo3.test_a_dangerous_command_is_approved_with_a_telegram_button(tmp_path)


def test_demo3_cron_delivery_on_its_tick(
    messaging_host: HostKind, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.e2e.gateway import test_demo3 as demo3

    demo3.test_a_cron_job_every_30m_reaches_the_telegram_chat_on_its_tick(tmp_path, monkeypatch)


@pytest.mark.parametrize("name", ["telegram", "discord", "slack"])
def test_demo3_chunker_busy_guards_and_default_deny(
    messaging_host: HostKind, tmp_path: Path, name: str
) -> None:
    from tests.e2e.gateway import test_demo3 as demo3

    demo3.test_chunker_busy_guards_and_default_deny(tmp_path, name)


# == the allow-list itself =========================================================================


def test_the_allow_list_is_well_formed() -> None:
    entries = load_allowlist(ALLOWLIST)
    for entry in entries:
        assert entry.case.split(".")[0] in {"cli", "bridge", "demo3"}, entry
    assert len({(e.case, e.facet, e.host) for e in entries}) == len(entries)


def test_an_entry_without_a_d_id_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "allow.yaml"
    bad.write_text(
        "differences:\n  - {case: x, facet: y, host: attached, d_id: none, why: because}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not a D-id"):
        load_allowlist(bad)
    bad.write_text(
        "differences:\n  - {case: x, facet: y, host: attached, d_id: D-?, why: because}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not a D-id"):  # numbered before it lands
        load_allowlist(bad)
    bad.write_text(
        "differences:\n  - {case: x, facet: y, host: attached, d_id: D-16.1, why: ''}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="no reason"):
        load_allowlist(bad)
