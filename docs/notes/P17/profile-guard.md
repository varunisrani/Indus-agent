# Task: Profile binding on attach (P17-24) Phase: 17 Owner: wave-28 builder L2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none — the row lists our own research
only: research 05 R05.14 (profile scoping in the backend), research 09 §3.10 "Connection to the
gateway" (describe, profile id must equal this process's home, else `profile_mismatch`), research
03 R03.23 (profile cross-talk). Code this task builds on: `service/ws_auth.py`
(`stable_profile_id`), `service/gateway_control.py` (client exchange, `describe`,
`session_ticket`), `service/remote/channel.py` (`grant_for`, `_handshake`),
`cli/commands/gateway_grant.py`, `service/creation_policy.py` (`scrub_launch_env`),
`gateway/ws/server.py`, `gateway/daemon/attach_verbs.py`, `foundation/scope.py`.
Evidence: none.

What it does (own words): a client of the per-profile daemon must only ever talk to the daemon of
*its own* profile. The client knows its profile home, so it knows the profile id it expects
(`stable_profile_id(home)`, 16 hex of a hash of the resolved home). Every answer that names a
profile — the control socket's `describe` and `session-ticket`, the grant helper's ensured
endpoint, the WebSocket's first frame `gateway.ready` — is compared with that expected id, and a
difference stops the client with `profile_mismatch` before it sends anything further: no ticket is
asked for after a mismatching `describe`, and no request (not even `client.capabilities`) goes over
a WebSocket whose `gateway.ready` names another profile. A socket file in our home that is served by
a process of another user is refused before a byte is written (listener uid check). On the daemon
side every RPC runs under the daemon's one `Scope` (home, env, secrets and terminal policy bound
together); the endpoint refuses to start over a scope whose home is not the profile its ticket book
serves, or whose secret store belongs to another home. The grant helper (and `ensure`) drop
launch-option env vars before anything that could start a daemon, so a daemon started on behalf of
a `INDUS_YOLO_MODE=1 indus --tui` run never runs with yolo. Multiplexed profiles (one daemon for
several homes) stay out of scope (P24).

Inputs → outputs: `expected_profile_id(home)`; `check_profile(expected, got, where=)` →
the id or `ProfileMismatch`; `check_described(home, described)`; `check_ready(expected, ready)`;
`listener_uid_ok(sock)`; `bind_daemon_scope(scope, profile_id)` → the scope or
`ProfileMismatch` / `ScopeNotBound`; `launch_free_env(env)` (the shared scrub).

Files: `src/indus/service/remote/profile_guard.py`, `tests/unit/service/remote/test_profile_guard.py`;
shared: `src/indus/service/remote/channel.py` (`grant_for` checks `describe` against the home's id
before minting; `_handshake` through the guard), `src/indus/service/gateway_control.py` (listener
uid check before sending; `peer_uid` moved into the guard), `src/indus/gateway/control_socket.py`
(`peer_uid` re-exported from the guard), `src/indus/gateway/ws/server.py` (`WsEndpoint` binds its
scope through the guard), `src/indus/gateway/daemon/process.py` (the endpoint is built inside the
mount's `try`, so a scope that cannot be bound leaves the daemon without a WebSocket, logged, as
any other mount failure), `src/indus/cli/commands/gateway_grant.py` (mismatch checks through the
guard), `src/indus/foundation/secrets.py` (`SecretStore.index_path`, read-only).

Edge cases / failure modes:
- `describe` answer with no `profile_id`, a non-string, or another id → `profile_mismatch`
  (missing counts as a mismatch, not as "anything goes"); terminal for the grant helper and the
  channel (no retry, hint names `INDUS_HOME`).
- B's socket reached through A's home (a symlink, a pointer file, a copied socket path): `describe`
  names B's id → refused before `session-ticket`, so B mints nothing for A.
- `gateway.ready` without `profile_id` (an older or foreign server) → refused for a client that
  expects one; the socket is closed with code 4005 (`PROFILE_MISMATCH`), nothing sent.
- OS that does not report the listener's uid (not Linux/macOS) → allowed: the home is already
  checked owner-only; a reported uid other than ours → `foreign_listener`, nothing written.
