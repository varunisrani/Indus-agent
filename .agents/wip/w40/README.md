# P24 integration (lead: Agent 2) — parked mid-gate at the 90% hand-back

`p24-int-wip.patch` (543 files incl. uv.lock; base 823caff = main BEFORE P23 fd44e16) is the integrated tree of ALL twelve
P24 slices (.agents/wip/w37: M1 R1 R2 B1 L1 X1; w38: S1 W1; w39: B4 U1 U2 U3). Land THIS, not the slice patches (kept in
w37/w38/w39 for reference only). Read landing-p24.md (the integrator's notes) and licences-p24.txt first.

On main (fd44e16+) `git apply --3way` conflicts with P23 in: docs/plan/16-what-we-change.md (P23 D-16.740..769 vs P24
D-16.770..829 — numeric order), src/indus/cli/app.py REGISTRY (P23 uninstall / update rows + P24 rows — union),
src/indus/foundation/config/schema.py (union), tests/contract/cli/test_help_catalogue.py (+ non_tty_table if it shows —
union), uv.lock (regenerate with `uv lock` online, then `uv sync --locked`). Then a FULL gate — the integrator's run 1 had 8
failures (all root-caused and fixed, files rerun green), run 2 was stopped at ~88% with no failure shown (NOT a pass), perf
never ran: run `pytest -m "not perf"` + perf to completion and root-cause anything red.

Verified by the integrator: ruff / format / pyright 0 / lint-imports / codegen --check; tooling.checks 11/11; pnpm lint,
fmt:check, -r typecheck, check-ipc, gen-bridge-types; root vitest 2766 passed; desktop Electron E2E 38 passed (incl. new
remote.electron.test.ts: U2's per-dial ticket against a real `indus serve` → D-16.691 flipped, D-16.685 updated); web
Playwright 9 passed. pnpm lock unchanged; new uv extras relay, browser-cloud, vault, egress, wake-sherpa (sherpa-onnx +
sherpa-onnx-core 1.13.8, Apache-2.0, pinned explicitly).

P24 status: done 05, 11–14, 18, 19, 23, 24, 26, 27, 33–38, 40, 41, 43–47 (46/47 callers owed); partial 01–04 (multiplex not
called from gateway run), 06 (room loop), 07 (message_agent), 08–09 (relay front), 10 (a2a_call registration), 15–17 (LSP in
the agent loop), 20 (TUI wake hand-off), 25 (preview server requests), 28 (learning graph read), 29 (RFC 8252 broker), 30
(no real-sshd test), 31/32 (shell consumers of the contribution registry), 39 (pm packages — owner), 42 (Compare owed); not
built 21/22 (D-16.15, post-1.0). Owed wiring recorded as D-16.799. Phase "Done when" NOT met (two-profile, room and
LSP-stop-gate demos need that wiring).
Owner items: D-16.800 wake engine (recommend sherpa-onnx default, Porcupine out); D-16.801 where `indus pm` gets ffmpeg
(today a third-party static GPL build); D-16.802 Lightpanda (AGPL) and agent-browser (npm-only) kept out of the pm catalogue.
Slip (integrator): a brief `git stash` of one file, popped at once — tree unchanged.
