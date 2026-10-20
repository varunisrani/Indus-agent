"""The dual-host fixture (P17-26): every surface test runs against both session hosts.

``session_host`` is parametrised over the two places an agent can run for a surface:

- ``inprocess`` — the agent runs in the surface's own process tree, as with ``--local``: the
  classic CLI builds its own host (``indus --local …``) and the TUI's protocol is served by its own
  ``indus tui-bridge --stdio`` child;
- ``attached`` — the agent runs in the profile's gateway daemon (a real ``indus gateway run``
  subprocess with the loopback fake model): the classic CLI attaches by default (``indus …``) and
  the TUI's protocol is the daemon's loopback WebSocket, dialled with a single-use ticket.

Both use one profile layout (``tests.fakes.daemon.ProfileHome``) and one scripted fake model on a
loopback port, so a test is written once and says what a person sees; any behaviour difference
between the hosts fails it unless the difference is listed, with a D-id, in
``tests/fixtures/service/dual_host_allowlist.yaml`` (``HostRig.same``).

The JSON-RPC peers (``StdioPeer`` for the bridge child, ``WsPeer`` for the daemon) give the same
view of the wire: every frame the server sent, in order, plus ``call`` and ``wait_for``. A server
request reaches both as the ``server_request`` frame, whether the server sent it as a notification
(the bridge) or as a request with an ``srq-`` id (the daemon); ``answer`` uses each one's way.

Every process a rig starts is stopped at teardown (``ProfileHome.stop_all`` signals only processes
whose environment names the rig's own temp home).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import queue
import re
import subprocess
import threading
import time
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Literal

import pytest
from ruamel.yaml import YAML

from indus.protocol.server_requests import SERVER_REQUEST_METHOD
from indus.service.remote.ws_client import TransportClosed, WsClientTransport
from indus.service.remote.ws_client import connect as ws_connect
from tests.fakes.daemon import ROOT, ProfileHome, RunningDaemon, pause
from tests.fakes.model import Script, load_script, parse_script
from tests.fakes.model.http_serve import HttpFakeModel, serving

__all__ = [
    "ALLOWLIST",
    "HOSTS",
    "Allowance",
    "HostKind",
    "HostRig",
    "MatrixDifference",
    "Peer",
    "StdioPeer",
    "WsPeer",
    "load_allowlist",
    "session_host",
    "steps_script",
]

type HostKind = Literal["inprocess", "attached"]
type Frame = dict[str, Any]

HOSTS: tuple[HostKind, ...] = ("inprocess", "attached")
ALLOWLIST = ROOT / "tests" / "fixtures" / "service" / "dual_host_allowlist.yaml"
TIMEOUT = 60.0
D_ID = re.compile(r"^D-\d+\.\d+$")
"""A numbered intended difference (the lead numbered the builders' ``D-?`` at integration)."""


# -- the allow-list --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Allowance:
    """One documented difference: in ``case``, ``facet`` differs on ``host`` (``d_id``, ``why``)."""

    case: str
    facet: str
    host: HostKind
    d_id: str
    why: str


class MatrixDifference(AssertionError):
    """The hosts behave differently and the allow-list does not name it (or names it stale)."""


def load_allowlist(path: Path = ALLOWLIST) -> list[Allowance]:
    """The allow-list entries; a malformed entry (no D-id, no reason, unknown host) fails."""
    data = YAML(typ="safe").load(path.read_text(encoding="utf-8")) or {}
    entries = data.get("differences") or []
    found: list[Allowance] = []
    for index, entry in enumerate(entries):
        where = f"{path.name} entry {index}"
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: not a mapping")
        missing = {"case", "facet", "host", "d_id", "why"} - set(entry)
        if missing:
            raise ValueError(f"{where}: missing {sorted(missing)}")
        if entry["host"] not in HOSTS:
            raise ValueError(f"{where}: host must be one of {HOSTS}")
        if not D_ID.match(str(entry["d_id"])):
            raise ValueError(f"{where}: d_id {entry['d_id']!r} is not a D-id")
        if not str(entry["why"]).strip():
            raise ValueError(f"{where}: no reason")
        found.append(
            Allowance(
                str(entry["case"]),
                str(entry["facet"]),
                entry["host"],
                str(entry["d_id"]),
                str(entry["why"]).strip(),
            )
        )
    keys = [(a.case, a.facet, a.host) for a in found]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{path.name}: an entry is listed twice")
    return found


# -- fake-model scripts ----------------------------------------------------------------------------


def steps_script(*turns: list[dict[str, Any]], name: str = "inline") -> Script:
    """A script from turns written as P03 step lists (``[{"text": "hi"}]``, ...)."""
    return parse_script({"turns": [{"steps": steps} for steps in turns]}, name)


# -- JSON-RPC peers --------------------------------------------------------------------------------


def _offer(frame: Frame) -> dict[str, Any]:
    """A server request's fields, flat: the bridge sends them as the notification's params; the
    daemon wraps them (``{request, replayed, generation}``) and may send them as a request."""
    params = dict(frame.get("params") or {})
    inner = params.pop("request", None)
    if isinstance(inner, dict):
        params = {**inner, **params}
    if "id" in frame:
        params["rpc_id"] = frame["id"]
    return params


class Peer:
    """The frames one connection received, in order, and calls answered by id."""

    kind: HostKind

    def __init__(self) -> None:
        self.frames: list[Frame] = []
        self.dropped: Callable[[Frame], bool] = lambda frame: False
        self._new: queue.Queue[Frame] = queue.Queue()
        self._ids = 0
        self._lock = threading.Lock()

    def _received(self, frame: Frame) -> None:
        if frame.get("method") == SERVER_REQUEST_METHOD:
            frame = {"jsonrpc": "2.0", "method": SERVER_REQUEST_METHOD, "params": _offer(frame)}
        if self.dropped(frame):
            return
        with self._lock:
            self.frames.append(frame)
        self._new.put(frame)

    def _write(self, frame: Frame) -> None:
        raise NotImplementedError

    def send(self, method: str, **params: Any) -> int:
        with self._lock:
            self._ids += 1
            rid = self._ids
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return rid

    def wait_for(self, check: Callable[[Frame], bool], what: str, limit: float = TIMEOUT) -> Frame:
        with self._lock:
            seen = list(self.frames)
        for frame in seen:
            if check(frame):
                return frame
        end = time.monotonic() + limit
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise AssertionError(f"{self.kind}: no {what}; {self.context()}")
            try:
                frame = self._new.get(timeout=min(left, 0.5))
            except queue.Empty:
                continue
            if check(frame):
                return frame

    def answer_of(self, rid: int, method: str, limit: float = TIMEOUT) -> Frame:
        return self.wait_for(
            lambda f: f.get("id") == rid and "method" not in f, f"answer to {method}", limit
        )

    def call(self, method: str, **params: Any) -> Any:
        answer = self.answer_of(self.send(method, **params), method)
        assert "result" in answer, f"{self.kind}: {method} → {answer}"
        return answer["result"]

    def notifications(self) -> list[Frame]:
        with self._lock:
            return [f for f in self.frames if "method" in f]

    def events(self, method: str) -> list[dict[str, Any]]:
        return [f["params"] for f in self.notifications() if f["method"] == method]

    def methods(self) -> list[str]:
        return [f["method"] for f in self.notifications()]

    def answer(self, session_id: str, offer: dict[str, Any], answer: dict[str, Any]) -> Any:
        """Answer a server request the way this host takes answers (the generation it named)."""
        params: dict[str, Any] = {
            "session_id": session_id,
            "request_id": offer["request_id"],
            "answer": answer,
        }
        if offer.get("generation") is not None:
            params["generation"] = offer["generation"]
        return self.call("server_request.answer", **params)

    def context(self) -> str:
        return f"frames: {[f.get('method', f.get('id')) for f in self.frames][-40:]}"

    def close(self) -> None:
        raise NotImplementedError


class StdioPeer(Peer):
    """``indus tui-bridge --stdio``: the TUI's own child (the in-process host)."""

    kind: HostKind = "inprocess"

    def __init__(self, profile: ProfileHome) -> None:
        super().__init__()
        self.stderr_path = profile.root / f"bridge-{time.monotonic_ns()}.err"
        self._stderr = self.stderr_path.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            profile.argv("tui-bridge", "--stdio"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            cwd=profile.work,
            env=profile.env(INDUS_PROTOCOL_STRICT="1"),
            text=True,
            encoding="utf-8",
        )
        self._reader = threading.Thread(target=self._read, daemon=True, name="stdio-peer")
        self._reader.start()

    def _read(self) -> None:
        out: IO[str] | None = self.proc.stdout
        assert out is not None
        for line in out:
            self._received(json.loads(line))

    def _write(self, frame: Frame) -> None:
        stdin = self.proc.stdin
        assert stdin is not None
        stdin.write(json.dumps(frame) + "\n")
        stdin.flush()

    def context(self) -> str:
        self._stderr.flush()
        tail = self.stderr_path.read_text(encoding="utf-8", errors="replace")[-3000:]
        return f"{super().context()}\nstderr:\n{tail}"

    def close(self) -> None:
        if self.proc.poll() is None:
            if self.proc.stdin is not None:
                with contextlib.suppress(OSError):
                    self.proc.stdin.close()
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        self._stderr.close()


class WsPeer(Peer):
    """The daemon's loopback WebSocket, dialled with a fresh single-use ticket (attached host)."""

    kind: HostKind = "attached"

    def __init__(self, profile: ProfileHome, daemon: RunningDaemon | None = None) -> None:
        super().__init__()
        self.profile = profile
        self.daemon = daemon
        self.closed: TransportClosed | None = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="ws-peer")
        self._thread.start()
        grant = profile.grant()
        self.profile_id = grant.profile_id
        self.instance_id = grant.instance_id
        self.transport: WsClientTransport = self._run(
            ws_connect(grant.ws_url, grant.subprotocols())
        )
        self._reading = asyncio.run_coroutine_threadsafe(self._read(), self._loop)

    def _run[T](self, coro: Coroutine[Any, Any, T], limit: float = 30.0) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(limit)

    async def _read(self) -> None:
        try:
            while True:
                self._received(json.loads(await self.transport.recv()))
        except TransportClosed as closed:
            self.closed = closed

    def _write(self, frame: Frame) -> None:
        self._run(self.transport.send(json.dumps(frame)))

    def context(self) -> str:
        tail = self.daemon.log()[-3000:] if self.daemon is not None else ""
        return f"{super().context()}\ndaemon:\n{tail}"

    def drop(self) -> None:
        """Lose the socket without a goodbye (as a killed client would)."""
        writer = self.transport._writer  # pyright: ignore[reportPrivateUsage]
        self._loop.call_soon_threadsafe(writer.transport.abort)

    def close(self) -> None:
        if not self._loop.is_closed():
            with contextlib.suppress(Exception):
                self._run(self.transport.close(), 5)
            with contextlib.suppress(Exception):
                self._reading.result(5)
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(5)
            self._loop.close()


