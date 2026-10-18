"""Per-profile log routing across real profile homes and threads (R03.23)."""

from __future__ import annotations

import contextvars
import logging
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.foundation import log as logmod
from indus.foundation.scope import Scope, make_scope, use_scope

LINES_PER_WORKER = 50


@pytest.fixture(autouse=True)
def clean_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    factory = logging.getLogRecordFactory()
    yield
    logmod.shutdown()
    root.handlers[:] = handlers
    root.setLevel(level)
    logging.setLogRecordFactory(factory)


def _profile(root: Path, name: str) -> Path:
    home = root / "profiles" / name
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("{}\n")
    return home


def test_threads_with_different_scopes_write_to_their_own_profile(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    homes = {"default": root, "work": _profile(root, "work"), "lab": _profile(root, "lab")}
    scopes: dict[str, Scope] = {name: make_scope(home) for name, home in homes.items()}
    logmod.setup(scopes["default"], "gateway")
    logger = logging.getLogger("indus.gateway.worker")
    start = threading.Barrier(len(homes) + 1)

    def worker(name: str) -> None:
        with use_scope(scopes[name]):
            start.wait()
            for i in range(LINES_PER_WORKER):
                logger.info("profile=%s n=%d", name, i)
            logger.warning("profile=%s done", name)

    def unscoped() -> None:
        start.wait()
        logger.info("profile=none from a bare thread")

    threads = [
        threading.Thread(target=contextvars.copy_context().run, args=(worker, name))
        for name in ("work", "lab")
    ]
    threads.append(threading.Thread(target=unscoped))
    threads.append(threading.Thread(target=worker, args=("default",)))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    logmod.flush()

    for name, home in homes.items():
        agent = (home / "logs" / "agent.log").read_text(encoding="utf-8")
        errors = (home / "logs" / "errors.log").read_text(encoding="utf-8")
        gateway = (home / "logs" / "gateway.log").read_text(encoding="utf-8")
        assert agent.count(f"profile={name} n=") == LINES_PER_WORKER
        assert f"profile={name} done" in errors
        assert f"profile={name} done" in gateway
        for other in homes:
            if other != name:
                assert f"profile={other} " not in agent
    # Work with no scope bound belongs to the process profile.
    assert "profile=none" in (root / "logs" / "agent.log").read_text(encoding="utf-8")

    assert logmod.release_home(homes["work"]) is True
    with use_scope(scopes["work"]):
        logger.info("profile=work after release")
    logmod.flush()
    work_log = (homes["work"] / "logs" / "agent.log").read_text(encoding="utf-8")
    assert "profile=work after release" in work_log


def test_profile_deleted_while_logging_falls_back_without_recreating(tmp_path: Path) -> None:
    import shutil

    root = tmp_path / "root"
    root.mkdir()
    work = _profile(root, "work")
    default_scope = make_scope(root)
    work_scope = make_scope(work)
    logmod.setup(default_scope, "cli")
    logger = logging.getLogger("indus.core.del")
    with use_scope(work_scope):
        logger.info("before delete")
    logmod.flush()
    shutil.rmtree(work)
    with use_scope(work_scope):
        logger.info("after delete")
    logmod.flush()
    assert not work.exists()
    assert "after delete" in (root / "logs" / "agent.log").read_text(encoding="utf-8")
