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
