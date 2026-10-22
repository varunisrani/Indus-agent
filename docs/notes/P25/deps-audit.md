# Task: Dependency audit (P25-04) Phase: P25 Owner: SEC2 (wave 43)

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `.github/workflows/supply-chain-audit.yml`
(the scan and bounds steps, for scope only: a diff scan for obfuscated exec/subprocess payloads and a check
that PyPI requirements carry an upper bound). Closed before writing.
Evidence: none.

What it does (own words): every package version the two lock files pin (`uv.lock`, `pnpm-lock.yaml`) is
looked up in the OSV database (and the npm side also through `pnpm audit --prod`). A known vulnerability
at or above the failing severity (high by default) fails the build unless `deps_audit_allow.json` lists that
advisory for that package with a reason and an expiry date that has not passed; an expired, malformed or
reason-less entry fails too, and an entry nothing matches any more is reported so it gets removed. The
network lookup runs in a dedicated CI job (and by hand with `--refresh`); its answer is kept in
`deps_audit_snapshot.json`, so the offline guard run (`python -m tooling.checks deps_audit`) evaluates the
same findings without network and warns when the locks changed after the snapshot. The same pass checks
provenance (every locked package from its registry with a hash; anything from git, a path or a bare URL is
named) and reports pin age (upload times uv records). With `--resolve-pins` the job also checks that every
pinned download in `scripts/install/pins.json` and `docker/pins.json` still answers and the Docker base
digest still resolves in its registry.

Inputs → outputs: lock files + OSV / pnpm-audit answers + allow file → pass/fail with one line per finding
(advisory id, package@version, severity, fixed versions), a JSON snapshot, a markdown report (`--report`).

Files: `tooling/checks/deps_audit.py`, `tooling/checks/deps_audit_allow.json`,
`tooling/checks/deps_audit_snapshot.json`, `tooling/checks/tests/test_deps_audit.py`,
`tooling/checks/tests/fixtures/deps/` (lock files with a known-vulnerable version), `tooling/checks/__init__.py`
(register the check), `.github/workflows/deps-audit.yml` (the network job).

Edge cases / failure modes:
- OSV entries without a GHSA severity: CVSS v3 vectors are scored (base score → band); an entry with no
  usable severity counts as `unknown`, which fails like `high` (fail closed; allow-list it with a reason);
- an advisory with aliases (GHSA / PYSEC / CVE) is matched in the allow file by any of its ids;
- the OSV batch API pages (`next_page_token`) and caps a batch at 1000 queries: chunks of 500;
- the network is down / a 5xx: the refresh is a setup error (exit 2), never a silent pass;
- `pnpm audit` not installed / no network: reported, the OSV result still stands;
- pnpm lock has two YAML documents (the package-manager pin and the project lock): both are read.

Our design differences / decisions:
- The reference scans diffs with patterns and checks upper bounds; we pin exact versions already (P00 pins
  check, D-16.52), so the audit is a vulnerability database lookup with an expiring allow-list instead.
- No new package: OSV over `urllib` (stdlib) with timeouts; `pnpm audit` is pnpm's own.

Tests we will write: fixture lock with a known-vulnerable version fails (offline, injected OSV answer);
allow-listed with a future expiry passes; expired fails; reason-less fails; alias match; severity bands and
CVSS scoring; pnpm lock parsing (scoped names, two documents, integrity); provenance refusals; snapshot
staleness warning; pnpm-audit JSON parsing; pin resolution with an injected HEAD.

## Decisions for the lead
- Failing threshold `high` (and `unknown`) per the wave brief; `moderate`/`low` are warnings. The threshold
  lives in the allow file (`fail_at`).
- The offline guard evaluates the committed snapshot; a stale snapshot is a warning there and the CI job
  (network) is the gate that refreshes and fails. The lead may prefer a stale snapshot to fail locally.
- The network job is its own workflow (`deps-audit.yml`: lock changes, weekly schedule, manual) so a
  database update can fail the audit without blocking every unrelated PR's main CI.

