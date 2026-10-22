# Task: Security regression suite, Python (P25-02) Phase: 25 Owner: wave-43 builder SEC1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none — our own suite over our own
boundaries (`16-what-we-change.md` §2.7, the research traps R02.4, R02.5, R02.27, R09.2–R09.4).
Evidence: none.

What it does (own words): `tests/security/` drives each trust boundary of the threat model
(`docs/internals/threat-model.md`) at its product entry point, with fakes only (no network, no
real home, fake credentials built at run time). One test id per testable row, so a regression in
any boundary fails a test whose name is the row id. The rows it covers:

- approvals: yolo / `approvals.mode: off` cannot pass the hardline floor or the user's deny globs
  (R02.4); a timeout is reported as `timeout`, not a refusal (R02.5); unattended runs deny a
  dangerous command at once (R02.27);
- files: path traversal and symlink escapes to Indus secret stores and credential folders refused
  for reads and writes; device and `/proc` leaks refused;
- network: SSRF to private, loopback, link-local and cloud-metadata addresses (literal, IPv6-wrapped,
  and through a name that resolves there) refused; metadata refused even with private URLs allowed;
  a credential in a URL refused before any fetch;
- tickets: daemon WS tickets are single use, purpose- and profile-bound, expire; the dashboard
  download ticket is single use;
- web: the dashboard refuses a credential in the URL; the host guard refuses a foreign Host;
- webhooks: signature checked on the raw body, timestamped scheme outside the window refused
  (replay), a bad timestamp header is a 401-style refusal and never an exception;
- messaging: unknown users are refused by default;
- profile scope: a scope's secrets and env do not leak into another profile's scope or a child
  environment;
- redaction: credential shapes and the scope's own secret values are masked in tool output;
- prompt injection: untrusted webhook text and tool output reach the model framed as data;
- archives: traversal and link members refused; a zip bomb (declared sizes far over the budget) is
  refused before anything is decompressed;
- plugin hub: a `dangerous` scan verdict blocks an install;
- installers / updater: a digest mismatch is refused (pinned to the existing install/update tests by
  the threat model rows; not re-run here, they need shells and fixtures of their own).

Inputs → outputs: product functions and small ASGI apps under fakes → pass / fail per row.

Files: `tests/security/__init__.py`, `tests/security/conftest.py`,
`tests/security/test_trust_boundaries.py`, `tests/security/test_archives.py`,
`tests/security/test_threat_model_doc.py` (every row of the document names a test that exists and
every case id here appears in the document).

Edge cases / failure modes: resolvers are injected (no DNS); clocks are fake; every secret is built
by concatenation at run time.

Our design differences / decisions:
- Several boundaries already have deep tests in their own packages; the suite re-pins the security
  property at the boundary's public entry, and the threat-model row cites both.
- Desktop and web browser-side cases are P25-03 (SEC2), not here.

Tests we will write: see the list above; one parametrized table per group.

## Decisions for the lead
- The suite imports the dashboard builder fixtures from `tests/unit/web/conftest.py` and the
  multiplex helpers from `tests/unit/gateway/multiplex/world.py` (no copies).
- Rows owned by P25-03 / P25-04 (desktop, browser, dependency audit) cite the tests that exist;
  SEC2 adds theirs.
- Traps ledger: R02.4, R02.5, R02.27, R09.2, R09.3, R09.4 now also cite this suite.
- Product fixes made here (each with a failing-first test): `tools/builtin/extract.py` (SEC-1),
  `gateway/http/webhook.py` (SEC-2), `core/untrusted.py` (SEC-3),
  `tools/builtin/media/imaging.py` (SEC-11).

## Status

Done (2026-10-21). `tests/security/`: `test_trust_boundaries.py` (approvals, files, env,
redaction, untrusted fence, SSRF, tickets, dashboard gates and sockets, webhooks, messaging,
profile scopes, plugin/hub verdicts), `test_archives.py` (document zip bomb, hub zip, profile
import, PNG bomb), `test_threat_model_doc.py` (every cited path resolves, every suite test is
cited with a known row, findings well formed): 69 passed. Each of the four fixes was checked
failing-first by restoring the base file: 7 suite tests failed without them.

Integration (wave 43, 2026-10-22, integrator): landed on main `bee44d9`; the suite gained
`test_inject_bot_and_preview_answers_reach_the_model_fenced` (SEC-5, SEC-12),
`test_preview_local_files_follow_the_read_guards` (SEC-13) and
`test_mux_served_profile_attach_stays_in_its_own_home` (B-MUX-3, D-16.877); both new fixes were
checked failing-first (the old `untrusted.py` / `preview.py` fail 4 tests). `tests/security/`: 72
passed. D-ids D-16.902 – D-16.905 (builder fixes), D-16.909 – D-16.911 (landing fixes).
