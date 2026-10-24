# W52 spec: desktop Settings pages, slice B

Status: spec ready (2026-10-24), Read + Note only. Written for the wave-52 page builder, who works with the
reference closed. Everything below is in our words; the only things taken from the reference are measurements,
sampled colours and short labels (1–4 words).

Read line: reference screenshots `$SP/compare/hermes/desktop/<slug>--{light,dark}.png` for the 33 slugs in §2;
reference source read for structure and behaviour only (`apps/desktop/src/app/settings/`: primitives, config-field,
config-subpages, constants (curated field lists), keybind-settings, notifications-settings, sessions-settings,
about-settings, uninstall-section, gateway-settings, credential-key-ui). Our side: `apps/desktop/src/settings/*`
(pages.ts, schemaPage.tsx, customPages.tsx, ConnectionsPage.tsx, UninstallSection.tsx, LicencesPage.tsx,
context.tsx, keybindings.ts, search.ts, settings.css), `apps/desktop/src/global.d.ts`, backend
`src/indus/foundation/config/schema.py`, `src/indus/web/routes/{config_env,config_view,system,analytics}.py`,
`src/indus/providers/registry.py`. Foundation note in progress: `$SP/wt-w51b/docs/notes/ui-parity/desktop-foundation.md`.

`$SP` = `/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad`.

---

## 0. What the reference looks like, in one paragraph

Every page is a quiet, flat list on the settings modal's white (dark: near-black) content area. A breadcrumb
("Settings › View › Page") sits top-left, then rows. A row is a two-column grid: label (13 px, medium) with an
optional one- or two-line grey description under it on the left; the control sits right-aligned in a column
about 214 px wide. Booleans are pill switches, enums are compact auto-width selects, numbers and strings are
214×30 inputs with a hair border, and "Not set" placeholders stand in for empty values. No schema keys, no
"saved" chatter, no bold headings inside pages. Rows are separated by whitespace only (≈ 63–67 px pitch for
rows with a description, 58 px without). Special pages (providers, gateways, about) use soft tinted cards
instead of bordered boxes. Our pages today are the opposite: bold 15 px labels, raw descriptions from the schema,
a monospace key line under every row, native checkboxes and selects, all fields of a prefix shown, a 50/50 split
with controls left of centre.

## 1. Scope and ownership

This slice builds the page **bodies** for the views listed below. The settings **shell** (modal card, left
category tree with icons and chevrons, search pill, breadcrumb, close button, footer icon row) is the W51-b
foundation's (`SettingsOverlay.tsx`, `settings.css`). Slice A (`desktop-settings-a`) owns Model, Chat,
Appearance, Workspace, Safety, Browser, Passwords.

Shared file conflict: both slices edit `settings/pages.ts`, `settings/strings.en.ts` (and other i18n packs),
`settings/search.ts` MANIFEST and the shared row primitives. Rule for the two builders: slice A lands the
primitives in §3 first if it is ahead; otherwise this slice creates `settings/primitives.tsx` with exactly the
API in §3.2 and slice A rebases onto it. Each slice edits only its own views' entries in `VIEWS`/`MANIFEST`.

Cross-slice field moves this slice needs (slice A must agree; see W52-B-3):
- `terminal.backend`, `terminal.timeout` leave Workspace › Shell → Advanced › Terminal backend
  (the reference puts only persistent shell + env passthrough on Workspace › Shell).
- `tool_output.max_chars` leaves Workspace › Files → Advanced › Output limits.
- `code_execution.*` except `code_execution.mode` → Advanced › Tool access (`code_execution.mode` stays with
  Workspace › Files, as in the reference).
- Chat › Voice page (wake word) moves into the new Voice view (§5.2).
- `agent.fast_mode` stays on Model › Main (slice A); Agent limits does not repeat it.

## 2. Slug → our page map

Our capture names are `settings-<view>-<page>` (manifest `$SP/compare/indus/desktop/manifest.json`, hash
`#/settings?view=<view>&page=<page>`). New page ids below are the ones to add to `pages.ts`; the capture kit must
learn them (copy the kit, add the new hashes; never edit the originals).

| Reference slug | Ref title | Our page now | Our page after (view/page) | Our capture slug after |
|---|---|---|---|---|
| settings-memory-and-context-persistent-memory | Memory & Context › Persistent memory | memory/persistent (`settings-memory-persistent`) | memory/persistent | settings-memory-persistent |
| settings-memory-and-context-context-and-compression | › Context & compression | memory/context | memory/context | settings-memory-context |
| settings-voice-voice-conversation | Voice › Voice conversation | chat/voice (wake word only) | **voice/conversation** (new view) | settings-voice-conversation |
| settings-voice-speech-to-text | Voice › Speech to text | — (stt.* unshown) | **voice/transcription** | settings-voice-transcription |
| settings-voice-text-to-speech | Voice › Text to speech | — (tts.* unshown) | **voice/speech** | settings-voice-speech |
| settings-advanced | Advanced › Desktop & startup | advanced/desktop | advanced/desktop | settings-advanced |
| settings-advanced-agent-limits | › Agent limits | advanced/runtime | advanced/runtime (label "Agent limits") | settings-advanced-runtime |
| settings-advanced-tool-access | › Tool access | advanced/tools | advanced/tools (label "Tool access") | settings-advanced-tools |
| settings-advanced-terminal-backend | › Terminal backend | advanced/terminal | advanced/terminal (label "Terminal backend") | settings-advanced-terminal |
| settings-advanced-subagents | › Subagents | (delegation.* inside advanced/tools) | **advanced/delegation** | settings-advanced-delegation |
| settings-advanced-output-limits | › Output limits | (workspace/files) | **advanced/output** | settings-advanced-output |
| settings-notifications-desktop-alerts | Notifications › Desktop alerts | notifications/alerts | notifications/alerts | settings-notifications-alerts |
| settings-notifications-sounds | › Sounds | (a toggle on alerts) | **notifications/sounds** | settings-notifications-sounds |
| settings-billing-overview | Billing › Overview | — | **billing/overview** (new view, usage-based, W52-B-5) | settings-billing-overview |
| settings-providers | Providers › Accounts | providers/accounts | providers/accounts | settings-providers |
| settings-keys | Providers › API keys | providers/keys | providers/keys | settings-keys |
| settings-providers-custom-endpoints | › Custom Endpoints | providers/endpoints | providers/endpoints | settings-providers-endpoints |
| settings-providers-local-models | › Local Models | — | **providers/local** (presets, W52-B-6) | settings-providers-local |
| settings-gateways-this-window | Gateways › This window | gateways/connection + gateways/storage | gateways/connection (merged, label "This window") | settings-gateways-connection |
| settings-gateways-saved-connections | › Saved connections | gateways/connections | gateways/connections (label "Saved connections") | settings-gateways-connections |
| settings-gateways-remote-updates | › Remote updates | — | **gateways/updates** (empty state, W52-B-7) | settings-gateways-updates |
| settings-shortcuts | Keyboard Shortcuts › Key bindings | keyboard/shortcuts | keyboard/shortcuts (label "Key bindings") | settings-shortcuts |
| settings-keyboard-shortcuts-hud-gesture | › HUD gesture | (3 global rows on shortcuts) | **keyboard/global** ("Global shortcuts", W52-B-8) | settings-keyboard-global |
| settings-keyboard-shortcuts-screen-capture | › Screen capture | — | not built (reference page is empty; W52-B-8) | — |
| settings-tools-and-keys-tools | Tools & Keys › Tools | (other-key form on providers/keys) | **tools/keys** (new view "Tools & Keys") | settings-tools-keys |
| settings-tools-and-keys-settings | › Settings | — | not built as a page (messaging settings live on our Messaging page; W52-B-9) | — |
| settings-sessions-archive-and-retention | Sessions › Archive & retention | sessions/archived | sessions/archived (label "Archive & retention") | settings-sessions-archived |
| settings-sessions-default-project-folder | › Default project folder | sessions/directory (sessions.* + cwd) | sessions/directory (label "Default project folder") | settings-sessions-directory |
| settings-plugins | Plugins | advanced/plugins | **plugins/main** (new top-level view, no child list) | settings-plugins |
| settings-about | About › Version & updates | about/version | about/version | settings-about |
| settings-about-uninstall | › Uninstall | (section inside about/version) | **about/uninstall** | settings-about-uninstall |
| (no ref; `settings-licences` missing) | — | about/licences | about/licences (kept, restyled) | settings-licences |
| (no ref) | — | advanced/profiles | advanced/profiles (kept last, restyled) | settings-advanced-profiles |

