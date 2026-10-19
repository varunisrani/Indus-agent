# Task: Consent: capabilities, dependencies, env (P13-22) Phase: 13 Owner: builder N3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/plugin_capabilities.py`
(capability ids, hash of the declared set, grant storage, pending on update),
`hermes_cli/plugins_cmd_install.py` (order: install → env prompts → enable? → deps consent →
capability consent).
Evidence: none.

What it does (own words): installing or enabling a plugin asks the person, once, for what the plugin
declares it wants beyond plain tools and hooks. Capabilities are consent and audit, not a sandbox: a
plugin runs in the Indus process with the person's rights (stated in `indus plugins info` and the
consent text).

Capabilities (ours, each with a one-line risk note shown at consent):
`tools.override` (replace a built-in tool), `llm.provider` (host model calls on another provider),
`llm.model` (choose the host model), `llm.agent` (attribute calls to another agent id),
`llm.profile` (run under another profile), `llm.task` (use the host's auxiliary task lanes),
`platform.actions` (act on connected chat platforms). An id Indus does not know is ignored and
logged, since nothing would honour a grant of it.

Grants: `plugins.entries.<id>.granted_capabilities: [{name, granted_at}]` and
`plugins.entries.<id>.capabilities_consent: {hash, declared, at}` where `hash` = sha256 of the sorted
declared set the person was shown. A refusal stores the consent record with no grants: the plugin
stays enabled minus those surfaces (`ctx.has_capability` is false). An update that adds capabilities
makes the hash differ; only the new ones are asked about and stay off until granted. Non-interactive
installs, updates and enables never grant and never prompt. `indus plugins capabilities <name>
--grant X / --revoke X` changes grants explicitly (a grant from the command line is the person's own
answer).

Python dependencies (`python_dependencies` in the manifest): a separate question. Every entry must be
pinned exactly (`name==version`), else the install refuses that step. Yes → `uv pip install --python
<this interpreter> <deps>` (runner injectable); no, no terminal, or a failed install → the plugin stays
on disk but disabled, with the command to retry printed.

`requires_env`: names missing from both the scope env and the secret store are asked for; `secret`
entries masked; values go to the profile's `SecretStore`, never config. No terminal → listed as
missing, nothing stored.

After install: "Enable now?" default **no** (`--enable/--no-enable` decide without asking). A memory
provider plugin (category `memory` or kind `exclusive`) asks instead whether to make it the active
`memory.provider`.

Files: `src/indus/plugins/consent.py`, `tests/unit/plugins/test_consent.py`.

Edge cases: config unreadable → nothing granted (fail closed); a grant list in the old string form is
still honoured by `has_capability` (P13-16 reads both); a declared set that shrank keeps earlier
grants of capabilities still declared and drops the others on the next consent.

Our design differences / decisions:
- Our capability ids and texts; no legacy `allow_*` mirror keys except the existing
  `allow_tool_override` opt-in, which P13-16 already treats as equivalent to `tools.override`.
- Dependencies install directly with `uv pip install` after consent; no separate admission
  transaction (no package-manager layer in this rebuild).

Tests we will write: interactive grant stored with hash and time; refusal leaves `has_capability`
false and the record present; update adding a capability asks only for it and leaves it off until
granted; non-TTY install grants nothing, leaves deps uninstalled and the plugin disabled; unpinned
dependency refused; requires_env stores masked secrets in the secret store; memory provider asks for
`memory.provider`.

## Decisions for the lead
- Capability ids above are ours (D-id needed with the scanner ones).

Compare checklist (filled after the rewrite; reopened `hermes_cli/plugin_capabilities.py`,
`hermes_cli/plugins_cmd_install.py`):
- [x] hash over the sorted declared set (`test_consent.py::test_interactive_grant_stored_with_hash`)
- [x] only additions asked on update (`::test_update_adding_capability_stays_off_until_granted`)
- [x] unknown ids dropped (`::test_unknown_capabilities_dropped`)
- [x] non-interactive never grants (`::test_non_tty_grants_nothing`)
- [x] deps consent separate, non-TTY skips and leaves disabled (`::test_non_tty_grants_nothing`)
- Deliberate differences: grants are mappings with a time each (the plan asks "when"); the
  reference stores names plus one consent record.
- Not taken: Node sidecar dependencies.

## Status

Done (2026-10-19).
