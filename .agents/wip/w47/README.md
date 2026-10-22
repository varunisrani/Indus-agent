# Wave 47 parked patches. Base = LOCAL snapshot commit 9a155bbb9a (waves 45+46 integration as of 07:08 UTC; not pushed).
# Apply with `git apply --3way` onto main AFTER waves 45+46 land (3-way needs the blobs: do it in this container, or fall
# back to plain apply + manual fix-up). Decisions are labelled W47-<slice>-<n>; the integrator assigns D-16 numbers.

## w47c.patch: daemon serves complete.slash, model.options, session.list to attached TUIs (done)
- 10 files, +482/-126. New service/picker_reads.py shared by tui_bridge and gateway/ws/methods.py (3 handlers; session-
  scoped calls need conn.subs.require; session.list reads the served profile's store). Bridge copies removed.
- Fail-first: tests/contract/gateway/test_ws_pickers.py (4 tests failed on base with -32601). pytest 658 + 617 + 1896
  contract (1 load flake: ssh deadline, passes alone). ruff/pyright/lint-imports/security_static/tooling.checks ok.
- W47-c-1 session.list lists every stored conversation of the profile to any attached connection (as --local);
  W47-c-2 D-16.549 partly closed; W47-c-3 next: complete.path, commands.catalog, config.get.

## w47f.patch: update --finish refreshes fetched pages; install.ps1 parity; stale-stamp pages (done; ps1 unverified on Windows)
- 11 files, +529/-23. cli/update/finish.py phase `web_pages` (child `python -m indus.web.bundle fetch`, 300 s, scope env;
  D-16.246 kept); web/bundle.py installed_release(), locate(release=) → kind `stale` (not served); web/command.py stale message;
  scripts/install/ps/20-stages.ps1 Install-IndusWebPages + marker version (install.ps1 rebuilt, ASCII, build --check fresh).
- Fail-first: 6 finish, 4 locate, 2 ps1 parity. pytest update/version/entry + integration/install 220, unit/web 388,
  security 89, contract 1893. ruff/pyright/lint-imports/security_static/tooling.checks ok.
- W47-f-1..3. Follow-up: pwsh tests + a real Windows source install (rehearsal row).

## w47a.patch: desktop composer stuck on Stop/Steer; tool rows lose detail after reload (done)
- 17 files, +492/-31. busyActions.ts follows the generation (turn ids seen in turn.started; newest ended generation);
  sharedActions subscribes for its lifetime (fixes composer-less sends). Protocol: optional HistoryMessage.arguments
  (codegen regenerated); tui_bridge display_messages and REST /api/sessions/{id}/messages mask tool-call arguments
  (redact_arguments; W47-a-4: REST now masks for every client); desktop restHistory + messageStream rebuild rows with args.
- Fail-first: 6 busyActions, messageStream, restHistory, 2 pytest. pytest 826 + 1525 + security 89; tsc/biome ok; desktop
  vitest 659 passed + 1 load timeout (Transcript 2,000-message window; passes alone).
- W47-a-1..4. MERGE NOTE: w47g also changes packages/protocol/src/generated/* (regenerate with codegen after applying both).
  Follow-up: localEcho binds the optimistic row to the admission id (pre-existing).

## w47h.patch: RS3 room approvals answered by the person; room events pushed (daemon side done; UI views not yet)
- 18 files, +1264/-31. gateway/rooms/loop.py TurnRequests + messaging viewer; room events approval.requested / .answered /
  .settled (only a person may write .answered; never in member prompts — SEC-15 kept); rpc.py groups.approve (open request,
  offered choice, first answer wins); new gateway/rooms/watch.py groups.watch/unwatch → groups.event notifications (WS only);
  protocol + codegen + protocol.md regenerated (method count 90 → 93; w47g also touches protocol — regenerate after both).
- Fail-first: 3 loop tests, watch import; security pin test. pytest 647 targeted + contract/security 1983;
  test_room_two_members 5/5. TS not checked (generated only).
- W47-h-1..3. Follow-up W47-h-3: desktop GroupRoom.tsx and TUI/web room views → groups.watch + approval card (then RS3 Done).

## w47d.patch: dashboard light-theme Chat tab, TUI slash-menu redraw, desktop slash contrast, session titles (done)
- 21 files, +531/-24. TUI follows terminal colour-scheme reports (CSI ? 996 n / 997 / mode 2031) and late OSC 11 answers
  (inputFilter onScheme, theme.followTerminalScheme); web xterm SchemeReporter answers them from data-theme; CompletionList
  count line takes a row + truncate; desktop highlighted description text-accent-fg; providers/auxiliary clean_title keeps
  the first line up to 60 chars with "…"; fake model titles use the full first line.
- Fail-first: CompletionList, Composer, keys onScheme, theme, clean_title. pytest 2930; tsc tui/web/desktop; vitest 210
  (+3 load timeouts in untouched files). Not yet seen in a real browser — the screenshot pass B checks it.
- W47-d-1..2. Follow-up: drop COLORFGBG / TERM_PROGRAM from the dashboard PTY child's env.

## w47g.patch: D-16.487 single_query flag on prompt.submit; client-side one_shot_answer retired (done)
- 19 files, +368/-129. PromptSubmitParams.single_query; daemon_host runs the turn with run_context single_query and the broker
  asks nobody (restored after); remote journal/host keep the flag; cli/attach.py sends it for chat -q and -z.
- Fail-first: end-to-end attach test (step 'human' → 'unattended'), daemon_host test, ws contract. pytest cli+protocol
  1967, service 584, contract+security 1983, attach integration 2, dual-host matrix 4. codegen/gen_reference fresh.
- W47-g-1..3 (older daemons refuse the new key: fails closed). Integrator updates D-16.554/1010/1025 text, RS26 and the
  dual-host-matrix note; closes LEAD-NOTES' "attached -q deny" follow-up.
