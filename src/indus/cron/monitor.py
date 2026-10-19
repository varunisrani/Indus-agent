"""Monitor mode: run a cheap source first and wake the agent only when its output changed (H7).

A job names either ``monitor_script`` (a file in ``<home>/scripts/``, run by the cron script runner
with the same folder and environment rules as pre-run scripts) or ``monitor_url`` (fetched over
http(s) through the P05 URL policy: 30 s, at most 256 KiB — a bigger body is an error). The exact
output bytes are hashed with SHA-256:

- same hash as the stored ``monitor_state.hash`` → ``no_change``: the run is skipped, no model call;
- no stored hash → ``first``: the prompt gets the current output;
- another hash → ``changed``: the prompt gets a unified diff against the last output (≤ 4,000
  characters) and the current output (≤ 8,000 characters);
- the source failing → ``error``: never a change; the stored hash and snapshot stay as they were.

On ``first``/``changed`` the new snapshot is written to
``<home>/cron/output/<job_id>/monitor_last_output.txt`` before the agent runs, and the outcome
carries the new ``monitor_state {hash, last_output_path}`` for the caller to store on the job.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

from indus.cron import ports
from indus.foundation import fsio
from indus.foundation.scope import Scope
from indus.safety.safe_transport import ConnectBlocked, TransportFactory, safe_client
from indus.safety.url_policy import Blocked, Resolver, check_url_async

__all__ = [
    "MAX_DIFF_CHARS",
    "MAX_OUTPUT_CHARS",
    "MAX_URL_BYTES",
    "SNAPSHOT_NAME",
    "URL_TIMEOUT_S",
    "MonitorOutcome",
    "check_monitor",
    "fetch_url",
    "has_monitor",
    "split_monitor",
    "validate_monitor",
]

log = logging.getLogger(__name__)

MAX_DIFF_CHARS = 4000
MAX_OUTPUT_CHARS = 8000
URL_TIMEOUT_S = 30.0
MAX_URL_BYTES = 256 * 1024
SNAPSHOT_NAME = "monitor_last_output.txt"
_CUT = "\n[… cut]"

type MonitorStatus = Literal["first", "changed", "no_change", "error"]
type ScriptRunner = Callable[[Scope, str, Any], Awaitable[tuple[bool, str]]]
"""``(scope, script name, job) -> (ok, stdout or error)`` — the cron script runner (P14-09)."""


async def _cron_script(scope: Scope, name: str, job: Any) -> tuple[bool, str]:
    """The cron script runner (P14-09): the same folder, environment and timeout as a job script."""
    from indus.cron.script import run_script

    result = await run_script(scope, ports.spec_of(job), script=name)
    return (True, result.stdout) if result.ok else (False, result.failure_text)


RUN_SCRIPT: ScriptRunner | None = _cron_script
"""The monitor's script runner (the cron script runner); tests pass ``run_script`` directly."""


@dataclass(frozen=True)
class MonitorOutcome:
    status: MonitorStatus
    block: str | None = None
    error: str | None = None
    state: dict[str, str] | None = None

    @property
    def should_run(self) -> bool:
        return self.status in ("first", "changed")


# -- specs --------------------------------------------------------------------------------------


def has_monitor(job: Any) -> bool:
    return bool(_text(ports.field(job, "monitor_script")) or _text(ports.field(job, "monitor_url")))


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def split_monitor(value: str | None) -> tuple[str | None, str | None]:
    """One user-facing ``monitor`` value → ``(monitor_script, monitor_url)``; '' clears both."""
    text = (value or "").strip()
    if not text:
        return None, None
    if text.lower().startswith(("http://", "https://")):
        return None, text
    return text, None


def validate_monitor(
    monitor_script: str | None, monitor_url: str | None, *, no_agent: bool = False
) -> str | None:
    """Why this monitor spec is refused, or None."""
    script, url = _text(monitor_script), _text(monitor_url)
    if not script and not url:
        return None
    if script and url:
        return "a job takes a monitor script or a monitor URL, not both"
    if no_agent:
        return "monitor mode wakes the agent, so it cannot be used on a script-only (no_agent) job"
    if url and not url.lower().startswith(("http://", "https://")):
        return f"the monitor URL must start with http:// or https:// (got {url!r})"
    return None


# -- fetching -------------------------------------------------------------------------------------


