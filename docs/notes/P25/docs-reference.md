# Task: Generated reference pages (P25-15) Phase: 25 Owner: builder D1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `website/docs/reference/` (the 14
page titles only, to check coverage: command line, CLI symbols, environment variables, FAQ, MCP
config, model catalogue, optional skills catalogue, package management, profile commands, skills
catalogue, slash commands, tools, toolsets, automation blueprints). Evidence: none.

What it does (own words): `scripts/docs/gen_reference.py` writes the reference section of the user
docs from the running code, so the pages can never drift from what the program accepts. Each page
starts with a marker line saying it is generated and must not be edited. `--check` regenerates in
memory and fails (exit 1, one line per file) when a committed page differs, when a page is missing,
or when a page carrying the marker is no longer produced. Hand-written pages in the same folder
(no marker, e.g. `trajectory-format.md`) are never touched.

Pages and their sources (all discovered from the code, never from lists in the generator):
- `cli.md`: the click tree of `indus` (`indus.cli.app.cli`, lazy commands included, hidden ones
  left out): usage, help text, options (flags, value type, default, help) and subcommands.
- `slash-commands.md`: `indus.service.commands.registry.REGISTRY`, grouped by category, with
  aliases, arguments, subcommands and the surfaces that offer each command.
- `configuration.md`: the Pydantic config models behind `DEFAULTS` (D-16.32): every key as a dotted
  path with type, default and description; free-form sections shown as such.
- `environment-variables.md`: every `INDUS_*` name read anywhere in `src/indus` (AST scan of string
  constants, plus the per-platform policy names the gateway registry builds), the vendor-standard
  key names of the bundled provider catalogue, every messaging platform's credential and policy
  names, and the secrets tools declare.
- `tools.md`: every tool any `register_tools(registry)` function in `src/indus` registers (found by
  an AST scan of the package, each module registered into its own registry so overlaps and failures
  stay local), with toolset and parameters; then the toolsets (`indus.tools.toolsets.TOOLSETS`) and
  what each resolves to.
- `protocol.md`: methods (`METHODS`, with params/result fields and the error codes each may
  return), notifications, events (`EVENTS`), server requests, and the one error-code table
  (`ERROR_TABLE`, D-16.27) with groups and out-of-band codes.
- `mcp-serve-tools.md`: the tools `indus mcp serve` offers, read from a server built by the same
  registration functions the command uses, with their argument schemas.
- `skills.md`: the starter skills shipped in `skills/` (front matter name, description, category).
- `messaging-platforms.md`: the gateway's platform registry (name, label, credential names, message
  cap, install hint).
- `providers.md`: the bundled provider catalogue (name, display name, key and base-URL env names,
  default endpoint).

Inputs → outputs: the installed `indus` package and `skills/` → `docs/user/reference/*.md`.
`python scripts/docs/gen_reference.py` (write), `--check` (stale report), `--out DIR`.

Edge cases / failure modes:
- output must be byte-stable across machines: no timestamps, no home or temp paths, no values that
  depend on the CPU count or the environment; defaults computed at import (paths under the home)
  are shown symbolically; sets are sorted.
- a tool module that needs an optional extra and fails to import: recorded under "Not listed" with
  the module name, so the page still says it exists; the CI lane has no extras, so the committed
  pages are generated the same way.
- an option whose default is a callable is shown as "computed".
- Markdown table cells escape `|` and newlines.
- P24 lands new commands, config sections and tools: they appear on the next run with no change
  to the generator (discovery, not lists); `--check` fails until the pages are regenerated.

Our design differences / decisions:
- One generator, ten pages; the reference has hand-maintained pages plus catalogue extractors.
- Descriptions come from the code's own help strings and docstrings; the generator holds a small
  table of one-line notes for the main `INDUS_*` names whose readers carry no description.
- No FAQ, package-management or profile-commands pages here: profile commands are in `cli.md`;
  the FAQ is not a reference page (user guide covers it).

Tests we will write:
- generator idempotent: two runs give identical bytes; `--check` passes on the committed tree.
- stale check: a page rendered after adding an option to a scratch click command differs, and
  `--check` against a folder holding the old page fails naming it; a missing page and an orphan
  generated page fail; a hand-written page is ignored.
- coverage: every registered tool, every toolset, every slash command, every top-level config
  key, every error code, every protocol method and event, every MCP serve tool, every starter
  skill and every top-level CLI command appears on its page.

## Decisions for the lead
- Generated pages live in `docs/user/reference/` beside the hand-written `trajectory-format.md`;
  the marker comment on line 1 is what tells them apart.
- `ci.yml` checks job runs `gen_reference.py --check` next to the installer freshness checks.

Compare checklist (filled after the rewrite; reopened `website/docs/reference/` titles):
- [x] command reference, slash commands, environment variables, tools, toolsets, skills catalogue, MCP (`tests/unit/docs/test_gen_reference.py::test_every_page_is_produced_with_the_marker`)
- [x] model catalogue → our `providers.md`
- Deliberate differences: profile commands are part of the generated CLI page; config keys get their own page (the reference documents config inside guide pages).
- Not taken: CLI symbols page, optional-skills and automation-blueprint catalogues (we ship no such catalogues), package management (P24 `indus pm` appears in `cli.md` when it lands), FAQ.

## Status

Done (2026-10-21). See the final report.
