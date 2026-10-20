Wave 25 builds from main d50d5d3. Landed: P00–P15 (gateway core, Telegram/Discord/Slack, Demo 3), P21, P22, P16-01 (HTTP
listener kit + identity helpers: gateway/adapters/_http.py, _identity.py; how-to docs/notes/P16/http-kit.md; template
tests/unit/gateway/adapters/http_sample.py), P16-05/06/08/09 (Signal, Matrix, Mattermost, Email), P17-01..07 (runtime
ledger, admissions, turn lease, liveness watchdog, generation fences, event log/replay, request re-delivery).
Adapter seams on main (use exactly these): `AdapterConfig.send_only`; `Adapter.register_fetchers(seat) -> undo | None`;
base `report_fatal`; `Capabilities.reply_opens_thread`; contract `AdapterCase.lacks`.
Parked (being integrated in parallel, do not touch their folders): whatsapp_cloud, sms, line, ntfy, irc.
Read in full first: your phase file (rows, Files to create/update, Fixtures, Traps, Acceptance, Run until green),
AGENTS.md, HANDOFF.md, docs/plan/phases/LEAD-NOTES.md (all P15/P16/P17 landing-note sections — P17 slices: "P17 wave 1
landing notes (integrator)" lists the wiring owed to P17-08..14), docs/plan/16-what-we-change.md §2.7 rows for your
phase, and the code your rows build on.

Slices (each owns only its tasks' files):
- K3 = P17-08 daemon session host with many viewers + P17-09 frozen creation policy. Do the wave-1 wiring owed to
  P17-08 (ticket.bind, HeldLease.waited reload, hard interrupt stops subagents, one EventLog per daemon with run_flusher
  and replay_epoch, per-session RequestBook + ServerRequestBroker(book=), fences.bind_requests, book.session_id follows
  compaction child ids).
- K4 = P17-10 single-use tickets + service/ws_auth.py, P17-11 control socket attach verbs, P17-12 shared RPC
  dispatcher. K3 is built in parallel: where you need the session host, stand it in under tests/.../_stubs_k4.py
  (FIRST LINE "# STAND-IN: removed at integration") and state the interface you assumed. Tickets: unguessable
  (secrets module), single use, short TTL, bound to purpose/profile; constant-time comparison; never logged.
- E6 = P16-11 Microsoft Teams adapter + P16-12 Microsoft Graph change-notification receiver.
- E7 = P16-13 Google Chat adapter + P16-20 QQ Bot adapter.
- E8 = P16-16 DingTalk adapter + P16-17 WeCom smart-robot adapter + P16-18 WeCom callback adapter (crypto vectors in
  tests/fixtures/gateway/crypto_vectors/, derived by you from the vendor specs with an independent tool, not our code).
Callback-receiving adapters build on the P16-01 kit (signature/JWT checks in constant time on the raw body before
parsing; loopback bind by default). P16 tasks never edit P15 core: put the exact registry rows and BUILTIN_CREDENTIALS
rows you need in your report. Vendor JWT/OAuth validation (Teams Bot Framework, Google Chat bearer tokens): verify
issuer, audience, expiry and signature against keys fetched through an injectable fetcher (a fake in tests, no
network); never accept unsigned or `alg: none` tokens.

Rules:
- P16: each adapter owns only its adapter folder, its wire fake and its tests, plus its own pyproject extra row and
  THIRD_PARTY_NOTICES rows. No P16 task edits a P15 core module: if you need a core change, STOP that part and
  report it to the lead with the exact change proposed. Each adapter implements `create_adapter(AdapterConfig)`,
  a send-only open for `indus send` (no inbound consumption), media fetchers via extras.intake.register_fetcher
  where the platform has media, joins the shared contract suite (tests/contract/gateway conftest parametrisation),
  and passes its Verify column. Check how the registry rows name your platform (src/indus/gateway/registry.py).
- Third-party libraries: optional extras with exact pins (uploaded ≥14 days before exclude-newer 2026-10-04),
  imported lazily, pyright-ignored; the contract suite must run your adapter over its wire fake WITHOUT the SDK
  installed. Licence must be on licenses_allow.json (MIT, BSD, ISC, Apache-2.0, PSF, MPL-2.0 …) — LGPL/GPL/AGPL
  are NOT allowed: if the natural library is copyleft, write our own minimal client over httpx/asyncio stdlib
  instead and record the decision. List every new package (incl. transitives you can see) with its licence.
  Leave uv.lock to the lead (`uv run --frozen` if needed).
- Credentials only from AdapterConfig / SecretStore (vendor env names per D-16.22); never in logs, exception
  text, reprs or argv. No credential-shaped literals anywhere (build fake tokens at runtime).
- No real network (socket guard stays on), never write under the real ~ or /root, no stray processes/sockets.
- Config rows: only your tasks' rows in foundation/config/schema.py and its test (list them as shared).
- D-ids: do not number them; list intended differences under "Decisions for the lead" in each task note.
- Every task: note first in docs/notes/<phase>/<slug>.md; Read the reference paths in your row, close them,
  write; Compare after; tests per Verify; update only your trap/ACC rows. Clean room: never copy reference code,
  comments or docs.
- Work only in your worktree; never commit.
Before finishing: uv run ruff check <paths>, uv run ruff format --check <paths>, uv run pyright <paths>,
uv run lint-imports, uv run pytest <your test dirs> tests/contract/gateway -q -p no:cacheprovider -m "not perf",
uv run python -m tooling.checks --require-ref.
Usage: the lead may message you to wrap up. Then stop starting new tasks, get finished tasks passing, report
done / partial.
Report: files (new / shared), task status, test/check results, wiring for the lead, decisions, licences,
follow-ups.
