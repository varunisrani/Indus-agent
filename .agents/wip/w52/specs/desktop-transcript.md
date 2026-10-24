# W52 spec: desktop-transcript (desktop surface)

Status: spec ready for the wave-52 builder. Written by a Read+Note spec writer on 2026-10-24. No code was written.
Read: reference screenshots `$SP/compare/hermes/desktop/{chat-conversation, chat-streaming, tool-row, tool-row-expanded,
tool-row-rejected, approval-card, error-state, notifications-toast, usage-stats-prompt, no-provider-chat}--{light,dark}.png`
(manifest.json titles and notes). Our pairs are in `$SP/compare/indus/desktop/` (no `tool-row-rejected`, `usage-stats-prompt`
or `no-provider-chat` capture yet). Reference source read for structure and behaviour only: apps/desktop/src
`components/assistant-ui/thread/assistant-message.tsx` (error card), `components/assistant-ui/tool/fallback.tsx` (tool rows),
`components/assistant-ui/tool/approval.tsx` + `lib/keybinds/approval-keys.ts` (approval), `components/notifications.tsx`
(toasts), `components/assistant-ui/thread/status.tsx` (turn activity), `components/shared-metrics/consent-strip.tsx`,
`components/chat/scaffold-row.tsx`, `styles.css` (conversation size variables). Nothing below is copied: values are
measured from the 1440x900 PNGs (python stdlib PNG decode) or described in our words.

`$SP` = `/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad`.

Depends on wave 51b (desktop foundation): tokens, shell, sidebar, title bar/tab strip, status bar, composer shell,
new-chat wordmark, base transcript styling. This slice owns the **contents of the transcript column** (user prompt
row, assistant prose and markdown blocks, tool rows, turn activity, approval card, error card), the **toast** surface,
the **composer notice strip** and the **no-provider** status. Composer menus/pickers belong to the composer slice; only
the Stop state of the send button is listed here because the streaming shot shows it.

---

## 0. Shared measurements (all slugs)

Window 1440x900. Left sidebar 0..260 (foundation). Title/tab strip 0..34 (bg = sidebar bg). Status bar 880..900.
Transcript column = x 260..1440, bg `--chat-bg`.

