# Lead decisions for wave 52 (Agent 1)
- Platform/app logos: letter tiles (no simple-icons: CC0 icon data is outside the licence guard's list). (desktop-capabilities W52-x-1)
- Bots: sidebar SESSIONS | BOTS tab as in the reference; keep the /bots route reachable for deep links but drop its nav entry. (W52-x-2)
- Webhooks "New route" disabled while the receiver is off (matches the reference). (web-channels-webhooks-pairing-env)
- Backend gaps: never invent endpoints; show the control disabled or a placeholder state and list it as a follow-up.
- Keys (desktop-composer-menus): Ctrl+K opens the palette (Ctrl+Shift+P kept as an alias); Ctrl+Tab opens the session
  switcher as in the reference (hold-to-reveal, release opens); pane-tab cycling moves to Ctrl+PageDown / Ctrl+PageUp.
  Update the keyboard-shortcuts settings page and its tests accordingly.
- Session row right-click menu: in-renderer menu wired only to existing actions (pin, archive, unread, copy ID, pop-out,
  rename via PATCH title, delete via DELETE /api/sessions/{id}); unsupported actions omitted.
- Telemetry/usage-stats consent (web metrics-consent-banner, desktop usage-stats-prompt): N/A by D-16.49 (no telemetry).
  Reuse the strip's look for our own notices (PressureBanner on web, restored-draft notice on desktop); mark the slug as an
  intended difference.
- Approval card: keep backend-offered choices, arming delay and data-choice; new string keys (bots/RoomApprovalCard.tsx shares
  approval.*). Prompt timestamps only for messages sent in the current window (no stored timestamp).
- HUD and quick-entry window sizes follow the reference (620x320 and 640x168); update the tests that pin the old sizes.
- Onboarding: no "recommended" tag, no provider logos, no "get a key" links (we have no such data); quick-entry "Send to"
  offers "Current chat" only.
- Parity beats our old layout presets: the reference's right-side arrangement becomes the default; keep #pane-tab-<id> ids
  and "Open <pane>" buttons for e2e.
- Web header slot: use the foundation's `HeaderAfterTitle` (asked w51a to provide it); never add a second one.
- Logs auto-refresh switch defaults OFF as in the reference; update the test that pins polling.
- `show_token_analytics` check moves into a shared hook (Analytics/Models show a hidden state when off).
- Desktop settings (W52-dsa-*): reference rows with no feature behind them render DISABLED with a short "not available
  yet" caption (not dropped). Voice becomes its own top-level settings item (as in the reference). Field ownership:
  browser.* → settings-a (Browser pages); terminal.* and tool_output.* → settings-b (Advanced › Terminal backend / Output
  limits). Chat text size range as in the reference. Keep e2e selectors: input[aria-label="Search settings"],
  [data-row="default-directory"], and alias page=desktop → Projects page. Billing page: omitted (no billing in Indus).
- Settings primitives + restyled schema-row renderer are built by the desktop foundation (w51b), so settings-a and
  settings-b only build pages. Both slices may add entries to settings/pages.ts, strings.en.ts and the search index —
  additive only, in their own blocks; the integrator merges. Field moves: as listed in desktop-settings-b W52-B-3/B-13
  and the earlier ownership rule (browser.* → a; terminal.*, tool_output.* → b).
- Pages for features we lack (Billing, local model runtime, cloud gateway, sounds, voice modes...) show only data we have,
  an empty state, or are omitted; Billing is omitted.
- Provider delete: only offer if saving null actually deletes (builder checks); /api/model/set body: read our own code.
