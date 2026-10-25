# Task: Desktop command center, cron, profiles, agents, artifacts, webhooks, learning map, session import, layout editor (W52-l) Phase: ui-parity Owner: builder w53l

Status: Built for wave 53 (2026-10-24), 13 of 14 slugs done, layout-editor partial; see Status at the end.

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): via the wave-52 spec
`.agents/wip/w52/specs/desktop-command-center.md` (Read + Note done by the spec writer from
`app/overlays/*`, `app/command-center/*`, `app/session-import/*`, `app/cron/index.tsx`,
`app/profiles/index.tsx`, `app/agents/index.tsx`, `app/artifacts/index.tsx`, `app/webhooks/index.tsx`,
`app/starmap/*`, `app/page-search-shell.tsx`, `components/ui/{button,segmented-control,text-tab,tab-dropdown,empty-state}.tsx`,
`components/chat/log-tail.tsx`, `components/pane-shell/tree/renderer/{edit-bar,layout-picker,tree-group}.tsx`);
this builder did not open reference source. Reference screenshots are not available in this container
(wave 53): the spec's measured numbers (px, hex) are the look reference.
Evidence: none (spec numbers only).

What it does (own words): the management surfaces of the desktop app take the reference's structure.
Command center, session import, scheduled jobs, profiles, agents, webhooks and the learning map open as
one full-window overlay card over the route they were opened from (paths unchanged); the command center
has a 208px tinted rail (Sessions, System, Usage, Maintenance) and a breadcrumb; the others use a
header + 208px searchable list + detail panel with centred empty states. Controls are quiet: underlined
text actions, text tabs, a small segmented control, filled blue only for the main action; tables become
two-line rows. Artifacts stays a page with centred text tabs and counts. Behaviour and data flows stay ours;
data our backend lacks is shown disabled or as a placeholder (listed as follow-ups), never invented.

Inputs → outputs: the existing REST routes (`/api/sessions*`, `/api/daemon*`, `/api/status`, `/api/logs`,
`/api/analytics/usage`, `/api/ops/*`, `/api/memory`, `/api/session-import/*`, `/api/cron/*`,
`/api/profiles*`, `/api/webhooks*`, `/api/learning/graph`, `/api/skills*`) and the bridge's `logs.recent` →
the overlays described above.

Edge cases / failure modes: a 409 from the maintenance gate shows who holds it; a profile that fails in
"All profiles" cron is named; the webhook secret lives only in component state; Esc closes a nested
dialog before the overlay; load errors keep a Retry.

Our design differences / decisions: see "Decisions" below.

## Compare checklist (from the spec §6; ticked after Compare)
- [x] All overlay slugs: card inset 48px, radius 12px, 1px #c8d5f1 / #232f47 border, X at top-right on the titlebar
      line, Esc and backdrop close, dimmed+blurred app behind.
- [x] command-center: 208px rail with 4 icon items (active boxed fill + border), breadcrumb "Command Center › Sessions",
      borderless search right, two-line session rows (13px title / 12px date), hover pin/export/delete, no stats line.
- [x] command-center-system: green dot + gateway line + version/active-sessions caption, text actions "Restart
      gateway" / "Update" right, "RECENT LOGS" label with file tabs + level tabs + log search on one row, tinted log well
      with amber rule on WARNING lines, mono 11px.
- [x] command-center-usage: 7d/30d/90d segmented right of the breadcrumb, 3 stat tiles (SESSIONS / API CALLS "—" /
      TOKENS IN/OUT with a cost hint), DAILY TOKENS label + legend + 96px bar chart + end dates, TOP MODELS / TOP SKILLS columns with empty lines.
- [x] command-center-maintenance: uppercase section labels, title+description rows with underlined text actions at the
      right, memory rows with sizes/"empty", action log well when something runs, no grey buttons.
- [x] session-import: 30px heading, subtitle, meta row with two icons, divider, tinted left column with the 3-way
      segmented control and empty text, right empty state with chat icon; selection → preview + footer button.