- symlinked home: the id is computed from the resolved path on both sides, so `~/.indus` as a
  symlink still matches.
- daemon scope whose home hashes to another id than the ticket book's (a bug in assembly) → the
  endpoint does not start (logged by the daemon, chat keeps running, no attach).
- secrets store opened for another home → same refusal; a scope without a secret store (tests,
  env-only setups) is accepted.
- launch env: `INDUS_YOLO_MODE`, `INDUS_IGNORE_RULES`, `INDUS_SAFE_MODE`,
  `INDUS_IGNORE_USER_CONFIG`, `INDUS_ACCEPT_HOOKS`, `INDUS_MODEL`, `INDUS_PROVIDER`, `INDUS_CWD`,
  `INDUS_LAUNCH_CWD`, every `INDUS_TUI_*` dropped (one list, `creation_policy`).

Our design differences / decisions:
- One module owns the client-side rules (`service/remote/profile_guard.py`); the channel, the grant
  helper and the control client call it instead of comparing ids inline.
- The comparison is against the id the client computes from its own home, not against the id the
  daemon's `describe` reported (before: `grant_for` trusted `describe` and only checked that later
  answers repeated it).
- `PROFILE_MISMATCH` (4005) is the WebSocket close code a client uses when it walks away from a
  mismatching ready frame (P17-25 table).
- Listener uid: refused only when the OS reports a different uid (`foreign_listener`), never when
  it reports nothing.

Tests we will write:
- client for profile A pointed at B's socket path (real control server serving B, A's socket path
  linked to it): `grant_for(A)` refuses `profile_mismatch`, B saw `describe` only, no ticket minted.
- ready frame with another or no profile id → refused, nothing sent on the transport.
- grant helper: `INDUS_YOLO_MODE` (and the TUI/model vars) never reach the env a daemon is started
  with (`ensure`'s child env), nor the scope `ensure` sees.
- listener uid: a socket pair reports our uid → ok; a stubbed other uid → refused before sending.
- `bind_daemon_scope`: matching scope ok; another home → mismatch; secret store of another home →
  refused; WS endpoint refuses such a scope; every daemon RPC runs with `current_scope()` the
  daemon's scope (a dispatcher probe).

## Decisions for the lead
- Missing `profile_id` in `describe` / `gateway.ready` is a mismatch (fail closed). A daemon
  older than P17-11 cannot be attached anyway.
- Listener uid check on the control client: `foreign_listener` refusal (new client error code on
  the control wire); unknown uid allowed.
- WebSocket close code for a client-side mismatch: 4005 (= `PROFILE_MISMATCH` in the table).
- `peer_uid` moved from `gateway/control_socket.py` into the guard (`socket_peer_uid`); the
  server keeps its `peer_uid` name as a one-line delegate (the gateway may import the service, not
  the other way round).
- `SecretStore` gained a read-only `index_path` so the daemon can tell which home its secrets
  belong to; a secret source without one is not checked.
- The TS client (`@indus/client`, TUI attach) already refuses a grant/ready of another profile
  (P17-19/20); no TS change here.

Compare checklist (filled after the rewrite; nothing from the reference was read — research only):
- [x] describe profile must equal this process's home, else `profile_mismatch` (research 09 §3.10)
  (test: `tests/unit/service/remote/test_profile_guard.py::test_client_for_a_refuses_b_socket_before_any_ticket`)
- [x] home + secrets + terminal policy bound together per RPC (R05.14) (tests:
  `test_profile_guard.py::test_every_daemon_rpc_runs_under_the_daemon_scope`,
  `::test_ws_endpoint_refuses_a_scope_of_another_profile`)
- [x] launch options never reach the daemon (R04.1 spirit) (test:
  `test_profile_guard.py::test_launch_env_never_reaches_a_daemon_the_grant_starts`)
- Deliberate differences: the reference's multiplexed host scopes (forwarded subject, `host`
  scope) are P24.
- Not taken: per-RPC re-reading of the secret store's home (one bind at endpoint start; the scope
  is immutable).

## Status

Done (2026-10-20). See the wave-28 L2 report.

Integration (2026-10-20): D-16.542 (binding, fail closed), D-16.543 (listener uid,
`index_path`).