async def fetch_url(
    scope: Scope,
    url: str,
    *,
    resolver: Resolver | None = None,
    inner_factory: TransportFactory | None = None,
) -> tuple[bool, bytes | str]:
    """``(True, body)`` or ``(False, reason)``; never raises."""
    if not url.lower().startswith(("http://", "https://")):
        return False, f"the monitor URL must be http(s): {url!r}"
    verdict = await check_url_async(url, scope, resolver=resolver)
    if isinstance(verdict, Blocked):
        return False, f"the monitor URL was refused: {verdict.reason}"
    client = safe_client(
        scope,
        resolver=resolver,
        inner_factory=inner_factory,
        timeout=httpx.Timeout(URL_TIMEOUT_S),
        headers={"User-Agent": "indus-cron-monitor"},
    )
    try:
        async with client:
            return await asyncio.wait_for(_read_capped(client, url), URL_TIMEOUT_S)
    except ConnectBlocked as exc:
        return False, f"the monitor URL was refused: {exc.verdict.reason}"
    except TimeoutError:
        return False, f"the monitor URL did not answer within {URL_TIMEOUT_S:g} s"
    except httpx.HTTPError as exc:
        return False, f"the monitor URL could not be fetched: {type(exc).__name__}"


async def _read_capped(client: httpx.AsyncClient, url: str) -> tuple[bool, bytes | str]:
    async with client.stream("GET", url) as response:
        if response.status_code >= 400:
            return False, f"the monitor URL answered HTTP {response.status_code}"
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_URL_BYTES:
                return False, f"the monitor URL returned more than {MAX_URL_BYTES // 1024} KiB"
            chunks.append(chunk)
        return True, b"".join(chunks)


# -- the check ------------------------------------------------------------------------------------


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - len(_CUT)] + _CUT


def build_diff(old: str, new: str) -> str:
    lines = difflib.unified_diff(
        old.splitlines(), new.splitlines(), fromfile="last run", tofile="this run", lineterm=""
    )
    return _cap("\n".join(lines), MAX_DIFF_CHARS)


def snapshot_path(scope: Scope, job_id: str) -> Path:
    return ports.output_dir(scope, job_id) / SNAPSHOT_NAME


async def _source_output(
    scope: Scope,
    job: Any,
    run_script: ScriptRunner | None,
    resolver: Resolver | None,
    inner_factory: TransportFactory | None,
) -> tuple[bool, bytes | str]:
    script = _text(ports.field(job, "monitor_script"))
    url = _text(ports.field(job, "monitor_url"))
    if script:
        runner = run_script if run_script is not None else RUN_SCRIPT
        if runner is None:
            return False, "the cron script runner is not available"
        try:
            ok, out = await runner(scope, script, job)
        except Exception as exc:
            return False, f"the monitor script failed: {type(exc).__name__}: {exc}"
        return (True, out.encode("utf-8", "replace")) if ok else (False, out or "script failed")
    if url:
        return await fetch_url(scope, url, resolver=resolver, inner_factory=inner_factory)
    return False, "the job has no monitor source"


async def check_monitor(
    scope: Scope,
    job: Any,
    *,
    run_script: ScriptRunner | None = None,
    resolver: Resolver | None = None,
    inner_factory: TransportFactory | None = None,
) -> MonitorOutcome:
    """Run the job's monitor source and decide whether the agent runs (see the module notes)."""
    job_id = str(ports.field(job, "id", "") or "")
    ok, produced = await _source_output(scope, job, run_script, resolver, inner_factory)
    if not ok or not isinstance(produced, bytes):
        return MonitorOutcome("error", error=str(produced))
    digest = hashlib.sha256(produced).hexdigest()
    state = ports.field(job, "monitor_state")
    last_hash = _text(ports.field(state, "hash")) if state is not None else ""
    if last_hash and last_hash == digest:
        return MonitorOutcome("no_change")
    current = produced.decode("utf-8", "replace")
    path = snapshot_path(scope, job_id)
    old = ""
    if last_hash:
        try:
            old = path.read_bytes().decode("utf-8", "replace")
        except OSError:
            old = ""
    shown = f"### Output now\n\n```\n{_cap(current, MAX_OUTPUT_CHARS)}\n```"
    if last_hash:
        status: MonitorStatus = "changed"
        block = (
            "## Monitored source changed\n\n"
            "The output of this job's monitor differs from the last run.\n\n"
            f"### What changed\n\n```diff\n{build_diff(old, current)}\n```\n\n{shown}"
        )
    else:
        status = "first"
        block = (
            "## Monitored source: first look\n\n"
            "This is the first time this job's monitor ran; there is nothing to compare with yet."
            f"\n\n{shown}"
        )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fsio.atomic_write_bytes(path, produced)
    except OSError as exc:
        log.warning("monitor snapshot for %s not written: %s", job_id, exc)
    return MonitorOutcome(
        status, block=block, state={"hash": digest, "last_output_path": str(path)}
    )
