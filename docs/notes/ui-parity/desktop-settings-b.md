# Task: Desktop Settings pages, slice B (W52-j) Phase: ui-parity Owner: builder w53j

Status: Built (2026-10-24, wave 53 builder j, base e402468 = main + w51b). Uncommitted, for the
integrator. 29 of the spec's 33 reference slugs have a page; Billing (lead decision), Screen capture
and Tools & Keys › Settings are not built (spec §2 / §8).

Read: spec `.agents/wip/w52/specs/desktop-settings-b.md` (measurements and colours sampled from the
reference screenshots by the spec writer; the screenshots themselves are not in this container),
`.agents/wip/w52/lead-decisions.md`, `docs/notes/ui-parity/desktop-foundation.md`; settings-a spec
§0–§1, §3.12–§3.14, §4 for the shared category list and the field moves. Reference source: not
opened in this wave (the spec already carries the structure notes). Evidence ids: none.

What it does (own words): every settings page of Memory & Context, Voice, Advanced, Notifications,
Providers, Gateways, Keyboard Shortcuts, Tools & Keys, Sessions, Plugins and About becomes a quiet
flat list on the overlay's content area: 13px labels with an optional grey line, controls on the
right (pill switches, compact selects, 214px fields, segmented controls, key chips), curated rows
first and the rest of a schema page behind "More settings", soft tinted cards for choices (provider
cards, connection mode cards) and a centred 672px column on About. Behaviour and data flows are
unchanged: schema rows still save one field at a time, keys go to the secret store, connection
changes still re-home through the switcher.

