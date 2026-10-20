# Task: app factory, serve modes and SPA serving (P18-01) Phase: 18 Owner: builder W1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/web_server.py` (app
construction, lines ~200–520 token and gate block), `hermes_cli/web_server_dashboard.py`
(`mount_spa` and the index injection), `hermes_cli/main.py` (`cmd_dashboard` only).
Evidence: none.

What it does (own words): one function builds the dashboard's FastAPI app for a profile scope in one
of two modes. `dashboard` serves the built React bundle and the JSON/WS API; `serve` is the headless
backend (the desktop shell's REST side): the same API, never any HTML, even if a bundle is on disk.
The SPA catch-all hands `index.html` to any unmatched GET so client routing works, but a path under
`api/` is a missing endpoint and answers a JSON 404 naming it. Files are served only when they
resolve inside the bundle directory; traversal (also percent-encoded) is refused. Hashed assets are
cached for a year as immutable, `index.html` is `no-store` so a reload always picks up a rotated
session token. A missing bundle at request time is a JSON 404 that names the build command and does
not latch: building while the process runs makes the next request succeed. Before `</head>` one
inline script sets the runtime globals the SPA reads (session token — only in loopback mode — base
path, gated flag, embedded-chat flag, the launcher's chosen profile, this backend's own profile); every
string is JSON-encoded with `<`, `>` and `&` escaped so a profile named `</script>` cannot break out.
The active theme's first-paint CSS is inlined there too.

`indus dashboard [--port 9119] [--host 127.0.0.1] [--no-open] [--isolated]`: one backend per machine.
A live machine backend that already serves the asked address is reused (the browser opens
`?profile=<name>` for a named profile); one that cannot serve the asked host/port makes the command
exit 78 naming its pid and URL; a port another process holds exits 75 before anything starts.
`--isolated` runs a separate backend for this profile only. Missing bundle in a source checkout with
`pnpm` on PATH → build once before serving, heap capped from the cgroup memory limit, half the cores.
`indus serve` runs headless; `indus serve discover --json [-p NAME]` reads the profile's serve record
and checks the server answers; `indus serve ensure --json` discovers or starts one detached and prints
`{url, port, pid, profile_id, code_version}` — never a token (the caller minted it and handed it over
in `INDUS_DASHBOARD_SESSION_TOKEN`). A server running another `code_version` is restarted once.
The dashboard attaches to the profile daemon through P17's ensure (in the background, never fatal) and
records the bound host for the Host guard.

Inputs → outputs: scope + mode + options → FastAPI app (`app.state.indus` holds the dashboard state);
CLI flags → exit codes 0 / 1 / 2 / 75 / 78 and a JSON line for `serve ensure|discover --json`;
runtime records `<root>/dashboard.json` (machine backend) and `<home>/serve.json` (headless,
per profile), both 0600 and written atomically.

Files: `src/indus/web/{__init__,app,spa,serve_runtime,state,command}.py` (the click commands live in
`indus.web.command`, not `cli/commands/dashboard.py`: cli and web are sibling layers, D-16.246),
tests `tests/unit/web/test_spa.py`, `tests/unit/web/test_app_modes.py`,
`tests/integration/web/test_serve_ensure.py`; shared: `src/indus/cli/app.py` (two lazy rows).

Edge cases / failure modes:
- bundle dir missing, index unreadable, assets dir missing → JSON 404 each time, recovers by itself.
- `..`, `%2e%2e`, `%2f`-encoded separators, backslashes, NUL → JSON 404 (never a file, never HTML).
- `/api` and `/api/...` with any method → JSON 404 (not 405, not HTML).
- serve mode: every unmatched path is JSON 404; no index, no assets, no token page.
- port 0 for serve (the OS picks); the record carries the real port once bound.
- stale record (pid gone, or pid reused: start time differs) → treated as absent and replaced.
- record of another profile (profile id mismatch in `/api/health`) → not ours, absent.
- ensure twice → one server; a version skew → stop (SIGTERM, bounded wait) and start once.
- the owner probe and port probe are injectable so the 75/78 paths are tested without real conflicts.

Our design differences / decisions:
- Names: `__INDUS_*` globals, `X-Indus-Session-Token`, `INDUS_DASHBOARD_SESSION_TOKEN`.
- No `--insecure` flag at all (D-16.25); no token page in serve mode (the desktop holds its token, D-16.89).
- The machine record lives under the machine root, the serve record under the profile home.
- Theme bootstrap: our light/dark/system pair only (user themes are D-16.51 / later); the inlined
  CSS sets `color-scheme` and first-paint colours.
- The daemon link is a background ensure; its verdict is shown by `/api/status` later (P18-13).

Tests we will write:
- TestClient: `/api/nope` JSON 404 (GET and POST); encoded traversal refused; index `no-store`,
  asset `immutable`; profile `</script>` escaped; gated index has no token; serve never HTML; missing
  bundle then built bundle served without a restart.
- CLI with fake owner/port probes: 75 and 78; reuse prints/opens `?profile=`.
- Integration: `serve ensure` twice starts one server (real loopback process, temp INDUS_HOME/HOME);
  output has no token; stale `code_version` restarts once (fake version passed in).

## Decisions for the lead
- Command module is `src/indus/web/command.py` (loaded by name from `cli.app.REGISTRY`), because
  `indus.cli` may not import `indus.web` (import-linter layer contract).
- Only the dashboard mode attaches to the daemon (`DaemonLink`); `indus serve` does not (the desktop
  ensures the daemon itself, D-16.89).
- `GET /api/health` (public) is added in `app.py`: `{ok, mode, profile_id, profile, code_version,
  auth_required}`; `serve discover/ensure` probe it.
- `web` extra: the row asks for extras `web` (fastapi, uvicorn, ptyprocess, pywinpty, argon2-cffi,
  pyjwt[crypto], psutil), but `web` is already the ddgs extra (P05-23) and fastapi/uvicorn/pyjwt/
  cryptography are base dependencies. W1 needs no new package: argon2id comes from `cryptography`'s
  `Argon2id` (already locked). Proposal: a `dashboard` extra for W2's `ptyprocess`/`pywinpty` and
  P18-16's `psutil` when they land.
- Bundle location: `src/indus/web/static/` by default (P18-29 owns the locator); `--bundle` is not a flag.
- Build on first launch only in a source checkout (`apps/web/package.json` present) with `pnpm` on PATH;
  heap = min(4096, half the cgroup limit) MiB, `UV_THREADPOOL_SIZE` = half the cores.
- `serve ensure` starts `indus serve --port 0`; the server writes its record after binding.

Compare checklist (filled after the rewrite; reopened the three files above):
- [x] unmatched GET → index; `api/` → JSON 404 (test: `tests/unit/web/test_spa.py`)
- [x] traversal refused; hashed assets immutable; index no-store
- [x] missing bundle is a per-request JSON 404, not latched
- [x] headless never serves the SPA
- [x] bootstrap script: six globals, token withheld when gated, `</` escaped, theme CSS before it
- [x] CSS `url(/…)` and index asset URLs rewritten under a prefix (P18-05)

Compare (integrator, 2026-10-20; reference reopened read-only, then closed): `hermes_cli/web_server.py` (lines ~200–520 and the CORS/app block), `hermes_cli/web_server_dashboard.py` (`mount_spa`, `_serve_index`, the headless catch-all, the CSS rewrite).
- [x] headless serve: the reference answers a token-only page at `/` when not gated; ours never answers HTML (the desktop mints the token, D-16.89) — D-16.586
- [x] index injection: same six globals, token withheld when gated, theme CSS before the bootstrap script, prefix rewrite of `/assets/`, `/fonts/`, favicon; CSS `url(/…)` rewrite under a prefix
- [x] unmatched `api/` → JSON 404 naming the path; missing dist per request, never latched
- [x] gap fixed at integration: the machine backend for a named-profile launch now runs on the default profile's own scope (D-16.584, `test_the_machine_backend_runs_on_the_default_profile_s_own_env`)
- [x] chat routes mounted in the factory in both modes (D-16.583, `tests/integration/web/test_chat_routes_gate.py`)

## Status

Done (2026-10-20), builder W1. Compare: done by the P18 integrator with the reference reopened (see the integrator Compare above).
