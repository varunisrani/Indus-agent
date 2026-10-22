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
