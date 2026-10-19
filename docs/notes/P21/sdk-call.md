# Task: SDK call wrapper (P21-02) Phase: 21 Owner: P21 slice B1 builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/vercel_sandbox.py`
(`_exception_chain`, `_extract_status_code`, `_is_transient_vercel_error`, `_retry_vercel_call`),
`tools/environments/modal.py` (`_AsyncWorker`), `tools/environments/daytona.py` (`_run_bash`).
Closed before writing. Evidence: none.

What it does (own words): vendor SDKs are blocking and some of them ignore their own timeouts, so
one stuck HTTP call could hang a tool call for ever (R02.30). Every vendor call of the Modal,
Daytona and Vercel backends goes through one helper: the call runs on a bounded worker pool with
our own deadline; past the deadline the caller gets a typed `BackendDeadline` at once, the stuck
thread is abandoned (Python cannot stop it), and the vendor's own cancel is fired once in the
background. Idempotent operations are retried on transient vendor failures; an exec never is. A
second guard sits inside the sandbox: every exec's command line is wrapped in
`timeout -k 2 <N> bash -c …` so the process stops even when the vendor never enforces a timeout.

Inputs → outputs (API B2 builds on):
- `Operation(name, deadline_s, idempotent)`; constants `CREATE` (300 s), `CREATE_BY_NAME` (300 s,
  idempotent), `GET` (30 s, idempotent), `SNAPSHOT` (60 s), `SNAPSHOT_READ` (60 s, idempotent),
  `STOP` (15 s, idempotent), `TERMINATE` (15 s, idempotent), `DELETE` (15 s, idempotent),
  `FILE` (30 s), `FILE_BULK` (120 s); `exec_operation(timeout)` → `Operation("exec", timeout + 10,
  idempotent=False)`. `dataclasses.replace(op, deadline_s=…)` for a backend-specific budget.
- `await sdk_call(op, fn, *, cancel=None, clock=None, sleeper=None, jitter=None, pool=SDK_POOL,
  label="") -> T`: runs `fn()` (a blocking zero-argument callable) on `SDK_POOL`
  (`BlockingPool("sdk", 8, queue_limit=64)`) with `op.deadline_s` on `clock`. On a miss: fires
  `cancel()` once on its own daemon thread (never awaited, errors logged at debug) and raises
  `BackendDeadline(operation, deadline_s)`. Transient errors on an idempotent op: up to 3 attempts,
  backoff `min(4, 0.5 * 2**(n-1)) * (0.5 + 0.5 * jitter())` awaited on `sleeper` (default
  `clock.sleep`). Any other error is raised unchanged.
- `status_of(exc) -> int | None` (walks `__cause__`/`__context__`; reads `status_code`,
  `response.status_code`, `status` as int, or a gRPC-style `status.name` mapped to HTTP:
  `UNAVAILABLE` 503, `RESOURCE_EXHAUSTED` 429, `INTERNAL` 500, `DEADLINE_EXCEEDED` 504).
- `is_transient(exc) -> bool`: any exception in the chain with a status in 408, 425, 429, 500–599,
  a `ConnectionError` (reset, aborted, refused, broken pipe), an `httpx.NetworkError` /
  `httpx.RemoteProtocolError`, or a vendor class whose name ends in `ConnectionError`.
- `guarded_argv(command, timeout, *, login=False) -> list[str]` =
  `["timeout", "-k", "2", "<ceil(timeout)>", "bash", ("-l",) "-c", command]`;
  `guarded_command(...) -> str` = the same as one shell-quoted string (for SDKs that take a string).
- `BackendDeadline` (`BackendError`, code `backend.deadline`): `.operation`, `.deadline_s`.

Files: `src/indus/backends/sdk_call.py`, `tests/unit/backends/test_sdk_call.py`.

Edge cases / failure modes:
- The pool is full of abandoned calls: `PoolFull` becomes a `BackendError` with a hint ("vendor calls
  are stuck"), never a hang.
- A deadline miss is not retried (the first call may still land).
- A cancel callback that itself hangs only holds its own daemon thread.
- Exception chains with cycles stop at the first repeat.
- `timeout <= 0` for the shell guard is raised to 1 s; fractions round up.
- The `timeout` program must exist in the sandbox image (coreutils); the backends' images have it.

Our design differences / decisions:
- One generic helper with named operations instead of per-vendor retry helpers and an async worker
  thread with its own loop: the vendor SDKs are called through their sync APIs on our pool.
- Jitter on the backoff (the reference sleeps a fixed 0.1 s × attempt).
- Shell guard `timeout -k 2` on every exec (the reference relies on the vendor's timeout).

Tests we will write:
- never-returning fake call → `BackendDeadline` at timeout + 10 s on a fake clock; cancel fired once
- 503 twice then success on an idempotent op (3 calls, two backoff sleeps with the right lengths)
- exec op never retried; 400 not retried; retries stop after 3 attempts
- status found through `__cause__` and `__context__` chains, `response.status_code`, gRPC names
- connection resets transient; a cyclic chain terminates
- the guard argv/string quotes `'`, `$()`, newlines (run through real bash where present)

## Decisions for the lead
- `GET` and `DELETE` budgets (30 s and 15 s) are not in the task row; chosen to match file ops and
  stop. Change the constants if a vendor needs more.
- Retry jitter factor 0.5–1.0 of the computed backoff.

Compare checklist (filled after the rewrite; reopened `tools/environments/vercel_sandbox.py`,
`tools/environments/modal.py`):
- [x] Transient codes 408, 425, 429, 5xx (the reference lists 500, 502, 503, 504; we take all 5xx)
  (test: `test_sdk_call.py::test_transient_status_table`)
- [x] Chain walk over cause and context with a cycle guard (test: `::test_status_in_chain`,
  `::test_cyclic_chain_terminates`)
- [x] Three attempts for idempotent calls only (test: `::test_transient_twice_then_success`,
  `::test_exec_never_retried`)
- Deliberate differences: our own deadline on every call and a fired cancel (the reference waits on
  the vendor for the async worker's timeout and only Modal/Daytona cancel); exec deadline is the
  command timeout + 10 s as the task row says (the reference's Modal worker waited timeout + 30 s);
  jittered exponential backoff instead of 0.1 s × attempt; the shell `timeout` guard.
- Not taken: the reference's class-name heuristics (`ratelimit`, `servererror`): the pinned SDKs'
  errors carry status codes (Daytona, Vercel) or gRPC statuses (Modal), which we read directly.
- Not taken: the reference's dedicated event-loop thread per Modal environment.

## Status

Done (2026-10-19).
