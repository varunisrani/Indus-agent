# Phase files

One file per phase (P00–P25), each with the same sections: goal, tasks, files to create, files to
update, fixtures and fakes, task notes, run until green, compare checklist, traps covered,
acceptance, done when. Paths and conventions come from
[00-layout-and-conventions.md](00-layout-and-conventions.md); the order and sizing come from
[../15-roadmap.md](../15-roadmap.md); the week-by-week view is
[../18-phase-wise-build-plan.md](../18-phase-wise-build-plan.md).

## Totals

| Phases | Weeks (serial) | Tasks | Files to create | Files to update | Fixtures and fakes | Task notes |
|---|---|---|---|---|---|---|
| 26 | 51.5 (about 45 calendar weeks with two tracks) | 648 | 2,184 | 248 | 117 | 646 |

"Files to create" is each phase's own count from its heading (grouped rows counted per path);
"Files to update" counts the rows of each phase's update table.

## Index

| Phase | File | Weeks | Tasks | Create | Update | Fixtures | Notes | Demo | Status |
|---|---|---|---|---|---|---|---|---|---|
| P00 | [P00-foundations.md](P00-foundations.md) | 1 | 14 | 71 | 6 | 3 | 10 | — | done (c790e93, ef25266) |
| P01 | [P01-platform-basics.md](P01-platform-basics.md) | 1.5 | 21 | 55 | 5 | 4 | 21 | — | done (cee5f44) |
| P02 | [P02-state-store.md](P02-state-store.md) | 1.5 | 23 | 52 | 6 | 3 | 23 | M0 (`indus doctor --minimal`) | done (98fa4e8, P02-21 in this commit) |
| P03 | [P03-providers-fake-model.md](P03-providers-fake-model.md) | 1.5 | 19 | 55 | 8 | 4 | 19 | — | done (ee74300) |
| P04 | [P04-turn-loop-tool-contract.md](P04-turn-loop-tool-contract.md) | 2 | 23 | 95 | 9 | 5 | 23 | — | done (87719b2, cc17d69, 4d1588d, 246d691; D-04.1 in this commit) |
| P05 | [P05-core-tools.md](P05-core-tools.md) | 2 | 24 | 73 | 8 | 6 | 24 | — | done (a26acb4) |
| P06 | [P06-classic-cli-demo1.md](P06-classic-cli-demo1.md) | 1 | 15 | 46 | 7 | 3 | 15 | **Demo 1** (M1) | done (2fcb1d8) |
| P07 | [P07-cli-commands.md](P07-cli-commands.md) | 2 | 26 | 68 | 5 | 4 | 26 | — | done (9bb6c46) |
| P08 | [P08-context-and-cost.md](P08-context-and-cost.md) | 1.5 | 19 | 49 | 9 | 4 | 19 | — | done (822b6cd wave 1; wave 2 in this commit) |
| P09 | [P09-learning-loop.md](P09-learning-loop.md) | 2 | 25 | 82 | 9 | 5 | 25 | M2 | done (3d55449 wave 1; wave 2 in this commit) |
| P10 | [P10-delegation-rich-tools.md](P10-delegation-rich-tools.md) | 2 | 26 | 69 | 11 | 6 | 26 | — | done (2193f87, f303495, 94d604e) |
| P11 | [P11-docker-ssh-backends.md](P11-docker-ssh-backends.md) | 1.5 | 21 | 50 | 11 | 3 | 21 | — | done (3ae8089) |
| P12 | [P12-tui-demo2.md](P12-tui-demo2.md) | 3 | 33 | 149 | 10 | 5 | 33 | **Demo 2** (M3) | done (94d604e, fa4fe5e, c66c233; Demo 2 in this commit) |
| P13 | [P13-mcp-plugins-hooks-hub.md](P13-mcp-plugins-hooks-hub.md) | 2 | 28 | 61 | 13 | 5 | 28 | — | done (78fdc96, aed252e; N3+N4 in this commit) |
| P14 | [P14-cron.md](P14-cron.md) | 1.5 | 19 | 47 | 9 | 4 | 19 | M4 | done (M4 demo; in this commit) |
| P15 | [P15-gateway-core-demo3.md](P15-gateway-core-demo3.md) | 2.5 | 36 | 131 | 11 | 6 | 36 | **Demo 3** | wave 1 building |
| P16 | [P16-more-platforms.md](P16-more-platforms.md) | 3 | 36 | 136 | 6 | 4 | 36 | — | planned |
| P17 | [P17-daemon-shared-backend.md](P17-daemon-shared-backend.md) | 2.5 | 27 | 65 | 17 | 2 | 27 | M5 | planned |
| P18 | [P18-web-dashboard.md](P18-web-dashboard.md) | 2.5 | 30 | 148 | 11 | 6 | 30 | — | planned |
| P19 | [P19-acp-mcp-serve.md](P19-acp-mcp-serve.md) | 1.5 | 18 | 54 | 10 | 4 | 18 | M6 | planned |
| P20 | [P20-desktop-demo4.md](P20-desktop-demo4.md) | 3.5 | 40 | 183 | 8 | 5 | 40 | **Demo 4** (M7) | planned |
| P21 | [P21-remaining-backends.md](P21-remaining-backends.md) | 1.5 | 16 | 40 | 7 | 3 | 17 | — | building (B1, B2) |
| P22 | [P22-research-tooling.md](P22-research-tooling.md) | 1 | 15 | 36 | 6 | 4 | 15 | — | building (R1) |
| P23 | [P23-packaging-release.md](P23-packaging-release.md) | 2 | 23 | 78 | 8 | 4 | 24 | — | planned |
| P24 | [P24-multiplex-extras.md](P24-multiplex-extras.md) | 3.5 | 47 | 218 | 27 | 11 | 47 | M8 | planned |
| P25 | [P25-hardening-parity-release.md](P25-hardening-parity-release.md) | 2 | 24 | 73 | 11 | 4 | 24 | M9 / **v1.0** | planned |

## How a phase runs

1. **Slice.** The lead splits the phase's tasks into slices that share no file: no path appears in
   the "Files to create" or "Files to update" of two slices that run at the same time. Shared
   registration files (CLI app, protocol modules, config schema, toolsets) get their lines applied
   by the lead when a slice lands, never by two builders at once.
2. **One builder per slice.** Each slice goes to one builder sub-agent in its own worktree. The
   builder writes the task note first (`docs/notes/Pnn/<slug>.md`), then works through
   **Read → Rewrite → Compare**: read the reference files the task names, close them, write our
   code and tests in our own words, then reopen the reference and fill the phase's compare
   checklist and the note.
3. **Run until green.** The builder runs the shared loop (00-layout §5) plus the phase's own
   commands until everything passes, including `python -m tooling.checks --require-ref`.
4. **Land.** The lead reviews and lands **one commit per slice**, moves the traps and acceptance
   rows the slice pinned from `Review:` to test references, and updates the Status column above
   (planned → in progress → done, with the commit).
5. **Deviations go back into the phase file.** When a builder has to change a path, a task's scope
   or a number, the change is written into the phase file (and 00-layout if a path or folder is
   new) in the same slice, and an intended difference gets a D-id row in
   [../16-what-we-change.md](../16-what-we-change.md) §2.7.
