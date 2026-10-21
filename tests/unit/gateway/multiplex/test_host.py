"""P24-01: the host lock, the rendezvous record, the second-run verdict and the serve verbs."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.profiles import tombstone
from indus.foundation.scope import Scope
from indus.gateway.control_socket import ControlServer, VerbRefused
from indus.gateway.multiplex import host as host_mod
from indus.gateway.multiplex.host import (
    HostLock,
    HostRecord,
    MultiplexHost,
    ServeRefused,
    clear_record,
    decide_second_run,
    ensure_state_dir,
    host_state_dir,
    mint_routed_ticket,
    publish_record,
    read_record,
    record_alive,
)
from indus.service import gateway_control as wire
from indus.service.ws_auth import WS_INTERACTIVE, TicketBook, TicketRefused, stable_profile_id
from indus.state.gateway_runtime import (
    PidRecord,
    ProcessIdentity,
    process_start_time,
    write_pid_record,
)
from tests.fakes.clock import FakeClock
from tests.unit.gateway.multiplex.world import launch_scope, make_profile

SUPERVISED = {"INDUS_GATEWAY_SUPERVISED": "1"}


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def root(isolated_home: Path) -> Path:
    return isolated_home


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state" / "indus"


@pytest.fixture
def launch(root: Path, fake_clock: FakeClock) -> Scope:
    return launch_scope(root, fake_clock)


def live_record(base: Path, *, served: tuple[str, ...] = ("default",), **kw: Any) -> HostRecord:
    fields: dict[str, Any] = {
        "pid": os.getpid(),
        "start_time": process_start_time(os.getpid()),
        "root": str(base),
        "home": str(base),
        "served_profiles": served,
    }
    fields.update(kw)
    return HostRecord(**fields)


class Ticker:
    """A monotonic clock that moves only when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps = 0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds


def decide(root: Path, state: Path, profile: str = "default", **kw: Any) -> host_mod.RunVerdict:
    ticker = kw.pop("ticker", Ticker())
    kw.setdefault("env", {})
    kw.setdefault("identify", lambda _home, _t: None)
    kw.setdefault("serve", lambda _home, _p, _t: wire.ControlReply(False, error="no_socket"))
    return decide_second_run(
        profile,
        root=root,
        record_dir=state,
        sleep=ticker.sleep,
        monotonic=ticker.monotonic,
        **kw,
    )


def answers(served: list[str], pid: int | None = None) -> Any:
    def identify(_home: Path, _timeout: float) -> Mapping[str, Any]:
        return {"pid": os.getpid() if pid is None else pid, "served_profiles": served}

    return identify


# -- state dir, lock and record --------------------------------------------------------------------


def test_the_state_dir_follows_xdg_state_home_else_home(tmp_path: Path) -> None:
    xdg = tmp_path / "xdg"
    assert host_state_dir({"XDG_STATE_HOME": str(xdg)}) == xdg / "indus"
    assert host_state_dir({"XDG_STATE_HOME": "relative", "HOME": str(tmp_path)}) == (
        tmp_path / ".local" / "state" / "indus"
    )


def test_the_record_dir_is_owner_only(state: Path) -> None:
    state.mkdir(parents=True, mode=0o755)
    os.chmod(state, 0o755)
    ensure_state_dir(state)
    assert stat.S_IMODE(state.stat().st_mode) == 0o700


def test_the_record_round_trips_owner_only(root: Path, state: Path) -> None:
    record = live_record(root, served=("default", "b"))
    publish_record(state, record)
    path = state / host_mod.RECORD_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert read_record(state) == record
    assert json.loads(path.read_text())["served_profiles"] == ["default", "b"]


@pytest.mark.parametrize(
    "text",
    ["not json", "[]", '{"pid": 0, "protocol": 1, "root": "/r", "home": "/r"}', '{"pid": 5}'],
)
def test_a_malformed_record_reads_as_none(state: Path, text: str) -> None:
    ensure_state_dir(state)
    (state / host_mod.RECORD_FILE).write_text(text)
    assert read_record(state) is None