Compare checklist (filled after the rewrite; reopened `supply-chain-audit.yml`):
- [x] scope: the reference's diff-pattern scan targets malicious code in PRs, not known advisories — ours
  is complementary (R10.30: heuristics paired with OSV); the static code patterns belong to SEC1's guards.
- [x] the upper-bound rule: covered more strictly by `tooling/checks/pins.py` (exact pins only).
- Deliberate differences: no PR-comment status assembly; our job fails with the finding lines in its log.
- Not taken: `.pth` / install-hook file scanning (SEC1 static guards own code patterns).

## Status

Done (2026-10-21). Audit results recorded below.

## Audit results (2026-10-21, through the agent proxy)

Command: `python -m tooling.checks.deps_audit --refresh --pnpm-audit --report` (snapshot committed),
then `--resolve-pins`. 877 locked packages (194 PyPI, 683 npm): all from their registry, all hashed.
Pin age (uv upload times): newest filelock 4.0.10 (2026-10-03), median cffi 2.1.1 (2026-08-03), oldest
toml 0.10.2 (2020-11-01); 10 uploads before 2023 (toml, ptyprocess, mergedeep, unpaddedbase64, base58,
pynacl 1.5.0, ghp-import, types-certifi, mdurl, colorama).

| Id | Package | Severity | Verdict |
|---|---|---|---|
| GHSA-6q48-6c7w-9gw2 | certifi 2026.7.22 (PyPI, runtime: httpx trust store) | critical | allow-listed to 2026-11-08 (SEC2-2): fix 2026.10.19 uploaded 2026-10-20, after exclude-newer and inside the 14-day rule |
| GHSA-2g87-pp6c-29x5, GHSA-5wr8-gcqf-gqf3, GHSA-9xw5-w5jj-pvcg | aiohttp 3.14.3 (slack extra, client only) | high | allow-listed to 2026-11-15 (SEC2-3): fix 3.14.4 uploaded 2026-10-05, one day after exclude-newer |
| 5 more aiohttp | aiohttp 3.14.3 | low/moderate | warnings (same bump) |
| GHSA-587h-7wmj-h5pm | vite 8.3.2 (npm, dev only) | high | allow-listed to 2026-11-15 (SEC2-4): fix 8.3.3 (2026-10-06) passes the npm age rule; bump at the next re-lock |
| 5 more vite | vite 8.3.2 | low/moderate | warnings (same bump) |
| GHSA-mrfv-m5wm-5w6w | pynacl 1.5.0 | moderate | warning; 1.6.2 (2026-01-01) is lockable now |
| 5 pypdf | pypdf 6.19.0 (docs extra) | low/moderate | warnings; fixes 6.20.0+ are after exclude-newer |
| GHSA-238p-pmpm-9mq7 | katex 0.16.47 (via mermaid, prod) | low | warning (`pnpm audit --prod`'s only row) |
| GHSA-hp3w-g68c-fv3c | sprintf-js 1.1.3 (electron-builder chain, dev) | moderate | warning; no fixed release |

Pinned downloads: all 20 primary URLs in `scripts/install/pins.json` and the `docker/pins.json` bundles
answer 200. The Docker Hub manifest HEAD for `debian@sha256:62bd…34ad` answered 429 here (rate limit
through the proxy): reported as "not verified", not as a failure; the CI job checks it again.

Licence re-check (`python -m tooling.checks licenses`, against `licenses_allow.json`): ok ("checked 79
dependencies", 0 issues). No package added by this task: the audit uses only the standard library and
pnpm's own `audit`.

Integration (wave 43, 2026-10-22, integrator): landed on main `bee44d9`; `tooling.checks deps_audit`
on the merged tree: ok (warn), 877 packages, 5 allow-listed advisories, 18 warnings, snapshot not
refreshed (offline, per the wave rules). The allow entries are finding SEC-17 of the threat model;
they expire 2026-11-08 (certifi) and 2026-11-15 (aiohttp, vite), after which the guard fails until
the re-lock. D-16.908.

## Wave 46 (RM8 re-lock, 2026-10-22, builder b)

- `[tool.uv] exclude-newer` 2026-10-04 → **2026-10-08T00:00:00Z** and `pins_meta.json` lock_date
  2026-10-18 → **2026-10-22** (exactly 14 days; the pins guard passes). Upload dates checked on PyPI /
  npm through the proxy: aiohttp 3.14.4 2026-10-05T17:43Z, pynacl 1.6.2 2026-01-01, vite 8.3.3
  2026-10-06T04:10Z (pnpm `minimumReleaseAge` 20160 accepted it). certifi 2026.10.19 (2026-10-20)
  is still after the cutoff and stays at 2026.7.22 (RM7, owner decision).
- `uv lock --upgrade-package aiohttp --upgrade-package pynacl`: only aiohttp 3.14.3 → 3.14.4
  (Apache-2.0 AND MIT, dist-info License-Expression) and pynacl 1.5.0 → 1.6.2 (Apache-2.0, bundles
  libsodium ISC: `licenses/LICENSE.libsodium.txt` in the wheel) moved; no other package, no new
  package (aiohttp's dependency ranges are unchanged; pynacl's cffi dependency gained a
  `platform_python_implementation != 'PyPy'` marker; cffi 2.1.1 already locked satisfies `>=2.0.0`).
- pynacl was not "lockable now" as RM8 said: discord.py 2.7.1 (the newest release) declares
  `PyNaCl<1.6,>=1.5.0` for its `voice` extra. `[tool.uv] override-dependencies = ["pynacl==1.6.2"]`
  lifts that cap to the one exact release (decision W46-b-2). Checked against it: discord.py's own
  `voice_client.has_nacl` is true, a real XChaCha20-Poly1305 AEAD round trip through
  `nacl.bindings` and `nacl.secret.Aead`, and the discord/slack adapter tests with both extras
  installed (below).
- `package.json` vite 8.3.2 → 8.3.3 and `pnpm install`: the lock diff is vite's own entry (version,
  integrity) and the five peer-suffix keys that name it (vitest, @vitest/mocker, @tailwindcss/vite);
  vite 8.3.3 has the same dependency ranges as 8.3.2, so nothing else moved. MIT.
- `python -m tooling.checks.deps_audit --refresh --pnpm-audit` (network, proxy): 877 packages, 0
  failing; 15 advisories left the snapshot (aiohttp ×7, vite ×7, pynacl ×1), none added; the four
  aiohttp/vite allow entries matched nothing and were dropped. The certifi entry stays (its reason
  now names exclude-newer 2026-10-08). Remaining warnings: pypdf ×5, katex, sprintf-js (unchanged).
- Tests: `tests/unit/gateway/adapters/{discord,slack}`, `tests/contract/gateway/test_vendor_names.py`,
  `tests/unit/cli/test_doctor_gateway.py`, `tests/unit/packaging/test_metadata.py` in a scratch venv
  with `--extra slack --extra discord-voice`: 215 passed, 10 skipped (coincurve / httpx-ws not in
  those extras). `apps/web`: `vite build` (vite 8.3.3) ok, vitest 34 files / 281 tests passed.
- Not done here (owned by the release area): `scripts/release/notices_data.json` keys the licence
  rows by version, so `notices.py --check` and four `tests/unit/release/test_notices.py` tests fail
  until its aiohttp row says 3.14.4 and its pynacl row 1.6.2 and `THIRD_PARTY_NOTICES.md` is
  regenerated (a four-line change; decision W46-b-3).

### Decisions for the lead

- W46-b-1 (D-16.994): exclude-newer moved to the latest the rule allows (lock date − 14 days), not just past
  aiohttp's upload. On a pull request the policy overlay reads `pins_meta.json` from the base branch
  (lock_date 2026-10-18), so the pins guard fails there until the new lock date is on main: land
  this on main directly or land `pins_meta.json` first.
- W46-b-2 (D-16.995): `override-dependencies = ["pynacl==1.6.2"]` over discord.py's `<1.6` cap (moderate
  advisory GHSA-mrfv-m5wm-5w6w). Revert is one line plus `uv lock`; drop it when discord.py allows
  1.6.
