# Task: `indus sessions` read/export side (P07-14) Phase: 07 Owner: P07 slice D builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/subcommands/sessions.py`
(parser flags only). Research `04-cli-config.md` §3.20, `03-state.md` §3.8; trap R03.16. Closed
before writing.
Evidence: none.

What it does (own words): the session history from the terminal. `list` shows the newest
conversations (title or preview, source, message count, last activity), `browse` is a numbered
picker that resumes the chosen one (without a terminal it is the same table as `list`), `rename`
sets a user title, `delete` removes a conversation (its compression chain and delegated children,
with tombstones so the id never comes back), `export` writes JSONL / Markdown / HTML with secrets
masked by default, `stats` sums the store up, `pin`/`unpin`/`pinned` manage the keep flag. Every
listing opens the store through `state.browse.open_readonly` (a `mode=ro` URI): no schema set-up,
no migration, no checkpoint, and a missing store is reported, never created (R03.16).

Inputs → outputs:
- `list [--source S] [--limit 20] [--workspace NEEDLE]` → table; no store → "No sessions yet." 0.
- `browse [--source] [--limit 500]` → TTY: numbered list, a number resumes it (`chat -r ID`);
  non-TTY: the list.
- `rename ID TITLE…` → "Renamed …" / exit 1 (unknown id, title taken).
- `delete ID [-y]` → confirm (TTY) / exit 1 without `-y` on non-TTY; exit 1 when guarded.
- `export [OUTPUT] [--format jsonl|md|html] [--session-id ID] [--redact/--no-redact] [--force]`
  plus the shared filters → files written owner-only through a temp file; `-` = stdout (jsonl).
- `stats`, `pin IDS…`, `unpin IDS…`, `pinned`.
- Shared filters (`session_filters`): `--older-than/--newer-than` (last activity),
  `--before/--after` (start time), durations `30s/5m/5h/2d/1w`, a bare number = days, or ISO
  date/time; `--source` (repeatable), `--title`, `--cwd`, `--min/--max-messages`, `--model`,
  `--provider`, `--dry-run`, `-y`.

Files: `src/indus/cli/commands/sessions.py`, `src/indus/cli/session_filters.py`,
`src/indus/cli/cmdkit.py` (small helpers my commands share), `tests/unit/cli/commands/test_sessions.py`.

Edge cases / failure modes:
- Store missing / never set up / newer schema / busy → one line each, never created.
- Ambiguous id prefix → exit 1 naming the problem; titles are accepted too.
- Export to an existing file without `--force` → exit 1, nothing written.
- Internal sources (`subagent`, `tool`, `oneshot`, board) hidden unless `--source` names them.
- Durations with junk (`5x`, `-2d`, empty) → usage error (exit 2).

Our design differences / decisions:
- Listing functions of P02 take a handle; the read-only view is wrapped in a tiny adapter with
  `scope` and `read()` so `listing.list_sessions` and `portability.export_session` run on the
  `mode=ro` connection.
- Formats are jsonl, md and html (qmd, trace and uploads are out of scope for P07).
- `delete` removes the whole compression chain (what the list shows as one row).

Tests we will write:
- Filter parser table; read-only open asserted (the URI has `mode=ro`, no handle opened, missing
  store not created); delete without `-y` on non-TTY exits 1; export redacts by default and
  `--no-redact` keeps the text; pin/unpin round trip; rename; stats; md/html output.

## Decisions for the lead
- `browse` resumes the picked session by calling the chat runner with `-r ID`; cancel = exit 0.
- `--workspace NEEDLE` matches a path substring or a basename of the git root / cwd.


Compare checklist (filled after the rewrite; reopened `hermes_cli/subcommands/sessions.py`):
- [x] `list --source --limit 20 --workspace NEEDLE` flags (test: `tests/unit/cli/commands/test_sessions.py::test_list_limit_and_workspace`)
- [x] read-only listing, nothing created (test: `tests/unit/cli/commands/test_sessions.py::test_list_opens_read_only_and_changes_nothing`)
- [x] `rename ID TITLE…` joins the words (test: `tests/unit/cli/commands/test_sessions.py::test_rename_by_prefix`)
- [x] `delete ID [-y]`; no hang without a terminal (test: `tests/unit/cli/commands/test_sessions.py::test_delete_without_yes_on_non_tty_exits_1`)
- [x] shared filters: durations, bare number = days, ISO times (test: `tests/unit/cli/commands/test_sessions.py::test_when_table`)
- [x] export `--force` refuses to overwrite otherwise (test: `tests/unit/cli/commands/test_sessions.py::test_export_refuses_to_overwrite_without_force`)
- Deliberate differences: export masks secrets by default (`--no-redact` to keep them; the
  reference redacts only on request outside its trace format); `--force` applies to every format;
  md/html of several sessions go one file per session into a folder (default
  `<home>/session-exports/`), one session into one file.
- Not taken: `qmd` and `trace` formats, uploads, `--only user-prompts`, `--lineage`,
  `--delete-after-verified`, and the gateway/billing filters (`--user`, `--chat-id`,
  `--chat-type`, `--branch`, token and cost ranges, `--end-reason`) — outside P07-14's row.

## Status

Done (2026-10-18). See the slice report for shared files (app.py registry lines, commands/__init__.py,
test_app.py help-order parsing).

## Integration (lead)

- `browse` asks through `cli.prompts.ask_text`; delete confirmation through `prompts.confirm`.
- Slice D's `test_app.py` help-order parsing change was dropped at integration: with the
  registry one-liners always shown (`lazy.py`, slices A/C) no row wraps.
