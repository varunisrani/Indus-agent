# Task: Completion, slash, shell, config and composer methods (P12-31) Phase: 12 Owner: wave-7 builder G

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tui_gateway/methods_complete.py`
(whole: `paste.collapse`, `complete.path`, `complete.slash`, model key methods),
`tui_gateway/methods_slash.py` (live-session slash outputs, side-effect mirroring; skimmed),
`tui_gateway/slash_worker.py` (worker protocol, watchdog poll and grace; first 90 lines),
`tui_gateway/methods_tools.py` (`command.dispatch`, `slash.exec`, `shell.exec` only). Research
`05-tui.md` §3.9–3.12.1, §3.18, §3.22, R05.12, R05.13, R04.27, R02.22. Ours:
`docs/notes/P06/slash-registry.md`, `slash-dispatch.md`, `bang-quick.md`, P05 approvals/redaction.
Evidence: none.

What it does (own words): the composer helpers the TUI calls besides sessions and prompts.
Completion for slash words (the shared registry, quick commands, skills) and for paths after `@`
(directive hints, `@file:` / `@folder:`, fuzzy basename search for a bare `@word`, else a directory
listing — taken through the session's terminal backend when that backend is not local, so a remote
sandbox shows its own tree and never the host's, R05.13). Slash commands the bridge owns run
in-process from the shared registry with the bridge's own handlers; a command the client must
handle itself (screen, picker, quit) is refused with a distinct "not mine" code, so the client
never mistakes a crash for "not mine" (R05.12). `command.dispatch` classifies a word as built-in,
quick alias, quick exec or skill and returns what the client should do. `!command` runs on the host
through the same dangerous-command detection as the agent (R04.27), in a child environment from the
one env builder (R02.22), with a 30 s limit and redacted, tail-cut output. Config methods read and
write the presentation settings and session switches; model methods list providers and save or drop
a key; paste collapse writes long pastes to a private file; image attach stages images for the next
prompt; drop detection recognises pasted file paths.

Inputs → outputs:
- `complete.slash {text, session_id?}` → `{items[{text, display, meta}]}`.
- `complete.path {word, session_id?, cwd?}` → `{items}` (≤ 30; bare `@` → hints).
- `commands.catalog {session_id?}` → `{commands[{name, aliases, description, category, args_hint,
  kind: builtin|quick|skill, runs: bridge|client|later}]}`.
- `slash.exec {session_id, command}` → `{output, prefill?, submit?, switch_to?}`; codes
  `SLASH_NOT_OWNED` (client-side or later-phase command), `SLASH_WORKER_FAILED` (handler crashed or
  passed its reply timeout: 45 s, floor 5 s), `UNSUPPORTED_COMMAND` (not a command at all).
- `command.dispatch {session_id, name, arg}` → `{kind: exec|alias|skill|send|prefill|plugin, …}`.
- `shell.exec {command, session_id?}` → `{stdout, stderr, code}`; empty → `TEXT_EMPTY`; hardline or
  dangerous → `SHELL_REFUSED`; over 30 s → `SHELL_TIMEOUT`.
- `config.get {key: full|mtime|<key>}`, `config.set {key, value, session_id?, confirm}` → keys
  `model`, `verbose`, `yolo`, `reasoning`, `fast`, `busy`, `theme`, `indicator`, `details`,
  `statusbar`, `mouse`; a model change mid-turn is deferred to the turn's end; a model the provider
  marks expensive needs `confirm: true` (`CONFIRM_REQUIRED`).
- `model.options {session_id?}`, `model.save_key {slug, api_key}`, `model.disconnect {slug}`,
  `reload.mcp {session_id?}`, `paste.collapse {text}` → `<home>/pastes/paste_<n>_<HHMMSS>.txt`
  (mode 0600), `image.attach {session_id, path}`, `input.detect_drop {text}`.

Files: `src/indus/tui_bridge/methods_complete.py`, `methods_slash.py`, `methods_shell.py`,
`methods_config.py`, `methods_composer.py`, `tests/fakes/backend/remote_fs.py`, tests
`tests/contract/tui_bridge/test_methods_complete.py`, `test_methods_slash.py`,
`test_methods_shell.py`, `test_methods_config.py`. Shared: `foundation/config/schema.py` (a `tui`
section: theme, indicator, details, statusbar, mouse, tool_progress) and its pinned-defaults test.

Edge cases / failure modes:
- path completion: hidden entries only when the typed part starts with `.`; VCS/cache folders
  skipped in `@` mode; `@folder:` lists folders only, `@file:` files only; an unreachable backend
  gives no items (never the host tree).
- slash: aliases resolve to the canonical entry; the registry's busy policy `reject` refuses a
  command while a turn runs (`SESSION_BUSY`); a handler exception → `SLASH_WORKER_FAILED` (never
  `SLASH_NOT_OWNED`).
- shell: `!!`-style input is the client's; the bridge gets the bare command. Output is redacted
  before the tail cut so a secret crossing the cut cannot survive as two halves; last 4,000 stdout
  and 2,000 stderr characters.
- config.set: unknown key → `INVALID_PARAMS` (`key` path); value of the wrong type → same; managed
  install → the config writer's refusal mapped to `INTERNAL_ERROR` with its message.
- paste: empty text → `TEXT_EMPTY`; file written atomically with 0600.
- image: missing file, not an image (by signature), over 20 MB → `INVALID_PARAMS` on `path`.

Our design differences / decisions:
- slash runs in-process (the registry is below `indus.cli`), so the worker subprocess fallback is not
  built: every bridge-owned handler lives in `methods_slash.py` and needs no CLI import. The worker
  codes still exist so a crash is distinct from "not mine".
- bridge-owned slash commands: `title`, `undo`, `retry`, `branch`, `compress`, `stop`, `steer`,
  `queue`, `status`, `usage`, `history`, `help`, `version`, `yolo`, `reasoning`, `fast`, `tools`.
  Client-owned: `new`, `clear`, `resume`, `sessions`, `model`, `config`, `quit`, `save`. Commands
  owned by later phases answer `SLASH_NOT_OWNED` with "arrives with Pxx".
- quick commands of type `exec` run through `shell.exec`'s path (guards, env, 30 s); `alias`
  re-dispatches (bounded hops).
- shell refuses dangerous commands outright (no approval card): the agent is the place for those.
- expensive-model confirmation reads `tui.confirm_models` (a list of model ids; empty by default)
  until P08 profiles carry prices.

Tests we will write:
- fake non-local backend lists its own tree for `@folder:` and plain paths; local fuzzy search caps
  at 30 and ranks folders first; bare `@` gives hints.
- slash: owned command runs (`/title`), client command → not-owned code, crashing handler →
  worker-failed code, timeout floor, busy reject; dispatch kinds (skill → send, quick alias, exec).
- shell: `rm -rf /` refused, `sleep 40` → timeout at 30 s with an injected clock, 10,000 characters
  of output keep the last 4,000 with a planted secret redacted, empty refused.
- config get/set round trip, deferred model switch while busy, confirm-required, mtime.
- paste file written 0600 with the placeholder; drop detection; image staging.

## Decisions for the lead
- New `tui` config section (shared schema change) holding the TUI presentation settings and
  `confirm_models`; pinned-defaults test updated.
- `reload.mcp` answers `{reloaded: false}` with a message until P13 provides a reload entry point.
- `model.save_key` stores the key in the profile's `SecretStore` under the provider's first env var
  name; `model.disconnect` deletes every env var name of the provider from it.

Compare checklist (filled after the rewrite; reopened `methods_complete.py`, `methods_tools.py`
`shell.exec` / `slash.exec` / `command.dispatch`):
- [x] remote backend listing, never host fallback (test: `tests/contract/tui_bridge/test_methods_complete.py::test_remote_backend_lists_its_own_tree`)
- [x] fuzzy basename cap 30, folders first on ties (test: `test_methods_complete.py::test_bare_word_fuzzy_search_is_capped`)
- [x] paste file name and placeholder (test: `test_methods_complete.py::test_paste_collapse_writes_a_private_file`)
- [x] shell: empty refused, hardline/dangerous refused, redact then tail (test: `test_methods_shell.py`)
- [x] dispatch stage order quick → skill → built-in; skill resolves to a prompt to send (test: `test_methods_slash.py::test_dispatch_kinds`)
- [x] worker failure distinct from "not mine" (test: `test_methods_slash.py::test_crash_is_not_not_mine`)
- Deliberate differences: the reference keeps a persistent CLI worker per session for slash
  commands; ours runs them in-process. The reference's shell timeout code shares a number with other
  meanings; ours has its own row.
- Not taken: profile mentions (`@profile`), plugin reference providers (P13 seam), code-skew guard,
  managed-install checks for model keys (the secret store refuses on its own), bundles (P09 CLI).

## Status

Done (2026-10-19).