- W46-b-3 (D-16.996): the notices rows are the release area's file; the builder left the patch for the
  integrator rather than editing `scripts/release/` in parallel with waves 44/45.

## Wave 48 (npm advisories katex / sprintf-js, 2026-10-22, builder f)

Status: done for katex and sprintf-js (both advisories left the snapshot); pypdf not bumped (fixes too new,
dates below). Read: none (dependency maintenance; no reference file).

- Fail first: `python -m tooling.checks.deps_audit --refresh --pnpm-audit --no-write` on main's lock
  (network, agent proxy): 877 packages, warnings GHSA-238p-pmpm-9mq7 katex@0.16.47 (low; `pnpm audit
  --prod`'s only row) and GHSA-hp3w-g68c-fv3c sprintf-js@1.1.3 (moderate).
- katex: only mermaid 11.17.2 brought 0.16.47 (`katex: ^0.16.47`); the fix is 0.18.2+. No mermaid 11.x
  allows 0.18 (mermaid 12 asks `^0.18.0`, a major bump of the diagram renderer). Override
  `mermaid>katex: 0.19.0`, the version `apps/desktop` already depends on (MIT, 2026-10-01). Mermaid uses
  only `(await import("katex")).default.renderToString(tex, {throwOnError, displayMode, output})`; checked
  against 0.19.0 from mermaid's own directory: it resolves 0.19.0, `output: "mathml"` renders, a parse
  error still throws `ParseError`. A desktop renderer `vite build` (to the scratchpad, deleted) has one
  katex chunk and no 0.16.47.
- sprintf-js: no fixed release exists (advisory range "through 1.1.3"). Its only path is
  app-builder-lib 26.17.0 → `@electron/get` 3.1.0 → optional `global-agent` 3.0.0 → `roarr` 2.15.4 →
  sprintf-js. No app-builder-lib release moves off `@electron/get ^3.0.0`. Override
  `@electron/get>global-agent: 4.1.3` (BSD-3-Clause, 2026-03-11): same CommonJS `bootstrap()` that
  `@electron/get`'s `initializeProxy()` calls (checked from `@electron/get`'s directory), no roarr.
