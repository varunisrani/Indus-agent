# Integrate parked P16 adapters: E4 (P16-04 WhatsApp Cloud, P16-10 SMS) + E5 (P16-23 LINE, P16-26 ntfy, P16-27 IRC)

Worktree: /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/verify8 (from main d50d5d3). Do NOT
commit. The lead commits and lands. Never write under the real ~ or /root. No network.
Running in parallel (do not touch their areas): builders K3/K4 (P17-08..12: src/indus/service/daemon_host.py,
viewers.py, creation_policy.py, ws_auth.py, service/rpc/, gateway/daemon/attach_verbs.py) and E6/E7 (gateway/adapters/
teams, graph*, google_chat, qqbot).

Patches (base 0a106d2): .agents/wip/w24/w24-e4.patch then w24-e5.patch (copies in scratchpad/patches/). Main has since
landed P16-05/06/08/09 (Signal, Matrix, Mattermost, Email) — which already brought the contract `AdapterCase.lacks`
and linear `_long_text` hunks (E4 applied E3's hunk verbatim; E5's adds tokens `media` and `caption`): merge as no-ops
plus E5's additions. Read .agents/wip/w24/README.md in full, the five builders' notes in docs/notes/P16/, LEAD-NOTES
"P16 E2 + E3 landing notes (integrator)" (the precedent for this integration) and "P16-01 landing notes",
docs/plan/phases/P16-more-platforms.md rows 04, 10, 23, 26, 27, AGENTS.md, HANDOFF.md.

Steps:
1. `git apply --3way` E4 then E5; resolve as unions (contract conftest: one import per case; config schema rows;
   traps rows R06.3/R06.4/R06.11/R06.16/R06.24; keep every existing case green).
2. Registry rows + BUILTIN_CREDENTIALS exactly as in the README (whatsapp_cloud, sms, line, ntfy, irc).
3. LEAD RULING — per-platform lock key: add an optional `lock_env: tuple[str, ...]` (name it to fit the code) on the
   registry entry, used by the daemon's token lock instead of BUILTIN_CREDENTIALS when set. Then:
   irc lock = (IRC_SERVER, IRC_NICKNAME) and ntfy lock = (NTFY_TOPIC,) WITHOUT putting them in BUILTIN_CREDENTIALS (so
   nick/server/topic are not masked as secrets); email lock = (EMAIL_ADDRESS, EMAIL_IMAP_HOST) (fixes the open seam from
   E2+E3 where the password was part of the lock key); signal keeps SIGNAL_ACCOUNT. Lock names stay hashed. Tests:
   two profiles with the same IRC server+nick collide; different nicks do not; the email password is not part of
   the key.
4. Move the duplicated `mask_number(s)` helpers (WhatsApp Cloud, SMS) into gateway/adapters/_identity.py.
5. Acceptance rows: add ACC-K9-2 and ACC-K10-4 (and any P16-23/26/27 rows the phase file lists) with test refs.
6. Security review: callback signatures (Meta X-Hub-Signature-256, Twilio X-Twilio-Signature URL signature,
   X-Line-Signature) in constant time on the raw body before parsing; missing app secret / public URL = fatal at
   connect; tokens and phone numbers masked in logs/reprs/errors; ntfy token only to https or local; IRC password
   never over plaintext unless allow_plaintext_auth; LINE served-media URLs short-lived and unguessable.
7. D-ids: D-16.400 … D-16.419 in docs/plan/16-what-we-change.md §2.7 (+ §3.1 range row) from the builders'
   "Decisions for the lead" and your rulings. LEAD-NOTES section "P16 E4 + E5 landing notes (integrator)".
8. No `# STAND-IN` first line anywhere; no conflict markers (grep '^<<<<<<< ' and '^>>>>>>> ').
9. Full gate, all green, no extras installed (`uv sync --locked`):
   uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run lint-imports
   INDUS_STRESS_SECONDS=10 uv run pytest -q -p no:cacheprovider -rf -m "not perf"
   uv run pytest -q -p no:cacheprovider -m perf     # alone, after
   uv run python -m tooling.checks --require-ref
   CI=true pnpm test --maxWorkers=1   (run `pnpm install --frozen-lockfile --offline` first if node_modules is missing)
   A failure that passes alone must pass alone 3x AND be root-caused — the last four intermittent failures were real
   bugs; never skip or mark tests.
10. Credential-literal scan of the diff (incl. Twilio AC/SK + 32 hex, Meta EAA…), no stray processes, nothing under
    /root or ~.
11. Usage: the lead may message you to wrap up; then stop, reach a consistent state, report.
Stage the final tree with `git add -A`.
Report: files changed beyond the patches, lock-key change, registry rows added, D-ids, security findings/fixes, gate
counts, follow-ups.
