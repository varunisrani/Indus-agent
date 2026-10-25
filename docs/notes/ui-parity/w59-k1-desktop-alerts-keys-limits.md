# Task: Desktop alerts, key bindings, output limits, voice rows, reply time and copy (W59-K1) Phase: ui-parity Owner: builder w59k1

Status: Done (2026-10-25) for all five items, each only as far as our app has the data or the
action. Uncommitted in the w59k1 worktree for the lead.

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): no reference source opened; the
second compare's gap list and screenshots were the input.
Evidence: reference screenshots of the second compare (never in the repo), desktop
`settings-notifications-desktop-alerts`, `settings-shortcuts`, `settings-advanced-output-limits`,
`settings-voice-speech-to-text`, `settings-voice-voice-conversation`, `chat-conversation`, light.

What it does (own words):
- Desktop alerts (W59-K1-1): the page had a master switch over two kinds. Our app raises OS
  notifications from three sources it already receives: `turn.done`, server requests (approval,
  clarify, sudo, secret) and a desktop plugin's `ctx.toast`. Each kind now has its own switch:
  Approval needed, Input needed (clarify / sudo / secret, split from approvals), Response ready,
  Turn failed (a `turn.done` that is not a clean completion, split from replies) and Plugin
  notifications (a plugin's message while the window is in the background; the in-app toast is
  unchanged). The master switch is on while any kind is on and writes every kind. The prefs file
  gains `input`, `turnFailed` and `plugins`; an older file without them takes the value of the
  switch that covered them before, so nobody gets alerts they had turned off.
- Not added (no event reaches the desktop): background task finished (the backend delivers a
  background job's completion only to a registered session sink, and no surface registers one),
  cron job finished (the desktop reads cron over REST, no run event), credit alerts (no credits).
- Key bindings (W59-K1-2): the stored overrides were never applied to anything; the rows for new
  chat (Ctrl+N), open settings (Ctrl+,) and go to the message box (Ctrl+L) had no handler. A new
  dispatcher (`settings/keyActions.ts`, installed by `PrefsEffects`) turns a keydown into an
  action through the person's effective bindings and announces it as a window event; the owner of
  the action runs it. It runs those three and eight new rebindable actions: open the model picker
  (Ctrl+Shift+M), reasoning effort up / down (unset by default; steps through the effort ladder,
  Auto counts as the default level, and tells the running session like the chip), start or stop
  dictation (unset), start or end a voice conversation (Ctrl+Alt+V), and a new Profiles group
  (switch to the default profile, next / previous profile; unset). A key the pane shell owns, a
  repeat or an already-handled keydown runs nothing. Built-in keys that work but are not
  rebindable are listed as plain chips: new window, right sidebar, layout editor, new chat tab,
  tab 1–9, previous tab, previous chat. Not added: a queue binding (Enter already queues while a
  turn runs; there is no "send next queued" action).
- Output limits (W59-K1-3): the schema has one more output limit, `file_read_max_chars`; it now
  also shows on Advanced › Output limits (owned by Workspace › Files & execution). The reference's
  file page and line length limits are constants in `read_file`, not settings.
- Voice (W59-K1-4): Voice conversation shows the recording rules up front (pause that ends a
  take, give up without speech); Speech to text shows the transcription device (auto / cpu /
  cuda). The push-to-talk key showed as a stored secret because the backend masked any setting
  ending in `key`; `record_key` (and `secret_store`, a choice of store, not a secret) are no longer
  masked (W59-K1-6, `config_view.is_secret_setting`). No STT on/off, echo or live-transcription
  setting exists, so none is shown.
- Reply time and copy (W59-K1-5): the REST history route already sends each stored row's time.
  The transcript keeps it; a prompt shows its stored time (or the time this window sent it), and
  a settled reply shows, on hover or focus, its stored time and a Copy button for its text. A
  reply loaded without a time (the WebSocket history fallback) shows only Copy. No branch button:
  `session.branch` copies the whole conversation, not from a message.

Compare checklist:
- [x] desktop alerts: five kinds + master + test button (reference: eight; three not backed)
- [x] key bindings: 18 rebindable + 14 built-in chips in five groups, 32 rows (reference: about 30)
- [x] output limits: three rows (reference: four; two are constants here)
- [x] voice conversation / speech to text: rows for every relevant setting that exists
- [x] reply row: time + copy (reference: time, copy, branch)

Tests: `turnNotices.test.ts` (kinds, plugin notice), `prefsBridge.test.ts` (master switch, ambient
patch), `prefs.test.ts` (migration of older files), `SettingsOverlay.test.tsx` (master writes every
kind, one kind alone), `keyActions.test.tsx` (dispatch, overrides, pane keys, profile targets),
`ModelPicker.test.tsx` (picker and effort keys), `settingsStates.test.tsx` (new rows and groups),
`search.test.ts` (output and voice pages), `restHistory.test.ts` and `Transcript.test.tsx` (stored
time, copy), `test_config_env_routes.py` (record_key and secret_store not masked).