Target `VIEW_IDS` order (slice A's views in brackets, unchanged by us): [model, chat, appearance, workspace,
safety, browser?, passwords?], memory, voice, advanced, notifications, billing, ── gap ──, providers, gateways,
keyboard, tools, sessions, ── gap ──, plugins, ── gap ──, about. The gaps are 10 px of extra space in the nav
(group separators), which the foundation's nav renders from a `group` field (add `group: "app" | "connect" |
"extend" | "about"` to `ViewDef`; tell W51-b if its nav does not read it yet).

View labels and nav icons (lucide-react, already a dependency): Memory & Context = `Brain`; Voice = `Mic`;
Advanced = `Wrench`; Notifications = `Bell`; Billing = `ChartColumn` (bar chart); Providers = `Zap`;
Gateways = `Globe` (reference uses a globe-on-stand glyph; `Earth`/`Globe` is our choice); Keyboard Shortcuts =
`Keyboard`; Tools & Keys = `KeyRound`; Sessions = `Archive`; Plugins = `LayoutGrid` (four squares); About = `Info`.
Child page icons (shown left of child labels, 14 px, tertiary colour): Persistent memory `Brain`, Context &
compression `FileText`, Voice conversation `Mic`, Speech to text `FileText`, Text to speech `Volume2`, Desktop &
startup `Monitor`, Agent limits `Cpu`, Tool access `Wrench`, Terminal backend `SquareTerminal`, Subagents
`Users`, Output limits `FileText`, Desktop alerts `Bell`, Sounds `Volume2`, Overview `ChartColumn`, Accounts
`CircleUser`, API keys `KeyRound`, Custom Endpoints `Globe`, Local Models `Cpu`, This window `Monitor`, Saved
connections `Globe`, Remote updates `Download`, Key bindings `Keyboard`, Global shortcuts `Keyboard`, Keys
`KeyRound`, Archive & retention `Archive`, Default project folder `FolderOpen`, Version & updates `Download`,
Uninstall `Trash2`, Licences `FileText`, Profiles `Users`. Plugins has no children and no chevron.

## 3. Shared page anatomy (all pages in this slice)

### 3.1 Measurements (1440×900 window, settings modal open)

- Modal card x 48–1391, y 48–851 (shell; foundation). Nav column x 48–257 (209 px). Content area starts x 257.
- Page gutter: content column x 315–1333 (58 px inset left and right of the 1076 px content area), i.e. the page
  body is `padding-inline: 58px`, width fills. Bottom padding ≈ 80 px so the last row can scroll clear.
- Breadcrumb baseline row y ≈ 78 (top of page body + 22 px); first row label at y ≈ 120 (≈ 34 px below the
  breadcrumb). Page intro text (when a page has one) sits at y ≈ 107, 12 px, tertiary.
- Row grid: `grid-template-columns: minmax(0,1fr) minmax(240px, 352px)`; control column right-aligned
  (`justify-self: end`), vertical align centre; row padding 12 px top/bottom; gap 12 px. Below ~672 px of
  page width the control drops under the description (container query on the row, not the viewport).
- Row pitch measured: label+description rows 63–68 px; label-only rows 58 px.
- Text input / number input: 214×30 (x 1119–1332), radius 4 px, 1 px border, 12 px horizontal padding, text
  13 px. Placeholder "Not set" for empty/null.
- Compact select (enum): auto width (e.g. "Built-in only" 113 px, "Compressor" 116 px, "(none)" 83 px), 30 px
  tall, 11 px padding left, chevron-down 12 px at 10 px from right, text 13 px; open list is the foundation's
  menu component. Long option lists (voices, models) use a 232 px wide free-text combobox (text input with a
  chevron that opens suggestions) — e.g. "marin" / "en-US-AriaNeural" fields at x 1101–1332.
- Switch: 36×20 pill (x 1297–1332, y 145–164 for a row at label y 144), knob 14 px. On: filled accent track,
  knob = page background colour. Off: page-background track with a 1 px stroke, knob = foreground colour
  (dark knob on light, light knob on dark).
- Segmented control (3 options): 288×24 (x 1043–1330), track tinted, 2 px inset, active segment 93×20 on page
  background with a faint shadow, labels 11.5 px; inactive label secondary colour.
- Small outline button ("Run again", "Send test notification", "Add connection", "Test"): height 24, radius 4,
  1 px stroke, 9–10 px padding, 12.5 px text, optional 14 px leading icon with 6 px gap.
- Primary button ("Save and reconnect", "Install runtime"): height 23–24, radius 3–4, accent fill, white text
  12.5 px medium, 8–10 px padding.
- Text button / link ("Have an API key instead?", "Choose", "Open logs", "Check now"): 12.5 px medium,
  secondary colour, 1 px dotted-looking underline 2 px under baseline (use `text-decoration: underline;
  text-underline-offset: 3px; text-decoration-color: <stroke>`), optional leading 13 px icon.
- Section heading inside a page ("Archived sessions", "Local runtime", "Updates", "This machine"): 14 px icon in
  tertiary colour + 13 px medium title, 8 px gap, 10 px margin below, 8 px above; optional count pill to the
  right (e.g. "Models 5").
- Empty state: centred, title 13.5 px medium foreground, one line under it 12 px tertiary, ~90 px below the
  last element above it.

### 3.2 Primitives (React, `apps/desktop/src/settings/primitives.tsx`, our code)

Build these once; every page in this slice uses them. Names are ours.
- `PageBody({intro?, children})` — the scroll container with the 58 px gutters; `intro` renders the 12 px
  tertiary intro line(s) under the breadcrumb.
- `SettingRow({title, description?, control?, below?, wide?, rowId?, fieldKey?})` — the grid in §3.1;
  `data-row` / `data-field` + `data-highlighted` kept (search and deep links rely on them; today's `Row` and
  `FieldRow` set them). `wide` puts `control` on the title line and `below` full width (lists, editors).
- `SwitchRow({title, description?, checked, disabled?, onChange})` — `role="switch"`, `aria-checked`,
  `aria-label=title`. Keep `role="switch"` + accessible name: `SettingsOverlay.test.tsx` finds
  `getByRole("switch", { name: "Minimise to the tray" })`.
- `Switch`, `CompactSelect({value, options:[{value,label}], onChange})` (a button + foundation menu; keyboard:
  Enter/Space open, arrows move, Escape closes), `FreeCombo` (input + suggestion list), `TextField`
  (214 px), `Segmented({options, value, onChange})` (`role="radiogroup"`, each `role="radio"`).
- `SectionHeading({icon, title, count?, aside?})`, `EmptyState({title, body?})`, `TintCard`, `DangerCard`,
  `Pill({tone: "neutral"|"accent"|"danger"})`, `Kbd` (use the foundation's `ui/Kbd` if it landed),
  `TextButton`, `OutlineButton`, `PrimaryButton` (use foundation `ui/` buttons when present).
- `MoreFields({children, open?})` — a quiet disclosure row "More settings" (12.5 px secondary, chevron) at the
  end of curated pages; opens to the remaining schema fields of the page (W52-B-2). When a search hit or link
  targets a field inside it, it opens itself.
- `SchemaField` — today's `FieldRow` logic (parse, save on blur/Enter, secret reveal, managed lock, per-row
  error) rendered with the new controls, the label from `fieldCopy.ts` (below), **no key line** (W52-B-1;
  the key goes into the row's `title` attribute for power users), and the save state shown as a 12 px tertiary
  note under the control only while saving or on error (no persistent "Saved").
- `settings/fieldCopy.ts` (new): `{ [schemaKey]: { label, description? } }` in OUR words for every curated
  field in this slice (§5). Fallback: today's `fieldLabel(key)` + schema description.

### 3.3 Colours (sampled from the reference PNGs; map onto the W51-b `--desk-*` tokens by role)

| Role | Light | Dark | Notes |
|---|---|---|---|
| Content bg | #fefefe | #0d1015 | page, inputs, outline buttons |
| Nav bg (shell) | #f6f8fa | #010409 | foundation |
| Label text (fg) | #2c3035 | #d8e0e6 | row titles, input text, breadcrumb current |
| Description / tertiary | #86888b | #81878d | descriptions, breadcrumb parents, stat labels, placeholders in key rows |
| Nav text / secondary | #575a5f | #aab1b6 | |
| Kbd/section caps text | #aaabad (section caps) / #838588 (kbd) | #5f6368 / #8a8e93 | |
| Search placeholder (borderless search) | #d9dadb | #303439 | |
| Input border | #e7e7e7 top hair + #ecf2fe outer glow 1 px | #080a0d + #0f141e | reads as a very faint border; use stroke-tertiary |
| Outline button stroke | #e3eaf8 | #18202e | |
| Accent (switch on, primary button, links, "Current" text) | #0053fd | #4a84fe | |
| Accent tint (selected card bg, recommended card) | #eff4fe | #101722 | |
| Accent tint stronger (Current pill bg) | #e4ecfd | #131b2c | |
| Selected card border | #0053fd 1 px, outer ring #cbdcfe 2 px | #4a84fe, ring #192643 | mode cards |
| Unselected card bg / border | #f0f2f8 / #ced9ef | #151a22 / #232d40 | mode cards |
| Banner bg (billing) | #e9edf6 | #171d28 | inner button stroke #d0dbf1 / #212b3f |
| Segmented track | #dfe6f4 | #1b2331 | active segment = content bg |
| Switch off: track stroke / knob | #e0e1e2 / #2c3035 | #282b30 / #d8e0e6 | |
| Kbd chip bg / border | #f0f1f1 / #c9d5e9 | #222429 / #2f384b | 23 px tall, min 23 px wide, radius 4 |
| Kbd "set" ghost chip bg | #f7f8f8 | #191c21 | text even fainter |
| Neutral pill bg / text | #f6f6f6 / #828487 | #1a1e24 / #878d94 | "Primary", "App-managed", "Up to 256K context" |
| Danger card bg / border | #fcf3f3 / #ecaaae | #191317 / #672728 | about error card, uninstall card (#eeb3b7 / #5c2425) |
| Danger inner card bg / border | #fdf8f8 / #e3e4e8 | #141115 / #25262d | uninstall option |
| Danger title text | #cf222e | #f85149 | |
| Danger pill bg | #f9e7e9 | #241619 | "Too big for this machine" style |
| Facts table bg / border | #fcfcfc / #dde2e7 | #0f1217 / #262a32 | about details |
| Key-row status dot (unset) | #c8d5f1 | #232f47 | set = accent |
| Index chip bg (endpoint "1") | #e0e1e1 | #32373d | |
| Divider hairline | #e3e7eb | #22262d | saved connections |
| Recommended card bottom edge | fades #e6eefe → #ffc098 | #172136 → #91542e | 1–2 px, left→right |

Typography: Inter (foundation). Row title 13 px / 500; description 12 px / 400, line-height 16 px; breadcrumb
12.5 px; inputs 13 px; small buttons 12.5 px; section caps (shortcut categories) 10.25 px / 600 / uppercase /
letter-spacing 0.12em; mono only for URLs in endpoint cards, install ids and commit hashes (12 px).
Nothing in page bodies is bold 15 px any more.

## 4. Behaviour rules kept from our app (do not change)

- Schema pages still save one field per change with `PUT /api/config` `{config: patchFor(key,value)}` for the
  "Applies to" profile; `config_managed` still locks the page with the backend's banner; secrets still never
  re-sent. `parseInput` unchanged (its unit test stays).
- Every hand-built row keeps its MANIFEST id so search and `row=` links keep landing (`search.test.ts`).
  Renamed pages need their MANIFEST `page` updated; moved rows (wake word, uninstall, update channel, quick
  entry, storage) get their new `view`/`page`.
- `resolveTarget("view=billing")` currently falls back to model/main and `search.test.ts` asserts it; once
  Billing exists, change that assertion to an unknown id such as `view=nope` (a deliberate test update).
- `VIEWS` id list is pinned by `search.test.ts` "has the 1.0 views"; update it to the new list (deliberate).

## 5. Pages

Notation: **Fields** = schema keys shown in this order as curated rows; **More** = the rest of the page's
prefix, in schema order, inside `MoreFields`. Labels/descriptions in quotes are ours (or short labels).

### 5.1 Memory & Context

Persistent memory (`memory/persistent`, prefix `memory.`)
- Ref layout: 2 switch rows with descriptions (label y 120, 188), 2 input rows without description (y 260,
  318), 1 select row with description (y 371). Values 2200 / 1375, select "Built-in only".
- Fields: `memory.notes_enabled` "Persistent memory" / "Keep durable notes the agent can reuse in later
  sessions." (switch) · `memory.profile_enabled` "User profile" / "Keep a short profile of your preferences."
  (switch) · `memory.notes_char_limit` "Memory budget" (input, no description) · `memory.profile_char_limit`
  "Profile budget" (input) · `memory.provider` "Memory provider" / "Extra memory plugin" — compact select fed
  by `GET /api/memory` → `providers[]` (name list) with first option value "" labelled "Built-in only".
  Saving keeps using `PUT /api/config` (or `PUT /api/memory/provider` if the builder finds the config write is
  refused for this key — check `system.py:set_memory`).
- More: `memory.write_approval`, `memory.review_interval`, `memory.provider_recall_spill`.

Context & compression (`memory/context`, prefixes `context.`, `compression.`; also `auxiliary.compression.*`
stays on Model › Auxiliary (slice A) — do not claim it)
- Ref: select row (Context engine, "Compressor"), switch (Auto-compression), input (threshold 0.5), switch
  (auto-raise, ours has none), input (target 0.2), input (protected 20), input with description (timeout 120).
- Fields: `context.engine` "Context engine" / "How long conversations are kept inside the model's window."
  (string in our schema → TextField with placeholder "builtin"; if `GET /api/config/schema` gives options,
  CompactSelect) · `compression.enabled` "Auto-compression" / "Summarise older turns when a conversation
  grows large." (switch) · `compression.threshold` "Compression threshold" · `compression.target_ratio`
  "Compression target" · `compression.min_tail_user_messages` "Protected recent messages" ·
  `compression.timeout_s` "Compression model timeout (s)" / "Seconds to wait for the summary model on each
  call."
- More: `threshold_tokens`, `model_thresholds`, `max_attempts`, `tail_mode`, `in_place`,
  `abort_on_summary_failure`, `idle_after_s`.
- Not ours: the reference's "Codex auto-raise" switch (no such setting) — omit.

### 5.2 Voice (new view `voice`, scoped: true)

All three reference pages open with the same info row: title "Voice recording shortcut", two description lines,
no control. Ours: one info row on Voice conversation only: "Push-to-talk key" with the current
`voice.record_key` as an input (it is a real setting in our schema) and description "Used by the terminal UI;
the desktop composer uses the Keyboard Shortcuts page." (our words). Do not repeat it on the other two pages.

Voice conversation (`voice/conversation`, prefix `voice.` + custom wake-word rows)
- Claim by explicit `fields`, not by the `voice.` prefix, because the first claim in table order wins and
  `voice.read_aloud` must land on Text to speech. Conversation `fields`: `voice.record_key` (above),
  `voice.max_seconds` "Max recording length" (input), and under More `voice.silence_threshold`,
  `voice.silence_seconds`, `voice.wait_seconds`.
- Then the existing wake-word block from `VoicePage` (`customPages.tsx:1534`), restyled: SwitchRow
  "Wake word" / "Say the wake phrase to start talking; Indus listens to the microphone while this is on."
  (our words; keep today's i18n key if its text is ours already) with the listener error shown as a 12 px
  danger note under the description. Its MANIFEST row moves to `view: "voice", page: "conversation"`.
- Ref rows we do not have: voice chat mode (chained/gpt-live), GPT-live voice/persona, client direct — omit.

Speech to text (`voice/transcription`, prefix `stt.`)
- Ref: switches (Speech to text, Echo transcripts, Live transcription), select (Local transcription model
  "Base"), input (Transcription language "Not set").
- Fields: `stt.provider` "Transcription provider" (FreeCombo, suggestions "local", "openai", "groq"; empty =
  placeholder "Automatic") · `stt.local_model` "Local transcription model" / "Model size for on-device
  transcription." (CompactSelect: tiny, base, small, medium, large-v3 — these are faster-whisper size names,
  public facts; keep free value if the stored one is not in the list) · `stt.language` "Transcription
  language" (input, placeholder "Not set") · `stt.model` "Cloud model" (input) .
- More: `stt.base_url`, `stt.local_device`, `stt.max_file_mb`.
- No master switch: our schema has no `stt.enabled`; do not invent one.

Text to speech (`voice/speech`, prefix `tts.`, plus `voice.read_aloud`)
- Ref: select (Text-to-speech provider "Edge"), switch (Read responses aloud), FreeCombo (Edge voice).
- Fields: `tts.provider` "Text-to-speech provider" (CompactSelect: piper, openai — the two engines our schema
  description names; keep unknown stored values as an extra option) · `voice.read_aloud` "Read responses aloud" /
  "Speak each final answer while voice is on." (switch) · `tts.voice` "Voice" (FreeCombo, no suggestions) ·
  `tts.speed` "Speaking speed".
- More: `tts.model`, `tts.base_url`, `tts.key_name`, `tts.piper_binary`, `tts.piper_model`.

### 5.3 Advanced

Desktop & startup (`advanced/desktop`, custom page `advanced-desktop`)
- Ref rows: Keep computer awake (segmented Off / While working / Always, with a two-line description), two
  switches we lack, Quick Entry (switch), Quick Entry shortcut (214 px input + description + status line
  "Shortcut is active."), Run onboarding again (outline button "Run again"), update local changes (select).
- Ours, in this order:
  1. "Keep computer awake" / "Stop this computer from sleeping. While working holds it only during a reply."
     — `Segmented` with Off / While working / Always → `prefs.update({keepAwake: "never"|"while-working"|"always"})`
     (was a native select).
  2. "Quick entry" / "Open a small composer from anywhere with a global shortcut." — Switch bound to
     `bridge.shortcuts.get()` → `shortcuts[action="quick-entry"].enabled`, set with
     `bridge.shortcuts.set({action: "quick-entry", enabled})`. `SettingsBridge` gains `shortcuts?:
     IndusDesktopShortcuts` (pass it from the overlay's bridge like the others).
  3. "Quick entry shortcut" / "Needs at least one modifier." + a status line under the description:
     "Shortcut is active." when `registered`, otherwise the `error` mapped to short text ("Taken by another
     app", "Not valid", "Conflicts with <action>") in danger colour. Control: today's `AcceleratorInput`
     restyled as a 214 px input showing the accelerator in the UI font (not mono). Writes `prefs.update(
     {quickEntryShortcut})` as today.
  4. "Run onboarding again" / "Open the first-run setup again with your current model." — OutlineButton
     "Run again" → `openOnboarding()` from settings context.
- Update channel row moves to About › Version & updates (§5.11); its MANIFEST row "update-channel" moves too.
- Not shown (no feature): DevTools blocking, external-links switch, update local-changes select.

Agent limits (`advanced/runtime`, `fields` instead of prefix for the curated part, prefix `agent.` for More)
- Ref: inputs Max agent steps ("Not set" placeholder), API retries (3); select Service tier.
- Fields: `agent.max_turns` "Max agent steps" / "Upper bound on model calls in one turn; 0 means no cap." ·
  `agent.api_max_retries` "API retries".
- More: every other `agent.*` except `agent.fast_mode` (owned by Model › Main): gateway_timeout,
  restart_drain_timeout, restart_after_turn_timeout, cron_drain_timeout, clarify_timeout,
  local_stream_stale_timeout, turn_liveness_s, run_budget_s, fast_window_s, reconnect_attention_after.

Tool access (`advanced/tools`)
- Ref: two inputs, no descriptions (Enabled toolsets "hermes-cli", Tool-use enforcement "auto").
- Fields: `prompt.tool_use_enforcement` "Tool-use enforcement" (input; it is `str|bool|list` — show strings
  as text, booleans as "true"/"false", lists comma-joined; parse back with today's rules) ·
  `code_execution.timeout` "Code run timeout" · `code_execution.max_tool_calls` "Tool calls per code run".
- More: `code_execution.kernel`, `kernel_idle_timeout`, `max_session_kernels`.
- No "Enabled toolsets" row: our toolsets are per platform (`platform_toolsets`) and managed on the
  Capabilities page; add a TextButton "Manage toolsets" in the page intro only if the shell exposes a way to
  open Capabilities from Settings (check `useProvidedShellChrome()`); otherwise omit.

Terminal backend (`advanced/terminal`, prefix `terminal.`)
- Ref: select Execution backend ("Local"), input Command timeout (180), four image inputs with descriptions,
  long values clipped inside the 214 px box.
- Fields: `terminal.backend` "Execution backend" / "Where commands run." (CompactSelect with local, docker,
  ssh, singularity, modal, daytona, vercel — the backends our schema/runtime names; builder: take the list
  from `GET /api/config/schema` options if present, else these) · `terminal.timeout` "Command timeout" ·
  `terminal.docker_image` "Docker image" / "Container image for the Docker backend." ·
  `terminal.singularity_image` "Singularity image" · `terminal.modal_image` "Modal image" ·
  `terminal.daytona_image` "Daytona image".
- More: the remaining ~40 `terminal.*` fields (ssh_*, docker_*, container_*, …).
- Note: `terminal.cwd` stays owned by Workspace › Projects (first claim); `terminal.persistent_shell` and
  `terminal.env_passthrough` by Workspace › Shell (slice A).

Subagents (`advanced/delegation`, new page, prefix `delegation.`)
- Ref: inputs Subagent model / provider ("Not set"), turn limit 250, parallel 10, timeout 0; select reasoning
  effort "(none)".
- Fields: `delegation.model` "Subagent model" (placeholder "Same as main") · `delegation.max_iterations`
  "Subagent turn limit" · `delegation.max_concurrent_children` "Parallel subagents" · `delegation.max_depth`
  "Nesting depth".
- More: `max_async_children`, `summary_max_chars`, `oneshot_max_children`, `child_toolsets`,
  `inherit_mcp_toolsets`, `worktree`.

Output limits (`advanced/output`, new page)
- Ref: four inputs, no descriptions (50000, 2000, 2000, 20).
- Fields: `tool_output.max_chars` "Tool output limit" · `file_read_max_chars` stays on Workspace › Files
  (slice A) — use `also: ["checkpoints.max_snapshots"]` to show "Checkpoint limit" here too (owned by Safety ›
  Checkpoints).

Profiles (`advanced/profiles`, kept, not in reference): restyle rows with `SettingRow`; no layout change.

### 5.4 Notifications

Desktop alerts (`notifications/alerts`)
- Ref: intro line "OS notifications (not in-app toasts). Per device." then 8 switch rows each with a
  one-line description (pitch 63.5 px), then OutlineButton with bell icon "Send test notification" (157×24 at
  y 647) and a tertiary footnote below it.
- Ours: intro (our words): "System notifications on this computer; in-app messages are not affected."
  1. "Enable notifications" / "Off silences every alert below." — on when both kinds are on; turning it off
     writes `{notifications: {turnComplete: false, approvals: false}}`, on writes both true (UI over existing
     prefs; W52-B-10).
  2. "Approval needed" / "A command is waiting for your answer." → `notifications.approvals` (today's label
     "Tell me when Indus needs an answer").
  3. "Response ready" / "A reply finished while the window was in the background." →
     `notifications.turnComplete`.
  Rows 2–3 are disabled (dimmed 50%) when 1 is off.
  4. OutlineButton `Bell` "Send test notification" → `bridge.notifications.show({title: "Indus Agent", body:
     "Notifications are working."})`; add `notifications?: IndusDesktopNotifications` to `SettingsBridge`.
     Show a 12 px tertiary result line under it ("Sent." / the error).
  5. Footnote: "Reply alerts only fire while Indus is in the background." (our words).
- Not shown (no event source in our app): input needed, turn failed, background task, credit alerts, plugin.

Sounds (`notifications/sounds`, new page)
- Ref: one row "Completion sound" + description, right side a 312 px select ("Two-note comfort") with an
  OutlineButton "▷ Preview" under it, right-aligned.
- Ours: "Completion sound" / "Plays when a reply finishes." — CompactSelect with two options: "Off" and
  "Two-note chime" (our only chime, `ambient/sound.ts`), mapped to `notifications.sounds` false/true; the
  "Preview" OutlineButton (`Play` icon) calls the existing chime player in `ambient/sound.ts` (export a
  `previewChime()` if needed). Preview disabled when "Off".
- MANIFEST row "notify-sounds" moves to this page.

### 5.5 Billing (new view `billing`, scoped: true; W52-B-5)

Overview (`billing/overview`)
- Ref layout: tinted banner card x 315–1332, y 99–206 (padding 16, radius ≈ 4): title 13 px medium, one line
  12 px tertiary, small button "Sign in" (57×23, banner-tinted bg with stroke). Under it, 26 px gap, a 3-column
  stat row (columns at x 315, 658, 1002 — equal thirds): label 12 px tertiary, value 18 px medium (an em dash
  when empty).
- Ours (no account system — never invent one): banner title "Billing is with your providers" (our words), body
  "Indus Agent does not bill you; each provider charges its own key. Estimates below come from your sessions."
  Button "Open API keys" → navigate to Providers › API keys (needs the context navigation in §6).
- Stats from `GET /api/analytics/usage?days=30` (`totals`): "Estimated cost (30 days)" → `$` + 2 decimals,
  "Tokens (30 days)" → input+output, compact (e.g. "1.2M"), "Sessions (30 days)". Loading: em dash; error:
  em dash and a 12 px tertiary note under the row with the message.

### 5.6 Providers

Accounts (`providers/accounts`)
- Ref: section heading with key icon "Connect an account" and a right-aligned TextButton "Have an API key
  instead?" (underlined); intro line; a highlighted card (accent tint, rounded 6, 67 px tall, chevron in accent
  on the right, bottom edge fading to orange) for the recommended provider with a small inverted badge; a plain
  card "Run models locally" (title 13 px semibold, one line, chevron); centred TextButton "Other providers ▾".
- Ours (data: `GET /api/model/options` → `providers[]` with `device_login`, `configured`, `label`,
  `description`, `family`):
  - Heading `KeyRound` "Connect an account"; aside TextButton "Have an API key instead?" → Providers › API keys.
  - Intro: "Sign in with a subscription account; no key to copy." (our words).
  - One highlighted card per `device_login` provider (label + description + a neutral pill "Signed in" when
    `configured`); the card is a button: click → `openOnboarding()` (today's sign-in flow). No "Recommended"
    badge (we do not recommend; W52-B-11). When there is no device-login provider (today), one highlighted
    card "Set up a provider" / "Pick a provider and connect it in a few steps." → `openOnboarding()`.
  - Plain card "Run models locally" / "No account needed; use a model server on this computer." → Providers ›
    Local Models.
  - "Other providers" disclosure (closed by default): rows for the remaining providers (label, description,
    pill "Key saved" when configured), each with a chevron → Providers › API keys focused on that provider's
    key row (`row=` link).

API keys (`providers/keys`)
- Ref: first a plain clickable row "Local / custom endpoint" + description + chevron (→ Custom Endpoints), then a
  borderless search field (magnifier icon, 12.5 px placeholder "Search providers…", faint), then provider rows at
  64 px pitch: status dot (8 px) + provider display name (13 px medium) on the left; on the right (x 972) a
  bare control showing "Paste <Provider> key" in tertiary text. Clicking a row expands it: the bare text becomes
  a 30 px input + primary "Save" icon button, a description line and a "Get a key ↗" link.
- Ours (data: `GET /api/env` rows with `category === "provider"` and `is_password`, plus `/api/model/options`
  for display names):
  - Top row "Local or custom endpoint" / "Any OpenAI-compatible server (llama.cpp, vLLM, Ollama…)." →
    Custom Endpoints.
  - Search filters by provider label and env name (client-side).
  - Row: dot (accent when `is_set`, else the unset colour), title = provider display name (from the
    description "API key for X" → X, or the options label); the env name shown in the expanded state only, as
    12 px mono tertiary. Right side collapsed: "Paste key" placeholder text, or the `redacted_value` in mono
    when set. Expanded: SecretInput 214 px + PrimaryButton "Save" (`PUT /api/env`), and when set a TextButton
    "Remove" (`DELETE /api/env`); `url` from the row (backend sends it) → TextButton "Get a key" with
    `ExternalLink` icon (opens via the existing external-link bridge).
  - The "Other key" add form moves to Tools & Keys (§5.9).
  - Group rows by provider: several env names for one provider (GEMINI/GOOGLE) render as one row with the first
    set name; builder may keep one row per env name if grouping is unclear (note it).

Custom Endpoints (`providers/endpoints`)
- Ref: a count chip ("1", 16×16, index-chip colours), then endpoint cards (border card, radius 4, padding 12):
  name 14 px medium + accent pill "✓ Active"; URL in 12 px mono tertiary; model and key-env line in mono; right
  side an outline "⚡ Use" button and a trash icon button. Below: heading "+ Edit Endpoint" and a bordered form
  card: two-column Name / Provider ID, Endpoint URL, API Mode segmented (4 options), Default Model select +
  Context input, API Key input ("Leave blank to keep current key"), two checkboxes, then Test (outline, ⚡),
  Save (primary, save icon), "New endpoint" text button.
- Ours (data: `config.providers.<name>` entries with `base_url` — our runtime accepts `base_url`, `api_mode`,
  `key_env`, `models`, `context_length`, `display_name`, `description` per `registry.py` CUSTOM_KEYS):
  - Count chip with the number of saved endpoints.
  - Card per entry: `display_name` or name; pill "Active" (accent) when `model.provider === name` (from
    config); URL mono; second line "`<first of models | none>` · `<key_env | no key>`" mono tertiary. Actions:
    OutlineButton `Zap` "Use" → `POST /api/model/set` with that provider (check the body shape in
    `routes`; it is the same call the model picker makes) — hidden when Active; IconButton `Trash2` → removes
    the entry with `PUT /api/config {providers: {<name>: null}}` only if the backend treats null as delete;
    if not, omit delete (W52-B-12). "Test" stays on each card as today (OutlineButton, result as a 12 px note).
    Clicking a card loads it into the form ("Edit endpoint").
  - Form card (heading `Plus` "Add endpoint" / "Edit endpoint"): Name (our `name`, lower-case id rule
    unchanged) and Display name side by side; Endpoint URL; API mode Segmented "Auto-detect / Chat
    Completions / Responses API / Anthropic Messages" → `api_mode` "" / "chat_completions" / "responses" /
    "anthropic_messages"; Default model CompactSelect filled from the last Test's `models` (free text allowed)
    → `models: [<id>]` only when chosen; Context input → `context_length`; API key SecretInput (placeholder
    "Leave blank to keep the current key" when editing). Checkbox "Use for new chats" → after save, call the
    same "Use" action. No "Discover models" checkbox (we always test on demand). Buttons: OutlineButton
    `Zap` "Test", PrimaryButton `Save` "Save", TextButton "New endpoint" (clears the form).
  - Save logic stays today's (config patch, then `PUT /api/env` for the key under `keyEnvFor(name)`), plus
    the extra keys above. `SettingsOverlay.test.tsx` clicks a button named "Test" and expects "The endpoint
    offers 2 models." — keep both.

Local Models (`providers/local`, new page; W52-B-6)
- Ref: sections "Local runtime" (with a version pill), "This machine" (GPU/RAM chips), "Models 5" (catalogue
  with download buttons and red "Too big" pills). We have no runtime installer, hardware probe or model
  downloads — do not imitate them.
- Ours: section `Zap` "Local model servers"; intro "Indus talks to a model server already running on this
  computer." Then one row per local preset our backend knows (`/api/model/options` providers whose `family`
  is local / names ollama, vllm, llamacpp, lmstudio): title = label ("Ollama (local)"), description = its
  default URL in mono tertiary, pills: "Running" (accent) / "Not found" (neutral) after a Test. Actions:
  OutlineButton "Test" → `POST /api/model/test-endpoint {base_url}`; PrimaryButton "Use" (enabled after a
  passing test) → `POST /api/model/set` for that provider. Empty / error: EmptyState "No local presets" .

### 5.7 Gateways

This window (`gateways/connection`, merges today's Connection + Secure storage pages)
- Ref: caption "Connection mode" (12 px, secondary, medium), then a 4-column grid of mode cards (each ≈ 258×148,
  gap 8; icon 14 px + title 13 px medium; 3–5 lines of 12 px description; the selected card has accent tint bg,
  1 px accent border, a 2 px soft accent ring and a check mark top-right; others tinted grey with a faint
  border; a help-circle icon after the title on two cards). Under the grid, right-aligned: TextButton "Save for
  next restart" and PrimaryButton "Save and reconnect". Then a switch row "Encrypt saved secrets with the OS
  keychain" (three-line description) and a row "Diagnostics" with a TextButton `FileText` "Open logs".
- Ours (data: `bridge.connection.get()` → `mode`; `getRemote/testRemote/saveRemote/forgetRemote`; `bridge.secrets`):
  - Three cards (3-column grid, same card style; W52-B-7): `Monitor` "Local gateway" / "Run Indus on this
    computer; works offline." · `Globe` "Remote gateway" / "Connect this window to an Indus running
    elsewhere." · `SquareTerminal` "Connect via SSH" / "Reach an Indus on another machine through SSH." The
    selected card follows `connection.get().mode` (local/remote); SSH is never "current" here — clicking it
    navigates to Saved connections (where SSH entries live) instead of selecting.
  - Selecting Remote reveals, under the grid, today's URL input + SecretInput token (full width, 30 px),
    "Test" OutlineButton and the http/ws leg result line; selecting Local while a remote is saved shows the
    note "Switching back forgets the saved remote." (our words).
  - Action buttons right-aligned under the grid: PrimaryButton "Save and reconnect" → Remote: today's
    `rehome(saveRemote)`; Local: `rehome(forgetRemote)`. No "Save for next restart" (no such call).
  - Switch row "Encrypt saved secrets with the OS keychain" / "Off keeps them in private files only you can
    read. Provider keys are not held here." → today's StoragePage logic (`secrets.setPolicy`), keychain-unusable
    reason as a 12 px danger note. MANIFEST row "secure-storage" moves here.
  - Row "Diagnostics" / "Show the desktop log in your file manager." → TextButton `FileText` "Open logs" →
    `bridge.logs.reveal()` (add `logs?: IndusDesktopLogs` to `SettingsBridge`).

Saved connections (`gateways/connections`, today's `ConnectionsPage.tsx`)
- Ref: two intro lines (12 px tertiary); per connection a row: `Monitor` icon + name 13 px medium + pills
  ("Current" accent tint with accent text; "Primary", "App-managed" neutral) + one description line; right
  side OutlineButton "Test". Then OutlineButton `Plus` "Add connection" (125×24). A hairline divider (full
  width, 25 px below), then a switch row "At startup, return to Sessions on the last-used gateway" + description.
- Ours: same layout; pills from our data: "Current" when it is this window's connection, "Primary" when
  primary, kind pill "SSH"/"Remote" for non-local; description = URL or SSH `user@host` (mono only for the
  URL). Row actions keep today's (Make primary, Sign in/out, Remove) but as quiet TextButtons that show on hover
  plus an always-visible OutlineButton "Test" when the bridge has a test call for saved entries (it has
  `test(input)` for the form; if no per-entry test, omit). "Add connection" opens today's `EntryForm` inline
  under the list (bordered form card like Custom Endpoints). The launch-mode switch replaces today's checkbox:
  "Open the last working connection at launch" / "Off opens the primary connection." →
  `connections.setLaunchMode({mode})`.

Remote updates (`gateways/updates`, new page, empty state only; W52-B-7)
- Ref: centred empty state at y ≈ 185: title "Managed updates", one line. Ours: title "Remote updates",
  body "Update a remote Indus on the machine it runs on." (our words). No controls.

### 5.8 Keyboard Shortcuts

Key bindings (`keyboard/shortcuts`, today's `KeyboardPage`)
- Ref: intro line left ("Click a shortcut to rebind it · Ctrl+/ reopens this panel.") with a right-aligned
  TextButton `RotateCcw` "Reset all" (12 px tertiary); a borderless search field "Search shortcuts…"; then
  category groups: caps header (10.25 px, 600, uppercase, 0.12em tracking, faint), rows at 32 px pitch:
  label 13 px (fg at 90%), right side the key chips (each key its own 23×23+ chip, 4 px gap: "Ctrl", "Shift",
  "M", "↵", "/"…), an unbound action shows one ghost chip "set". Hover row: faint row highlight; a reset (or
  clear) icon appears at the far right on hover. Clicking the chips enters capture mode: the chip area shows
  one accent-tinted chip "Press keys…"; Escape cancels, Backspace clears.
- Ours: intro "Click a shortcut, then press the new keys. Backspace clears it; Escape keeps it." (our words,
  close to today's string) + "Reset all" → writes `{keybindings: {}}` (all defaults). Search filters labels.
  Groups (our KEY_ACTIONS): GENERAL (New chat, Open settings, Show or hide the sidebar), COMPOSER (Go to the
  message box), TABS (Close tab, Reopen the last closed tab, Next tab), CONVERSATION (Find in the
  conversation). Chips: split the accelerator on "+"; display `CommandOrControl` as "Ctrl" (⌘ on macOS via
  `facts.platform`), `Control` "Ctrl", `Shift` "Shift", `Alt` "Alt" (⌥ on mac), `Enter` "↵", `Tab` "Tab",
  letters upper-case. Capture, validation (`normalizeAccelerator`), conflicts (`conflictsFor`) and per-row
  reset stay today's logic; a conflict shows an amber warning icon before the chips with the conflict text as
  its tooltip, plus today's alert line at the top.
- Global shortcuts (quick entry, HUD snap, HUD toggle) leave this page (next page).

Global shortcuts (`keyboard/global`, new page replacing the reference's "HUD gesture"; W52-B-8)
- Ref HUD gesture page: one switch row with a two-line description. We have no tap gesture; we do have three
  system-wide shortcuts.
- Ours: three rows in the key-binding row style (same chips + capture) for "Quick entry", "Bring the HUD to the
  pointer", "Show or hide the HUD" (today's labels) writing `prefs.update({quickEntryShortcut |
  hudSnapShortcut | hudToggleShortcut})`; each with a status line from `bridge.shortcuts.get()` (registered /
  error) in 12 px tertiary or danger. Intro: "These work even when Indus is in the background."

### 5.9 Tools & Keys (new view `tools`, scoped: true; W52-B-9)

Keys (`tools/keys`)
- Ref Tools page: rows like API keys (dot + UPPER CASE NAME + bare "Paste … key" / "Optional" / "https://…"
  placeholder on the right), 64 px pitch, for every tool credential the agent knows.
- Ours (data: `GET /api/env` rows whose category is not "provider" — today "custom", i.e. keys the person
  added): same row component as API keys, title = env name with "_" → " " (as the reference shows names),
  right side "Paste key" / redacted value; expanded: SecretInput + Save + Remove. EmptyState when none: title
  "No tool keys yet", body "Add a key a tool or skill asks for." Under the list, the "Add a key" form moved from
  API keys: name input (upper-case rule `^[A-Z][A-Z0-9_]*$`, placeholder "EXAMPLE_API_KEY") + SecretInput +
  PrimaryButton "Save" (today's `save` call).
- No "Settings" child page (messaging env settings are on our Messaging page). The view therefore has one
  child; the nav still shows it as a view with one child (like Billing).

### 5.10 Sessions

Archive & retention (`sessions/archived`, today's `ArchivedPage` + `sessions.*` fields)
- Ref: switch row "Auto-archive stale chats" (two-line description), then section heading `Archive`
  "Archived sessions" with an intro line, then EmptyState "Nothing archived" / "Archive a chat to hide it
  here." centred ≈ 90 px lower. With items: rows (title, relative date) with "Restore" and a delete icon.
- Ours: first two schema rows (claim `sessions.retention_days`, `sessions.auto_prune` here with `fields`):
  `sessions.auto_prune` "Delete old sessions" / "Remove ended sessions after the retention period. Pinned and
  archived ones follow the same rule." — builder must confirm the second sentence against the pruning code or
  drop it · `sessions.retention_days` "Keep sessions for (days)". Then SectionHeading `Archive` "Archived
  sessions", intro "Archived chats are hidden from the sidebar and keep all their messages." Then the list
  (today's `GET /api/sessions?archived=only&limit=100`): SettingRow title = session title, control =
  OutlineButton "Restore" (today's PATCH). EmptyState "Nothing archived" / "Archived chats show up here."
- More: `sessions.resume_message_limit`, `sessions.search_slow_ms`.

Default project folder (`sessions/directory`)
- Ref: intro line; one row: title = the folder path or "Not set"; description "Defaults to <home path>"; right
  side TextButton `FolderOpen` "Choose" (and "Clear" when set).
- Ours: the per-profile default directory (`store.setDefaultDirectory`, the sidebar project store used by
  Workspace › Desktop today; W52-B-13). Row title = current value or "Not set"; description "New sessions start
  here; empty uses the profile's working folder (<terminal.cwd value>)." Control: TextButton "Change" that turns
  the title into a 30 px full-width input (Enter saves, Escape cancels) — we have no native folder picker in the
  bridge; TextButton "Clear" when set. The `terminal.cwd` `also` on this page goes away (it stays on Workspace ›
  Projects).

### 5.11 Plugins (new top-level view `plugins`, single page `main`)

- Ref: no breadcrumb child (breadcrumb "Settings › Plugins"); a right-aligned OutlineButton "Manage plugins →"
  at y 118, intro line, centred "No plugin has settings yet." (13.5 px medium) ≈ 110 px lower.
- Ours: today's `advanced/plugins` content (desktop plugin files from `bridge.plugins.list()`), restyled:
  right-aligned OutlineButton `RefreshCw` "Check again" (rescan) in the reference's button slot; intro "Plugins
  add pages, panes and sidebar entries. They run with the app's full rights; turn on only plugins you trust."
  (shorten of today's own text). Rows: plugin file name 13 px medium + its state as description, Switch on the
  right if today's page toggles them (keep exactly today's controls). EmptyState "No desktop plugins
  installed."
- The nav shows Plugins with no chevron and no children (`ViewDef.pages.length === 1` + a `flat: true` flag the
  shell reads; tell W51-b).

### 5.12 About

Version & updates (`about/version`)
- Ref: centred hero (logo art 56 px, name 18 px semibold, "Version …" 12 px tertiary), then a 672 px centred
  column (x 488–1159): SectionHeading `RefreshCw` "Updates"; an update status card (danger card when the check
  failed: red title 14 px medium, message, "Last checked 14 min ago", footer with TextButton `RefreshCw` "Check
  now" left and TextButton `ExternalLink` "Release notes" right); then a facts table card (rows 28 px: label
  tertiary left, value right; mono for commit/install id, accent for the commit link).
- Ours:
  - Hero: the foundation's `Wordmark` ("INDUS AGENT", no logo art — no reference assets) at ~28 px, then
    "Indus Agent" 18 px semibold, then "Version <appVersion>" 12 px tertiary.
  - Column 672 px centred. SectionHeading `RefreshCw` "Updates".
  - Status card from `IndusUpdateStatus`: idle → neutral card "You're up to date" + "Last checked <relative
    checkedAt>"; checking → "Checking…"; available/downloading → accent-tint card "Version <version> is
    available" + progress bar when `progress`; ready → PrimaryButton "Restart to update" (`updates.apply`);
    error → danger card, title "Couldn't check for updates", body `error`; manual → neutral card with
    `manualHint` in mono and `downloadUrl` as TextButton "Download". Footer TextButton "Check now"
    (`updates.check`). No release-notes link unless the status carries a URL (it does not).
  - Row (inside the column) "Update channel" CompactSelect Stable / Canary (moved from Advanced › Desktop).
  - Facts card: "App version", "Indus runtime" (`connection.get().codeVersion`), "Update channel",
    "Install type" (`uninstall.summary().installKind`), "Indus home" (`summary.home`, mono, wraps).
  - Diagnostics (below the facts, same column): SectionHeading `FileText` "Desktop log" with aside
    OutlineButtons "Show log file" and "Copy diagnostics" (today's), then today's `LogsViewer` collapsed behind
    a TextButton "Show recent lines" (W52-B-14).

Uninstall (`about/uninstall`, new page; today's `UninstallSection` moved out of the version page)
- Ref: danger card in the 672 px column at y 99: title "Uninstall Hermes" 14 px medium, one line 12 px, then
  option cards inside (inner danger card, `Trash2` icon + title 14 px medium + one line), hover deepens the
  border. Clicking an option swaps the card content for a confirm state (danger title, consequence text,
  destructive "Yes, uninstall" + text "Cancel").
- Ours: same card; title "Uninstall Indus Agent"; options from `summary.levels`: gui "Remove the app only" /
  "Your agent, settings and chats stay.", keep-data "Remove the app and runtime" / "Your settings and chats
  stay.", full "Remove everything" / "Deletes the app, runtime and all data." (adapt to today's strings, our
  words). Confirm state keeps today's flow: one confirm for gui/keep-data, typed phrase (`confirmPhrase`) for
  full. When `allowed` is false, the card shows the reason only (today's test "a Nix-owned receipt hides all
  options and shows why"). `UninstallSection.test.tsx` must keep passing (keep its button names and texts or
  update deliberately).

Licences (`about/licences`, kept): restyle with `PageBody`; the notices text in a scrollable mono 12 px block
on the facts-card surface.

## 6. Files to change

- `apps/desktop/src/settings/pages.ts` — new views/pages per §2, `group`/`flat` fields, curated `fields` +
  page-level `more` lists (or a `curated: string[]` field: rows in `curated` render first, the rest of the
  page's claim under More), MANIFEST moves/renames (wake-word, update-channel, quick-entry, notify-*, storage,
  uninstall, archived-sessions, default-directory, keybindings).
- `apps/desktop/src/settings/primitives.tsx` (new), `fieldCopy.ts` (new), `settings.css` (page-body rules only;
  shell rules are W51-b's), `schemaPage.tsx` (use primitives, curated + More, no key line).
- `apps/desktop/src/settings/customPages.tsx` — split by area to keep files under the size guard: new
  `pages/notifications.tsx`, `pages/providers.tsx` (accounts, keys, endpoints, local), `pages/gateways.tsx`,
  `pages/keyboard.tsx`, `pages/sessions.tsx`, `pages/about.tsx`, `pages/advanced.tsx`, `pages/voice.tsx`,
  `pages/billing.tsx`, `pages/tools.tsx`, `pages/plugins.tsx`; `customPages.tsx` keeps the `CustomPage` switch.
  New `CustomPage` ids: `voice-wake` (rows appended to the schema page), `notifications-sounds`, `billing`,
  `providers-local`, `gateways-updates`, `keyboard-global`, `tools-keys`, `sessions-directory`, `about-uninstall`.
- `apps/desktop/src/settings/context.tsx` — `SettingsBridge` gains `shortcuts`, `notifications`, `logs`,
  `connections`, `plugins` (whatever the moved pages use); context gains `go(target: {view, page, row?})` so
  pages can link to other pages (overlay implements it with its existing target state).
- `apps/desktop/src/settings/SettingsOverlay.tsx` — pass the extra bridge groups and `go` (coordinate with W51-b).
- `apps/desktop/src/settings/ConnectionsPage.tsx`, `UninstallSection.tsx`, `LicencesPage.tsx` — restyle.
- `apps/desktop/src/settings/strings.en.ts` + every pack under `src/i18n` (`packs.test.ts` checks key parity):
  new labels/descriptions; remove keys no longer used.
- `apps/desktop/src/settings/keybindings.ts` — add a `group` per action and an accelerator → chip-token helper
  (`acceleratorKeys(accel, platform): string[]`), unit-tested.
- Capture kit (copy in `$S`): new hashes for the new pages and renamed slugs in §2.

Tests affected (update only where they pin text/markup we deliberately change):
`settings/search.test.ts` (views list, billing fallback, field ownership for terminal/cwd/directory),
`settings/SettingsOverlay.test.tsx` (switch name, Test button, licences, endpoint text), `schemaPage.test.tsx`
(control kinds: switch instead of checkbox — it asserts roles; keep roles), `UninstallSection.test.tsx`,
`keybindings.test.ts`, `i18n/packs.test.ts`, `e2e/core/surfaces.electron.test.ts` (opens
`view=appearance&page=window` only — unaffected). New tests: primitives (switch/segmented roles and keyboard),
curated + More ordering and auto-open on a deep link, notifications master switch writes both prefs, sounds
select ↔ `sounds` pref, chip tokens per platform, billing totals formatting, Local Models test→use flow with a
fake REST, keys row expand/save/remove.

## 7. Build order

1. Primitives + `fieldCopy.ts` + schemaPage restyle (curated + More, no key line) — unlocks Memory, Voice,
   Advanced schema pages at once. Coordinate primitives with slice A first.
2. `pages.ts` restructure + context `go` + extra bridge groups; fix `search.test.ts`.
3. Memory (2), Advanced (6 incl. Profiles), Voice (3).
4. Notifications (2), Sessions (2), Plugins, Keyboard (2).
5. Providers (4), Tools & Keys, Billing.
6. Gateways (3), About (3).
7. i18n packs, tests, capture both themes, PDF, close gaps.

## 8. Compare checklist (tick in Compare, both themes)

- [ ] settings-memory-persistent ↔ settings-memory-and-context-persistent-memory
- [ ] settings-memory-context ↔ settings-memory-and-context-context-and-compression
- [ ] settings-voice-conversation ↔ settings-voice-voice-conversation
- [ ] settings-voice-transcription ↔ settings-voice-speech-to-text
- [ ] settings-voice-speech ↔ settings-voice-text-to-speech
- [ ] settings-advanced ↔ settings-advanced (segmented keep-awake, quick entry switch + shortcut, Run again)
- [ ] settings-advanced-runtime ↔ settings-advanced-agent-limits
- [ ] settings-advanced-tools ↔ settings-advanced-tool-access
- [ ] settings-advanced-terminal ↔ settings-advanced-terminal-backend
- [ ] settings-advanced-delegation ↔ settings-advanced-subagents
- [ ] settings-advanced-output ↔ settings-advanced-output-limits
- [ ] settings-notifications-alerts ↔ settings-notifications-desktop-alerts (switches, test button, footnote)
- [ ] settings-notifications-sounds ↔ settings-notifications-sounds (select + Preview)
- [ ] settings-billing-overview ↔ settings-billing-overview (banner + 3 stats)
- [ ] settings-providers ↔ settings-providers (heading + link, highlighted card, local card, Other providers)
- [ ] settings-keys ↔ settings-keys (top endpoint row, search, dot rows, bare paste control)
- [ ] settings-providers-endpoints ↔ settings-providers-custom-endpoints (count chip, card, form card)
- [ ] settings-providers-local ↔ settings-providers-local-models (section heading, preset rows; deliberate content difference)
- [ ] settings-gateways-connection ↔ settings-gateways-this-window (mode cards, keychain switch, Open logs)
- [ ] settings-gateways-connections ↔ settings-gateways-saved-connections (pills, Add connection, divider, launch switch)
- [ ] settings-gateways-updates ↔ settings-gateways-remote-updates (empty state)
- [ ] settings-shortcuts ↔ settings-shortcuts (Reset all, search, caps groups, kbd chips, ghost "set")
- [ ] settings-keyboard-global ↔ settings-keyboard-shortcuts-hud-gesture (deliberately different content)
- [ ] settings-tools-keys ↔ settings-tools-and-keys-tools (row style)
- [ ] settings-sessions-archived ↔ settings-sessions-archive-and-retention (switch, heading, empty state)
- [ ] settings-sessions-directory ↔ settings-sessions-default-project-folder (path title, Change/Clear)
- [ ] settings-plugins ↔ settings-plugins (button slot, intro, centred empty state; no nav chevron)
- [ ] settings-about ↔ settings-about (hero, Updates card, facts card)
- [ ] settings-about-uninstall ↔ settings-about-uninstall (danger card, option card)
- [ ] settings-licences (no reference; consistent with About styling)
- [ ] Nothing in any page body shows a schema key line, bold 15 px label, native checkbox or native select.
- [ ] Search "keychain", "quick entry", "sounds", "archived", "uninstall" each lands on the new page and
      highlights the row.
- Not built: settings-keyboard-shortcuts-screen-capture (reference page is blank), settings-tools-and-keys-settings.

## 9. Decisions (for the lead; labelled W52-B-n)

- W52-B-1: page bodies drop the monospace schema-key line (kept as the row's `title` tooltip) to match the
  reference's clean rows.
- W52-B-2: schema pages show a curated, reference-like set of rows first and keep every other field of the page
  behind "More settings", so no setting becomes unreachable and search still finds them.
- W52-B-3: field moves between slices listed in §1 (terminal backend/timeout, tool output, code execution,
  voice) — slice A must apply the matching removals.
- W52-B-4: new views Voice, Billing, Tools & Keys and a top-level Plugins view; Gateways › Secure storage
  merges into This window; Uninstall gets its own About page.
- W52-B-5: Billing shows usage estimates from `/api/analytics/usage` and a "billing is with your providers"
  banner — no account, balance or plan (we have none).
- W52-B-6: Local Models lists our local server presets with Test/Use instead of the reference's runtime
  installer and model catalogue (no such backend).
- W52-B-7: Gateways has three mode cards (no Cloud); SSH routes to Saved connections; Remote updates is an
  empty state; no "Save for next restart".
- W52-B-8: the reference's HUD gesture page becomes "Global shortcuts" (our three system-wide shortcuts);
  Screen capture is not built.
- W52-B-9: Tools & Keys has one page listing non-provider stored keys plus the add form; messaging settings stay
  on the Messaging page.
- W52-B-10: the notifications master switch is a UI composition over the two existing prefs; kinds we cannot
  detect are not shown.
- W52-B-11: no "Recommended" badge on provider cards.
- W52-B-12: endpoint delete only if `PUT /api/config` with `null` deletes the entry (builder verifies in the
  config merge code); otherwise no trash button.
- W52-B-13: Sessions › Default project folder shows the sidebar store's default directory (the same value
  Workspace › Desktop edits); the MANIFEST row "default-directory" moves here and Workspace links to it — slice A
  to agree.
- W52-B-14: the desktop log viewer stays on About but collapsed, below the reference-style facts card.

## 10. Gaps (backend/bridge data we do not have; never faked)

Voice chat mode / GPT-live / client-direct; STT master switch, echo and live transcription; DevTools block,
external-link switch, update local-changes policy; notification kinds beyond reply/approval; sound presets
beyond one chime; billing account, balance, plan, auto-refill; local runtime install, hardware probe, model
downloads; Hermes-Cloud-style sign-in; save-for-next-restart; managed remote updates; HUD tap gesture; screen
capture shortcuts; tool-key catalogue (only stored keys are listed); native folder picker; release notes URL;
commit / build origin / install id facts.
