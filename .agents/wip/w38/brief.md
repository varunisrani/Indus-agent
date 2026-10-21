Wave 38 (P24 backend, second half) builds from main e52fc08. Wave 37 (P24-01..18, 23, 24) is parked in .agents/wip/w37 — read its README for the seams it added (multiplex scopes, pm ensure/tool_path, LSP, rooms). Landed: P00–P19, P21, P22; P20-01..24. In parallel (do not
touch their files): the P20-25..40 integrator (apps/desktop), the P23 integrator (scripts/install, docker/, nix/, flake.nix,
src/indus/cli/update, scripts/release, .github/workflows/{docker,release,canary}.yml, src/indus/cli/uninstall.py).
Read in full first: docs/plan/phases/P24-multiplex-extras.md (your rows, Files, Fixtures, Traps, Acceptance, Run until green,
Done when, and the "Depends on" line), AGENTS.md, HANDOFF.md, docs/plan/phases/LEAD-NOTES.md (P15, P16, P17, P18 sections your
rows rely on), docs/plan/16-what-we-change.md §2.7 rows for P24, the task notes your rows name, and the code your rows build on.

Slices (each owns only its tasks' files; stand in other slices' pieces only under tests/.../_stubs_<slice>.py with FIRST LINE
"# STAND-IN: removed at integration", and state the interface you assumed):
- W1 = P24-38 cloud browser providers, user CDP endpoint, real-profile mode; P24-39 alternative engines and browser_exec (use
  indus.pm.tool_path from w37-x1 if you need a binary — stand it in); P24-40 browser_vault model-blind autofill; P24-41 hybrid
  routing of private URLs; P24-47 browser-control broker. Vendor clients as optional extras; real-profile mode never copies a
  browser profile's cookies elsewhere; the vault never shows secrets to the model.
- S1 = P24-42 egress credential-injection proxy for Docker sandboxes; P24-43 memory caps for background processes; P24-44 plugin
  packs; P24-45 more hub sources, skill publish and snapshots; P24-46 suggested jobs and blueprints. The egress proxy injects
  credentials only for allow-listed hosts and never logs them; packs and hub sources verify digests / signatures with existing
  libraries (no hand-written crypto).
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
