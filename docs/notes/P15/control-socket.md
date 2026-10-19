# Task: Control socket (P15-27) Phase: 15 Owner: wave-20 builder W4

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/control_socket.py` (all:
path choice, pointer file, wire, limits, verbs, client). Research 06 §3.21, traps R06.18.
Closed before writing.
Evidence: none.

What it does (own words): a running gateway answers a few local questions over a Unix socket in
its profile home, so the CLI (status, stop), the cron tick guard and later the updater can ask
"is a gateway alive for this profile, and what is it doing?" without guessing from pid files.
A well-formed `identify` answer is the liveness proof (R06.18). The socket is never TCP: the file
system permissions are the access control, so the socket is 0600 and the server also checks the
connecting peer's uid where the OS tells it (SO_PEERCRED on Linux, LOCAL_PEERCRED on macOS).
A client refuses a home directory other users can write to (they could swap the socket or the
pointer file under us).
Wire: one exchange per connection. The client writes one JSON line
`{protocol: 1, verb, id, params?}` (any other key is rejected), the server writes one JSON line
`{ok, protocol, id, result | error, verbs?}` and closes. Request ≤ 64 KiB, response ≤ 512 KiB
(else `response_too_large`), client timeout 2.0 s.
Verbs now: `identify` (pid, start time, home, profile, supervisor, code version), `status`
(runtime status + answering pid), `drain`, `reload-plugins`, `pause-for-update`; the last three
are answered by the daemon (P15-24/25) through handlers it hands in, else `unsupported`.
P17 adds `session-ticket` and `describe` with the same `register(verb, handler)` call.
Long homes: a Unix socket path is limited (~104 bytes on macOS, 108 on Linux). When
`<home>/gateway.sock` is longer than 100 bytes the server binds in a fresh private directory made
with `mkdtemp` under `$XDG_RUNTIME_DIR` (when private) or the temp dir, and writes its path into
`<home>/gateway.sock.path` (0600). Clients follow the pointer only when the pointer is a regular
file we own with no group/other bits and the socket's directory is ours and 0700.

Inputs → outputs:
- server `ControlServer(home, *, identity, handlers)`: `start()` (raises `ControlSocketError`
  when it cannot bind: startup fails, P15-24), `stop()`, `register(verb, handler)`,
  `handle_line(raw, peer_ok) -> bytes` (pure, testable), `path`.
- client (shared, `indus.service.gateway_control`, layer rule D-16.246): `query(home, verb,
  params=None, timeout=2.0) -> ControlReply{ok, result, error, verbs}` (never raises; errors are
  codes: `no_socket`, `unsafe_home`, `unreachable`, `timeout`, `bad_response`, and the server's
  own codes), `identify(home) -> dict | None`, `socket_path_for(home)`, `server_paths(home)`,
  `home_is_safe(path)`.

Files: `src/indus/gateway/control_socket.py`, `src/indus/service/gateway_control.py`,
`tests/unit/gateway/test_control_socket.py`.

Edge cases / failure modes:
- request with no newline before EOF, or over 64 KiB: answered `request_too_large` /
  `bad_request`, never read without bound; a peer that sends nothing is dropped after 2 s.
- not JSON, not an object, unknown keys, `protocol` ≠ 1, non-string verb, params not an object,
  id not str/int or longer than 128 chars → `bad_request` (structured, with the id when usable).
- handler raises → `failed` with the exception type only (no message text: no secrets leak);
  handler slow → `timeout` after its budget, the connection is answered anyway.
- handler result not JSON-serialisable or over 512 KiB → `response_too_large` (distinct, so a
  caller never reads an oversized answer as "drain refused").
- stale socket file from a crash: removed when it is a socket we own and nobody answers on it;
  a live answer means another gateway serves the home → `ControlSocketError`; a non-socket file
  or a symlink at the path, or a socket owned by someone else → `ControlSocketError`.
- stop removes only the files it created (dev/inode check) and its temp directory.
- peer of another uid → `forbidden` answer, nothing else; uid unreadable (other OSes) → rely on
  the 0600 mode.
- client: missing socket → `no_socket`; refused/reset → `unreachable`; no answer in time →
  `timeout`; reply longer than 512 KiB or not JSON → `bad_response`.

Our design differences / decisions:
- Names: `gateway.sock`, `gateway.sock.path`; fallback dir prefix `indus-gw-`.
- The client lives in `indus.service.gateway_control` (not `gateway/control_client.py` as the
  phase file lists): the CLI and the cron guard import it and must not import `indus.gateway`
  (D-16.246 layer rule).
- The fallback directory is always `mkdtemp` (unpredictable name) instead of a predictable name
  with a squat check: one path, nothing to squat.
- Over-size requests get a `request_too_large` answer instead of a silent close.
- Unknown keys are rejected for every verb (the reference rejects them only on private verbs).
- `supervisor` in identify: `systemd` (INVOCATION_ID), `launchd` (XPC_SERVICE_NAME equals our
  label prefix), `external` (`INDUS_GATEWAY_SUPERVISED=1`), else `manual`.
- Windows named pipe: not in 1.0 (D-16.55); `start()` raises on Windows.
- Handlers may be sync (run on the `io` blocking pool, D-04.1) or async; per-verb budget 2 s by
  default, the daemon passes longer ones for drain/reload.

Tests we will write:
- wire table through `handle_line`: good identify, unknown verb lists verbs, extra key,
  protocol 2, bad JSON, params not object, long id; handler raise/slow; oversize result.
- real socket in a tmp home: identify round trip, 0600 mode, 65 KiB request rejected, stale
  socket replaced, live socket refused, symlink refused, stop cleans up.
- long-path home uses the pointer file; pointer with bad mode refused by the client.
- group-writable / world-writable home refused by the client (private user group allowed).
- peer uid mismatch answered `forbidden` (peer uid reader faked).
- identify answers within 2 s while a fake turn blocks on an event (the loop is free).

## Decisions for the lead
- Client module path `src/indus/service/gateway_control.py` (layer rule) instead of
  `src/indus/gateway/control_client.py`.
- P15-21 (cron tick guard, `indus.cron` layer) cannot import `indus.service`; the probe should be
  injected by the CLI (`indus cron tick` passes `lambda: gateway_control.identify(home)`), or the
  wire module moves down to `indus.state`/`indus.foundation` if the lead prefers.

Compare checklist (filled after the rewrite; reopened `gateway/control_socket.py`):
- [x] path + pointer fallback, 100-byte margin (test: `test_control_socket.py::test_long_home_uses_a_pointer_file`)
- [x] 0600 socket, 0177 umask during bind (test: `::test_identify_round_trip_and_owner_only_socket`)
- [x] request ≤64 KiB, response ≤512 KiB → `response_too_large` (tests: `::test_request_over_64_kib_is_refused`, `::test_oversized_result_is_response_too_large`)
- [x] unknown verb lists the verbs; id echoed (test: `::test_wire_table`)
- [x] leftover socket replaced only when nobody answers; foreign file / symlink refused (tests: `::test_stale_socket_is_replaced`, `::test_a_live_socket_is_never_taken_over`, `::test_symlink_or_foreign_file_refused`)
- [x] cleanup only of files we created (dev/inode) and the temp dir (test: `::test_stop_removes_only_its_own_files`, `::test_long_home_uses_a_pointer_file`)
- [x] client never raises, every failure is a code to fall back on (test: `::test_client_without_a_server`)
- [x] peer uid check (tests: `::test_another_users_peer_is_forbidden`, `::test_the_real_peer_check_sees_us`)
- [x] gap found: the reference's `status` answer carries the answer time — added `answered_at`
  (test: `::test_status_answers_with_the_answering_pid`)
- [x] gap found while testing: closing on a refused or oversized request with unread input can
  reset the connection before the peer reads the refusal — the server now reads (bounded) first.
- Deliberate differences: startup fails when the socket cannot bind (phase row P15-24; the
  reference logs and goes on); `mkdtemp` fallback dir always; over-size requests answered;
  unknown keys rejected for every verb; failure answers carry the exception type only, never its
  message; built-in verbs answered on the loop, daemon verbs on the `io` pool or as coroutines
  with a per-verb budget.
- Not taken: Windows named pipe (D-16.55), session tickets and private verbs (P17), multiplex
  profile verbs (P24).

## Status

Done (2026-10-19). Shared changes: none outside the files above besides the wiring in
`gateway/daemon/side_services.py` (P15-29 note). The daemon (W3, P15-24) calls
`side_services.attach(daemon)` after its pid lock and maps `ControlSocketError` to a failed
startup.