| Token (proposed name; reuse the foundation's if it already has one) | Light | Dark | Where sampled |
|---|---|---|---|
| `--chat-bg` main transcript background | `#fefefe` | `#0d1015` | chat-conversation (800,600) |
| `--chrome-bg` sidebar / tab strip / status bar | `#f6f8fa` | `#010409` | (130,600), (800,10) |
| `--fg` prose text | `#2c3035` | `#d8e0e6` | darkest/brightest ink of reply text |
| `--fg-secondary` (approval label, toast title) | `#595c60` | `#adb4b9` | approval "Command" label |
| `--fg-tertiary` / scaffold text (tool rows, activity line) | `#9e9fa2` | `#696e73` | tool row label |
| `--fg-faint` (list numerals, carets, dismiss X) | `#a8abae` | `#53585d` | caret / X ink |
| `--accent` | `#0053fd` | `#4a84fe` | tab underline, Run button, running dots |
| `--on-accent` (text on accent) | `#ffffff` | `#161616` | Run label (dark theme text is dark) |
| `--stroke-prompt` user prompt border | `#d8e1f2` | `#1e2a40` | prompt box edge |
| `--prompt-bg` user prompt fill | `#fcfcfc` | `#0f1621` | inside prompt box |
| `--stroke-card` approval card / composer border | `#c8d5f1` | `#232f47` | approval card edge |
| `--stroke-subtle` tool shell, table rules | `#e5ebf7` (table `#dae3f4`) | `#171e2a` (table `#1d2535`) | expanded tool shell edge |
| `--surface-code` code block fill | `#fbfbfc` | `#0e0f12` | code block |
| `--surface-inset` terminal command box fill | `#f4f5f9` | `#12161d` | expanded terminal box |
| `--stroke-inset` command box border | `#dde5f3` | `#1b2331` | |
| `--surface-chip` exit badge fill, secondary button | `#e1e6f3` / button `#e9edf6` | `#1b222f` / button `#171d28` | exit badge, "Always allow" |
| `--success` (exit 0) | `#1f9e7a`-ish (ink `#62bea1` antialiased) | `#0b8462` | exit badge text |
| `--warning` (approval waiting dot; non-zero exit) | `#fe9a00` | `#fe9a00` | tab/session dot in approval shot |
| `--destructive` (derive card tints from it) | ≈ `#c8040f` | ≈ `#f8514a` | solved from the 35%/7% mixes below |
| error card border (35% destructive over bg) | `#eba7ab` | `#692828` | |
| error card fill (7% destructive over bg) | `#fbeeef` | `#1e1418` | |
| error card text (destructive mixed toward secondary) | `#b62e38` | `#e86761` | |
| error pill border (30%) | `#f0bec2` | `#502122` | |
| rejected tool row ink (destructive at scaffold fade) | `#de6a72` | `#aa3b37` | |
| toast fill | `#fdfdfd` | `#15171b` | |
| toast edge | `#f4f4f4` (+ shadow) | `#1a1d21` | |
| notice strip fill / border | `#fbfbfc` / `#dfe4e8` | `#0e0f13` / `#23282d` | usage strip |
| scrollbar thumb (thin, ~6 px, at x 1432..1438) | `#e4e4e4` | `#212428` | error-state |

Typography (font = the foundation's sans; measured cap heights, Inter-like metrics):
- Prose and user prompt: **15 px**, weight 400, line-height ~20 px. Bold = 600, italic real italic.
- Markdown h2/h3 ("Shot list"): ~16–17 px, weight 600, no extra top margin when first in the reply.
- Table text 14 px; header weight 500.
- Code (block and inline): mono 13 px, line-height 20 px.
- Scaffold lines (tool rows, activity line, "Running …"): **12 px**, weight 400, `--fg-tertiary`.
- Scaffold meta (durations, timers): 10 px tabular numerals.
- Error card: 12.5 px; title weight 500; details 11.5 px.
- Approval card: label 12 px, command mono 12 px; buttons 13 px weight 500.
- Toast: 12 px. Notice strip: 11.7 px.

Radii are small everywhere (the reference runs a near-square scale): prompt box 3 px, buttons 2 px, approval card 3 px,
error card 3 px, toast 4 px, table 4 px, cmd box 4 px, expanded tool shell 5 px, code block 6 px, composer 6–7 px,
notice strip top corners 5 px, error action pills fully round.

Horizontal rhythm inside the transcript column (x measured in the window):
- Content gutter 24 px each side: prompt box spans x 284..1416.
- Assistant prose, tool rows, code, tables, error card are indented **12 px** more (x 296), right edge 1416
  (1408 when the scrollbar shows). Use one `--message-indent: 12px` variable.
- Approval stack is **not** indented (x 284) and is max **576 px** wide (284..860).
- No max reading width: prose and tables run the full column.

Vertical rhythm: prompt box → first reply block 6 px; between prose blocks ~12 px; between back-to-back scaffold rows
4–6 px; prose ↔ scaffold 12 px.

---

## 1. `chat-conversation` — finished markdown reply

Layout (from top of the transcript column, y in window px):
- Prompt row: bordered box x 284..1416, y 43..81 (38 px). 1 px `--stroke-prompt`, fill `--prompt-bg`, radius 3, padding
  8 px 12 px. Text 15 px `--fg`. It is **sticky**: pinned at the top of the scroller while its answer scrolls under it
  (ours already sticks `.turn > .msg-user`; keep). Long prompts clamp to ~4 lines with a bottom fade; click expands.
- Right end of the prompt row, shown on hover/focus only (visible in the capture because the pointer was there): time
  "1:04 PM" (11 px, `--fg-faint`) and a 14 px "undo/restore" icon button. See Mapping for data.
- Reply (x 296..1416): heading → paragraph → ordered list (numerals in `--fg-faint`, item spacing 29 px, number column
  ~20 px) → code block (x 296..1416, y 257..317, fill `--surface-code`, no border, radius 6, padding 10 px 12 px, syntax
  colours: keyword red-pink, function purple, number blue — light and dark use the theme's syntax tokens) → table.
- Table: x 296..1416, 1 px border `--stroke-subtle` (table variant), radius 4, header row 36 px with a bottom rule,
  body rows 31 px, **no inner vertical rules, no zebra**, cell padding ~10 px horizontal. Columns size naturally (here
  split at ~x 836).
- Timeline rail: a short 8 px grey dash at the right edge (x ~1426..1433, vertically centred) per prompt; the active
  prompt's dash is accent. Ours hides the rail below 2 prompts — show it from 1 prompt.
- No avatar, no "You/Assistant" labels, no bubble around the reply.

Component inventory: prompt box (text, hover time, hover restore button); markdown blocks (h, p, ol/ul, inline code,
code block with a copy button on hover top-right, table, links in accent); timeline rail.

States: hover on prompt row shows time + restore; hover on code block shows a 20 px copy icon at top-right.

## 2. `chat-streaming` — reply in flight

- Same prompt row (no hover controls).
- Partial reply text ("Hello") at x 296, 15 px.
- **Turn activity row** directly under the streamed text, x 296, y ~121..133: a 12x12 px square drawn as a fine
  checkerboard/dither in `--accent` at ~50% (light pixels `#75a1fd`, dark `#2d4e91`), radius 2, then the elapsed timer
  "9s" (10 px, accent at ~35%: `#a1c0fe` light). It replaces our `<Loader size="sm">` spinner and shows for the whole
  running turn (not only before the first token). Optional label text after the timer uses the scaffold style with a
  left-to-right shimmer.
- Composer send button becomes **Stop**: 26 px round, fill `#2c3035` light / `#d8dfe6` dark, with a 10 px square glyph
  in the chat bg colour. (Composer slice owns the button; this slice only needs the state to be correct.)
- Tab dot and the sidebar session-row dot turn `--accent` while running (grey `#a8abae` / `#53585d` when idle,
  `--warning` `#fe9a00` while an approval waits). The dots are 6 px; foundation renders them, we supply state.
- Timeline dash for the active prompt is accent.

## 3. `tool-row` — collapsed

- One line at x 297, height ~20 px, directly under the prompt (y center 97). No box, no border.
- Leading glyph cell 14x14: the **tool kind icon** in `--fg-tertiary` (terminal = a small rounded square with a prompt
  chevron; file read/write = file icon; search = magnifier; fetch = globe; other = wrench). **Success shows no check
  mark** — the kind icon stays. Running = small spinner in the same cell. Failure = alert-circle in destructive.
- Label (12 px, `--fg-tertiary`, sans, not mono): verb + target in plain words, e.g. "Ran echo scene-01.fountain
  scene-02.fountain". Target is part of the sentence, not a code chip. While running the target part shimmers.
- Trailing items in the same line (all 10 px `--fg-faint`, tabular): diff stats for edits (+N green, −N red),
  duration after finishing (optional; reference hides it for terminal rows), live timer while running.
- Disclosure caret: hidden at rest (opacity 0), fades in on hover right after the label; rotates when open.
- Dismiss X (12 px) appears on hover at the far right once the turn has settled; clicking hides that row for this
  view only (pure client state).
- The whole line is the toggle button (keyboard: Enter/Space; `aria-expanded`).
- Spacing: the next prose block starts ~12 px below.

## 4. `tool-row-expanded`

- The row gains a shell: 1 px `--stroke-subtle`, radius 5, x 296..1416 (y 88..167 in the shot).
- Header strip (the same line as collapsed) gets 8 px/6 px padding and a bottom rule `--stroke-subtle`; caret now
  visible pointing down after the label; dismiss X visible at 80% opacity at the right edge (x ~1390).
- Body padding 6 px, gap 6 px. For **terminal** tools: a single command box (x 303..1409, 32 px tall, fill
  `--surface-inset`, 1 px `--stroke-inset`, radius 4, mono 11 px `--fg-tertiary`): accent "$ " prompt, the command,
  and at the right an **exit badge** "exit 0" (mono 9.5 px, chip fill `--surface-chip`, radius 3, padding 1 px 4 px,
  green when 0, amber when non-zero). Output, when present, follows as a section (below).
- For other tools (and terminal output): section label in caps (10.4 px, weight 500, letter-spacing 0.08em,
  `--fg-tertiary`) then a pre block (mono 11 px, `--surface-inset` fill, radius 4, padding 6 px 8 px, max height ~320 px
  scroll). Error output renders in destructive with the first line as a summary.
- Copy icon (20 px) at the body's top-right, faint until hover.
- Images from the result keep showing in the body (ours already does).

## 5. `tool-row-rejected` — a denied approval

- Collapsed row like §3 but **red**: alert-circle glyph and label in the rejected ink (`#de6a72` light / `#aa3b37` dark).
  Label still reads "Ran <command>". Then the model's follow-up prose as normal.
- No separate "denied" badge; the colour carries it. Expanding shows the blocked result text (our terminal result
  carries `status: "blocked"` + `error`).

## 6. `approval-card` — dangerous command waiting

Placement: after the transcript rows inside the scroller (ours: `CardStack` as Transcript footer — keep), x 284, max
width 576 px, 8 px block padding around the stack.
- **Activity line** above the card (x 284, y ~111, scaffold 12 px `--fg-tertiary`, shimmer): "Running <command>". The
  pending tool row for the same call is not shown separately while the card is up (one line, not two).
- Card: 1 px `--stroke-card`, radius 3, fill `--chat-bg`, height ~97 px for a one-line command.
  - Header row (padding 10 px 10 px 0): 14 px terminal icon + "Command" label (12 px `--fg-secondary`). When the
    request has no command (description only) the label reads "Command details". When several approvals wait, a
    right-aligned "2 / 3" counter (11 px tabular, `--fg-tertiary`).
  - Command: mono 12 px `--fg`, padding 8 px 10 px, wraps, max ~160 px tall then scrolls, selectable.
  - Action row right-aligned, padding 4 px 8 px 8 px, gap 6 px, buttons 24 px tall radius 2:
    1. **Reject** — text button, 13 px `--fg-tertiary` (`#86888b`), no fill.
    2. **Always allow… ▾** — secondary button, fill `--surface-chip` (`#e9edf6` / `#171d28`), text `--fg`, 12 px
       chevron. Opens a menu (min width 176 px, align end): "Allow this session", "Always allow" (opens a confirm
       dialog that repeats the command in a mono box; buttons Cancel / Always allow (destructive)), separator-free
       "Reject" item in destructive.
    3. **Run ↵** — primary, `--accent` fill, `--on-accent` text, a "↵" glyph at 60% opacity after the label.
- Keyboard: **Enter = Run, Escape = Reject** while the card is the active stack in the focused chat and focus is not
  in another editable field; a single dispatcher, not one listener per card. Busy state: the clicked button shows a
  12 px spinner and all buttons disable.
- Swipe left on the card = reject (optional, low priority).
- While waiting: tab dot and session dot are `--warning` orange; Stop button in composer.

## 7. `error-state` — turn error card

Placement: inside the failed assistant turn, x 296..1408 (indented), 6 px below the previous block; the turn's partial
prose that arrived before the failure follows/precedes as normal paragraphs.
- Card: 1 px error border, error fill, radius 3, padding 8 px 12 px, gap 6 px, text 12.5 px error text colour,
  `role="alert"`.
- Row 1: title (weight 500), e.g. a generic "Couldn't finish this reply" in our words (not the reference sentence),
  and a 14 px dismiss X at top-right (70% opacity, 100% on hover). Row 2: one-sentence body in our words.
- Row 3: a "Details" disclosure (▸ triangle, 11.5 px, 70% opacity) — collapsed by default; open shows the raw failure
  text in mono, pre-wrap. **This is where `format_error`-style codes go** (fixes the bare-code bug).
- Row 4: action pills, wrap, gap 6 px: fully round, 1 px pill border, transparent fill (hover = 10% destructive),
  padding 2 px 9 px, 11.5 px text, optional 12 px leading icon. Reference order: Retry (refresh icon) · Open logs ·
  Send diagnostics (upload icon) · Copy error details (copy icon); provider/auth failures add Choose a model / Switch
  provider / Update key / New session ahead of them.
- There is **no** separate line above the card repeating the failure (ours prints "The model provider could not
  answer: …" above the card — drop the duplicate; the sentence goes into Details).

## 8. `notifications-toast`

- Region: **top-centre** of the window, top = title strip (34 px) + 12 px, width min(448 px, 100% − 32 px), above
  dialogs (highest z). Toasts for errors, warnings and actions go here; quiet confirmations may use a bottom-right
  region (24 rem wide, 16 px from the corner).
- Toast (x 500..940, y 58..155): fill toast fill, faint 1 px edge, radius 4, soft drop shadow (light ~0 8px 24px
  rgba(0,0,0,.06); dark relies on the edge). Grid: icon | text | close.
  - Icon 16 px in `--accent` for warning/info (triangle-alert in the shot), destructive for errors, green for success;
    padding-left 16 px.
  - Title 12 px (weight 500), message 12 px `--fg-secondary` below (wraps, max ~4.5 lines then scrolls), optional meta
    line 12 px muted.
  - Optional "▸ Details" disclosure (12 px muted) with a mono pre inside a tinted box.
  - Optional action buttons row (primary + outline, size sm).
  - Close X 12 px at top-right, muted.
- Stack: only the newest toast shows; when more exist, a strip below it: "Show N more" and "Clear all" text buttons.
  Auto-dismiss for info/success (~5 s), errors stay until closed. Enter/leave: fade + 8 px slide.

## 9. `usage-stats-prompt` — composer notice strip

What it shows in the reference: a first-launch consent strip for shared usage counts, sitting on top of the composer.
**We have no telemetry (decision D-16.49: none; opt-in only if the owner asks) — do not build a consent prompt or any
counting.** Build the **strip component** only, and use it for notices we already have:
- Geometry: x 279..1421 (inset 3 px from the composer's 276..1424), 26 px tall, flush on the composer's top edge (its
  bottom edge is the composer border), top corners radius 5, 1 px border notice-strip border, fill notice-strip fill.
- Content: 12 px leading icon (`--fg-tertiary`), bold-ish lead phrase (weight 500, `--fg` at 92%), muted hint text after
  it, then right-aligned text buttons (12 px, `--fg`; the last one muted "Details"), gap ~10 px. Truncates on narrow
  widths.
- Our uses: the "restored draft" strip with Undo (`chat/composer/goneSession.ts` already shows an inline strip), and
  optionally the update-available notice. Compare note: mark this slug "intentionally different (D-16.49)".
- The wordmark splash in this shot is the foundation's new-chat empty state.

## 10. `no-provider-chat` — new chat with no model configured

- Same new-chat splash as the foundation's empty state (our "INDUS AGENT" wordmark, our tagline).
- Status bar left: a 12 px warning-circle icon + "Backend" in `--warning` orange (`#e17100`) + "needs setup" muted
  (`#9c9fa2` / `#61666b`), replacing "Backend ready". Clicking it opens onboarding/provider setup.
- Composer model chip: the reference shows a broken empty glyph — **do not copy the bug**. Show our "Choose a model"
  label in the muted chip style; clicking it opens the model picker / provider setup. (Also fixes our "Choose a model
  after first chat" bug only when the model is really missing.)
- Sending is allowed to try; the resulting failure renders as the §7 card with "Choose a model" first.

---

## Mapping to our code (apps/desktop/src)

| Slug | Our files that change | Data available | Gaps / rule |
|---|---|---|---|
| chat-conversation | `chat/Transcript.tsx` (UserRow, AssistantRow, Timeline min count), `chat/Markdown.tsx` + `chat/RichBlocks.tsx` (code block copy button, table chrome), `styles.css` §transcript + §markdown (lines ~598–776) | text, markdown, `persisted`, `rowId` | **Prompt time**: `UserItem` has no timestamp and `HistoryMessage` has none. Add an optional client-side `sentAt` set by local echo (`chat/localEcho.ts`) for prompts sent in this window; hide the time otherwise. **Restore button**: wire to our existing undo/branch path if the composer slice exposes it (rowId is the branch point); otherwise omit — do not invent a backend call. |
| chat-streaming | `chat/Transcript.tsx` (replace `<Loader>` with a new `TurnActivity` row: dither square + elapsed timer), new small component `chat/TurnActivity.tsx` | `item.streaming`, `transcript.running`; start time: take `Date.now()` when the item first becomes streaming (client) | Timer is client-side only. Stop button state: composer slice (ours has Stop/Steer/Queue buttons — they must collapse to the single round Stop in the send slot; Steer/Queue move to the composer slice's menu). Running/approval dots: shell/sidebar from `transcript.running` and open approval requests (`useOpenServerRequests`). |
| tool-row | `chat/ToolRow.tsx`, `chat/toolSummary.ts` (add `kind` → icon map; keep verbs), `styles.css` .tool-* | name, args, state, progress, durationMs, result | Keep hooks used by tests/e2e/kit: `.tool-row`, `data-ui="transcript-tool-row"`, `data-state`, the toggle `<button aria-expanded>`. Remove the success check icon and the chevron at rest. Label becomes plain text: verb + target (drop `<code class="tool-target">` styling; keep the element if tests read it). Dismiss = local `useState`. |
| tool-row-expanded | `chat/ToolRow.tsx` | `result` JSON for terminal tools has `output`, `exit_code`, `error`, `status` (`src/indus/tools/builtin/terminal_result.py`) | Parse result safely (try JSON, fall back to raw). Terminal: command box + exit badge + output section. Others: "Arguments" / "Output" sections in caps (keep i18n keys `tool.arguments`, `tool.output`, `tool.noOutput`, `tool.showAll`). Copy = `app/clipboard.ts`. |
| tool-row-rejected | `chat/ToolRow.tsx`, `toolSummary.toolOutcome` (already "failed" for "denied"/"blocked") | state `failed` | Verify a denied call yields `failed` (result has `"status": "blocked"` + an error sentence). If the error field is empty and only `status:"blocked"` is present, treat `blocked` as failed. |
| approval-card | `chat/cards/ApprovalCard.tsx`, `chat/cards/CardStack.tsx` (stack chrome, max-width 576, activity line), new keys in `chat/strings.en.ts` | `request.command`, `description`, `choices` (backend-offered), `replayed`; answer via `open.answer({kind:"approval", choice, reason?})` | Keep behaviour: backend-offered choices only, `deny` always available, arming delay + explicit-press guard, `data-choice` on every choice control, replay note. Map: Run = `once`; menu = `session` / `always` (only if offered) / `deny`; Reject = `deny`. **Deny reason**: no visible input in the reference — keep the feature behind the menu item "Reject with reason…" that reveals the input in the card (decision for the builder; record as W52-x-n). Add new string keys (`approval.run`, `approval.reject`, `approval.alwaysMenu`, `approval.command`, `approval.commandDetails`, `approval.running`, `approval.alwaysConfirm*`) — **do not change** `approval.once/session/always/deny` values: `bots/RoomApprovalCard.tsx` shares them. Keyboard: Enter=once, Escape=deny (keep o/s/a/d too). Confirm dialog for "always": Radix Dialog from the foundation. |
| error-state | `chat/ErrorCard.tsx`, `chat/Transcript.tsx` (drop the duplicate notice line for the failed turn), `chat/ChatView.tsx` (actions), `chat/failure.ts` | `layer`, `message`, `reason`, `retryable`, `resetsAt`; actions retry / choose-model / open-settings / sign-in / reconnect / open-logs / new-session | Title/body from layer (rewrite our `failure.layer.*` strings into a headline + one-sentence body pair in our words); raw `message` (e.g. `format_error`) goes into Details. "Send diagnostics": we have no upload — **omit**. "Copy error details": client-side copy of layer + reason + message (`app/clipboard.ts`). Dismiss X: client-only hide for this view. Where is the duplicate line from? Not confirmed: either a `notice` event (`messageStream.ts` ~l.380, `NoticeItem` level `error`) or the turn's final assistant text carrying the failure sentence. Trace it with the fake backend first; then hide it at render time when it repeats the error item's `message` (no protocol change). |
| notifications-toast | New `app/Toasts.tsx` (one region + `notify()` store), replace the ad-hoc `.toast` renderers in `app/Shell.tsx` (ConnectionWarning), `deepLinks/DeepLinkHost.tsx` (note), plugin toasts (`contrib/host.ts` dispatches `PLUGIN_TOAST_EVENT`; a grep found no listener, so plugin toasts may not render today — have the region listen and route them into `notify()`), `updates/UpdateNotice.tsx` if it is a toast; CSS `.toast` (styles.css ~285) | text, tone/level, optional action | Our backend sends no "low disk" toast; capture our deep-link refusal toast for Compare (the kit already does). Keep `role="status"`/`"alert"` semantics. Check `DeepLinkHost` tests and the kit's toast selector after the move. |
| usage-stats-prompt | New `chat/composer/NoticeStrip.tsx` (component), used by the gone-session restored strip | goneSession restore + Undo | **No telemetry** (D-16.49). Compare: intentionally different. |
| no-provider-chat | `app/StatusBar.tsx` (backend status item), `chat/ModelPicker.tsx` chip label (`model.none`) | `GET /api/model/options` → `providers[]` (configured flags) + `current` (as `onboarding/Onboarding.tsx` reads it) | Add a tiny shared hook (e.g. `onboarding/useProviderReady.ts`) reading that route once per connection and after onboarding closes; status item click = reopen onboarding (existing `reopen` flow event). |

Tests affected (update only where the markup/text deliberately changes):
- `chat/cards/CardStack.test.tsx` — finds buttons by name "Allow once", "Allow for this session", "Deny" and the reason
  input label. After the change: Run (`data-choice="once"`), menu items (open the menu first; Radix menus need
  `pointerDown`/keyboard in jsdom), Reject (`data-choice="deny"`), reason input behind "Reject with reason…". Keep the
  arming, drag and keyboard assertions — they are behaviour.
- `chat/Transcript.test.tsx` — reads `.msg-user`, `.tool-row`, `[data-ui="transcript-tool-row"]`, turn grouping; keep
  these hooks. Loader assertion (if any) changes to the activity row.
- `app/FindBar.test.tsx` and `app/findInTranscript.ts` — rely on `.msg`, `.msg-user`, `.msg-bubble`; keep the classes.
- `e2e/fixtures.ts` (`.msg-user`, `.msg-user .msg-bubble`, `.msg-assistant .msg-text`) and
  `e2e/core/chat.electron.test.ts` (`.tool-row` count) — keep.
- `bots/GroupRoom.test.tsx` uses `RoomApprovalCard` (bots slice) — untouched if string keys are not changed.
- `app/StatusBar.test.tsx` — new status item; add a case.
- Capture kit (`$SP/compare/work-i-desktop/kit/desktop-all.ts`, copy it, never edit the original): it clicks the
  button whose text is "Allow once" (lines ~130, 390–394) → change to "Run"; add captures for `tool-row-rejected`
  (answer Reject on the approval, wait for the red row), `no-provider-chat` (fake backend with no provider, choose
  later) and a stand-in for `usage-stats-prompt` (the restored-draft strip) if reachable.

---

## Build order

1. Tokens: confirm the foundation (w51b note `docs/notes/ui-parity/desktop-foundation.md`) has the §0 colours; add the
   missing ones (scaffold text, stroke-subtle, surface-inset, surface-chip, destructive tints, notice strip, toast).
2. Transcript scaffolding: indent variable, prompt box (sticky, border, padding, hover slot), prose sizes, block gaps.
3. Markdown blocks: code block chrome + copy, table chrome, list numerals, heading sizes.
4. Tool row collapsed (kind icons, plain label, hidden caret, dismiss), then expanded (shell, terminal box + exit badge,
   sections, copy), then rejected colour.
5. Turn activity row (dither square + timer) replacing the loader; running/approval dot state hand-off to shell.
6. Approval card + activity line + menu + confirm dialog + Enter/Escape; update CardStack tests.
7. Error card (headline/body/Details/pills/dismiss), drop the duplicate notice line; update tests.
8. Toast region + `notify()`; migrate the three ad-hoc toasts.
9. Notice strip component + restored-draft use.
10. No-provider status item + chip label.
11. Re-capture both themes with the copied kit; PDF side by side; iterate.

## Compare checklist (tick per theme, light / dark)

- [ ] chat-conversation: prompt box full width x 284..1416, 1 px border, radius 3, 15 px text, sticky.
- [ ] chat-conversation: reply indented 12 px; no avatar/labels; heading 16–17 px semibold; numerals faint.
- [ ] chat-conversation: code block faint fill, no border, radius 6; copy on hover.
- [ ] chat-conversation: table outer border radius 4, header rule only, no zebra, no inner verticals.
- [ ] chat-conversation: timeline dash visible with one prompt.
- [ ] chat-streaming: dither square + seconds timer under the partial text; no spinner.
- [ ] chat-streaming: round Stop in composer; accent tab + row dots.
- [ ] tool-row: single 12 px tertiary line, kind icon, no check, no chevron at rest, plain "Ran …" label.
- [ ] tool-row-expanded: 5 px shell, header rule, "$" accent command box, green "exit 0" badge, X at right.
- [ ] tool-row-rejected: red alert icon + red label, then the follow-up prose.
- [ ] approval-card: "Running …" line above; 576 px card, terminal icon + "Command"; mono command.
- [ ] approval-card: Reject (text) · Always allow… ▾ (secondary) · Run ↵ (primary); Enter/Escape work.
- [ ] approval-card: orange dots while waiting.
- [ ] error-state: tinted card, title + one sentence, collapsed Details holds the raw code, round pills, X.
- [ ] error-state: no duplicate red notice line; no "Send diagnostics".
- [ ] notifications-toast: top-centre under the title strip, ~440 px, icon | text | X, soft shadow.
- [ ] usage-stats-prompt: notice strip geometry on the composer edge (used by restored draft); slug marked D-16.49.
- [ ] no-provider-chat: status bar orange "Backend needs setup"; chip "Choose a model" (no empty glyph).
- [ ] Existing vitest + desktop e2e pass; kit copy updated for "Run".
