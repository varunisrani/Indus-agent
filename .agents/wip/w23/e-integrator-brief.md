# Integrate parked P16 adapters: E2 (P16-06 Matrix, P16-08 Mattermost) + E3 (P16-05 Signal, P16-09 Email)

Worktree: /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/verify7 (from main 0a106d2). Do NOT
commit. The lead commits and lands. Never write under the real ~ or /root. No network.
Running in parallel (do not touch their folders): builders E4 (gateway/adapters/whatsapp_cloud?, sms) and E5 (line,
ntfy, irc) from the same main.

Patches (base 78be7b3, older than main): .agents/wip/w23/w23-e2.patch, w23-e3.patch (copies in scratchpad/patches/).
Main has since landed P15-33..36 (Telegram/Discord/Slack + Demo 3) which settled the adapter seams, P16-01 kit,
P17-01..07. Read .agents/wip/w23/README.md (E2 + E3 sections) in full, both builders' notes in docs/notes/P16/,
LEAD-NOTES "P15 wave 22 + Demo 3 landing notes" and "P16-01 landing notes", docs/plan/phases/P16-more-platforms.md
rows 05, 06, 08, 09, AGENTS.md, HANDOFF.md.

Steps:
1. `git apply --3way` E2 then E3; resolve as unions (contract conftest: one registration per case; config schema rows;
   THIRD_PARTY_NOTICES; traps/ACC rows). Contract-suite changes from E3 (`AdapterCase.lacks`, linear `_long_text`,
   EmailWire in test_vendor_names) must keep every existing case (incl. telegram/discord/slack) green.
2. Bring all four adapters onto main's seams (LEAD RULINGS):
   - Send-only: `AdapterConfig.send_only` only. E3's `inbound_enabled` attribute goes; Signal/Email read
     config.send_only. E2's sink-gated start stays (it already does no inbound work without a sink) but must also
     honour send_only. Keep a test per adapter that `indus send` does no inbound work.
   - Media: `register_fetchers(seat) -> undo | None` (base default None). E2's register_fetchers and E3's
     fetch_attachment become this shape; ExtrasPart already registers on platform up/down.
   - Fatal state: E2's `lost_handler(reason, fatal)` and E3's suggested report_failure become the base
     `report_fatal` (refused token / rejected socket upgrade / refused login). Remove adapter-private hooks.
   - Registry rows + BUILTIN_CREDENTIALS: add exactly the rows in the README (matrix, mattermost, signal, email),
     tokens/passwords masked, URLs and hosts not in the credential table. Token-lock key: Signal = SIGNAL_ACCOUNT,
     email = address + IMAP host (if the registry supports a per-platform lock key; else open seam).
   - Matrix `split_target`: register with cron targets only if P16-32's seam exists; else open seam.
3. Re-lock (`uv lock`): only the `mattermost` extra metadata should change (httpx-ws/wsproto already locked). Report.
4. Security review: credentials never in logs/reprs/argv/exception text (phone numbers masked); email: From trusted
   only via the pinned authserv-id Authentication-Results rule, no remote content, header-only screening caps;
   Matrix/Mattermost tokens only in headers; mention defusing; untrusted inbound content.
5. D-ids: D-16.380 … D-16.399 in docs/plan/16-what-we-change.md §2.7 (+ §3.1 range row) from both builders'
   "Decisions for the lead" plus your rulings. LEAD-NOTES section "P16 E2 + E3 landing notes (integrator)".
6. No `# STAND-IN` first line anywhere; no conflict markers anywhere (grep for '^<<<<<<< ' and '^>>>>>>> ').
7. Full gate, all green, no extras installed (`uv sync --locked`):
   uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run lint-imports
   INDUS_STRESS_SECONDS=10 uv run pytest -q -p no:cacheprovider -rf -m "not perf"
   uv run pytest -q -p no:cacheprovider -m perf     # alone, after
   uv run python -m tooling.checks --require-ref
   CI=true pnpm test --maxWorkers=1
   Also run the Mattermost httpx-ws test once with `--extra mattermost`. A failure that passes alone must pass alone
   3x AND be root-caused (recent intermittent failures were real bugs); never skip or mark tests.
8. Credential-literal scan of the diff, no stray processes, nothing under /root or ~.
9. Usage: the lead may message you to wrap up; then stop, reach a consistent state, report.
Stage the final tree with `git add -A`.
Report: files changed beyond the patches, seam reconciliation, registry rows added, D-ids, security findings/fixes,
gate counts, follow-ups.