def test_a_record_owned_by_another_user_is_ignored(
    root: Path, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_record(state, live_record(root))
    real = os.getuid()
    monkeypatch.setattr(host_mod.os, "getuid", lambda: real + 1)
    assert read_record(state) is None


def test_clear_leaves_another_owners_record(root: Path, state: Path) -> None:
    publish_record(state, live_record(root, pid=os.getpid() + 100_000))
    assert clear_record(state) is False
    assert read_record(state) is not None
    publish_record(state, live_record(root))
    assert clear_record(state) is True
    assert read_record(state) is None


def test_the_lock_is_exclusive_and_observable(state: Path) -> None:
    first, second = HostLock(state), HostLock(state)
    assert first.observed_free() is True  # no lock file yet
    assert first.try_acquire()
    assert not second.try_acquire()
    assert second.observed_free() is False
    first.release()
    assert second.observed_free() is True
    assert second.try_acquire()
    second.release()


def test_a_record_with_a_recycled_pid_is_stale(root: Path, state: Path) -> None:
    holder = HostLock(state)
    assert holder.try_acquire()
    try:
        recycled = live_record(root, start_time=(process_start_time(os.getpid()) or 0) + 999)
        publish_record(state, recycled)
        assert record_alive(recycled, HostLock(state)) is False
        assert decide(root, state, identify=answers(["default"])).outcome == "start"
    finally:
        holder.release()


def test_a_free_host_lock_makes_a_live_looking_record_stale(root: Path, state: Path) -> None:
    publish_record(state, live_record(root))
    verdict = decide(root, state, identify=answers(["default"]))
    assert verdict.outcome == "start"


def test_no_record_and_a_free_lock_starts(root: Path, state: Path) -> None:
    assert decide(root, state).outcome == "start"


# -- the second run --------------------------------------------------------------------------------


@pytest.fixture
def owner(root: Path, state: Path) -> Any:
    lock = HostLock(state)
    assert lock.try_acquire()
    publish_record(state, live_record(root, served=("default", "b")))
    yield lock
    lock.release()


def test_a_served_profile_attaches_with_0_at_a_shell_and_75_supervised(
    root: Path, state: Path, owner: HostLock
) -> None:
    shell = decide(root, state, "b", identify=answers(["default", "b"]))
    assert (shell.outcome, shell.exit_code, shell.owner_pid) == ("attach", 0, os.getpid())
    service = decide(root, state, "b", identify=answers(["default", "b"]), env=SUPERVISED)
    assert (service.outcome, service.exit_code) == ("attach", 75)


def test_an_owner_that_never_answers_is_a_transient_refusal(
    root: Path, state: Path, owner: HostLock
) -> None:
    ticker = Ticker()
    calls: list[Path] = []

    def silent(home: Path, _timeout: float) -> None:
        calls.append(home)

    verdict = decide(root, state, identify=silent, ticker=ticker)
    assert (verdict.outcome, verdict.exit_code, verdict.transient) == ("refuse", 75, True)
    assert len(calls) > 1 and ticker.now >= host_mod.ATTACH_WAIT_S
    assert calls[0] == root  # the owner's launch home names its control socket


def test_an_owner_that_answers_late_in_the_wait_attaches(
    root: Path, state: Path, owner: HostLock
) -> None:
    ticker = Ticker()

    def late(_home: Path, _timeout: float) -> Mapping[str, Any] | None:
        return {"pid": os.getpid(), "served_profiles": ["default"]} if ticker.now >= 1 else None

    assert decide(root, state, identify=late, ticker=ticker).outcome == "attach"


def test_an_identify_answer_naming_another_home_is_not_the_owner(
    root: Path, state: Path, owner: HostLock, tmp_path: Path
) -> None:
    def elsewhere(_home: Path, _timeout: float) -> Mapping[str, Any]:
        return {"pid": os.getpid(), "home": str(tmp_path / "x"), "served_profiles": ["default"]}

    assert decide(root, state, identify=elsewhere).exit_code == 75

    def same(_home: Path, _timeout: float) -> Mapping[str, Any]:
        return {"pid": os.getpid(), "home": str(root), "served_profiles": ["default"]}

    assert decide(root, state, identify=same).outcome == "attach"


def test_an_identify_answer_from_another_pid_is_not_the_owner(
    root: Path, state: Path, owner: HostLock
) -> None:
    verdict = decide(root, state, identify=answers(["default"], pid=os.getpid() + 1))
    assert (verdict.outcome, verdict.exit_code) == ("refuse", 75)


def test_an_unserved_profile_is_served_then_attached(
    root: Path, state: Path, owner: HostLock
) -> None:
    asked: list[tuple[str, float]] = []

    def serve(_home: Path, profile: str, timeout: float) -> wire.ControlReply:
        asked.append((profile, timeout))
        return wire.ControlReply(True, {"served": profile, "served_profiles": ["default", "c"]})

    verdict = decide(root, state, "c", identify=answers(["default", "b"]), serve=serve)
    assert verdict.outcome == "attach"
    assert asked == [("c", host_mod.SERVE_TIMEOUT_S)]


def test_an_owner_still_serving_is_a_transient_refusal(
    root: Path, state: Path, owner: HostLock
) -> None:
    def pending(_home: Path, _p: str, _t: float) -> wire.ControlReply:
        return wire.ControlReply(True, {"pending": True, "served_profiles": ["default"]})

    def timed_out(_home: Path, _p: str, _t: float) -> wire.ControlReply:
        return wire.ControlReply(False, error="timeout")

    for serve in (pending, timed_out):
        verdict = decide(root, state, "c", identify=answers(["default"]), serve=serve)
        assert (verdict.outcome, verdict.exit_code, verdict.transient) == ("refuse", 75, True)


def test_an_owner_that_will_not_serve_refuses_for_good(
    root: Path, state: Path, owner: HostLock
) -> None:
    def refuse(_home: Path, _p: str, _t: float) -> wire.ControlReply:
        return wire.ControlReply(False, error="unknown_profile", detail="no live profile 'zz'")

    shell = decide(root, state, "zz", identify=answers(["default"]), serve=refuse)
    assert (shell.outcome, shell.exit_code, shell.transient) == ("refuse", 1, False)
    assert "no live profile" in shell.message
    service = decide(root, state, "zz", identify=answers(["default"]), serve=refuse, env=SUPERVISED)
    assert service.exit_code == 78


def test_a_held_lock_without_a_record_is_a_transient_refusal(root: Path, state: Path) -> None:
    lock = HostLock(state)
    assert lock.try_acquire()
    try:
        verdict = decide(root, state)
        assert (verdict.outcome, verdict.exit_code) == ("refuse", 75)
    finally:
        lock.release()


def test_an_owner_of_another_root_refuses(root: Path, state: Path, tmp_path: Path) -> None:
    lock = HostLock(state)
    assert lock.try_acquire()
    try:
        other = tmp_path / "other-root"
        publish_record(state, live_record(root, root=str(other), home=str(other)))
        verdict = decide(root, state, identify=answers(["default"]))
        assert (verdict.outcome, verdict.exit_code) == ("refuse", 1)
    finally:
        lock.release()


# -- serving ---------------------------------------------------------------------------------------


class Hooks:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.gate: asyncio.Event | None = None
        self.fail: set[str] = set()

    async def serve(self, name: str, home: Path) -> None:
        self.calls.append(("serve", name))
        if self.gate is not None:
            await self.gate.wait()
        if name in self.fail:
            raise RuntimeError("adapter start failed")

    async def unserve(self, name: str, _home: Path) -> None:
        self.calls.append(("unserve", name))


def make_host(launch: Scope, state: Path, hooks: Hooks, **kw: Any) -> MultiplexHost:
    return MultiplexHost(
        launch, record_dir=state, on_serve=hooks.serve, on_unserve=hooks.unserve, **kw
    )


def test_claim_publishes_and_close_clears(launch: Scope, state: Path) -> None:
    host = make_host(launch, state, Hooks())
    assert host.claim()
    record = read_record(state)
    assert record is not None and record.served_profiles == ("default",)
    assert record.root == str(launch.root) and record.pid == os.getpid()
    assert not make_host(launch, state, Hooks()).claim()  # the lock is held
    host.close()
    assert read_record(state) is None and HostLock(state).observed_free() is True


def test_serve_unserve_and_rescan(launch: Scope, state: Path, root: Path) -> None:
    make_profile(root, "b")
    make_profile(root, "c")
    hooks = Hooks()
    host = make_host(launch, state, hooks)
    assert host.claim()

    async def main() -> None:
        served = await host.serve("B")
        assert served == {"served": "b", "served_profiles": ["default", "b"]}
        assert (await host.serve("b"))["already"] is True
        with pytest.raises(ServeRefused) as unknown:
            await host.serve("nobody")
        assert unknown.value.code == "unknown_profile"
        with pytest.raises(ServeRefused) as bad:
            await host.serve("../x")
        assert bad.value.code == "invalid_profile"
        record = read_record(state)
        assert record is not None and record.served_profiles == ("default", "b")
        rescanned = await host.rescan()
        assert rescanned["added"] == ["c"] and rescanned["removed"] == []
        tombstone(root, "b")
        rescanned = await host.rescan()
        assert rescanned["removed"] == ["b"]
        assert host.served_profiles() == ["default", "c"]
        assert (await host.unserve("c"))["served_profiles"] == ["default"]
        with pytest.raises(ServeRefused) as missing:
            await host.unserve("c")
        assert missing.value.code == "not_served"

    run(main())
    assert hooks.calls == [("serve", "b"), ("serve", "c"), ("unserve", "b"), ("unserve", "c")]
    host.close()


def test_the_launch_profile_cannot_be_unserved(launch: Scope, state: Path) -> None:
    host = make_host(launch, state, Hooks())

    async def main() -> None:
        with pytest.raises(ServeRefused) as refused:
            await host.unserve("default")
        assert refused.value.code == "launch_profile"
        assert (await host.rescan())["removed"] == []

    run(main())
    assert host.served_profiles() == ["default"]


def test_a_profile_whose_adapters_fail_is_not_served(
    launch: Scope, state: Path, root: Path
) -> None:
    make_profile(root, "b")
    hooks = Hooks()
    hooks.fail.add("b")
    host = make_host(launch, state, hooks)

    async def main() -> None:
        with pytest.raises(ServeRefused) as failed:
            await host.serve("b")
        assert failed.value.code == "serve_failed"
        assert (await host.rescan())["failed"] == ["b"]

    run(main())
    assert host.served_profiles() == ["default"]


def test_a_profile_running_its_own_gateway_is_not_taken_on(
    launch: Scope, state: Path, root: Path
) -> None:
    home_b = make_profile(root, "b")
    parent = os.getppid()  # a live process that is not us: "b's own gateway"
    write_pid_record(home_b, PidRecord(ProcessIdentity(parent, process_start_time(parent))))
    hooks = Hooks()
    host = make_host(launch, state, hooks)

    async def main() -> None:
        with pytest.raises(ServeRefused) as refused:
            await host.serve("b")
        assert refused.value.code == "own_gateway"
        assert (await host.rescan())["failed"] == ["b"]

    run(main())
    assert hooks.calls == [] and host.served_profiles() == ["default"]


def test_rescan_answers_pending_past_five_seconds(
    launch: Scope, state: Path, root: Path, fake_clock: FakeClock
) -> None:
    make_profile(root, "b")
    hooks = Hooks()
    host = make_host(launch, state, hooks)
    handler = host.control_verbs()["rescan-profiles"][0]

    async def main() -> None:
        hooks.gate = asyncio.Event()
        answer = asyncio.ensure_future(handler({}))
        await fake_clock.run_until_idle()
        assert not answer.done()
        fake_clock.advance(host_mod.VERB_BUDGET_S - 0.5)
        await fake_clock.run_until_idle()
        assert not answer.done()
        fake_clock.advance(0.5)
        result = await answer
        assert result == {"pending": True, "served_profiles": ["default"]}
        hooks.gate.set()
        await host.aclose()
        assert host.served_profiles() == ["default", "b"]

    run(main())


def test_a_fast_verb_answers_its_result_and_refusals_carry_codes(
    launch: Scope, state: Path, root: Path
) -> None:
    make_profile(root, "b")
    host = make_host(launch, state, Hooks())
    verbs = host.control_verbs()
    assert set(verbs) == set(host_mod.PROFILE_VERBS)

    async def main() -> None:
        assert (await verbs["serve-profile"][0]({"name": "b"}))["served"] == "b"
        with pytest.raises(VerbRefused) as refused:
            await verbs["unserve-profile"][0]({"name": "default"})
        assert refused.value.code == "launch_profile"
        with pytest.raises(VerbRefused) as missing:
            await verbs["serve-profile"][0]({})
        assert missing.value.code == "bad_request"

    run(main())


def test_the_verbs_are_owner_only_on_the_control_socket(
    launch: Scope, state: Path, root: Path
) -> None:
    make_profile(root, "b")
    host = make_host(launch, state, Hooks())
    server = ControlServer(root, env={})
    for verb, (handler, seconds) in host.control_verbs().items():
        server.register(verb, handler, timeout=seconds)
    line = json.dumps(
        {"protocol": 1, "verb": "serve-profile", "id": 1, "params": {"name": "b"}}
    ).encode()

    async def main() -> None:
        stranger = json.loads(await server.handle_line(line))
        assert (stranger["ok"], stranger["error"]) == (False, "forbidden")
        mine = json.loads(await server.handle_line(line, peer_verified=True))
        assert mine["ok"] and mine["result"]["served_profiles"] == ["default", "b"]

    run(main())
    unregistered = ControlServer(root, env={})
    answer = run(unregistered.handle_line(line, peer_verified=True))
    assert json.loads(answer)["error"] == "unsupported"


def test_identify_fields_name_the_served_set(launch: Scope, state: Path) -> None:
    host = make_host(launch, state, Hooks())
    assert host.identify_fields() == {
        "multiplex": True,
        "root": str(launch.root),
        "served_profiles": ["default"],
    }


# -- tickets ---------------------------------------------------------------------------------------


def test_tickets_are_minted_for_served_profiles_only(
    launch: Scope, state: Path, root: Path
) -> None:
    home_b = make_profile(root, "b")
    host = make_host(launch, state, Hooks())
    book = TicketBook.for_home(root, served=host.served_ticket_ids)
    with pytest.raises(TicketRefused) as unserved:
        mint_routed_ticket(book, host, "b", WS_INTERACTIVE)
    assert unserved.value.reason == "wrong_profile"
    with pytest.raises(TicketRefused) as direct:
        book.mint(WS_INTERACTIVE, profile_id=stable_profile_id(home_b))
    assert direct.value.reason == "wrong_profile"

    run(host.serve("b"))
    minted = mint_routed_ticket(book, host, "b", WS_INTERACTIVE)
    assert minted.profile_id == stable_profile_id(home_b)
    own = book.mint(WS_INTERACTIVE)
    assert book.redeem(own.ticket, purpose=WS_INTERACTIVE).profile_id == stable_profile_id(root)

    run(host.unserve("b"))
    with pytest.raises(TicketRefused) as gone:
        book.redeem(minted.ticket, purpose=WS_INTERACTIVE)
    assert gone.value.reason == "wrong_profile"
    with pytest.raises(TicketRefused) as used:
        book.redeem(minted.ticket, purpose=WS_INTERACTIVE)
    assert used.value.reason == "used"  # the refused redemption spent it


def test_a_served_set_that_cannot_be_read_refuses_tickets(root: Path) -> None:
    def broken() -> frozenset[str]:
        raise RuntimeError("no set")

    book = TicketBook.for_home(root, served=broken)
    with pytest.raises(TicketRefused) as refused:
        book.mint(WS_INTERACTIVE)
    assert refused.value.reason == "wrong_profile"
