# Wave 43 parked patches (P25 security). Base: 7a5463a.

## w43-sec2.patch: P25-03 desktop + web security regression suite, P25-04 dependency audit — done (Electron spec never run here)
- Builder checks: ruff, format, pyright 0, lint-imports; contract + unit web + tooling/checks/tests 2582 passed / 54 skipped;
  tooling.checks 13/13 (deps_audit warns on 18); pnpm lint (885) / fmt / typecheck (desktop, web, web e2e) / check:ipc; desktop
  vitest 1556 passed; web Playwright auth-gates.spec.ts 5 passed; Electron security-audit.spec.ts 6 skipped (no binary — first
  run in the integrator's Electron lane).
- FINDING SEC2-1 (medium, FIXED): dashboard HTML + bundle had no framing policy (clickjacking the loopback dashboard whose page
  holds the session token). src/indus/web/spa.py now sends X-Frame-Options: DENY, CSP frame-ancestors 'none', nosniff; tests
  tests/unit/web/test_spa.py::test_no_bundle_answer_may_be_framed + auth-gates.spec.ts.
- FINDING SEC2-2 (HIGH, allow-listed to 2026-11-08): certifi 2026.7.22 critical GHSA-6q48-6c7w-9gw2 (emSign roots); fix
  2026.10.19 is newer than exclude-newer / 14-day rule — OWNER/LEAD: grant a dated exception or move exclude-newer, re-lock.
- SEC2-3 (high, allow-listed to 2026-11-15): aiohttp 3.14.3 (slack extra, client only) → 3.14.4 (uploaded 2026-10-05).
- SEC2-4 (high, dev-only, allow-listed to 2026-11-15): vite 8.3.2 GHSA-587h-7wmj-h5pm → 8.3.3 at the next re-lock.
- Audit snapshot 2026-10-21: 877 packages (194 PyPI, 683 npm) all registry + hashed; 23 advisories (5 allowed, 18 warnings incl.
  pynacl 1.5.0 → 1.6.2 lockable now, pypdf, katex via mermaid, sprintf-js, vite); 20 pinned URLs 200; Docker base digest
  "not verified" (Hub 429); licences ok (79); oldest pin toml 0.10.2. Allow entries EXPIRE on purpose → tooling.checks fails from
  2026-11-09 until re-locked.
- New: tooling/checks/{deps_audit.py (offline guard on the committed snapshot; --refresh --pnpm-audit --resolve-pins --report
  network mode: OSV batch + aliases + CVSS3, pnpm audit --prod, pinned-download HEAD checks), deps_audit_allow.json,
  deps_audit_snapshot.json}, tests + fixtures, .github/workflows/deps-audit.yml (lock changes, weekly, manual),
  apps/desktop/electron/security/audit.test.ts (138 IPC channels × 9 foreign senders refused; strict schemas — 2 reviewed open
  values; CSP; navigation / window.open denied; webview hardening; permissions; external opener; media scheme; deep links),
  apps/desktop/e2e/security-audit.spec.ts, apps/web/e2e/auth-gates.spec.ts (Host guard, WS credential placement / Origin,
  framing, gated-mode cookies / CSRF / 401 / ticket-in-URL refused); notes security-ts.md, deps-audit.md.
- Shared: tooling/checks/__init__.py (registers deps_audit — SEC1 also edits NAMES), apps/desktop/e2e/vitest.config.ts, traps
  R08.8, R09.2, R09.3, R09.4, R10.30, ACC-S8-3.
- Decisions (D-ids): fail threshold high (+ unknown); stale snapshot warns offline, network job is the gate; audit in its own
  workflow; foreign-frame IPC proven at the registry level + Electron spec.
- Smaller follow-ups not done: SPA script CSP (one inline bootstrap → hash policy); cookies.read accepts the unprefixed name on
  https; IpcRegistry.seal() doesn't sender-check unregistered sync channels; gated mode lets a request with no Origin past CSRF
  (non-browser; SameSite=Lax limits it).
- Slip: two early npx vitest probes wrote two npm debug logs under /root/.npm/_logs; the builder deleted exactly those two.

## w43-sec1.patch (SEC1: P25-01 threat model + P25-02 Python security suite)

Base: see `w43-sec1.patch.base` (7a5463a). 33 files, +2109/-5. Builder gates green on its slice:
ruff, pyright, lint-imports, `tooling.checks --require-ref` (13 checks), pytest 4658 passed / 59 skipped.

- `docs/internals/threat-model.md`: 67 boundary rows, 11 findings (SEC-1..11).
- `tooling/checks/security_static.py`: 15 AST rules over src/ and scripts/, dated allow-list
  `security_allow.json` (13 reviewed entries), fixtures as `.pysrc`.
- `tests/security/`: trust boundaries, archives, threat-model doc cross-check (69 tests).
- Fixes: SEC-1 zip bomb (`extract.py` zip budget), SEC-2 webhook timestamp crash, SEC-3 `a2a_call`
  fenced, SEC-11 PNG inflate bounded (`imaging.py`).
- Open: SEC-4 (execute_code / delegate_task fence, needs design), SEC-5 (message_agent fence),
  SEC-6, SEC-8. Re-check B-LSP-1 and preview rows after w42-wb lands.
- Integrate together with w43-sec2.patch, after w42 lands (the threat model cites w42 surfaces).
