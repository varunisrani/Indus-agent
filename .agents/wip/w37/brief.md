Wave 37 (P24 backend, first half) builds from main bd38080. Landed: P00–P19, P21, P22; P20-01..24. In parallel (do not
touch their files): the P20-25..40 integrator (apps/desktop), the P23 integrator (scripts/install, docker/, nix/, flake.nix,
src/indus/cli/update, scripts/release, .github/workflows/{docker,release,canary}.yml, src/indus/cli/uninstall.py).
Read in full first: docs/plan/phases/P24-multiplex-extras.md (your rows, Files, Fixtures, Traps, Acceptance, Run until green,
Done when, and the "Depends on" line), AGENTS.md, HANDOFF.md, docs/plan/phases/LEAD-NOTES.md (P15, P16, P17, P18 sections your
rows rely on), docs/plan/16-what-we-change.md §2.7 rows for P24, the task notes your rows name, and the code your rows build on.

Slices (each owns only its tasks' files; stand in other slices' pieces only under tests/.../_stubs_<slice>.py with FIRST LINE
"# STAND-IN: removed at integration", and state the interface you assumed):
- M1 = P24-01 multiplex host lock and profile serving, P24-02 scope binding and fail-closed secret reads, P24-03 profile routes
  and delivery identity, P24-04 shared ingress. Security-critical: one profile's secrets never readable from another's scope.
- R1 = P24-05 group room store, P24-06 group room driver, P24-07 bot-to-bot messaging and peer gateways.
- R2 = P24-08 relay connector transport, P24-09 relay adapter and ops, P24-10 A2A adapter (both directions). Our own relay
  and A2A protocol code over httpx / websockets libraries already in the lock; no hand-written crypto (signing/HMAC via
  cryptography / stdlib hmac).
- B1 = P24-11 task board store, P24-12 board dispatcher and worker lanes, P24-13 board tools, review flow and `indus
  kanban`, P24-14 projects.
- L1 = P24-15 LSP client and manager, P24-16 edit diagnostics and the LSP stop gate, P24-17 verify and verify-on-stop.
  Language servers in tests are fakes speaking LSP over stdio (no real servers downloaded).
- X1 = P24-18 `indus import-agent`, P24-23 `indus pm` lock, store and ensure, P24-24 `indus pm` commands. Import reads only
  files the user points at; never copies another product's credentials without an explicit flag and a prompt.
P24-19/20/25..37 (desktop, TUI, dashboard) wait for P20 to land; P24-38..47 come in the next wave.

Rules:
- ENV RULE: `export S=<your scratch dir>` as ONE statement, then `export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache
  UV_CACHE_DIR=$S/home/.cache/uv npm_config_cache=$S/home/.npm` as a SECOND statement, BEFORE your first `uv` command. Leave
  TMPDIR unset (long paths break Unix sockets). Never write under the real ~, /root or /.
- New libraries only if the row names one: optional extra, exact pins, uploaded >= 14 days before exclude-newer 2026-10-04,
  licence on licenses_allow.json (no LGPL/GPL/AGPL), imported lazily; leave uv.lock to the lead and list each package.
- No credential-shaped literals anywhere (build fake tokens at runtime); credentials only via the SecretStore / scopes; never in
  logs, argv, URLs or exception text. No real network (socket guard stays on; loopback only where a row needs it). Never pkill
  by pattern; stop every process you start.
- Config rows: only your tasks' rows in foundation/config/schema.py and its test (list them as shared).
- D-ids: do not number them; list intended differences under "Decisions for the lead" in each task note.
- Every task: note first in docs/notes/P24/<slug>.md; Read the reference paths in your row, close them, write; Compare after;
  tests per Verify; update only your trap/ACC rows. Clean room: never copy reference code, comments or docs.
- Work only in your worktree; never commit.
Before finishing: uv run ruff check <paths>, uv run ruff format --check <paths>, uv run pyright <paths>, uv run lint-imports,
uv run pytest <your test dirs> -q -p no:cacheprovider -m "not perf", uv run python -m tooling.checks --require-ref.
Usage: the lead may message you to wrap up. Then stop starting new tasks, get finished tasks passing, report done / partial.
Report: files (new / shared), task status, test/check results, wiring for the lead, decisions, packages + licences,
follow-ups.
