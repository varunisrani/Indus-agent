Wave 22 builds the three chat adapters of P15 from main (base given in your prompt). Landed: P00–P14, P15-01..32
(fake platform harness + wire fakes P15-01, gateway config/AdapterConfig, registry, sessions, auth, formatting
P15-15, chunker, busy guards, delivery ledger/router/`indus send`, cron in the daemon, approvals/clarify over chat,
media intake, daemon parts, control socket, API server, webhooks), P21, P22. Read in full first: your phase file
docs/plan/phases/P15-gateway-core-demo3.md (your row + the file table + the extras row), AGENTS.md, HANDOFF.md,
docs/plan/phases/LEAD-NOTES.md "P15 wave 1 …" and "P15 waves 2–3 landing notes (lead)" (open seams!),
src/indus/gateway/adapters/base.py, src/indus/gateway/registry.py, src/indus/gateway/config.py (AdapterConfig),
tests/fakes/platform/core.py + your wire fake, tests/contract/gateway/{conftest,test_adapter_contract,test_vendor_names}.py,
src/indus/gateway/extras/intake.py (register_fetcher), src/indus/gateway/delivery/send_command.py.

Slices (each owns only its task's files; P15-36 Demo 3 is done at integration, not by you):
- T = P15-33 Telegram. LICENCE RULE: python-telegram-bot is LGPL-3.0, not on licenses_allow.json. Do NOT depend on
  it. Write our own minimal async Bot API client over httpx (already a core dependency) in
  src/indus/gateway/adapters/telegram/bot_api.py exposing the method names and error classes the existing
  telegram_wire fake models (getUpdates long polling, getMe, sendMessage, editMessageText, answerCallbackQuery,
  setMyCommands, sendChatAction, getFile, media sends, setMessageReaction …; RetryAfter/BadRequest/Forbidden/
  TimedOut/NetworkError). Bot token goes in the URL path per the Bot API: it must never appear in logs, exception
  text, reprs or argv — redact it in every error path and test that. Change the vendor-name drift check so the
  Telegram surface is checked against our client (and Telegram's documented method names), not a library; no
  `telegram` extra needed (record a decision). Webhook mode only with a secret token.
- D = P15-34 Discord. discord.py (MIT) in extra `discord` with an exact pin (upload ≥14 days before the
  exclude-newer date 2026-10-04 in pyproject); import lazily, pyright-ignore the import.
- S = P15-35 Slack. slack-bolt + slack-sdk (MIT) in extra `slack`, exact pins, same rules; Socket Mode
  (aiohttp only if the pinned libs require it — check licences of anything new and list them).

Every adapter must also:
1. Implement `create_adapter(config: AdapterConfig)` in src/indus/gateway/adapters/<name>/__init__.py (the registry
   already lazily points at `indus.gateway.adapters.<name>:create_adapter` — check registry.py and keep its row).
2. Provide a **send-only open** used by `indus send` (one-shot: no polling/socket listener, no update consumption —
   Telegram must not call getUpdates in send-only mode) — see the LEAD-NOTES open seam and send_command.py. Add a
   test that `indus send <platform>` over the wire fake does no inbound work.
3. Register media download fetchers with `extras.intake.register_fetcher` (voice/photo/document per platform).
4. Join the shared adapter contract suite (tests/contract/gateway conftest parametrisation) and pass it, plus your
   row's Verify column. No real network: the socket guard fixture must stay on.
5. Credentials only from AdapterConfig/SecretStore (vendor env names per D-16.22); never logged/echoed.

Shared files (keep edits minimal and list them): pyproject.toml extras (your row only), THIRD_PARTY_NOTICES.md
rows for your new packages (and transitives you can see), tests/fakes/platform/<your>_wire.py (extend the fake
and its SURFACE together), tests/contract/gateway/conftest.py + test_vendor_names.py, trap/ACC rows for your task,
config schema rows only if your row needs new keys. Leave uv.lock to the lead (use `uv run --frozen` if your
extra is not locked; tests must skip cleanly when the SDK is absent — CI installs no extras; the contract suite
must still run your adapter over the wire fake WITHOUT the SDK installed, so keep SDK imports behind the lazy
seam and let the fake stand in).
Known issue on main: tests/unit/gateway/core/test_commands.py imports PyYAML (fixed in the next landing); ignore
that one collection error if you see it.
D-ids: do not number them; list intended differences under "Decisions for the lead" in your task note.
Every task: note first in docs/notes/P15/<slug>.md; Read the reference paths in your row, close them, write;
Compare after; tests per Verify. Clean room: never copy reference code/comments/docs. No credential-shaped
literals (build fake tokens at runtime; Telegram shape [0-9]{8,10}:[A-Za-z0-9_-]{35} must never appear literally).
Never write under the real ~ or /root. Work only in your worktree; never commit.
Before finishing: uv run ruff check <paths>, uv run ruff format --check <paths>, uv run pyright <paths>,
uv run lint-imports, uv run pytest tests/unit/gateway tests/contract/gateway -q -p no:cacheprovider -m "not perf",
uv run python -m tooling.checks --require-ref.
Usage: the lead may message you to wrap up. Then stop starting new work, get finished parts passing, report
done / partial.
Report: files (new/shared), task status, test/check results, wiring for the lead, decisions, follow-ups.