- `pnpm install` (agent proxy; "Lockfile passes supply-chain policies", `minimumReleaseAge` 20160) then
  `pnpm install --frozen-lockfile` ok. node_modules now match the lock (vite 8.3.3, the wave-46 gap closed).
  877 → 868 locked packages.
- `--refresh --pnpm-audit` after: 0 failing, `pnpm audit --prod` 0 rows; katex and sprintf-js left the
  snapshot. Remaining warnings: pypdf ×5.
- `THIRD_PARTY_NOTICES.md` regenerated (katex 0.16.47 and commander 8.3.0 no longer shipped: 262 → 260
  npm packages) and `notices.py refresh` dropped those two rows from `notices_data.json`.
- pypdf 6.19.0 (5 advisories): fixes 6.20.0 (uploaded 2026-10-09T10:49Z), 6.20.1 (2026-10-12T16:14Z),
  6.21.1 (2026-10-17) are all after `exclude-newer` 2026-10-08, so `uv.lock` is unchanged. The pins
  guard allows exclude-newer up to lock_date − 14 days: 6.20.0 becomes lockable with lock_date
  2026-10-24, 6.20.1 (all four moderates) with lock_date 2026-10-27, 6.21.1 (the low one) 2026-11-01.

### Decisions for the lead

- W48-f-1 (D-16.1087): `mermaid>katex: 0.19.0` (outside mermaid's declared `^0.16.47`) rather than mermaid 12; drop
  it when mermaid moves to a release that allows katex >= 0.18.2.
- W48-f-2 (D-16.1088): `@electron/get>global-agent: 4.1.3` over app-builder-lib's `@electron/get` 3 (sprintf-js has
  no fixed release); only used when electron-builder downloads Electron with `ELECTRON_GET_USE_PROXY`.
- W48-f-3 (D-16.1089): pypdf stays at 6.19.0 until the lock date allows 6.20.1 (2026-10-27).
