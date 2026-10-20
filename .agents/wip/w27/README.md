# Wave 27 — parked builder patches

## w27-e11.patch: P16-31 setup wizard + P16-32 target parsers — being integrated by the lead (verify13)

## w27-k9.patch: P17-22 messaging on the shared host — done (base ac389c3)
- Builder checks: unit/contract/integration/e2e gateway + service 2680 passed / 53 skipped (Demo 3 3x green, gateway_e2e
  crash recovery green); ruff, pyright, lint-imports, tooling.checks ok. No new packages. Full suite + perf not run.
- New gateway/core/shared_host.py (MessagingHost), test_messaging_on_shared_host.py, docs/notes/P17/messaging-shared-host.md.
- Shared edits: gateway/core/admission.py (admissions when pipeline.shared set; in-process path unchanged otherwise),
  daemon/recovery.py (reads the runtime ledger), daemon/process.py (host open, WS mount, stop order), daemon/lifecycle.py
  (drain stops each claim's own admission), daemon/side_services.py (AttachVerbs gets book + host view + ws_url),
  core/commands.py (/bg via intake helpers), test_control_part.py (asserts the mounted endpoint), traps R03.21/R06.19,
  ACC-B25-2.
- Wiring: one instance_id per daemon start (TicketBook + owner epoch); start order … RECOVERY parts → host opens → boot
  recovery → held notices released → held inbound replayed → BACKGROUND → WS mounted → ready; stop order WS endpoint →
  BACKGROUND → platforms → RECOVERY → pipeline → replies → automation → shared.aclose → host.aclose → closers → CONTROL
  (book revoked) → token locks/store.
- Fixed: D-16.339 race again on the admission path (listener marks the claim finished from the running ticket).
- Decisions: `deliver` seam (non-messaging principals post to chat only with payload deliver: true); no drain gate=; host
  opens late (falls back to in-process turns if it cannot open); DaemonOptions shared_host/attach_ws default on; WS
  origins empty; paused conversation drops further messages; /stop edge with a host-started turn queued ahead.
- NOT done (needs a landed-contract change, lead to decide): AdmissionService.discard_unknown(id, generation, *,
  terminal_write=None) so resolve_unknown can close the lost turn in its transcript.
- Follow-ups: P17-23 idle exit ignores messaging viewers; P17-18/25 `deliver` field; abandon stop task at loop end.
