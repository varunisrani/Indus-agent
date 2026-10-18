"""foundation.log: queue logging, per-home routing, redaction, rotation, stdio purity."""

from __future__ import annotations

import asyncio
import logging
import stat
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from indus.foundation import log as logmod
from indus.foundation.scope import ConfigSource, Scope, make_scope, use_scope
from tests.fakes.clock import checkpoint

SECRET = "sk-test-key-0000000000000000"


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


def _home(tmp_path: Path, name: str = "home") -> Path:
    home = tmp_path / name
    home.mkdir(parents=True, exist_ok=True)
    return home


class FakeConfig:
    """A config source for values the real schema would reject: ``get("a.b")`` on nested dicts."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = data

    def get(self, path: str) -> object:
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return None
            node = node[part]  # pyright: ignore[reportUnknownVariableType]
        return node  # pyright: ignore[reportUnknownVariableType]


def _scope(
    home: Path, *, env: Mapping[str, str] | None = None, config: ConfigSource | None = None
) -> Scope:
    return make_scope(home, env=env).with_config(config)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_records_from_two_tasks_land_in_their_own_homes(tmp_path: Path) -> None:
    root = _home(tmp_path, "root")
    work = _home(tmp_path, "root/profiles/work")
    process = _scope(root)
    other = _scope(work)
    logmod.setup(process, "cli")
    logger = logging.getLogger("indus.core.test")

    async def emit(scope: Any, text: str) -> None:
        with use_scope(scope):
            for i in range(5):
                logger.info("%s line %d", text, i)
                await checkpoint()  # interleave the two tasks

    async def main() -> None:
        await asyncio.gather(emit(process, "from-default"), emit(other, "from-work"))

    asyncio.run(main())
    logmod.flush()
    default_log = _read(root / "logs" / "agent.log")
    work_log = _read(work / "logs" / "agent.log")
    assert default_log.count("from-default") == 5
    assert "from-work" not in default_log
    assert work_log.count("from-work") == 5
    assert "from-default" not in work_log


def test_warning_reaches_errors_log_and_info_does_not(tmp_path: Path) -> None:
    home = _home(tmp_path)
    logmod.setup(_scope(home), "cli")
    logger = logging.getLogger("indus.core.x")
    logger.info("plain info")
    logger.warning("careful now")
    logmod.flush()
    agent = _read(home / "logs" / "agent.log")
    errors = _read(home / "logs" / "errors.log")
    assert "plain info" in agent and "careful now" in agent
    assert "careful now" in errors
    assert "plain info" not in errors


def test_secret_in_message_is_masked_in_the_file(tmp_path: Path) -> None:
    home = _home(tmp_path)
    logmod.setup(_scope(home, env={"OPENAI_API_KEY": SECRET}), "cli")
    logging.getLogger("indus.providers.x").warning("calling with key %s", SECRET)
    try:
        raise RuntimeError(f"boom {SECRET}")
    except RuntimeError:
        logging.getLogger("indus.providers.x").exception("failed")
    logmod.flush()
    for name in ("agent.log", "errors.log"):
        text = _read(home / "logs" / name)
        assert "calling with key" in text
        assert SECRET not in text


def test_stdio_mode_keeps_stdout_empty(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    home = _home(tmp_path)
    # A library that attached a stdout handler before setup must not keep writing there.
    logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))
    logmod.setup(_scope(home), "stdio")
    logger = logging.getLogger("indus.mcp_serve.x")
    logger.info("quiet info")
    logger.warning("loud warning")
    logmod.flush()
    out, err = capfd.readouterr()
    assert out == ""
    assert "loud warning" in err
    assert "quiet info" in _read(home / "logs" / "agent.log")


def test_piped_stdout_becomes_line_buffered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    piped = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", piped)
    logmod.setup(_scope(_home(tmp_path)), "cli")
    assert piped.line_buffering is True


def test_cli_mode_without_verbose_has_no_console_output(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    home = _home(tmp_path)
    logmod.setup(_scope(home), "cli")
    logging.getLogger("indus.cli.x").warning("only in files")
    logmod.flush()
    out, err = capfd.readouterr()
    assert out == "" and err == ""


def test_verbose_adds_debug_on_stderr(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    home = _home(tmp_path)
    logmod.setup(_scope(home), "cli", verbose=True)
    logging.getLogger("indus.cli.x").debug("deep detail")
    logmod.flush()
    out, err = capfd.readouterr()
    assert out == ""
    assert "deep detail" in err


def test_rotation_keeps_backup_count(tmp_path: Path) -> None:
    home = _home(tmp_path)
    config = FakeConfig({"logging": {"max_size_mb": 0.0005, "backup_count": 2}})
    logmod.setup(_scope(home, config=config), "cli")
    logger = logging.getLogger("indus.core.rot")
    for i in range(200):
        logger.info("record number %04d with some padding to fill the file", i)
    logmod.flush()
    logs = home / "logs"
    assert (logs / "agent.log").exists()
    assert (logs / "agent.log.1").exists()
    assert (logs / "agent.log.2").exists()
    assert not (logs / "agent.log.3").exists()
    assert (logs / "agent.log").stat().st_size <= 600


def test_external_rotation_reopens_the_file(tmp_path: Path) -> None:
    home = _home(tmp_path)
    logmod.setup(_scope(home), "cli")
    logger = logging.getLogger("indus.core.mv")
    logger.info("before the move")
    logmod.flush()
    agent = home / "logs" / "agent.log"
    agent.rename(home / "logs" / "agent.log.old")
    logger.info("after the move")
    logmod.flush()
    assert "after the move" in _read(agent)
    assert "before the move" not in _read(agent)
    assert "after the move" not in _read(home / "logs" / "agent.log.old")


def test_drain_returns_within_timeout_when_the_writer_is_stuck(tmp_path: Path) -> None:
    home = _home(tmp_path)
    logmod.setup(_scope(home), "cli")
    live = logmod._live  # pyright: ignore[reportPrivateUsage]
    assert live is not None
    release = threading.Event()
    real_emit = live.router.emit

    def stuck_emit(record: logging.LogRecord) -> None:
        release.wait(5.0)
        real_emit(record)

    live.router.emit = stuck_emit  # type: ignore[method-assign]
    logging.getLogger("indus.core.stuck").warning("waiting")
    started = time.monotonic()
    assert logmod.drain(timeout=0.1) is False
    assert time.monotonic() - started < 1.0
    release.set()
    assert logmod.drain(timeout=5.0) is True
    assert "waiting" in _read(home / "logs" / "agent.log")


def test_session_and_turn_tags_appear(tmp_path: Path) -> None:
    home = _home(tmp_path)
    logmod.setup(_scope(home), "cli")
    logger = logging.getLogger("indus.core.tags")
    with logmod.log_tags(session_id="20260101_090000_abcdef", turn_id="3"):
        logger.info("tagged")
    logger.info("untagged")
    logmod.flush()
    lines = _read(home / "logs" / "agent.log").splitlines()
    tagged = next(line for line in lines if "tagged" in line and "untagged" not in line)
    assert "[20260101_090000_abcdef:3]" in tagged
    assert "[" not in next(line for line in lines if "untagged" in line)


def test_gateway_file_only_in_gateway_mode_and_for_gateway_loggers(tmp_path: Path) -> None:
    home = _home(tmp_path)
    logmod.setup(_scope(home), "gateway")
    logging.getLogger("indus.gateway.telegram").info("gateway news")
    logging.getLogger("indus.core.loop").info("core news")
    logmod.flush()
    gateway = _read(home / "logs" / "gateway.log")
    assert "gateway news" in gateway
    assert "core news" not in gateway
    assert not (home / "logs" / "gui.log").exists()

    logmod.setup(_scope(home), "gui")
    logging.getLogger("indus.web.app").info("gui news")
    logging.getLogger("indus.gateway.telegram").info("gateway later")
    logmod.flush()
    assert "gui news" in _read(home / "logs" / "gui.log")
    assert "gateway later" not in _read(home / "logs" / "gateway.log")


def test_noisy_loggers_pinned_at_warning(tmp_path: Path) -> None:
    logmod.setup(_scope(_home(tmp_path)), "cli")
    for name in logmod.NOISY_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING


def test_files_owner_only(tmp_path: Path) -> None:
    home = _home(tmp_path)
    logs = logmod.setup(_scope(home), "cli")
    logging.getLogger("indus.core.p").warning("x")
    logmod.flush()
    assert stat.S_IMODE(logs.stat().st_mode) == 0o700
    for name in ("agent.log", "errors.log"):
        assert stat.S_IMODE((logs / name).stat().st_mode) == 0o600


def test_deleted_or_tombstoned_routed_home_is_never_recreated(tmp_path: Path) -> None:
    root = _home(tmp_path, "root")
    gone = root / "profiles" / "gone"  # never created
    dead = _home(tmp_path, "root/profiles/dead")
    (root / "profiles" / ".deleted").mkdir()
    (root / "profiles" / ".deleted" / "dead").write_text("2026-01-01T00:00:00Z")
    logmod.setup(_scope(root), "cli")
    logger = logging.getLogger("indus.core.route")
    with use_scope(_scope(gone)):
        logger.info("for a missing profile")
    with use_scope(_scope(dead)):
        logger.info("for a tombstoned profile")
    logmod.flush()
    assert not gone.exists()
    assert not (dead / "logs").exists()
    text = _read(root / "logs" / "agent.log")
    assert "for a missing profile" in text
    assert "for a tombstoned profile" in text


def test_release_home_closes_and_reopens_on_demand(tmp_path: Path) -> None:
    root = _home(tmp_path, "root")
    work = _home(tmp_path, "root/profiles/work")
    logmod.setup(_scope(root), "cli")
    with use_scope(_scope(work)):
        logging.getLogger("indus.core.r").info("first")
    logmod.flush()
    assert logmod.release_home(work) is True
    assert logmod.release_home(work) is False


def test_setup_twice_leaves_one_queue_handler(tmp_path: Path) -> None:
    home = _home(tmp_path)
    logmod.setup(_scope(home), "cli")
    logmod.setup(_scope(home), "cli")
    queued = [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logmod._NoFormatQueueHandler)  # pyright: ignore[reportPrivateUsage]
    ]
    assert len(queued) == 1


def test_unknown_mode_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        logmod.setup(_scope(_home(tmp_path)), "daemon")  # type: ignore[arg-type]


def test_unknown_level_falls_back_to_info(tmp_path: Path) -> None:
    home = _home(tmp_path)
    config = FakeConfig({"logging": {"level": "LOUD"}})
    logmod.setup(_scope(home, config=config), "cli")
    logging.getLogger("indus.core.lvl").info("still written")
    logging.getLogger("indus.core.lvl").debug("not written")
    logmod.flush()
    text = _read(home / "logs" / "agent.log")
    assert "still written" in text
    assert "not written" not in text


def test_configured_debug_level_reaches_agent_log_only(tmp_path: Path) -> None:
    home = _home(tmp_path)
    config = FakeConfig({"logging": {"level": "debug"}})
    logmod.setup(_scope(home, config=config), "cli")
    logging.getLogger("indus.core.lvl").debug("fine detail")
    logmod.flush()
    assert "fine detail" in _read(home / "logs" / "agent.log")
    assert "fine detail" not in _read(home / "logs" / "errors.log")
