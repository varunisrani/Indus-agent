# Task: Desktop transcript, tool rows, cards, toasts (W52-g) Phase: ui-parity Owner: builder w52g

Status: Done for wave 52 (2026-10-24), except the items under "Remaining" at the end.

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): no reference source opened by this
builder; the wave-52 spec `w52-specs/desktop-transcript.md` (written by a Read+Note spec writer from
the reference's transcript, tool, approval, notification and status components) is the note's source.
Evidence: reference screenshots in the wave-51 compare set (never in the repo), desktop slugs
`chat-conversation`, `chat-streaming`, `tool-row`, `tool-row-expanded`, `tool-row-rejected`,
`approval-card`, `error-state`, `notifications-toast`, `usage-stats-prompt`, `no-provider-chat`, each
`--light` and `--dark`.

What it does (own words): the inside of the chat column takes the reference's quiet look. The prompt
is a full-width hairline box that sticks to the top while its answer scrolls; the answer is plain
prose indented a little under it. A tool call is one faint 12px line ("Ran echo …") with the tool's
kind icon; it opens into a thin shell showing a `$ command` box with an exit badge, or labelled
argument/output sections. A refused call is the same line in red. While a turn runs, a small dithered
accent square with a seconds counter sits under the text instead of a spinner. A dangerous command
asks in a narrow card under a "Running …" line: Reject · Always allow… ▾ · Run ↵, with Enter = Run and
Escape = Reject. A failed turn shows one tinted card (headline, one sentence, a closed "Details" with
the raw failure, round action pills, dismiss X) and no duplicate red line above it. Toasts appear at
the top centre under the title band. The composer gets a thin notice strip on its top edge (used for
the restored draft). With no provider set up the status bar says "Backend needs setup" in orange.

Inputs → outputs: transcript items (user / assistant parts / error / notice), open server requests,
connection state, `GET /api/model/options` → DOM in the chat column, the toast region and the status
bar. No protocol or API change.

Files: `apps/desktop/src/chat/{Transcript,ToolRow,ErrorCard,TurnActivity,Timeline,toolSummary,
strings.en}.ts(x)`, `chat/cards/{ApprovalCard,CardStack}.tsx`, `chat/composer/NoticeStrip.tsx`,
`app/Toasts.tsx`, `app/StatusBar.tsx`, `onboarding/useProviderReady.ts`, `chat/transcript.css` (new,
this slice's styles), tests next to them.

Edge cases / failure modes:
- tool result not JSON, JSON without `exit_code`, `status: "blocked"` with an empty error → failed.
- approval with no command (description only) → "Command details"; several approvals → "n / m".
- Enter/Escape only when focus is not in another editable field and the card stack is in this chat.
- arming delay and press-started-here guard stay for every approval control, menu items included.
- error card dismiss and tool-row dismiss are view-only (client state); nothing is sent.
- prompt time only for prompts sent in this window (local echo); history rows show none.

Our design differences / decisions:
- W52-g-1: "Reject with reason…" is a menu item that reveals the reason input inside the card (the
  reference has no reason input; we keep our feature behind the menu).
- W52-g-2: "Send diagnostics" is omitted (we have no upload); "Copy error details" copies layer,
  reason and raw message on the client.
- W52-g-3: the restore/undo button on the prompt row is shown only if an undo path exists in our
  code; otherwise omitted (no invented backend call).
- W52-g-4: the usage-stats consent strip is not built (D-16.49, no telemetry); its strip look is the
  `NoticeStrip` component, used for the restored-draft notice. Slug marked intentionally different.
- W52-g-5: the error card headline and body are our own sentences per failure layer.

Tests we will write:
- ToolRow: kind icon, no check at rest, terminal command box + exit badge, rejected = failed state.
- toolOutcome: `status: "blocked"` alone is failed.
- CardStack/ApprovalCard: Run / Reject / menu choices keep `data-choice`, arming, Enter/Escape.
- ErrorCard: details hold the raw message, copy action, dismiss.
- Transcript: no duplicate notice line next to an error card; activity row while streaming.
- Toasts: region renders newest, "Show N more", auto-dismiss of info.
- StatusBar: "needs setup" when no provider is configured.

## Decisions for the lead
- W52-g-6: the approval header shows the backend's description after "Command" (muted, one line);
  the reference shows the label only. Drop `.approval-why` if exact parity is wanted.
- W52-g-7: the open terminal row also shows an OUTPUT section when there is output (the reference
  shot shows only the command box); useful, and cheap to hide.
- W52-g-8: one new desk token `attention` (orange; light #e17100, dark #fe9a00) for the waiting /
  needs-setup state; `MenuItem` gained optional `tone: "danger"` and `data` (`data-*` attributes).
- W52-g-9: a failed turn's reply that is exactly one of our backend's failure sentences
  (`loop_texts`) is shown in the error card's Details instead of above the card (render-time only).

Compare checklist (filled after the rewrite; screenshots reopened):
- [x] chat-conversation: prompt box full width, 1px border, radius 3, 15px text, sticky.
- [x] chat-conversation: reply indented 12px; no avatar/labels; heading 16–17px semibold; numerals faint.
- [x] chat-conversation: code block faint fill, no border, radius 6; copy on hover.
- [x] chat-conversation: table outer border radius 4, header rule only, no zebra, no inner verticals.
- [x] chat-conversation: timeline dash visible with one prompt.
- [x] chat-streaming: dither square + seconds timer under the partial text; no spinner.
- [ ] chat-streaming: round Stop in composer; accent tab + row dots. (shell/composer state: not done here)
- [x] tool-row: single 12px tertiary line, kind icon, no check, no chevron at rest, plain "Ran …" label.
- [x] tool-row-expanded: 5px shell, header rule, "$" accent command box, green "exit 0" badge, X at right.
- [x] tool-row-rejected: red alert icon + red label, then the follow-up prose.
- [x] approval-card: "Running …" line above; 576px card, terminal icon + "Command"; mono command.
- [x] approval-card: Reject (text) · Always allow… ▾ (secondary) · Run ↵ (primary); Enter/Escape work.
- [ ] approval-card: orange dots while waiting. (shell/composer state: not done here)
- [x] error-state: tinted card, title + one sentence, collapsed Details holds the raw code, round pills, X.
- [x] error-state: no duplicate red notice line; no "Send diagnostics".
- [x] notifications-toast: top-centre under the title strip, ~440px, icon | text | X, soft shadow.
- [x] usage-stats-prompt: notice strip geometry on the composer edge (restored draft); slug D-16.49.
- [x] no-provider-chat: status bar orange "Backend needs setup"; chip "Choose a model" (no empty glyph).
- [x] Existing vitest pass (82 files / 688 tests full run, then 38 / 333 targeted after the last
  change, plus `src/chat/transcriptLook.test.tsx` 11 tests); kit copy uses "Run".
- [ ] Desktop Electron e2e not run (lead stop); `e2e/core/chat.electron.test.ts` and
  `testing/electron/wiredApp.electron.test.ts` were updated to click "Run".
- Deliberate differences: usage-stats-prompt not built (D-16.49) — the strip is `NoticeStrip`, used
  for the restored draft; no "Send diagnostics" pill; no prompt restore/undo button (no undo path in
  our code, W52-g-3); approval header shows the description (W52-g-6).

## Remaining
- Running/approval dot colours on the tab and sidebar row (shell/sidebar state hand-off).
- Composer's single round Stop (Steer/Queue still shown) — composer slice.
- Prompt clamp-to-4-lines click-to-expand (clamped, no expand control yet).
- Swipe-to-reject and the busy spinner on approval buttons.
- usage-stats-prompt / restored-draft strip not captured (needs a vanished session).

## Integration (wave 53, desktop)
Applied on the desktop foundation with w52h and w52m (`git apply --3way`): the composer's restored-draft
`NoticeStrip` and w52h's composer (attach menu, drawer, help) merged (lucide import union), `Shell.tsx`
mounts both `ToastRegion` and the session switcher, `ui.css` keeps both menu rules (disabled rows and
the red icon on danger rows). Decision ids: W52-g-1, W52-g-6 → D-16.1216; W52-g-2, W52-g-5, W52-g-9 →
D-16.1217; W52-g-3 → D-16.1218; W52-g-4 → D-16.1219; W52-g-7 → D-16.1220; W52-g-8 → D-16.1221. The
Electron e2e lane (incl. `wiredApp` clicking "Run") ran green at integration; the review pane now
uses this slice's toasts (D-16.1228). Remaining items above are unchanged (follow-ups).