Files (new): `settings/fieldCopy.ts`, `settings/stringsB.en.ts`, `settings/pagesB/{kit,advanced,
notifications,keys,providers,gateways,keyboard,sessions,about,voice,plugins}.tsx`,
`settings/pagesB/pagesB.css`.
Files (shared, additive blocks): `settings/pages.ts` (my views/pages, CustomPage ids, `after`
field, MANIFEST moves + a settings-b rows block), `settings/navMeta.tsx` (voice/tools icons,
gateways = Globe, `pageIcon` for child pages), `settings/SettingsOverlay.tsx` (child page icon,
`page.after` block, `go` in context), `settings/context.tsx` (`go`, bridge groups shortcuts /
notifications / logs / external), `settings/schemaPage.tsx` (field copy: label, description,
placeholder, string options / suggestions; curated order), `settings/search.ts` (copy labels; the
key-derived label kept as a keyword), `settings/keybindings.ts` (+ groups, `acceleratorKeys`),
`settings/strings.en.ts` (labels of my views/pages changed in place; 5 unused keys removed),
`i18n/en.ts` (spreads `SETTINGS_B_STRINGS`), `app/manage/strings.en.ts` (one value: "Saved
connections").
Files restyled (owned by this slice): `settings/customPages.tsx` (my pages moved to `pagesB/`;
`SecretInput` moved to `pagesB/kit.tsx`, still re-exported), `ConnectionsPage.tsx` (list part),
`UninstallSection.tsx`, `LicencesPage.tsx`.
Tests: `search.test.ts` (views list, field ownership, billing fallback → `view=nope`, keychain /
canary rows moved, 8 new cases), `SettingsOverlay.test.tsx` (API key row opens first; 5 new
cases: notifications master switch, sounds select, Local models test→use, key row remove, curated +
More fold opening on a deep link), `keybindings.test.ts` (chips per platform, groups).

Edge cases / failure modes:
- Fields claimed elsewhere first never show twice (first claim wins); `voice.*` is claimed by
  explicit `fields` so `voice.read_aloud` lands on Text to speech.
- A string offered as a select keeps an unknown stored value selectable.
- Endpoint "Use" needs a model (the backend refuses an empty one): disabled / explained.
- The keys list shows one row per provider title (GEMINI / GOOGLE share "Google Gemini"); the set
  name wins.
- A bridge group missing in a window (tests, older main) shows "Not available in this build yet."

Decisions (W52-j-n, for the lead):
- W52-j-1: Billing omitted (lead decision overrides spec §5.5); spec W52-B-5 not built.
- W52-j-2: `default-directory` MANIFEST row stays where slice A puts it (Workspace › Projects; the
  e2e fills `[data-row="default-directory"] input`); Sessions › Default project folder edits the same
  store value under its own row id `sessions-directory` (W52-B-13 adapted).
- W52-j-3: field moves done here (W52-B-3): `terminal.backend`, `terminal.timeout` removed from
  Workspace › Shell; `tool_output.max_chars` removed from Workspace › Files; chat/voice page removed
  from the Chat view (wake word → Voice). Slice A makes the same removals in its tree.
- W52-j-4: the notifications master switch reads "on" while either kind is on (spec said both);
  off writes both kinds off, on writes both on.
- W52-j-5: endpoint delete not offered: `PUT /api/config` is additive (writer.save "nothing is
  deleted by omission"), so `null` would not remove the entry (W52-B-12).
- W52-j-6: Saved connections shows "Primary", "Last used" and a kind pill; "Current" is not shown
  (the list has no "this window's connection" flag). Row actions are quiet text buttons that brighten
  on hover; no per-entry Test (the bridge tests form input only).
- W52-j-7: the About log viewer sits behind "Show recent lines"; its own buttons (reveal, copy
  diagnostics) stay inside it, so no duplicate aside buttons.
- W52-j-8: Local models lists ollama / vllm / llamacpp / lmstudio from `/api/model/options` with
  their documented default URLs (renderer table; the options route carries no base URL).
- W52-j-9: rows the spec shows without a description hide the schema's description (field copy
  `description: ""`); the key stays in the label's tooltip and in search.

Compare checklist (our capture, both themes, against the spec's numbers; reference PNGs not
available in this container — measured values from the spec):
- [x] settings-memory-persistent (switch rows 120/188, inputs 214×30 at x 1119–1332, provider field)
- [x] settings-memory-context (curated 6 + More)
- [x] settings-voice-conversation (push-to-talk, max length, wake word row)
- [x] settings-voice-transcription (provider combo, local model select, language "Not set")
- [x] settings-voice-speech (provider select Piper/OpenAI, read aloud switch, voice, speed)
- [x] settings-advanced (segmented keep-awake, quick entry switch + chip shortcut + status, Run again)
- [x] settings-advanced-runtime / -tools / -terminal (select "Local" + 4 image rows) / -delegation / -output
- [x] settings-notifications-alerts (intro, master + 2 switches, test button, footnote)
- [x] settings-notifications-sounds (select + Preview)
- [ ] settings-billing-overview — not built (W52-j-1)
- [x] settings-providers (heading + link, highlighted card, local card, Other providers)
- [x] settings-keys (top endpoint row, borderless search, dot rows, bare "Paste key")
- [x] settings-providers-endpoints (count chip, cards, form card with segmented API mode)
- [x] settings-providers-local (section heading, preset rows with Test / Use; deliberate content)
- [x] settings-gateways-connection (3 mode cards, Save and reconnect, keychain switch, Open logs)
- [x] settings-gateways-connections (pills, Add connection, divider, launch switch)
- [x] settings-gateways-updates (empty state)
- [x] settings-shortcuts (Reset all, search, caps groups, key chips, hover reset)
- [x] settings-keyboard-global (three chip rows with live status; deliberately different content)
- [x] settings-tools-keys (row style, empty state, add form)
- [x] settings-sessions-archived (switch, heading, empty state)
- [x] settings-sessions-directory (path title, Change / Clear)
- [x] settings-plugins (inventory on the page frame; no nav chevron)
- [x] settings-about (wordmark hero, Updates card, channel, facts card)
- [x] settings-about-uninstall (danger card; option cards when allowed)
- [x] settings-licences (intro + notices block)
- [x] no schema key line, no bold 15px label, no native checkbox in a row (one checkbox remains in the
  endpoint form: "Use for new chats", as in the reference form)
- [x] search "keychain", "quick entry", "completion sound", "archived", "uninstall" land on the new
  page (`search.test.ts`)
- Not built: settings-keyboard-shortcuts-screen-capture, settings-tools-and-keys-settings, billing.

Still differs (follow-ups): Plugins page keeps the inventory's own markup (rescan button and cards
from `contrib/PluginsPage.tsx`, shared with the Command center) instead of the reference's
right-aligned button slot; endpoint cards have no trash button (W52-j-5); provider cards have no
gradient bottom edge; capture kit copy lives outside the repo (`$S/w53j-kit/settings-b.ts`).

## Integration (wave 53, settings)
Landed together with slice A (W52-i) on main + w51b + w52 g/h/m. Decision ids (§2.7 of
`docs/plan/16-what-we-change.md`): W52-j-1 → D-16.1231; W52-j-2 → D-16.1238; W52-j-3 → D-16.1237;
W52-j-4 → D-16.1241; W52-j-5 → D-16.1242; W52-j-6 → D-16.1243; W52-j-7 → D-16.1244; W52-j-8 →
D-16.1245; W52-j-9 → D-16.1240. Merged at integration: `fieldCopy.ts` is the one field-copy map (slice
A's entries added; labels, descriptions and placeholders are now message keys `settingsB.field.*` in
`stringsB.en.ts`; suggestions are read through a function); `PageDef.after` sits beside slice A's
`PageDef.order`; one `PAGE_ICONS` map; the context carries both slices' additions (D-16.1239,
D-16.1246).