- [x] cron: header "Scheduled jobs" + count, search, "No scheduled jobs yet", "+" row, BLUEPRINTS list with rocket
      icons, right empty with watch icon and blue "New cron".
- [x] cron-new-dialog: 512px dialog, Start from / Name (Optional) / Prompt (mono placeholder) / Frequency + Deliver to
      (checkbox box) / Model (Optional, disabled) / summary well with cron expression / Cancel + Create cron.
- [x] profiles: header + count, list with home icon row (active fill), detail name + "Default" pill, model meta in mono,
      PERSONA.MD label + description, framed mono editor, right-aligned save button disabled until dirty.
- [x] agents: centred robot icon + "No live subagents" + description, nothing else.
- [x] artifacts: page (not overlay), centred text tabs with counts, refresh icon right, centred empty title + text.
- [x] webhooks: blue-tinted banner with icon, title, body, "Enable webhooks" button; centred globe empty + disabled
      "New subscription"; after enabling, restart banner.
- [x] starmap: centred lightbulb + "Nothing learned yet" + description; with data, graph fills the card with floating
      timeline, legend, share buttons.
- [x] empty-state: sidebar RESULTS label in primary blue, one clear button, `No sessions match “…”.` in 12px tertiary.
- [~] layout-editor (partial: card, mode cards, 5 template miniatures, disabled CUSTOM, Reset/Done, kbd hint, dashed zones; no per-zone grip chips): dashed zones with grip chips, floating 416px "Layouts" card with kbd hint, Reset/Done, interface
      mode cards, 5 template miniatures, CUSTOM section (disabled actions).
- [x] Dark theme: rail #010409, card #0d1015, dialog #15171c, primary #4a84fe with dark text on filled buttons.


Compared against the spec's numbers with our own captures (`$S/w53l-shots`, 15 pairs light + dark,
kit copy `$S/w53l-kit/cc.ts`); the reference screenshots were not available in this container.

Files: `app/overlay/kit.tsx` + `kit.css` (new: TextAction, CapsLabel, TextTabs, QuietSearch, StatTile,
LogTail, PanelRow, PanelPill, MetaGrid, PanelBlock, DetailHead, Banner, InlineError, Field),
`app/overlay/OverlayFrame.tsx` / `overlay.css` (OverlayCard `titlebarActions`, `surface`, `id`, card
focus on open; ListDetail reshaped to the panel: subtitle, banner, listTop/listEmpty/listFooter,
icon-only "+" row, `whole` empty), `ui/components.tsx` / `ui.css` (EmptyState `description` + `look="panel"`,
SmallDialog `size="wide"` + `closeLabel`), `app/command-center/{CommandCenter.tsx,commandCenter.css,strings.en.ts}`,
`app/session-import/*` (new), `app/cron/{CronOverlay.tsx,CronDialog.tsx,cron.css}`,
`app/profiles/{ProfilesOverlay.tsx,profiles.css}`, `app/webhooks/{WebhooksPage.tsx,webhooks.css}`,
`app/agents/{AgentsOverlay.tsx,agents.css}`, `app/learning-map/{LearningMap.tsx,learningMap.css}`,
`app/artifacts/{ArtifactsPage.tsx,artifacts.css}`, `panes/{LayoutEditor.tsx,layoutEditor.css}`, and the
shared `app/routes.ts`, `app/pages.tsx`, `app/Shell.tsx`, `app/manage/{managementSlots.tsx,common.tsx}`,
`panes/PaneShell.tsx`, `sidebar/{Sidebar.tsx,sidebar.css,strings.en.ts}`, `i18n/en.ts` (one spread).

## Decisions

- W52-l-1: command center, webhooks, learning map and session import are overlays (reference
  structure); paths unchanged. The command center keeps its section in `?section=`. The footer's
  "More" menu lists Webhooks, Learning map, Import a conversation, Profiles, Agents; the palette lists
  them with the other overlays.
