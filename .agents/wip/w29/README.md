# Wave 29 parked patches (P18, P19)

## w29-a1.patch: P19-01..07 ACP entry, initialize/authenticate, backends (local + attached), session/new, load/resume/fork, list — done (base e02d3f5)
- Builder checks: unit/contract acp + schema 115 passed; config, cli/test_app, protocol, state sessions passed; contract cli
  -k "acp or table" 3 passed; ruff, pyright, lint-imports, tooling.checks ok. Full suite and perf not run.
- NEW PACKAGE: agent-client-protocol==0.12.1 (Apache-2.0 from the sdist LICENSE; schema v1.19.0, protocol 1) in the `acp`
  extra AND the dev group; uv.lock restored — integrator re-locks online (ACP tests import the SDK).
- New: src/indus/acp/{agent,auth,backend,local_host,attached_host,store_view,sessions,history,listing,paths,stdout_guard,
  entry,command}.py; tests unit/acp, contract/acp (conftest, harness, tests), tests/fakes/acp_client.py,
  tests/fakes/daemon.py (CONFLICT: L3 (w28-l3) also adds tests/fakes/daemon.py — merge both harnesses), notes
  docs/notes/P19/acp-*.md.
- Shared: pyproject (extra + dev pin), acp/__init__.py (one-line docstring), cli/app.py (lazy acp row), cli/entry.py
  (STDIO_COMMANDS += acp), schema acp.allow_local_fallback (default false) + test, state/sessions.py (set_cwd/set_cwd_in),
  protocol/attach.py (ACP_EDITOR_POLICY), contract cli non_tty_table.yaml, THIRD_PARTY_NOTICES, 00-layout §4,
  16-what-we-change D-16.29/D-16.83 pointers.
- Decisions (D-ids): SDK pin + dev group; schema v1.19.0 has no `models` field -> model picker as configOptions select
  category "model", no session/set_model (affects P19-12); command in src/indus/acp/command.py (layer contract), not
  cli/commands/acp.py; no daemon auto-start on first dial (D-16.83); attached launch options beyond cwd/model/toolsets
  refused (D-16.490); fork returns child id without replay; authenticate failure -32000; session/list reads the store
  without a daemon; local model/rewind/reset answer mutation_unavailable until P19-12.
- Daemon interface assumed (served by tests/fakes/daemon.py; today's daemon answers -32601): describe stands in for
  runtime.describe; session.mutate {session_id, operation (editor_mcp|branch|workspace), payload, request_id}; config.set
  show_reasoning; session.usage; admission.cancel. Until the daemon advertises acp.editor_policy and serves these,
  attached session/new answers acp_policy_unavailable; --local works.
- Follow-ups: trap rows R09.9/13/14/16 and ACC-M1-1/2, M2-1/2/3, M8-1/2, Q7-2; tool kinds in replay (P19-10); approval
  re-offered at load by a second editor test; local frozen launch policy not written to the session row;
  `indus tools install browser` for --setup-browser.

P18-01..10, 17..22 (W1, W2, F1) LANDED as 077ef96; their sections and patches were pruned.
