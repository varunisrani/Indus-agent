# Task: Malware preflight for package-runner servers (P13-11) Phase: 13 Owner: wave-16 builder N2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/mcp_tool.py` (OSV preflight
call site: bound, fail-open, order before any argv rewrite). R07.22. Ours: `mcp/config.py`
(`stdio_launch`), `mcp/connect.py` (`open_session`), `mcp/lifecycle.py` (`Opener`),
`foundation/clock.py`, `foundation/fsio.py`. API checked 2026-10-19: OSV `POST
https://api.osv.dev/v1/query` with `{"package": {"name", "ecosystem"}, "version"?}` → `{"vulns":
[{"id", "summary", ...}]}`; ecosystems `npm`, `PyPI`; malicious-package advisories use the `MAL-`
id prefix (OpenSSF malicious-packages feed imported into OSV).

What it does (own words): before a stdio server whose command is a package runner starts, find the
package it would fetch and ask OSV whether that package (at that version) is known malware.
- Runners: `npx` / `bunx` / `pnpm dlx` → npm; `uvx` and
  `pipx run` → PyPI. The command is matched by its basename (`.cmd`/`.exe` stripped). Runner
  options that take a value are skipped (`-p/--package` names the package for npx; `--from` for
  uvx; `--spec` for pipx run); `-y`/`--yes` and other flags are skipped.
- Package spec parsing: npm `name@version` / `@scope/name@version` (a range or tag like `latest`
  is no exact version → queried without version); PyPI `name==version`, extras `name[x]==v`,
  other specifiers → no exact version.
- Query OSV over httpx with a 12 s wall bound on the injected clock (the request runs on the event
  loop without blocking it). Any advisory whose id starts with `MAL-` refuses to start:
  `McpMalwareRefused` naming the advisory id(s) — permanent (parks the server). Timeout, network
  error or a bad answer → start anyway with one log line (fail open).
- Results (hit or clean) are cached for 24 h per (ecosystem, name, version) in
  `<home>/cache/mcp/osv.json` (and in memory); failures are not cached.
- `guarded_opener(opener)` wraps `connect.open_session` (lifecycle's `Opener` shape) so the check
  runs inside the server's owner task before the spawn; `mcp.osv_preflight: false` turns it off.

Files: `src/indus/mcp/osv.py`, `tests/unit/mcp/test_osv.py`.

Edge cases: non-runner command → no check, no request; runner with no package argument → no check;
`npx` with only flags → no check; unparseable OSV JSON → fail open; cache file corrupt → ignored and
rewritten.

Our design differences / decisions:
- Unversioned packages are still checked (by name; a `MAL-` advisory for the name refuses).
- Cache on disk under the profile's cache dir (survives restarts); the reference behaviour for the
  cache was not in the read set.

Tests planned: MockTransport: malicious package refused with its id; timeout at 12 s (fake clock)
→ starts, logged; network error → starts; cache hit avoids a second request (and survives a new
checker on the same home); entries older than 24 h re-queried; non-runner command not checked;
argument parsing table (npx -y, @scope, uvx --from, pipx run --spec, pnpm dlx, bunx); the guarded
opener refuses before spawning.

## Decisions for the lead
- Config row `mcp.osv_preflight` (default true) under the P13-01 `mcp` section (shared:
  `foundation/config/schema.py`).
- Shared: `foundation/errors.py` code `mcp.malware_refused`; `foundation/config/schema.py` row
  `mcp.osv_preflight` and its defaults test.

## Compare checklist (after writing)
(reopened `tools/mcp_tool.py` preflight call site)
- [x] check runs on the real command/args before any spawn or argv rewrite (`guarded_opener`, `test_guarded_opener_refuses_before_spawning`)
- [x] 12 s wall bound, fail open with a log line (`test_timeout_fails_open_after_twelve_seconds`, `test_network_error_fails_open`)
- [x] a hit refuses with the advisory id (`test_malicious_package_is_refused_with_its_advisory_id`); non-runners untouched (`test_non_runner_command_makes_no_request`)
- [x] 24 h cache per (ecosystem, name, version), on disk (`test_answers_are_cached_for_a_day_on_disk`)
- Not taken: the reference's cached-npx binary swap after the check (spawn optimisation, not in the row).

## Status
Done (2026-10-19). `src/indus/mcp/osv.py`, `tests/unit/mcp/test_osv.py`.