- W52-l-2: log levels are the reference's all / info / warning / error plus our debug as a fifth text
  tab; 200 lines per read (was 500).
- W52-l-3: Usage keeps an "API CALLS" tile with "—" (not counted yet) and shows our cost as the
  tokens tile's hint ("0.15 USD estimated"); top models / top skills show their empty lines.
- W52-l-4: Maintenance rows in the reference style: Diagnostics (doctor, backup live; security audit
  and debug share disabled "Not available yet"), Sessions (delete empty with the stats line, prune old
  sessions with an inline 64px days field, prune checkpoints, "Import from another app" → Open),
  Skill curator (muted "Unavailable" pill, actions disabled), Memory data (sizes from `/api/memory`,
  reset actions disabled — no reset route). Actions tail `/api/actions/{name}/status` every 1.2s in a
  compact log well; a 409 shows "Not now: …". Session export is a disabled icon (no bridge save path).
- W52-l-5: Cron keeps "All profiles" (ours) as a small checkbox under the search, with the owner as
  row meta; the dialog's Model select is disabled with "Default (global model)" (no per-job model);
  deliver targets are checkboxes joined with commas; our free-text schedules stay valid via Custom.
- W52-l-6: Learning map keeps the force layout (no radial time rings); filters as text tabs, the
  playback slider floats top-centre, Share / Load code are icon buttons; a loaded code shows in place
  with "Back to this map".
- W52-l-7: Layout editor: the "Layouts" card opens from a new band button ("Edit layout") or
  Ctrl+Shift+\ and dashes/blurs the zones; the band's preset select and Advanced/Simple radios stay
  (W51-b-9, e2e and the existing tests use them) instead of being replaced; "New grid layout" and
  "Save current arrangement" are disabled (no custom templates in our store); per-zone grip chips are
  not built (panes still move by tab drag and drop).
- W52-l-8: shared parts were added to the foundation's files (EmptyState, SmallDialog, OverlayCard,
  ListDetail) and the overlay kit lives in `app/overlay/kit.tsx` beside the frames; `ManageOverlay`
  retired (no users left; `PageFrame`/`LoadState` stay for Messaging).
- W52-l-9: session import previews messages as plain pre-wrapped text (no markdown renderer in the
  overlay), first 60 messages with a note; "Reading from <host>" uses the remote connection's host.
- W52-l-10: sidebar search no-match is a plain 12px caption with a period, no icon.
- W52-l-11: Profiles: "Skills" shows "—" for every profile (capabilities.summary not wired here);
  path line and ".env" pill omitted (no data).

## Integration (wave 53, desktop k + l)
Merged with slice k on f3f7772. Ids: W52-l-1 … W52-l-11 = D-16.1257 … D-16.1267; integration D-16.1268 (one
control kit). The overlay kit moved from `app/overlay/kit.tsx` to `app/kit.tsx` (+ `kit.css`) and is shared with
the Capabilities / Messaging pages: one `TextTabs` (default panel row; `look="page"` for page headers), one
`QuietSearch` (the foundation `ListDetail` list search uses it too), one `PanelPill` (tones renamed: the soft
primary "good" is now `info`, the green "ok" is now `good`), one `MetaGrid` (`framed` variant), one `DetailHead`.

## Status

Done: command-center (4 sections), command-center-system, -usage, -maintenance, session-import, cron,
cron-new-dialog, profiles, agents, artifacts, webhooks, starmap, empty-state. Partial: layout-editor
(see W52-l-7). Not re-checked against reference pixels (no reference screenshots in this container).

Follow-ups (backend, placeholders in this slice): usage API-call count, per-model and per-skill usage;
security audit, debug share, skill curator status/pause/run, memory file reset; session export save
path through main; session-import per-candidate message count, "already imported", paging; cron
per-job model; profile home path, skill count, `.env` flag; webhook skills per route; saved custom
layout templates and "new grid layout"; per-zone grip chips in layout edit mode; markdown in the
import preview; artifacts paging (client-side pager not built, counts only).
