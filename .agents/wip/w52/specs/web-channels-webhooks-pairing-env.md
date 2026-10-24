# W52 spec: web Channels / Webhooks / Pairing / Keys (env)

Slice `web-channels-webhooks-pairing-env`. Surface: web (apps/web). Slugs: `channels`, `webhooks`, `pairing`, `env`
(our extra capture `env-custom` is the lower half of `env`).
Read (Note phase, 2026-10-24): reference screenshots `$SP/compare/hermes/web/{channels,webhooks,pairing,env}--{light,dark}.png`;
reference source read for structure/behaviour only: web/src/pages/{ChannelsPage,WebhooksPage,PairingPage,EnvPage}.tsx,
web/src/components/OAuthProvidersCard.tsx. Ours: `$SP/compare/indus/web/{channels,webhooks,pairing,env,env-custom}--*.png`,
apps/web/src/pages/{Channels,Webhooks,Pairing,Env}Page.tsx (+ .test.tsx), lib/restTypes.ts, lib/api.ts, ui/*, shell/PageHeader.tsx.
Clean room: everything below is our own description; no reference code/CSS/class names. Labels quoted are 1–4 words.

`$SP` = `/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad`.

---

## 0. Shared look for all four pages (use the wave-51 foundation; listed here so you can check it)

All colours sampled from the 1440x900 PNGs (light = blue preset, dark = teal preset). The foundation note
(docs/notes/ui-parity/web-foundation.md, being written by wt-w51a) owns the token names; map these to its tokens
and only add a token if it is missing.

| role | dark | light | where seen |
|---|---|---|---|
| page / header / sidebar / card background (all the same, flat) | `#041c1c` | `#e8f2fd` | everywhere |
| foreground (titles, active nav, button fill) | `#ffe6cb` | `#0053fd` | page title, names |
| muted text (descriptions, section headings, empty text) | `#ccbda7` | `#2f73fd` | card descriptions |
| tertiary text (small hints, footer captions) | `#a69e8c` | `#528bfd` | "changes saved" note |
| faint text (OAuth card description line) | `#949080` | `#6397fd` | env |
| chrome rule (header bottom, sidebar right edge, switch border) | `#36443f` | `#bad3fd` | 1px |
| card border / row dividers / panel divider | `#293935` | `#c5dafd` | 1px |
| tab-chip border (env sub-nav) | `#162a28` | `#d7e6fd` | 1px |
| secondary badge fill / border / text | `#172b29` / `#3a4640` / `#e7d1b9` | `#d5e6fd` / `#b6d0fd` / `#1963fd` | "Disabled" badge |
| success badge fill / border / text | `#0d382a` / `#1e6943` / `#43ce78` | `#d0efea` / `#a7e9ca` / `#55df89` | "recommended" |
| outline badge border / text | `#4f5850` / `#b2a895` | `#a2c2fc` / `#4a86fe` | "Device code" |
| warning (icon, accent) | `#ffbd38` | `#ffbd38` | webhook icon |
| warning card border (warning at ~50%) | `#826d2a` | `#f4d89a` | receiver-disabled card |
| primary button fill / text | `#ffe6cb` / `#041c1c` | `#0053fd` / `#e8f2fd` | RESTART, CONFIGURE |
| primary button disabled fill / text | `#293935` / `#ffe6cb` | `#c5dafd` / `#0053fd` | NEW SUBSCRIPTION (off) |
| outlined button border+text, transparent fill | `#ffe6cb` | `#0053fd` | MANUAL SETUP, Hide Advanced |
| inline `code` chip: fill = foreground, text = background | `#ffe6cb` on `#041c1c` | `#0053fd` on `#e8f2fd` | paths, commands |
| command chip at 60% opacity (OAuth rows) | `#9a9585` fill | `#5d92fd` fill | env |
| switch off: border / track / knob | `#36443f` / `#102524` / `#686c61` | `#bad3fd` / `#dcebfd` / `#8bb2fd` | channel rows |

Shape and type rules seen on these pages:
- **No rounded corners** anywhere (cards, buttons, badges, inputs, switch are square). Only exception: an inner
  sub-panel inside a card uses a 2px radius. No shadows on cards; dialogs get a large soft shadow.
- Cards: 1px card border, same background as the page (cards are outlined, not filled).
- Buttons `sm`: 28px tall, ~12px horizontal padding, 16px icon on the left with 8px gap, label in **monospace
  bold, upper case, ~11px, letter-spacing ~0.15em**. Ghost buttons: no border/fill, same mono label (the
  "Test" ghost on channels is mixed case, ~12px, same tracking). Icon buttons: 28x28 ghost.
- Badges: 24px tall, 1px border, ~6px horizontal padding, text ~12px in the **condensed** display face with
  ~0.05em tracking, mixed case as given ("Disabled", "recommended", "Device code").
- Section heading (Pairing/Webhooks lists): 16px icon + label, **sans bold ~15px, letter-spacing ~0.12em**, muted
  colour, mixed case with a count in parentheses, e.g. "Pending requests (0)".
- Card title (Keys cards): **wide display face, upper case, ~18px, letter-spacing ~0.05em**, foreground colour,
  20px muted icon to its left with 8px gap.
- Channel names and the Telegram panel title are in the **pixel-serif** face (~14px, normal case); env tab chips
  also use it (upper case ~11px, tracking ~0.08em).
- Body sans: 14px for banner/intro lines, 12px for descriptions/hints/meta.
- Page title in the header is the wide display face ~16px (foundation's PageHeader does this).
- Empty state of a list: a bordered card 80px tall (32px top/bottom padding), one centred 14px muted line.
- Loading state of each page: a single centred spinner ~96px below the header (not skeletons).
- Errors: toasts (foundation Toasts), not inline boxes, except form-validation lines (12px destructive text under
  the field).

Page frame (measured): sidebar 240px incl. 1px right rule; header 52px + 1px rule; header title at x=262;
header action button right edge at x=1418 (22px from window edge), vertically centred (y 12–40).
Content column: left edge x=263, right edge x=1407 (24px left padding from the sidebar rule, 32px on the right),
first block starts 22–24px under the header rule. Vertical rhythm between blocks: **24px**; between items of
one list: **12px**; heading → first card: 12px.

Header slots needed: the existing end slot (`HeaderActions`) and, for Keys, an **after-title slot** (a row of chips
right after the title, 12px gap). If the foundation's PageHeader has no such slot, add `HeaderAfterTitle` next to
`HeaderActions` in apps/web/src/shell/PageHeader.tsx (same portal pattern, a `div` placed right after the `h1`,
and make the `h1` not `flex-1` so the chips sit beside it).

---

## 1. `channels` — messaging platforms

### Reference layout (1440x900)
- Header: title "Channels"; end slot: primary button **RESTART GATEWAY** with a circular-arrow icon (160x28).
- Block 1 (y 75–126, 52px tall): bordered card, 16px padding, one line: muted "wifi-off" icon (16px) + 14px muted
  sentence saying the gateway is down, naming the start command in an inline code chip and pointing at the header
  button. Shown only when the gateway is not running and no restart is pending.
- Block 2 (y 150–182): 12px muted paragraph: "N of M channels configured", where credentials are written (path in an
  inline code chip, may wrap), and that enabled channels connect on the next restart.
- Block 3: vertical list (12px gap) of platform cards, full content width, 16px padding:
  - Left: 20px state icon (antenna "radio" icon muted; check-circle in success colour when connected; warning
    triangle in destructive colour when failed), 10px gap, then a column: name (pixel-serif 14px) + state badge
    (8px gap); under it a 12px muted description (one line). Optional extra lines: an error line in destructive
    colour; a callback URL line.
  - Right cluster, vertically centred, 8px gaps: switch (34x20, square), ghost **Test** button with a plug icon,
    primary **CONFIGURE** button with a sliders icon (113x28). Collapsed card height 73px.
  - Telegram card (y 207–483) has no Configure button; instead an inner panel (16px inset, 2px radius, 1px border,
    background slightly tinted) with a pixel-serif title, a 12px muted line, then two columns split by a 1px
    vertical divider: left "QUICK SETUP" (12px medium upper case) + success badge "recommended", a 12px paragraph,
    primary **CREATE WITH QR**; right "USE YOUR OWN BOT", a 12px paragraph, outlined **MANUAL SETUP** (robot icon).
  - WhatsApp card (below the fold) has a similar inner panel for QR pairing.
- States: "Disabled" (secondary badge), "Not configured" (outline), "Connected" (success), "Restart to apply",
  "Gateway stopped", "Disconnected" (warning), "Start failed"/"Error" (destructive).
- Restart-needed banner (replaces block 1 after a save): warning-bordered card, triangle icon, one sentence, primary
  **RESTART NOW**.
- Configure dialog (not captured; from source): centred, max width 512px, card background, 1px border, big
  shadow, overlay = page background at ~85%. Header (20px padding, 12px bottom, bottom rule): title in display face
  16px with wide tracking ("Configure <name>"), optional small "Setup guide" link with external-link icon; close X
  icon button top-right. Body (20px padding, 16px gaps, scrolls when tall): platform description (12px muted),
  then per field: label (+ " *" when required) with an optional info icon, optional 12px description, input
  (password type for secrets; placeholder shows the masked stored value or the key name), 12px destructive error
  line. Footer right-aligned: ghost **Cancel**, primary **SAVE & ENABLE** (shows a spinner while saving).
- Test: result arrives as a toast. Toggle switch: spinner replaces the switch while saving.

### Ours today
Two-column grid of big cards with all fields inline, Save/Test inside each card, rounded corners, clay/teal look,
"Needs setup" warn badges, a yellow box for "library is not installed". Header button "Restart daemon".

### Build (ChannelsPage.tsx)
1. Page container: `flex flex-col` gap 24px, padding per §0 (drop the page's own `p-4` if the foundation frame pads).
2. Header end: keep `Restart daemon` behaviour (`useActionStarter(() => api.daemon.restart())`), restyle as the
   foundation primary sm button with a rotate icon, label "Restart daemon" (our product word is daemon; it renders
   upper case). Spinner + "Restarting…" while `restart.pending`.
3. Banners (keep our logic, restyle):
   - `RestartBanner` (keep `role="status"`, `data-testid="restart-banner"`, its two sentences and the
     "Start daemon"/"Restart daemon" button text): warning-bordered card, 16px padding, triangle icon in warning
     colour, text 14px foreground, primary sm button on the right.
   - Daemon-not-running notice: bordered card (normal border), wifi-off icon, 14px muted sentence in our words,
     `indus gateway start` in an inline code chip. Condition unchanged (`!daemon_running && !mark`).
   - `ActionLog` for the restart stays where it is (between banners and list).
4. Meta line (new, 12px muted): "`{configured} of {total} channels configured.`" plus our own short clause that
   credentials are kept in this profile's secret store and enabled channels connect on the daemon's next start.
   We have **no env-file path** in `MessagingPlatformsResponse` → do not show a path chip.
5. Platform list: `ul` single column, 12px gap (replace `lg:grid-cols-2`). Each `li` keeps
   `data-testid="platform-<id>"`. Row = bordered card, 16px padding, row layout from the reference:
   - state icon by `platform.state` (connected → check-circle success; `fatal` → triangle destructive; else antenna
     muted), name in pixel-serif, badge from `stateBadge()` with tones remapped:
     `connected`→success, `pending_restart`/`retrying`→warning, `daemon_stopped`→warning ("Daemon stopped"),
     `fatal`→destructive ("Failed"), `not_configured`→outline ("Needs setup" — keep our label; tests read it),
     `disabled`→secondary ("Switched off"), `paused`→secondary, unknown→outline.
     If the foundation `Badge` lacks success/outline/secondary tones, extend `BadgeTone` (keep old names as aliases
     so other pages still compile).
   - description 12px muted; `!installed` → one 12px warning-colour line "Library not installed: <install_hint>"
     (replaces the yellow box); `reason` → 12px muted line.
   - test result: keep `data-testid="test-result-<id>"`, shown as a 12px line under the description (success
     colour when ok, warning otherwise) **and** a toast. (Keeping the inline line keeps the current test.)
   - right cluster: `Switch` (keep aria-label `Enable <name>`; checked = `enabled && !explicitly_disabled`; while
     the toggle save is pending show a small spinner in its place), ghost **Test** (plug icon; "Testing…" while
     pending), primary **Configure** (sliders icon) — **for every platform including Telegram** (we have no QR
     onboarding API → do not draw the Telegram/WhatsApp inner panels; W52 decision candidate).
     Hide Configure when `fields.length === 0`.
6. Configure dialog (new; use the foundation `Modal`/Dialog): title "Configure <name>"; body = platform
   description, then our fields (moved out of the card):
   - label `KEY` + " *" when required (mono 12px), a 12px muted kind hint ("secret" / "setting" / "access rule"),
     input (password for `is_secret`, `autoComplete=new-password`; text otherwise with `value` prefilled),
     hint under it for set secrets: keep the exact text `Stored (<preview>). Leave blank to keep it.` / `Not set.`
     (tests match it); set fields get an outlined sm **Clear** button beside the input with aria-label
     `Clear <KEY>` (calls `save.mutate({clear_env:[key]})` as today).
   - `problem` → 12px destructive line with `role="alert"` (keep "Nothing to save" wording).
   - footer: ghost **Cancel** (closes, discards drafts), primary **Save** (submit; same `changedValues` →
     `api.messaging.update(id,{env})`). Keep the label "Save" (behaviour unchanged: saving does not switch the
     platform on; the switch does). Close the dialog on success; toast "<name> saved."; restart mark as today.
   - Drafts: initialise from `initialDrafts(platform.fields)` each time the dialog opens.
7. Loading → centred spinner; error → `QueryState` error (restyled by foundation); empty list → empty card
   (80px) "No messaging platforms in this install."

### Data available vs missing
Available: id, name, description, installed/install_hint, enabled/explicitly_disabled, configured, state, reason,
daemon_running, fields[]. Missing (do not invent): env path, docs/setup URL, per-field prompt/description/help,
ingress/callback URL, Telegram/WhatsApp QR onboarding, list-type allowlist fields (keep plain text input).

### Tests affected
`ChannelsPage.test.tsx`: field lookups (`getByLabelText(/^TELEGRAM_BOT_TOKEN/)`, Save, Clear) now happen inside
the dialog → open it first with `row.getByRole("button",{name:"Configure"})` and query `within(dialog)`. Badge,
switch, Test, restart-banner and action-log assertions should hold unchanged.

---

## 2. `webhooks`

### Reference layout
- Header end: primary sm **NEW SUBSCRIPTION** with plus icon (168x28); disabled (muted fill, see §0) while the
  receiver is off.
- Receiver-disabled card (y 75–181, 106px): **warning-bordered** card, 24px vertical / 16px horizontal padding.
  Left: 20px webhook icon in warning colour, 12px gap, column: 14px medium foreground title "Webhook receiver
  disabled", 14px muted 2-line explanation (ours in our own words). Right, vertically centred: primary sm
  **ENABLE WEBHOOKS** with the webhook icon (160x27).
- After enabling: a plain bordered card with a rotating-arrow icon (warning colour) and a muted 14px line while the
  gateway restarts; if a restart is still needed: warning-bordered card, triangle icon, sentence, primary
  **RESTART GATEWAY**.
- Section: heading (webhook icon + "Subscriptions (N)", style §0) at y≈216; 12px muted hint line 4px under it;
  then list (12px gap). Empty: 80px card "No webhook subscriptions yet." (ours: our own short sentence).
- Subscription card (from source): 16px padding; left column (60% opacity when disabled): name 14px medium +
  outline badge (delivery) + secondary "deliver only" + warning "disabled" badges; optional 12px description; row of
  secondary badges for events ("(all)" when none); 12px mono URL (truncates) + copy icon button. Right: ghost
  **ENABLE/DISABLE** text button and a destructive ghost trash icon button.
- New-subscription dialog: same dialog frame as channels; fields: Name, Description, Events (comma list), two
  columns "Deliver to" (select) | "Deliver only" (checkbox + short label), Prompt (textarea, 80px min, mono),
  footer primary **CREATE**. After create the same dialog shows: one muted sentence, "Webhook URL" box (1px border,
  mono 12px, copy button), "Secret (shown once)" box (warning border/tint), primary **DONE**.
- Delete: confirm dialog.

### Ours today
Receiver switch card, one route card per route with badges/switch/Delete, a long inline "Add a route" form below,
a created-route box inline.

### Build (WebhooksPage.tsx)
1. Header end (new): primary sm "New route" with plus icon (our noun is route; renders upper case). Opens the
   create dialog. Disable it while `!receiver_enabled` (matches the reference look; creating while off still
   works server-side, so decision W52-x: keep disabled for parity, or keep enabled — pick one and record it).
2. Receiver block:
   - `!receiver_enabled`: warning-bordered card as above. Title "Webhook receiver off"; body in our words: it
     listens at `<base_url>` (inline code chip) once switched on and the daemon restarts. Button primary sm
     "Enable webhooks" → `api.webhooks.setReceiver(true)` (existing). On success: toast (keep our text) and show
     the restart notice card: muted rotate icon in warning colour + "Receiver on; it starts with the daemon's next
     start." + primary sm **Restart daemon** → `useActionStarter(() => api.daemon.restart())` + `ActionLog`
     (same pattern as Channels). Our API does not tell us when the restart finished; hide the notice when the
     ActionLog finishes (`onFinished`) or on navigation.
   - `receiver_enabled`: compact bordered card (16px padding): webhook icon (muted) + "Webhook receiver" 14px
     medium + 12px muted "Listening at <base_url chip>" + the existing `Switch` on the right (aria-label
     `Webhook receiver on`, tests use it) so it can be switched off again. (Reference has no off control; we keep
     ours, styled quietly.)
3. Section heading "Routes (N)" with the webhook icon, style §0; hint line (12px muted, our words): changes to
   routes apply at once while the receiver runs; a switched-off route refuses requests.
4. Route card (`li`, keep `data-testid="webhook-<name>"`, list aria-label "Webhook routes"): reference card layout:
   - left column, 60% opacity when `!enabled`: name 14px medium; badges: outline delivery (`deliver ?? "log"`),
     secondary "direct" when `deliver_only`, warning "off" when disabled, secondary "config file" when
     `source === "config"`, success "secret set" / warning "no secret" (tests read "no secret"); 12px muted
     description; event badges (secondary, "all" when empty); 12px muted line `Signature: <auth>` + keep the text
     fragments `Events: …` and `Delivery: …` (tests regex them) — render as one 12px muted meta line; URL row
     (mono 12px, truncate) + `CopyButton` (icon-only ghost, aria-label "Copy the route URL").
   - right cluster: keep the `Switch` (aria-label `Enable <name>`, disabled for config routes) instead of the
     ENABLE/DISABLE text button (tests click the switch); destructive ghost icon button with trash icon,
     aria-label **"Delete"** (tests query name "Delete"); hidden for config routes (keep "Change this route in
     config.yaml." 12px muted line there).
5. Create dialog (new; foundation Modal, aria-labelled title "New route"): move `CreateRoute` fields into it, in
   the reference order: Name (placeholder like `github-push`), Description, Events, then a 2-column row:
   Deliver to (keep our free-text target input + hint; we have no platform list for a select — the backend takes
   a target string) | Deliver only (checkbox + short label), then **Signature check** select (ours, keep
   `WEBHOOK_AUTHS` labels; full width), Prompt textarea (mono, min 80px). Field hints become 12px muted lines.
   Validation lines `role="alert"` unchanged. Footer: primary **Add route** (keep label; tests click it).
   Success: the dialog body switches to the created view: keep region aria-label "New webhook route",
   `data-testid="webhook-secret"`, both copy buttons' aria-labels, the "You will not see it again" sentence
   (tests regex it) and the **I have copied it** button (closes dialog, forgets the secret). URL box: 1px border,
   mono 12px; secret box: warning border + warning tint at ~10%.
   Keep `gcTime: 0` and keeping the secret only in component state; closing/navigating must drop it (test).
6. Delete confirm: existing `ConfirmDialog` restyled by foundation; keep texts/labels.
7. Loading → centred spinner; error → QueryState error.

### Data available vs missing
Available: receiver_enabled, base_url, routes[] (name, source, url, auth, events, prompt, deliver, deliver_only,
description, created_at, enabled, secret_set), create (returns secret once), setEnabled, setReceiver, remove,
daemon restart. Missing: restart outcome of the receiver (no "restart_started" in our setReceiver answer — the
mock returns `restart_needed: true`; use that to show the restart notice), list of delivery platforms for a select.

### Tests affected
`WebhooksPage.test.tsx`: create flow must open the dialog first (header button "New route"); `getByLabelText`
for fields then works inside the dialog; region "New webhook route" lives in the dialog. Receiver switch and
route switch/Delete tests hold if names are kept. If the header button is disabled while the receiver is off,
fixtures already default `webhookReceiver: true`.

---

## 3. `pairing`

### Reference layout
- Header end: primary sm **CLEAR PENDING** with trash icon (144x28). Asks for confirmation.
- Section 1 at y≈86 (first block 22px under header): heading people icon + "Pending requests (N)"; card list 12px
  under it. Empty card y 108–188 (80px): centred muted "No pending pairing requests" (ours: own words).
- Section 2 (24px below): heading shield-check icon + "Approved users (N)"; empty card y 244–324.
- Pending row card (16px padding): left column: outline badge with platform + 14px medium name (truncate); under it
  12px muted user id and "Nm ago"; right: primary sm **APPROVE** with check icon (spinner while busy).
- Approved row card: outline platform badge + 14px medium user id, optional 12px muted user name; right: ghost
  destructive X icon button "Revoke" → confirm dialog.

### Ours today
Plain headings, plain text empty lines, rounded list rows, plus an "Approve by code" card at the bottom.

### Build (PairingPage.tsx)
1. Header end: keep `Clear pending` button (opens our ConfirmDialog; keep disabled when no pending — tests check
   it), restyled primary sm with trash icon.
2. Two `section`s (keep aria-labels "Pending requests"/"Approved users"), heading style §0 with icons
   (users / shield-check), counts in parentheses (keep the exact "Pending requests (N)" text).
3. Empty: 80px bordered card, centred 14px muted line; keep our sentences ("Nobody is waiting to be paired.",
   "No approved users yet.") — they are ours already.
4. Pending row: card, keep `data-testid="pending-<request_id>"`; outline badge `platform`, 14px medium name
   (`user_name ?? user_id`), 12px muted line `user_id · <ageText>` (keep `ageText`; tests read "2 min ago");
   primary sm **Approve** with check icon, disabled while pending.
5. Approved row: card, keep `data-testid="approved-<platform>-<user_id>"`; outline platform badge, 14px medium
   `user_id`, 12px muted `user_name` when present; right: ghost destructive icon button with an X icon,
   aria-label **"Revoke"** (tests use the name) → existing ConfirmDialog.
6. Approve by code (ours, no reference equivalent; keep — codes are never listed so this is how a code gets
   approved): third section, 24px below, heading key icon + "Approve by code" (heading must stay inside a
   `section` so `closest("section")` in the test finds the form), one bordered card 16px padding with the inline
   form: Platform input (datalist), Pairing code input (mono), primary sm **Approve**; 12px destructive
   `role="alert"` line below on problems.
7. Loading → centred spinner; errors → toast + QueryState error.

### Data available vs missing
All needed data exists (`pending[]`, `approved[]`, approve by request_id or code, revoke, clear). Nothing missing.

### Tests affected
`PairingPage.test.tsx` should pass if test ids, button names and the "Approve by code" heading inside a `section`
are kept. The current form is a `Card` — make sure the `section` wraps heading + card.

---

## 4. `env` — Keys

### Reference layout
- Header: title "Keys" then, 12px to its right, a row of jump chips (21px tall, 1px faint border, ~8px padding,
  4–5px gaps, pixel-serif upper case ~11px muted, foreground on hover): OAUTH, PROVIDERS, TOOLS, GATEWAY, SETTINGS,
  CUSTOM KEYS. Click = smooth scroll to that card. No end-slot button.
- Intro row (y 78–112): left column: 14px muted line "where keys are stored" with the path in an inline code chip;
  12px tertiary line under it (changes saved at once, sessions pick them up). Right: outlined sm button
  **Hide Advanced** / **Show Advanced** (126x27; mixed case here, mono bold with tracking).
- Card "PROVIDER LOGINS (OAUTH)" (from y 136): header 24px padding, shield icon + display-face title, refresh icon
  button at the far right; 12px faint "N of M connected" line (pixel-serif); 1px rule under the header (y 216).
  Rows (84px each, 1px dividers, 16px side padding): left 20px shield-off icon (shield-check in success colour when
  connected), 10px gap, column: name 14px medium foreground + outline badge "Device code" / "External CLI"; 12px
  muted status line; a command in a 60%-opacity code chip followed by a small "Copy" text button. Right: external
  link icon button (docs) and, for device-code providers, primary sm **LOGIN** (62x26).
- Card "PROVIDERS" (below the fold, from source): bolt icon, "N of M providers configured"; body = one collapsible
  row per provider group: chevron, bold 14px name with letter-spacing, success badge "N set" when any; right:
  "Get key" link (12px, primary colour, external icon) + 12px tertiary "N keys". Expanded: compact rows per key.
- Category cards (Tools / Gateway / Settings) with "show more/less" toggles — we have no such categories.
- Card "Custom keys": key icon + title; "N custom keys" line; 12px tertiary hint; set rows; then a dashed-border
  add box: label, name input (mono, upper-cased, validated as an env name, 12px destructive error), primary sm
  **ADD** with plus.
- Key rows:
  - compact unset (inside provider groups): mono 12px key name, 12px tertiary description, right "Get key" link
    and outlined sm **SET** (pencil). Hover brightens the text.
  - set (full): bordered box 16px padding: mono key + success badge "set"; 12px muted description; a read-only
    value box (1px border, muted fill ~30%, mono 12px, masked value) + outlined sm **REPLACE** (pencil) + outlined
    destructive **CLEAR** (trash). (Reference also has an eye "reveal" button — we must not: R04.15.)
  - editing: input (mono 12px, autofocus) + primary sm **SAVE** + outlined **CANCEL** (X).

### Ours today
A switch "Show advanced keys", one rounded card per category with every key as label + description + always-open
password input + Save; custom card at the bottom with Replace/Delete.

### Build (EnvPage.tsx)
1. Header after-title chips (see §0 slot): "OAuth", "Providers", "Custom keys" (only categories we have:
   backend `KeyMeta.category` is `provider` or `custom`; any other category string that appears gets its own card
   and chip with a capitalised label). Buttons, `aria-label` on the `nav`: "Jump to section".
2. Intro row: 14px muted sentence in our words: keys are kept in this profile's secret store (no path chip: our
   API does not return a path); 12px tertiary: a saved key is used by new turns at once. Right: outlined sm toggle
   button "Show advanced" / "Hide advanced" with `aria-pressed`, default **off** (our current default; reference
   defaults on). Shows the hidden count in a `title`.
3. OAuth card (`id="section-oauth"`): data = `api.model.options()` (`["models","options"]` query already used by
   ModelsPage/ProfileBuilder — reuse the same key). Rows = providers with `device_login` or `auth_type === "oauth"`.
   Row: shield-check (success) when `configured`, else shield-off (muted); name = `label`; badge outline
   "Device code" when `device_login`, else "CLI sign-in"; 12px muted status "Connected" / "Not connected";
   command chip `indus auth add <name> --type oauth` (60% opacity chip) + ghost "Copy" (our `CopyButton`).
   Right: primary sm **Login** when `device_login` → expands the existing device-code flow under the row.
   Extract `DeviceLogin` from ModelsPage.tsx into a shared module (e.g. `pages/models/DeviceLogin.tsx`) and import
   it in both pages (ModelsPage tests must stay green). No docs link (we have no URL) → omit the external icon.
   Header refresh icon button → `invalidateQueries(["models","options"])`. Count line "N of M connected".
   Empty: "No sign-in providers." centred muted.
4. Providers card (`id="section-providers"`): bolt icon + "Providers" title; "N of M providers configured".
   Group `category === "provider"` keys by provider label = the text after "for " in `description`
   (`API key for X` / `Endpoint URL override for X` — both written by our own catalog), fallback the key name.
   Group row (collapsed by default; keep a group expanded if it holds a key being edited): chevron, bold name,
   success badge "N set", "Get key" link when a key has `url`, tertiary "N keys". Expanded body: key rows.
5. Custom keys card (`id="section-custom"`): category `custom` (and any unknown category without a card) — rows +
   dashed add box: name input (upper-case it, validate `^[A-Za-z_][A-Za-z0-9_]*$`, 12px destructive message) +
   **Add** → adds a local editing row; the value is saved through the normal `api.env.set` path.
6. Key row component (one component, three looks as above), keep:
   - `data-testid="key-<NAME>"` on the row, `data-testid="preview-<NAME>"` on the masked value,
   - input `type="password"` (ours; never show values), `aria-label="New value for <NAME>"`,
   - buttons: **Save** (submit), **Replace** (set keys, opens edit), **Delete** → rename to **Clear** only if you
     update the test; confirm dialog stays ("Remove" confirm label),
   - "Get a key ↗" link text currently pinned by `/Get a key/` — use "Get key" + external icon and update the regex,
     or keep "Get a key"; either is fine,
   - badge texts "set" / "not set" (tests read them).
   Unset keys show the compact look with a **Set** button; clicking it shows the input + Save + Cancel.
7. Loading → centred spinner; error → QueryState.

### Data available vs missing
Available: `/api/env` (category, description, advanced, is_password, is_set, redacted_value, url), set, remove;
`/api/model/options` providers (name, label, configured, device_login, auth_type); `/api/providers/login/*`.
Missing (do not add): env file path, reveal endpoint (forbidden by R04.15), tool/gateway/setting key categories,
"tools that use this key" lists, provider docs URLs for OAuth rows, OAuth "External CLI" metadata beyond auth_type.

### Tests affected
`EnvPage.test.tsx`: the fixtures use categories "providers"/"tools"/"advanced" (not our backend's
"provider"/"custom") — with the plan above those land in their own cards (unknown-category rule), so rows render
fully. Update: the advanced toggle is now a button (`getByRole("button",{name:/Show advanced/})`); unset rows need a
click on **Set** before the input exists; if groups collapse, only `provider` category is grouped so fixtures are
not affected. `ModelsPage.test.tsx` must still pass after extracting `DeviceLogin`. Mock API: add
`/api/model/options` to the env-page tests' expectations only if the mock does not already serve it (it does for
ModelsPage).

---

## 5. Files that change

- apps/web/src/pages/ChannelsPage.tsx (+ test) — list rows, Configure dialog, banners restyle.
- apps/web/src/pages/WebhooksPage.tsx (+ test) — receiver card states, header button, create dialog, route card.
- apps/web/src/pages/PairingPage.tsx (+ test only if a name moves) — headings with icons, card rows, empty cards.
- apps/web/src/pages/EnvPage.tsx (+ test) — chips, intro, OAuth card, provider groups, custom card, key row.
- apps/web/src/pages/models/DeviceLogin.tsx (new, extracted) and ModelsPage.tsx (import it).
- apps/web/src/shell/PageHeader.tsx — `HeaderAfterTitle` slot if the foundation did not add one.
- apps/web/src/ui/controls.tsx — only if the foundation lacks: badge tones success/outline/secondary/destructive,
  button variants primary/outlined/ghost/destructive with `sm` + icon prefix, a page `Spinner`, an `EmptyCard`.
  Prefer the foundation's names; do not fork components per page.
- Icons: lucide-react (allowed list) — radio/antenna, check-circle, alert-triangle, wifi-off, rotate-cw, plug-zap,
  sliders (settings-2), webhook, plus, trash-2, x, users, shield-check, shield-off, key-round, zap, chevron-right/
  down, external-link, copy, check, pencil, save, refresh-cw. Use whatever version the foundation pinned.

## 6. Build order
1. Confirm foundation tokens/components (§0 table) exist; add the after-title header slot if missing.
2. Pairing (smallest; establishes section heading, empty card, list-row card, icon buttons).
3. Webhooks (adds warning card, header primary button disabled state, dialog with form + created view).
4. Channels (reuses dialog; list rows with switch/test/configure; banners).
5. Keys (chips slot, OAuth card with extracted DeviceLogin, provider groups, key row states, custom add box).
6. Run vitest for apps/web, fix pinned-markup tests as listed; biome; tsc.
7. Re-capture with the kit (copy `$SP/compare/work-i-web/kit` into your `$S`) in both themes; build the PDF;
   compare against `$SP/compare/hermes/web/<slug>--{light,dark}.png`.

## 7. Compare checklist (tick per slug, both themes)

Common
- [ ] Page background = header = card background (flat); cards are 1px outlined, square corners, no shadow.
- [ ] Content column 263→1407 at 1440 wide; 24px between blocks, 12px between list items.
- [ ] Header action button: primary, 28px tall, mono bold upper case with wide tracking, icon left, right edge 22px
      from window edge.
- [ ] Section headings: icon + bold tracked muted label with "(N)".
- [ ] Empty lists: 80px bordered card with one centred muted line.
- [ ] Loading: one centred spinner; errors as toasts.
- [ ] Dark colours match §0 (bg `#041c1c`, fg `#ffe6cb`, muted `#ccbda7`, card border `#293935`); light ditto
      (`#e8f2fd`, `#0053fd`, `#2f73fd`, `#c5dafd`).

channels
- [ ] Single-column list of short rows (73px collapsed), no inline fields.
- [ ] Row: state icon, pixel-serif name + state badge, muted description; right: square switch, ghost Test,
      primary CONFIGURE.
- [ ] Daemon-down notice card with wifi-off icon and inline code chip; meta line "N of M channels configured".
- [ ] Restart banner = warning-bordered card with triangle icon and primary button.
- [ ] Configure opens a 512px dialog with header rule, close X, fields, Cancel + Save footer.
- [ ] Known difference recorded: no Telegram/WhatsApp QR panels (no API).

webhooks
- [ ] Receiver off: warning-bordered card (border `#826d2a` dark / `#f4d89a` light), warning icon, title + 2-line
      text, primary ENABLE WEBHOOKS on the right; header NEW ROUTE disabled (muted fill).
- [ ] Receiver on: compact card with base URL chip and switch.
- [ ] Heading "Routes (N)" + hint line; route cards with badges, events, URL + copy, switch + trash icon.
- [ ] Create dialog and created view (URL box, warning-tinted secret box, I have copied it).

pairing
- [ ] Header CLEAR PENDING primary with trash icon.
- [ ] Two headed sections with 80px empty cards at y≈108 and y≈244 (when both empty).
- [ ] Rows: outline platform badge + name; Approve primary / Revoke X icon.
- [ ] Approve-by-code section restyled (our extra; recorded as a difference).

env
- [ ] Title "Keys" followed by jump chips in the header.
- [ ] Intro two lines + outlined Show/Hide advanced button on the right.
- [ ] OAuth card: display-face upper-case title with shield icon, refresh icon, count line, 84px rows with
      shield-off icon, name + outline badge, status line, faded command chip + Copy, LOGIN button.
- [ ] Providers card with collapsible group rows (chevron, name, N set, Get key, N keys).
- [ ] Custom keys card with set rows (masked value box, Replace, Clear) and dashed add box.
- [ ] No reveal (eye) button anywhere.
