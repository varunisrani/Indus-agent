# Task: `indus mcp` CLI (P13-12) Phase: 13 Owner: wave-16 builder N2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/mcp_config.py` (verbs,
probe, checklist precedence, exit codes). Ours: `cli/app.py` (`REGISTRY`), `cli/cmdkit.py`,
`cli/prompts.py` (`io_for`, `confirm`, checklist), `cli/commands/_support.py`,
`foundation/config/writer.py` (round-trip writer), `mcp/config.py`, `mcp/connect.py`
(`Connection`), `mcp/naming.py` (`NameFilter`), `mcp/oauth.py` (P13-09), contract tests
`tests/contract/cli/*`.

What it does (own words):
- `list`: one row per configured server: name, transport (command or URL, shortened), state, tools.
  State is `disabled` / `lazy` / `configured` from the config; when the caller has live statuses
  (the `/mcp` chat command passes the running manager's), `ready` / `connecting` / `cooling down` /
  `parked` / `recycled` with live tool counts. Tools: "all", "N selected", "-N excluded" (from the
  filter). Broken entries are listed with their problem.
- `add <name> --command CMD [--arg A ...] [--env K=V ...] | --url URL [--header K=V ...]
  [--transport http|sse] [--auth oauth] [--preset NAME] [--lazy] [--trust full|untrusted]
  [--no-probe]`: builds the entry (presets fill command/args or url when neither is given),
  validates it with the P13-01 model, probes it (connect, list tools) unless `--no-probe`; on a
  failed probe saves it disabled only with confirmation (non-TTY: saved disabled with a note);
  an existing name needs confirmation (`--force` overrides; non-TTY without it → exit 1). Header
  values that look like secrets are refused: use `${NAME}`.
- `test <name>`: connect once (OAuth handler in background mode), list tools, report time and tool
  count; on failure print the reason; for HTTP failures the status, URL and the first 200
  characters of the body (redacted). Exit 0 ok, 1 failed or unknown server.
- `remove <name>`: drop the entry (confirm on a TTY unless `--yes`) and its stored OAuth state.
- `enable` / `disable <name>`: `enabled: true|false`.
- `login <name> [--device]`: P13-09 interactive login (needs `auth: oauth` and a URL).
- `configure <name> [--include T ...] [--all]`: probe tools; on a TTY a checklist pre-checked from
  the current filter (include → those; exclude → all but those; none → all); writing `include`
  only when not all are checked (all checked → the filter is removed). Non-TTY: flags only; without
  flags exit 2.
- Every write goes through `writer.save` (comments kept). Removal of keys uses the user's raw
  config with `replace=True` (the writer's own way).
- `/reload-mcp` and `/mcp` in chat: registered by P13-08 (N1) in the service command registry;
  `status_lines(rows)` here is the shared formatter.

Files: `src/indus/cli/commands/mcp.py`, `tests/unit/cli/commands/test_mcp.py`. Shared:
`src/indus/cli/app.py` (REGISTRY row), `tests/contract/cli/non_tty_table.yaml` (rows),
`tests/contract/cli/test_help_catalogue.py` (golden order row).

Edge cases: unknown server → guidance, exit 1; `--command` and `--url`
together → usage error; bad `--env`/`--header` shape → usage error; config managed → error text.

Our design differences / decisions:
- No catalog/picker/install/serve verbs (D-16.45; `mcp serve` lives elsewhere).
- `test` probes in background mode: it never starts a browser login (use `login`).
- Exit codes: `test` 0/1 only (an unknown server is guidance, exit 1): the P07-26 contract allows
  0/1/2; the reference uses 3 for an unknown server.

Tests planned (CliRunner, fake stdio server, in-process HTTP fake): each verb; `configure` writes
`include` for a partial selection and no filter for all; `test` against an HTTP 500 prints status
and body start; non-TTY `configure` without flags exits 2; `add` round trip keeps comments;
`remove` drops OAuth secrets; non-TTY table rows.

## Decisions for the lead
- Presets (ours, minimal): `filesystem` (npx `@modelcontextprotocol/server-filesystem` with
  `${workspaceFolder}`), `fetch` (uvx `mcp-server-fetch`), `memory`, `git`. Names only; the owner
  may extend.
- `add` does not offer the tool checklist (use `configure`); a partial `configure` selection
  always writes `include` (an exclude-mode filter becomes an include list), per the row.
- Shared: `cli/app.py` REGISTRY row `mcp`, `tests/contract/cli/non_tty_table.yaml` rows,
  `tests/contract/cli/test_help_catalogue.py` golden order; ledger lines in
  `docs/internals/acceptance-map.md` (ACC-G19-1).

## Compare checklist (after writing)
(reopened `hermes_cli/mcp_config.py`)
- [x] add: overwrite confirmation, validation, probe, failed probe → save disabled on confirmation (`test_add_probes_then_saves_keeping_comments`, `test_failed_probe_without_a_terminal_saves_it_turned_off`, `test_existing_name_needs_force_without_a_terminal`)
- [x] secrets never in config (ours refuses secret-looking header/env values; the reference prompts for a token into `.env`) (`test_add_refuses_a_secret_in_a_header`, `test_header_with_a_reference_is_kept`)
- [x] list: transport, tool selection summary, enabled state (+ ours: lazy, live states) (`test_list_shows_state_and_tool_selection`)
- [x] test: tools and timing; failure reason redacted, HTTP status/URL/body start (`test_test_lists_the_tools`, `test_test_against_http_500_prints_status_url_and_body_start`)
- [x] remove drops OAuth state (`test_remove_drops_the_entry_and_the_stored_sign_in`); enable/disable (`test_enable_and_disable`)
- [x] configure: pre-check precedence include → exclude → all (`test_configure_pre_checks_from_the_current_filter`); all checked removes the filter (`test_configure_writes_include_for_a_partial_selection_and_removes_it_for_all`); non-TTY needs flags (`test_configure_without_a_terminal_needs_flags`)
- [x] login browser/device (`test_login_runs_the_oauth_sign_in`, `test_login_refuses_a_server_without_oauth`)
- Not taken: catalog/picker/install/serve, `reauth --all`, bearer-token prompt, exit 3.

## Status
Done (2026-10-19). `src/indus/cli/commands/mcp.py`, `tests/unit/cli/commands/test_mcp.py`.
`/reload-mcp` and `/mcp` chat commands are left to P13-08 (N1); `status_rows(scope, live=...)`
and `status_lines(...)` are the formatter they can use.
