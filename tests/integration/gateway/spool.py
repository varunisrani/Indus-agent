"""Real gateway daemon processes for the P15-24..26 tests.

``SpoolHome`` lays out a profile home in the test's tmp dir: a config that points the model at the
loopback fake (``tests/fakes/model/http_serve.py``), enables the spool plugin (copied from
``tests/fixtures/gateway/spool_plugin``) and its platform, and keeps the token-lock dir inside the
tmp dir. ``start`` runs the daemon as a child process; ``send`` and ``outbound`` speak the spool
files; ``stray_processes`` finds any process still carrying this home in its environment.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from indus.state.gateway_runtime import RuntimeStatus, read_status

ROOT = Path(__file__).resolve().parents[3]
PLUGIN = ROOT / "tests" / "fixtures" / "gateway" / "spool_plugin"
ALLOWED = "200"
TIMEOUT = 60.0


_TICK = threading.Event()  # never set: ``wait`` is a wall-clock pause between polls


def _pause(seconds: float) -> None:
    _TICK.wait(seconds)


@dataclass
class SpoolHome:
    root: Path
    model_port: int
    env_extra: dict[str, str] = field(default_factory=dict[str, str])

    def __post_init__(self) -> None:
        self.home = self.root / "home"
        self.spool = self.root / "spool"
        self.work = self.root / "work"
        for folder in (self.home, self.spool / "inbound", self.work):
            folder.mkdir(parents=True, exist_ok=True)
        shutil.copytree(PLUGIN, self.home / "plugins" / "spool", dirs_exist_ok=True)
        (self.home / "config.yaml").write_text(
            "model:\n"
            "  default: fake-model\n"
            "  provider: custom\n"
            f"  base_url: http://127.0.0.1:{self.model_port}/v1\n"
            "plugins:\n"
            "  enabled: [spool]\n"
            "platforms:\n"
            "  spool:\n"
            "    enabled: true\n"
            f"    dir: {json.dumps(str(self.spool))}\n",
            encoding="utf-8",
        )
        self._count = 0

    def env(self, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("INDUS_")}
        for name in ("NOTIFY_SOCKET", "WATCHDOG_USEC", "TELEGRAM_BOT_TOKEN"):
            env.pop(name, None)
        env.update(
            {
                "INDUS_HOME": str(self.home),
                "INDUS_SPOOL_ALLOWED_USERS": ALLOWED,
                "INDUS_GATEWAY_LOCK_DIR": str(self.root / "token-locks"),
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
                "PYTHONPATH": str(ROOT),
                **self.env_extra,
                **extra,
            }
        )
        return env

    def start(self, *args: str, **env: str) -> Daemon:
        log = (self.root / f"daemon-{time.monotonic_ns()}.err").open("w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-m", "indus", "gateway", "run", *args],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            cwd=self.work,
            env=self.env(**env),
        )
        return Daemon(self, proc, Path(log.name))

    def send(self, text: str, *, user: str = ALLOWED) -> None:
        self._count += 1
        name = f"{time.monotonic_ns():020d}-{self._count:04d}"
        body = {"chat_id": user, "user_id": user, "text": text, "message_id": f"in-{name}"}
        temp = self.spool / "inbound" / f"{name}.part"
        temp.write_text(json.dumps(body), encoding="utf-8")
        temp.rename(temp.with_suffix(".json"))

    def outbound(self, chat_id: str = ALLOWED) -> list[str]:
        path = self.spool / "outbound.jsonl"
        if not path.exists():
            return []
        lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return [str(line["text"]) for line in lines if line["chat_id"] == chat_id]

    def status(self) -> RuntimeStatus | None:
        return read_status(self.home)

    def stray_processes(self) -> list[int]:
        """Pids (other than ours) whose environment names this home."""
        marker = f"INDUS_HOME={self.home}".encode()
        found: list[int] = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                environ = (entry / "environ").read_bytes()
            except OSError:
                continue
            if marker in environ.split(b"\0"):
                found.append(int(entry.name))
        return found


@dataclass
class Daemon:
    home: SpoolHome
    proc: subprocess.Popen[bytes]
    log_path: Path

    def log(self) -> str:
        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        gateway_log = self.home.home / "logs" / "gateway.log"
        if gateway_log.exists():
            text += "\n--- gateway.log ---\n" + gateway_log.read_text(errors="replace")[-8000:]
        return text

    def until(self, check: Callable[[], Any], what: str, limit: float = TIMEOUT) -> Any:
        end = time.monotonic() + limit
        while True:
            found = check()
            if found:
                return found
            if self.proc.poll() is not None and not check():
                raise AssertionError(
                    f"daemon exited ({self.proc.returncode}) before {what}:\n{self.log()}"
                )
            if time.monotonic() > end:
                raise AssertionError(f"timed out waiting for {what}:\n{self.log()}")
            _pause(0.05)

    def running(self) -> RuntimeStatus:
        def check() -> RuntimeStatus | None:
            status = self.home.status()
            ok = status is not None and status.pid == self.proc.pid and status.state == "running"
            return status if ok else None

        return self.until(check, "the running state")

    def signal(self, signum: int) -> None:
        self.proc.send_signal(signum)

    def wait(self, limit: float = TIMEOUT) -> int:
        try:
            return self.proc.wait(limit)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            raise AssertionError(f"daemon did not exit within {limit} s:\n{self.log()}") from None

    def stop(self) -> int:
        if self.proc.poll() is None:
            self.signal(signal.SIGTERM)
        return self.wait()

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
