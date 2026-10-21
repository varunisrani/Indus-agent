# Parity report v1 (product side)

The parity pass runs the same scripted flows on Indus Agent and on the reference, compares the
captures step by step, and classifies every difference (intended with a D-id, bug fixed, reference
gap, or unknown for the lead). This first report covers the **product side**: the tooling, the
twelve flows run on Indus Agent, and the obligation ledger. The reference side is the owner's run
in the REA sandbox (`~/rea-lab`); no reference capture exists yet, so nothing has been compared.

Tooling: `scripts/parity/runner.py` (flows, setup table, captures), `terminal_driver.py`
(terminal and stdio transcripts), `scripted_model_server.py` (the model both apps talk to),
`telegram_fake.py` (Bot API stand-in for the gateway flow), `scenarios.py` (scenario checks),
`compare.py` (divergences and classification), `ledger.py` (obligations). Task notes:
`docs/notes/P25/parity-runner.md`, `scripted-model.md`, `parity-scenarios.md`,
`obligation-ledger.md`, `parity-compare.md`, `rea-version.md`.

## Commands

```bash
uv run python scripts/parity/runner.py --list                  # the flows and how each is set up
uv run python scripts/parity/runner.py --all --dry-run         # product side, no REA, no compare
uv run python scripts/parity/runner.py --serve dashboard-sessions --browser <chromium>
#   … run the printed REA input through REA's capture_browser_scenario, save the result, then:
uv run python scripts/parity/runner.py --import dashboard-sessions <result.json>
uv run python scripts/parity/ledger.py --check                 # ledger self-test
uv run python scripts/parity/runner.py --all --ref ~/rea-lab/parity/ref --ledger   # with captures
```

Reference side (owner, REA sandbox, fresh home per flow, clean environment): start
`scripted_model_server.py` on loopback, point the reference's OpenAI-compatible base URL at
`http://127.0.0.1:<port>/v1` with the flow's script as the model id, run the reference-side
scenario files (`~/rea-lab/parity/<flow>.<ref>.json`, same step ids) and save the captures as
`~/rea-lab/parity/ref/<flow>.ref.capture.json`.

## Versions

| Part | Version |
|---|---|
| Indus Agent | 0.0.0 at `34a3315` (wave 41 worktree, P24 not yet landed) |
| Python / Node | 3.13.16 / 22.22.0 |
| REA | rea-agents 6.7.0; browser provider v1, Chromium 141.0.7390.37 |
| Scenario contract | REA `capture_browser_scenario` as published 2026-10-21 (`rea-version.md`) |

## Flows (product side, 2026-10-21)

| Flow | Surface | Driver | Script | Result |
|---|---|---|---|---|
| first-run-setup | CLI setup | terminal | echo | passed (30 steps) |
| cli-chat-approve | classic CLI | terminal | approve_or_decline | passed (17) |
| cli-resume-search | classic CLI | terminal | echo | passed (28) |
| tui-stream-interrupt | TUI | terminal | long_stream | passed (11); the model server saw the stream cancelled |
| tui-model-picker | TUI | terminal | echo | passed (20) |
| dashboard-sessions | dashboard | REA browser | echo | passed (6) through REA, `ev_a7e0821d…273` |
| dashboard-chat-tab | dashboard | REA browser | echo | passed (10) through REA, `ev_04e715c1…044` |
| desktop-onboarding | desktop | REA browser (connect) | echo | not run: packaged app + CDP (owner lane); 6 hooks owed |
| desktop-chat-panes | desktop | REA browser (connect) | edit_file | not run: same; 3 hooks owed |
| gateway-telegram-approve | gateway | platform | approve_or_decline | passed (12): real `indus gateway run`, Bot API stand-in |
| cron-schedule-run | cron | terminal | echo | passed (13) |
| acp-session | editor protocol | stdio | echo | passed (10) |

## Counts

- Divergences: 0 compared, 0 classified, 0 unclassified (no reference capture).
- Obligations (18): verified 0, open 16 (reference capture owed), blocked 0, failed 0, not run 2
  (desktop). A plain `--all --dry-run` leaves the two dashboard flows not run (open 14, not run 4).
- Bugs found in the product by the pass: none fixed in this wave. One observation for triage:
  the TUI model picker lists no models for a custom endpoint whose `/v1/models` lists six (setup
  offers them); typing an id works.
- Corrections to `16-what-we-change.md`: none (no capture contradicts it yet).

## Limits

- **Roles, not words.** The summary finds structural differences (accessibility roles at
  `wait_for` steps, terminal cards and prompts, protocol message keys); wording and behaviour after
  the last step are not compared.
- **Terminal flows** are compared through our transcript driver's structure (regions, cards,
  prompts, exit codes), not through REA; a key a terminal app reads raw is sent as bytes.
- **One app per flow, one flow at a time.** Two-device flows stay in the e2e suites.
- **The scripted model decides from the conversation it is sent**, so both apps hold the same
  conversation only as far as they send the same tool results; the tool it calls is found by its
  parameters (a `command`, a `questions`/`question`, a `path` with `content`), not by name.
- **Desktop flows** need the packaged app with its renderer reachable over CDP, plus nine test
  hooks the desktop does not carry yet (`OWED_HOOKS`).
- **The REA runs here** asked for lighter capture blocks than the files (to keep the answers
  small); the owner's runs use the files as they are.

## Next

1. Owner: reference captures for the ten runnable flows in the REA sandbox (and the desktop pair
   with the packaged reference app), then `runner.py --all --ref … --ledger` and REA's
   `compare_web_captures` per browser flow.
2. Lead: classify every divergence (rules in `parity/ledger/rules.json`, new D-ids in 16 §2.7),
   fix bugs with a failing test first, rerun the flow; add the desktop hooks after P24.
