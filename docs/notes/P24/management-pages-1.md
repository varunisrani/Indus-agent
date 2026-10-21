# Task: Management pages, part 1 — messaging, webhooks, scheduled jobs, profiles (P24-27) Phase: 24 Owner: wave-39 builder U2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/src/app/messaging/index.tsx`
(card structure only), `apps/desktop/src/app/messaging/telegram-qr-setup.tsx` (the flow: phases,
polling, expiry, allowlist review), `apps/desktop/src/app/cron/` (file list), `apps/desktop/src/app/profiles/`
(file list). Research 08 §3.19. Our P18 notes `page-channels.md`, `page-profiles-models.md`, the
REST routes they use (`web/routes/{messaging,webhooks,cron,profiles,daemon}.py`), P16-31 setup steps
(`gateway/setup`), P14 cron methods. Closed before writing.
Evidence: none.

What it does (own words): four desktop surfaces over the daemon's existing REST routes, reached
through main's REST proxy (no credential in the renderer).

Messaging page (`/messaging`): one card per platform from `GET /api/messaging/platforms` — name,
state badge (connected, not configured, disabled, needs restart, gateway stopped), its credential
fields (masked preview only; a blank field keeps the stored value; values go to the keychain/env
through `PUT /api/messaging/platforms/{id}`), the allowlist field, an enable switch and a Test
button. A save that answers `restart_needed` shows a "Restart now" banner. The banner records the
daemon instance id that was live when the change was saved and stays until the window's
connection reports a different instance id (a reconnect to the same instance does not clear it);
"Restart now" calls `POST /api/daemon/restart`.

Telegram quick setup (our flow; the reference uses a hosted pairing service we do not have): (1)
"Create a bot" opens Telegram's BotFather link in the system browser; (2) the person pastes the bot
token → saved for the telegram platform, enabled, gateway restarted; (3) a QR code and link to
`t.me/<bot username>` (username from the platform test) is shown with "send any message to your
bot"; (4) the page polls `GET /api/pairing` every 2 s for a pending Telegram request — the first one
gives the person's numeric user id; (5) the person confirms the id (or types another, digits only),
it is saved as the allowlist and the pending request approved, and the gateway restarted again. The
wait times out after 10 minutes. The bot link is shown as a large link plus copyable text; no QR
image is drawn (the row names no QR dependency — decision below).

Webhooks page (`/webhooks`): list (`GET /api/webhooks`), create (name, events, prompt, delivery,
auth) — the signing secret is shown once from the create answer and kept only in component state —,
enable/disable (`PUT /api/webhooks/{name}/enabled`), delete for subscribed routes; config routes are
read-only.

Scheduled jobs overlay (`/cron`): list jobs, optionally aggregated across profiles (one
`GET /api/cron/jobs` per profile from `GET /api/profiles`, rows tagged with their profile); every
action (pause, resume, run now, edit, delete) goes to the owning profile (`profile` on the request);
create from blueprints (`GET /api/cron/blueprints`) or blank; run history from
`GET /api/cron/jobs/{id}` (its `runs`), delivery targets from `/api/cron/delivery-targets`.

Profiles overlay (`/profiles`): list, create, rename, delete (typed-name confirmation; rename/delete
through main's profile prepare/commit like Settings › Profiles), export archive (keys stripped by the
server; the page states it), import archive, persona file editing (`GET/PUT
/api/profiles/{name}/persona`; a missing file is explained and "Create" writes it).

Inputs → outputs: REST calls listed above → page state; restart banner `{profile, instanceId}` per
window in `sessionStorage`.

Files: `apps/desktop/src/app/messaging/{MessagingPage,TelegramQrSetup}.tsx` + test,
`apps/desktop/src/app/webhooks/WebhooksPage.tsx`, `apps/desktop/src/app/cron/CronOverlay.tsx` + test,
`apps/desktop/src/app/profiles/ProfilesOverlay.tsx` + test, `apps/desktop/src/app/manage/*` (shared
helpers: REST hook, restart banner, strings); shared: `src/app/routes.ts`, `src/app/App.tsx`,
`src/app/pages.tsx`, `src/i18n/en.ts`.

Edge cases / failure modes:
- not connected → each page shows "Indus is not connected" with Retry.
- a platform not installed → card shows the install hint, no fields.
- token field left blank on save → the stored value is kept (never sent as empty).
- pairing poll error → keep polling with a hint; after 10 min → reset with "timed out".
- user id not digits / out of 3–20 digits → refused inline.
- aggregated cron: one profile failing to list → its rows missing with a warning, others shown.
- profile export: the server strips keys; the archive path is shown, not opened.
- persona missing → explained, Create writes an empty file.

Our design differences / decisions:
- Restart banner keyed by the daemon's instance id from the desktop connection (the web page uses
  the pid, D-16.651); the desktop has the instance id.
- No hosted bot-minting service: BotFather link + paste token + first-message id detection.
- No QR library is added (no new dependency named by the row): the bot link is shown as a large
  link and copyable text; a QR is follow-up (decision below).

Tests we will write:
- restart banner clears only when the instance id changes (same id after reconnect keeps it).
- aggregated cron list sends pause to the owning profile.
- profile export archive contains no key material (fixture check of the request and the server's
  stripped listing).
- telegram setup: token saved, first pending pairing id detected, allowlist saved + approved.
- webhooks: the secret is shown once and not kept after closing the dialog.

## Decisions for the lead
- QR: no dependency (row names none). Follow-up: add a pinned QR encoder (e.g. `qrcode`) if wanted.
- Webhooks is a page at `/webhooks` (new reserved name in `routes.ts`).

Compare checklist (filled after the rewrite; reopened `messaging/index.tsx`, `telegram-qr-setup.tsx`):
- [x] per-platform card: state badge, credential fields masked, enable, test (test: `MessagingPage.test.tsx::a card shows masked credentials and saves only changed fields`)
- [x] restart banner until a new instance (test: `MessagingPage.test.tsx::the restart banner clears only when the instance id changes`)
- [x] telegram flow: wait, detect the owner id, review allowlist (numeric only), apply (test: `MessagingPage.test.tsx::telegram quick setup detects the user id from the first message`)
- [x] pairing expiry resets (test: `MessagingPage.test.tsx::telegram quick setup needs a token and times out after ten minutes`)
- Deliberate differences: no pairing service (BotFather + token paste); instance-id banner.
- Not taken: per-platform rich setup wizards beyond the field list (P16-31 steps are shown as the
  field help text from the server's description).

## Status

Done (2026-10-21). Compare (reference reopened: messaging card structure, Telegram flow): poll 2 s,
expiry reset, numeric id check and allowlist review match; the hosted pairing service is replaced by
BotFather + token paste (decision above). Extra fixture check of the export archive:
`tests/contract/web/test_profile_export_keys.py` (keys in `.env`, `.env.local`, `auth.json` and the
secret store never reach the zip). Shared files: `src/app/{routes,pages,App}`, `src/main.tsx`,
`src/i18n/en.ts` (one spread line).
