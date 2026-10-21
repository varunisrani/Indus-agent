Wave 43 (P25 security, P25-01..04) builds from main 7a5463a (P24 landed 5c0cff8, P25-05..19 landed 08bcf51). Wave-42 WA
(gateway/daemon wiring) runs in parallel; WB/WC wiring is parked in .agents/wip/w42 and lands soon — read their READMEs so your
threat model covers what they switch on (multiplex scopes, relay, A2A, LSP servers, preview automation, plugin contributions).
Read in full first: docs/plan/phases/P25-hardening-parity-release.md (rows 01–04, Traps, Acceptance, Done when), AGENTS.md,
HANDOFF.md, docs/plan/phases/LEAD-NOTES.md (every "Security review" paragraph), docs/plan/01-clean-room-protocol.md, the traps
ledger (docs/internals/traps-ledger.md) and acceptance map, tooling/checks/* (existing guards).

Slices:
- SEC1 = P25-01 threat model + static guards (docs/internals/threat-model.md over every surface: CLI, daemon control socket +
  WS, web dashboard + serve, ACP, MCP serve, gateway adapters + webhooks + relay + A2A, desktop main/preload/renderer, installers,
  update, plugins/hub/packs, egress proxy, browser vault; new static guards in tooling/checks for the classes found: e.g.
  subprocess shell=True, yaml.load, pickle, eval/exec, tempfile.mktemp, requests without timeout, secrets in argv/URLs/logs,
  os.environ reads outside the allowed module, unsafe tarfile/zipfile extraction, chmod modes, XML parsers without defusing)
  + P25-02 Python security regression suite (tests/security/: one test per threat-model row that is testable here — path
  traversal, SSRF to private/metadata, symlink escapes, ticket replay, profile scope leaks, redaction, approval bypass, prompt-
  injection framing of untrusted tool output, archive bombs, token-in-URL refusal, webhook signature replay window).
- SEC2 = P25-03 desktop + web security regression suite (vitest + the Electron E2E harness patterns + Playwright where needed:
  CSP, IPC schema validation for every channel, navigation/window.open refusal, preview guest isolation, deep-link
  confirmation, media scheme confinement, dashboard auth gates/CSRF/Host guard/cookies, WS credential placement) + P25-04
  dependency audit (Python + npm: known-vulnerability scan with an OFFLINE advisory snapshot if no network tool is allowed —
  otherwise pip-audit / pnpm audit through the proxy, results recorded; licence re-check vs licenses_allow.json; pin-age and
  provenance report; a CI job that runs the audit and fails on high/critical without a dated, reasoned exception).
Findings: a real vulnerability you find is FIXED in the smallest correct way with a regression test, and listed in your report
under "Findings" with severity. If a fix is large or touches another slice's area, record it precisely instead.

Rules: as in .agents/wip/w42/brief-wiring.md "Rules" (ENV RULE two export statements, TMPDIR unset, pytest
`--basetemp=/tmp/<slice>pt` short path deleted after each run, delete build output, pnpm store pnpm-store-p20 frozen, no new
runtime packages (dev-only audit tools allowed with exact pins + licence check, list them), no credential-shaped literals (build
fake tokens at runtime), no real network except the audit through the proxy, never pkill by pattern, never git stash, never
write under the real ~, /root or /). Task notes in docs/notes/P25/<slug>.md first; clean room. Never commit.
Before finishing: ruff / format / pyright on your paths, lint-imports, pytest on your test dirs + tests/contract (short basetemp),
tooling.checks --require-ref (your new guards must pass on the current tree — fix real hits or allow-list with dated reasons);
TS checks if you touch TS.
Report: files, task status, findings (severity, fix, test), guards added + hits, audit results, decisions, follow-ups.
