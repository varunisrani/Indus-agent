"""The launch adapter: `indus chat` → the classic REPL with slash commands wired (P06-03/04/09)."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from rich.console import Console

from indus.cli import launch as launch_mod
from indus.cli.handoff import ReplLaunch
from indus.cli.resume import MODEL_OVERRIDE_KEY
from indus.cli.session_select import LAUNCH_KEY, LaunchPolicy
from indus.cli.slash.context import SlashContext
from indus.foundation.scope import Scope
from indus.providers.route import RuntimeRoute
from indus.state import messages, registry, sessions
from indus.state.registry import StoreHandle
from tests.fakes.service import SESSION, FakeHost
from tests.unit.cli.slash._fakes import FakePort, route_of

OTHER = "20260101_080000_other1"


@pytest.fixture(autouse=True)
def keep_cwd() -> Iterator[None]:
    here = os.getcwd()
    yield
    os.chdir(here)


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()
    registry.close_all()


def make_launch(scope: Scope, store: StoreHandle, tmp_path: Path, **values: Any) -> ReplLaunch:
    fields: dict[str, Any] = {
        "scope": scope,
        "host": cast(Any, FakeHost(SESSION)),
        "session_id": SESSION,
        "policy": LaunchPolicy(cwd=str(tmp_path), toolsets=("web",)),
        "model": "m1",
        "provider": "fake",
        "cwd": tmp_path,
        "store": store,
    }
    fields.update(values)
    return ReplLaunch(**fields)


class Captured:
    def __init__(self) -> None:
        self.args: tuple[Any, ...] = ()
        self.kwargs: dict[str, Any] = {}

    def __call__(self, *args: Any, **kwargs: Any) -> int:
        self.args, self.kwargs = args, kwargs
        return 0


class FakeRepl:
    def __init__(self) -> None:
        self.posted: list[Callable[[], Any]] = []
        self.sent: list[str] = []
        self.notes: list[str] = []
        self.printed: list[Any] = []
        self.options = SimpleNamespace(model="m1")

    def post(self, fn: Callable[[], Any]) -> None:
        self.posted.append(fn)

    def send(self, text: str) -> None:
        self.sent.append(text)

    def note(self, text: str) -> None:
        self.notes.append(text)

    def print(self, renderable: Any) -> None:
        self.printed.append(renderable)


def plain(renderable: Any) -> str:
    console = Console(width=100, record=True, color_system=None, file=open(os.devnull, "w"))  # noqa: SIM115
    console.print(renderable)
    return console.export_text()


def test_run_launch_builds_the_context_and_seeds_the_first_turn(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    run = Captured()
    launch = make_launch(scope, store, tmp_path, seed="/help !ls")
    assert launch_mod.run_launch(launch, run=run) == 0
    host, session_id = run.args
    assert session_id == SESSION and host is launch.host
    ctx = run.kwargs["slash_ctx"]
    assert isinstance(ctx, SlashContext)
    assert ctx.store is store and ctx.session_id == SESSION
    assert ctx.create_params.cwd == str(tmp_path) and ctx.create_params.toolsets == ["web"]
    assert ctx.state.toolsets == ["web"]
    assert run.kwargs["options"].model == "m1"
    assert run.kwargs["recap"] is None
    assert "Indus Agent" in plain(run.kwargs["banner"])
    repl = FakeRepl()
    run.kwargs["on_ready"](repl)
    for job in repl.posted:
        job()
    assert repl.sent == ["/help !ls"]  # sent literally, never dispatched


def test_resumed_launch_shows_the_recap(scope: Scope, store: StoreHandle, tmp_path: Path) -> None:
    run = Captured()
    history = [
        {"role": "user", "content": "create hello.py"},
        {"role": "assistant", "content": "Done."},
    ]
    launch = make_launch(scope, store, tmp_path, resumed=True, history=history, title="demo")
    launch_mod.run_launch(launch, run=run)
    shown = plain(run.kwargs["recap"])
    assert "create hello.py" in shown and "Done." in shown
    assert run.kwargs["options"].title == "demo"


def test_session_cwd_reads_the_row(scope: Scope, store: StoreHandle, tmp_path: Path) -> None:
    sessions.create(store, SESSION, "cli", cwd=str(tmp_path / "proj"))
    ctx = launch_mod.build_context(make_launch(scope, store, tmp_path), FakePort())
    assert ctx.session_cwd is not None and ctx.session_cwd() == str(tmp_path / "proj")


def test_on_resume_restores_and_puts_the_switched_model_back(
    scope: Scope, store: StoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from indus.providers import route as routes

    def fake_resolve(_scope: Scope, *, model: str | None = None, **_: Any) -> RuntimeRoute:
        return route_of(model or "m1")

    monkeypatch.setattr(routes, "resolve", fake_resolve)
    where = tmp_path / "older"
    where.mkdir()
    record = LaunchPolicy(cwd=str(where), model="m1", provider="fake").record()
    override = {"provider": "fake", "model": "m2"}
    sessions.create(
        store,
        OTHER,
        "cli",
        cwd=str(where),
        model_config={LAUNCH_KEY: record, MODEL_OVERRIDE_KEY: override},
    )
    messages.append(store, OTHER, {"role": "user", "content": "earlier question"})
    messages.append(store, OTHER, {"role": "assistant", "content": "earlier answer"})
    port = FakePort()
    launch = make_launch(scope, store, tmp_path)
    ctx = launch_mod.build_context(launch, port)
    repl = FakeRepl()
    on_resume = launch_mod.make_on_resume(launch, ctx, cast(Any, {"repl": repl}))
    on_resume(OTHER)
    assert Path.cwd() == where.resolve()
    assert ("switch_route", ("fake", "m2", None, True)) in port.calls
    assert repl.options.model == "m2"
    assert any("m2" in note for note in repl.notes)
