# Wave 48 parked patches (base 1b6e711). Integrator assigns D-16 numbers to W48-<slice>-<n> decisions.

## w48d.patch: dashboard PTY child env, terminal tool schema on native Windows, admin console on Windows (done)
- 8 files, +346/-19. web/ws/pty_route.py drops HOST_TERMINAL_NAMES (COLORFGBG, TERM_PROGRAM(_VERSION), KITTY_WINDOW_ID,
  WT_SESSION, ZED_TERM, ITERM_SESSION_ID, VTE_VERSION, TMUX(_PANE), ZELLIJ, STY) and pins TERM/COLORTERM;
  tools/builtin/terminal.py schema_overrides → WINDOWS_HOST_SCHEMA (no background/pty/notify/heartbeat/persist);
  service/console.py SubprocessRunner uses start_windows_process (job object) on win32.
- Fail-first: pty env, terminal schema, 3 console tests. pytest 5189 passed. ruff/pyright/lint-imports/gen_reference/
  tooling.checks ok (deps_audit warns: katex, sprintf-js — slice f; NEW pypdf 6.19.0 moderate advisories, fixed 6.20.x).
- W48-d-1..3. Follow-ups: plan() still turns `&` / timeout > 600 s into background on a Windows host; wave-47
  test_windows_starter_boots_and_stops_the_tree raced once in 4 (0.5 s sleep vs first echo); real Windows lane.
