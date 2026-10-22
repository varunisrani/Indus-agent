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
