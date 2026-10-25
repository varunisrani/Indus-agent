# Task: web Channels, Webhooks, Pairing, Keys (W52-d) Phase: ui-parity Owner: builder w52d

## Status

Done (2026-10-24). All four pages rebuilt on the foundation; re-captured in both presets (Clear
Blue / Deep Teal) and set beside the reference (side-by-side PDF, builder scratch only). Shared
files edited: `apps/web/src/ui/controls.tsx` (own block + small additive props, W52-d-1),
`apps/web/src/ui/CopyButton.tsx`, `apps/web/src/pages/ModelsPage.tsx` (one `export`, W52-d-2).
No packages added. Extra captures: `channels-configure`, `webhooks-on`, `webhooks-new`,
`env-providers`, `env-custom`.

Read: reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056` — structure/behaviour only, through the
lead's Note-phase spec (`w52-specs/web-channels-webhooks-pairing-env.md`), which read
`web/src/pages/ChannelsPage.tsx`, `web/src/pages/WebhooksPage.tsx`, `web/src/pages/PairingPage.tsx`,
`web/src/pages/EnvPage.tsx`, `web/src/components/OAuthProvidersCard.tsx`. This builder opened no
reference source; it compared against the owner's screenshots only: slugs `channels`, `webhooks`,
`pairing`, `env` (light = blue preset, dark = teal preset). None of them are in the repo.

What it does (own words): the four admin pages that wire the agent to the outside world get the
reference look on the wave-51 foundation: outlined square cards, icon + tracked section headings
with counts, short list rows with the actions on the right, dialogs for editing instead of long
inline forms, a header with typewriter primary buttons (and, on Keys, jump chips after the title).
Data calls and behaviour are unchanged.

Inputs → outputs: our REST answers (`/api/messaging/platforms`, `/api/webhooks`, `/api/pairing`,
`/api/env`, `/api/model/options`) → the same mutations as before (platform update/test, daemon
restart, route create/enable/delete, receiver switch, pairing approve/revoke/clear, key set/remove,
device-code sign-in).

Edge cases / failure modes: daemon stopped (notice card, restart banner wording "Start daemon");
platform library missing (warning line, not a box); config-file webhook routes (read-only, no
delete); receiver off (warning card, "New route" disabled); the route secret only in the create
dialog's state (gone on close or navigation); empty lists (80 px empty cards); unknown key
categories (own card + chip); a key name that is not an env name (inline error).

## Decisions

- W52-d-1: shared pieces added to the foundation's own files (additive, marked W52-d), not forked
  per page: `ui/controls.tsx` gains `SectionHeading`, `EmptyCard`, `PageSpinner`, `CodeChip`,
  `Callout` (bordered notice, `tone` neutral/warning, icon, optional title and action), a
  `secondary` Badge tone, a `danger-outline` Button tone, `CardHeader.subtitle` and `Card.id`;
  `ui/CopyButton.tsx` gains `look` = `button` (old default) | `icon` | `text`.
- W52-d-2: `DeviceLogin` is exported from `pages/ModelsPage.tsx` (one word) and imported by Keys,
  instead of moving it to `pages/models/DeviceLogin.tsx`: slice b rewrites ModelsPage in parallel
  and keeps DeviceLogin there, so a move would collide; the integrator may move it after merging.
- W52-d-3: Webhooks "New route" is disabled while the receiver is off (lead decision, parity).
  After "Enable webhooks" a warning card offers "Restart daemon" (ActionLog tails it; the card
  goes when the run finishes) — our receiver answer has no restart outcome.
- W52-d-4: Channels: every platform gets Configure (fields in a dialog); no Telegram/WhatsApp QR
  panels (we have no QR onboarding API). Saving closes the dialog; Clear keeps it open. The toggle
  shows a spinner in place of the switch while saving. Test results show as a toast and as a
  12 px line under the description. The "not configured" reason line is hidden (the badge says
  it); other reasons show.
- W52-d-5: Keys: chips only for cards we have (OAuth, Providers, any extra category, Custom keys).
  The OAuth card lists providers with a device-code flow or an oauth auth type; today our
  `/api/model/options` marks none, so it shows its empty line ("No sign-in providers."). The
  advanced toggle stays off by default (ours). Delete on a set key is renamed **Clear** (still
  confirmed with "Remove"); "Get a key" link text kept with an external-link icon. No reveal.
- W52-d-6: Pairing keeps our "Approve by code" as a third headed section (codes are never listed,
  so this is how one gets approved) — an intended difference.

## Compare checklist

Common
- [x] Page background = header = card background (flat); cards are 1px outlined, square corners, no shadow.
- [x] Content column 263→1407 at 1440 wide; 24px between blocks, 12px between list items.
- [x] Header action button: primary, 28px tall, mono bold upper case with wide tracking, icon left, right edge 22px
      from window edge.
- [x] Section headings: icon + bold tracked muted label with "(N)".
- [x] Empty lists: 80px bordered card with one centred muted line.
- [x] Loading: one centred spinner; errors as toasts.
- [x] Dark colours match (bg, fg, muted, card border); light ditto.

channels
- [x] Single-column list of short rows (73px collapsed), no inline fields.
- [x] Row: state icon, pixel-serif name + state badge, muted description; right: square switch, ghost Test,
      primary CONFIGURE.
- [x] Daemon-down notice card with wifi-off icon and inline code chip; meta line "N of M channels configured".
- [x] Restart banner = warning-bordered card with triangle icon and primary button.
- [x] Configure opens a 512px dialog with header rule, close X, fields, Cancel + Save footer.
- [x] Known difference recorded: no Telegram/WhatsApp QR panels (no API).

webhooks
- [x] Receiver off: warning-bordered card, warning icon, title + 2-line text, primary ENABLE WEBHOOKS on the
      right; header NEW ROUTE disabled (muted fill).
- [x] Receiver on: compact card with base URL chip and switch.
- [x] Heading "Routes (N)" + hint line; route cards with badges, events, URL + copy, switch + trash icon.
- [x] Create dialog and created view (URL box, warning-tinted secret box, I have copied it).

pairing
- [x] Header CLEAR PENDING primary with trash icon.
- [x] Two headed sections with 80px empty cards at y≈108 and y≈244 (when both empty).
- [x] Rows: outline platform badge + name; Approve primary / Revoke X icon.
- [x] Approve-by-code section restyled (our extra; recorded as a difference).

env
- [x] Title "Keys" followed by jump chips in the header.
- [x] Intro two lines + outlined Show/Hide advanced button on the right.
- [x] OAuth card: display-face upper-case title with shield icon, refresh icon, count line, rows with
      shield-off icon, name + outline badge, status line, faded command chip + Copy, LOGIN button.
- [x] Providers card with collapsible group rows (chevron, name, N set, Get key, N keys).
- [x] Custom keys card with set rows (masked value box, Replace, Clear) and dashed add box.
- [x] No reveal (eye) button anywhere.

Deliberate differences (recorded): no Telegram/WhatsApp QR panels (no API); no env-file path chip
(our API has none); our route card keeps a switch instead of an Enable/Disable text button and adds
Signature/Events/Delivery meta; the receiver-on card keeps a switch (the reference has no off
control); Pairing keeps "Approve by code" and "Clear pending" stays disabled with nothing pending;
Keys: advanced off by default, no Tools/Gateway/Settings cards (no such categories), OAuth card is
empty today (no provider exposes a device-code flow in `/api/model/options`), no docs links, no
reveal button (R04.15). Light-preset warning colour is the foundation's darker amber, so the
warning border reads brown-amber where the reference's is pale yellow (token, not page).

Follow-ups: `/api/model/options` could expose the PKCE sign-in (OpenRouter) so the OAuth card
lists it; an env-file/secret-store location field for the Keys intro; a delivery-target list for
the route dialog's "Deliver to" select; move `DeviceLogin` to `pages/models/` after slice b lands.

## Integration (wave 53)

W52-d-1..3 are D-16.1199, W52-d-4..6 are D-16.1200. Shared pieces merged (D-16.1206): `SectionHeading` is slice c's (no second copy), `PageSpinner` is this slice's, the Keys card count lines pass `below=` (CardHeader sets a string there as the serif count line with this slice's padding), `DeviceLogin` stays exported from `ModelsPage.tsx`. The light warning token is now the reference's `#ffbd38` (D-16.1207).
