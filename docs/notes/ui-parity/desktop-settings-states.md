# Task: Desktop settings and page states that were not true (W54-D2) Phase: ui-parity Owner: builder w54d2

Status: Built (2026-10-25, wave 54 builder D2, base 7b59d33). Uncommitted, for the integrator.

Read: the wave 54 gap list (desktop lines for settings-passwords-password-managers,
settings-voice-voice-conversation, settings-gateways-this-window, settings-about-uninstall,
settings-licences, settings-chat-personality, profiles, settings-providers-custom-endpoints, cron,
settings-shortcuts, settings-notifications-desktop-alerts) and the reference and Indus screenshots of
those slugs, both themes (scratchpad Evidence, not in the repo). Reference source: not opened (the
screenshots carry the layout). Evidence ids: none.

What it does (own words): several settings pages showed a state that was not true. Each one is
traced to its cause and fixed there, then the page shows the real state:

- Password managers read `browser.vault_sources` (a wish list that defaults to every source) as if
  it said what works, so 1Password and Bitwarden read "On" with nothing installed. Main now looks
  the manager programs up on the PATH the backend inherits (`op`, `bw`; the new bridge call
  `vault.detect`), and the page shows "Not detected" with no switch for a missing tool. With a
  remote backend nothing is looked up and no row claims a state.
- The keychain switch was bound to the stored wish (`store`), not what the run uses (`effective`),
  so it read ON while secrets were in files. It now follows `effective`, is disabled when no
  keychain exists, and the reason is a quiet line.
- The wake word's launch arm asked the daemon before the connection was up, failed with "not
  connected", and the page showed that as a red error although nobody had asked for anything. The
  arm now waits for the connection's `ready`; a launch arm that still finds no connection, or is
  told the feature is off, stays quiet (the person's own switch still gets every reason), and only
  a failed or timed-out start is shown in red.
- Uninstall: when ownership cannot be confirmed, main now names the missing proof (no answer from
  the CLI, unreadable answer, not an install of ours, another home, no receipt) and the page adds
  the CLI commands the person can run, plus a line when the window is a development run.
- Licences: a development run reads the checkout's own `THIRD_PARTY_NOTICES.md` (the e2e/capture
  harness copies the app outside the checkout, so its shots still show the "missing" line).
- Personality: a full-width editor under its title with Save below (was a field-sized box).
- Profiles opens on the profile this window runs on (the list's `current`), else the default.
- Custom endpoints lists the main model's own endpoint (`model.base_url`) as the active card when
  it is not a saved entry; clicking it fills the form so it can be saved under a name.
- Key bindings lists the composer's built-in keys (the ones its help card lists) as fixed chips.

Decisions: W54-D2-1 detection in main over the PATH, no backend endpoint (the backend has none and
the brief forbids inventing one); W54-D2-2 the switch shows `effective`; W54-D2-3 wake arm waits
for `ready`; W54-D2-4 named reasons + manual commands, never an uninstall option we cannot prove;
W54-D2-5 dev licences from the checkout; W54-D2-6 persona editor; W54-D2-7 profiles default
selection; W54-D2-8 inline endpoint card; W54-D2-9 fixed composer keys, no new rebindable actions.

Left on purpose: cron blueprints (our REST route serves skill blueprints only; the left column
already shows when there are jobs or blueprints), desktop alerts (3 real toggles; no fake rows).

Compare checklist:
- [x] password managers: Not detected without the tool, no switch
- [x] voice: no red error when nothing failed
- [x] gateways: switch matches reality, quiet reason
- [x] uninstall: says what is missing and what the person can do
- [x] licences: dev run shows the notices
- [x] personality: full-width editor
- [x] profiles: opens with a profile selected
- [x] custom endpoints: active endpoint card
- [ ] cron: blueprint list needs data the backend does not serve yet (follow-up)
