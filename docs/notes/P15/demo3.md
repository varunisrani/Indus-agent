# Task: Demo 3 integration suite (P15-36) Phase: 15 Owner: integrator (wave 22)

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none — the row reads no reference
(it uses the phase's own modules); the wave-22 builders' task notes (`telegram.md`, `discord.md`,
`slack-adapter.md`) and the P15 phase file (row 36, Acceptance, Done when) instead.
Evidence: none.

What it does (own words): proves the M5 milestone end to end in CI with fakes. One gateway daemon,
every default part (control socket, ledger, router, extras with chat prompts, media intake and the
cron host), the **real** Telegram, Discord and Slack adapters each over its wire fake, one
scripted model behind one session service. Four checks: (1) the same agent answers a DM on all
three platforms; (2) a dangerous command waits for approval and a Telegram inline button grants
it (a stranger's press is answered but changes nothing); (3) the process is SIGKILLed inside the
Telegram send of a reply (ledger row `attempting`, no acknowledgement), a new process boots on
the same home and the chat shows the reply once, labelled as a possible duplicate, with no model
call; (4) a cron job "every 30m" delivering to `telegram` is not run at 29 minutes and reaches the
Telegram home chat once on the first tick after the half hour. Plus, per platform: the chunker
(a 47,000-character reply arrives in pieces within each cap, nothing lost), the busy guards
(`/status` and `/stop` answer inline while a turn runs) and default-deny (a stranger's DM never
reaches the agent). A `network`-marked script runs the same steps with real bots for the owner.

Inputs → outputs: wire-fake inbound (DMs, button presses) and the fake clock → texts, cards and
edits on the fake platforms, ledger rows, model calls, cron attempts.

Files: `tests/e2e/gateway/{__init__,test_demo3,demo3_child,live_demo3}.py`,
`tests/integration/gateway/three_platforms.py` (the shared kit),
`tests/integration/gateway/test_three_platforms.py` (fetcher and fatal-state wiring).

Edge cases / failure modes:
- the daemon runs on a fake clock: Telegram long-polls on it (1 s idle pause), the send slot is
  1 s per chat, the cron ticker ticks every 60 s on it; `drive` advances it in small steps while
  the Discord and Slack fakes hand over their queued events;
- Telegram text is MarkdownV2-escaped: assertions strip backslashes;
- the crash child must die inside the platform call, after `attempting` (patched `send_message`),
  and the second boot runs 2 minutes later on the fake clock (rows are not "from the future");
- a stop right after a reply went out must not interrupt the finished turn (found here, fixed in
  `daemon/lifecycle.py` + `Claim.finished`, D-16.339).

Our design differences / decisions:
- in-process daemon for checks 1, 2 and 4 (a fake clock and wire fakes need one process); child
  processes only for the SIGKILL step (D-16.340);
- the cron runner is the scripted P14 agent (as in the P15-21 tests), so the chat model is not
  called for the cron job;
- the live script reads credentials only through the profile's settings and checks child output
  for them without echoing it.

Tests we will write: the four M5 checks; the parametrised chunker / busy / default-deny check on
each platform; fetchers registered on up and removed on down for all three adapters; a revoked
Telegram token parks the platform as fatal; the drain regression.

## Decisions for the lead
- The kit lives under `tests/integration/gateway/` (shared with the wiring tests); the e2e
  module imports it.
- The live script is a pytest module named outside `test_*.py`, so only an explicit path runs
  it; `INDUS_LIVE=1` and `INDUS_HOME` are required, the approval and Discord/Slack chat steps are
  checked by eye (a person presses the button).

Compare checklist: not applicable (no reference read); the M5 acceptance mapping is in
`docs/internals/acceptance-map.md` (ACC-K5-1, K6-1, K7-1, E8-1, K30-1, H3-1, H10-1, K14-1,
K23-1, K25-1).

## Status

Done (2026-10-19). All seven Demo 3 tests pass in CI with fakes; the live script is unrun here
(needs real bots).