# -- one host under test ---------------------------------------------------------------------------


@dataclass
class HostRig:
    """A profile, the fake model and (attached) a running daemon; surfaces reach it the same way."""

    kind: HostKind
    root: Path
    model: HttpFakeModel
    allowances: list[Allowance]
    profile: ProfileHome = field(init=False)
    daemon: RunningDaemon | None = None
    peers: list[Peer] = field(default_factory=list[Peer])

    def __post_init__(self) -> None:
        self.profile = ProfileHome(self.root, self.model.port)

    # -- the model ------------------------------------------------------------------------------

    def use(self, script: Script | str | Path) -> None:
        """Answer from ``script`` from now on (a fresh scripted model on the same port)."""
        loaded = script if isinstance(script, Script) else load_script(script)
        fresh = HttpFakeModel(loaded)
        self.model.model = fresh.model

    # -- the host -------------------------------------------------------------------------------

    def start(self) -> HostRig:
        if self.kind == "attached":
            self.daemon = self.profile.start_daemon()
            self.daemon.attachable()
        return self

    def indus(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        """One classic-CLI run: ``--local`` in process, else attached to the daemon."""
        argv = ("--local", *args) if self.kind == "inprocess" else args
        return self.profile.indus(*argv, stdin=stdin)

    def spawn(self, *args: str) -> subprocess.Popen[str]:
        """A classic-CLI run left running (the caller ends it)."""
        argv = ("--local", *args) if self.kind == "inprocess" else args
        return self.profile.spawn(*argv)

    def peer(self, *, features: tuple[str, ...] = ("server_requests",)) -> Peer:
        """A JSON-RPC connection as the TUI makes one, after ``gateway.ready`` and capabilities."""
        peer: Peer = (
            StdioPeer(self.profile)
            if self.kind == "inprocess"
            else WsPeer(self.profile, self.daemon)
        )
        self.peers.append(peer)
        ready = peer.wait_for(lambda f: True, "the first frame")
        assert ready.get("method") == "gateway.ready", ready
        peer.call("client.capabilities", features=list(features), surface="tui")
        return peer

    # -- the matrix -----------------------------------------------------------------------------

    def allowed(self, case: str, facet: str) -> Allowance | None:
        for entry in self.allowances:
            if (entry.case, entry.facet, entry.host) == (case, facet, self.kind):
                return entry
        return None

    def same(self, case: str, facet: str, got: object, want: object) -> None:
        """``got`` must equal ``want`` on this host, unless the allow-list names the difference;
        an allow-listed facet that no longer differs is stale and fails too."""
        entry = self.allowed(case, facet)
        if got == want:
            if entry is not None:
                raise MatrixDifference(
                    f"{case}/{facet}: the {self.kind} host now behaves like the other one; "
                    f"remove the allow-list entry ({entry.d_id})"
                )
            return
        if entry is not None:
            return
        raise MatrixDifference(
            f"{case}/{facet} differs on the {self.kind} host:\n  got:  {got!r}\n  want: {want!r}"
        )

    def stop(self) -> None:
        for peer in self.peers:
            with contextlib.suppress(Exception):
                peer.close()
        self.profile.stop_all()


@contextlib.contextmanager
def host_rig(
    kind: HostKind, root: Path, script: Script | str | Path | None = None
) -> Iterator[HostRig]:
    """One rig for ``kind`` in ``root`` (its own fake model), stopped on the way out."""
    with serving(script if script is not None else steps_script([{"text": "unused"}])) as model:
        rig = HostRig(kind, root, model, load_allowlist())
        try:
            yield rig.start()
        finally:
            rig.stop()
            leftover = rig.profile.stray_processes()
            assert leftover == [], f"processes naming the home are still running: {leftover}"


@pytest.fixture(params=HOSTS)
def session_host(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[HostRig]:
    """The rig for one host kind (``inprocess`` / ``attached``) in this test's temp dir."""
    with host_rig(request.param, tmp_path / request.param) as rig:
        yield rig


def settle(seconds: float = 0.3) -> None:
    """A short wall-clock pause for frames that may still be on their way."""
    pause(seconds)
