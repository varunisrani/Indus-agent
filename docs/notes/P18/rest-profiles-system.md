# Task: REST: profiles, models, files, system operations (P18-16) Phase: 18 Owner: wave-30 builder R3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/web_routers/profiles.py`
(route list: list, create, active get/set, rename, delete, persona, description, model,
export/import), `hermes_cli/web_routers/models.py` (info, options, auxiliary, set),
`hermes_cli/web_routers/files.py` + `hermes_cli/web_server_files.py` (`_resolve_managed_path`:
root rules, `..` refusal, canonical path under the root; sensitive names; token download; streamed
upload to a temp file then rename), `hermes_cli/web_routers/ops.py` (credential pool with 1-based
index, memory provider, doctor/backup/import/checkpoints as spawned actions, shell hooks),
`hermes_cli/web_routers/actions.py` (update check payload, action status tail). F1's notes from
`w29-f1.patch` (`page-logs-files.md`, `rest-client.md`, `lib/{api,restTypes}.ts`). Our
`foundation/profiles.py` (ids, tombstones, sticky active), `cli/commands/profile.py`,
`cli/commands/{doctor,backup,import_cmd,checkpoints,hooks,auth}.py` (rules only: `web` may not
import `cli`), `plugins/shell_hooks.py`, `providers/{registry,profile}.py`,
`providers/auth/{device_code,flows,pool_model,store}.py`, `learning/memory/providers.py`,
`foundation/config/schema.py` (model, auxiliary, memory), `cli/version.py`. Reference closed
before writing. Evidence: none.

What it does (own words): the backend of the Profiles, Models, Files and System pages.

- **Profiles** (machine-level: the profile is in the path, never `?profile=`): list
  `{profiles: [{name, is_default, is_sticky, is_current, model, provider, description,
  has_persona}], active, current}`; create `{name, clone_from?, clone_all?, description?,
  no_skills?}`, rename `PATCH {new_name}` and delete run the real `indus profile …` command in a
  child process (the CLI owns the staging dir, the secret moves, the tombstone and the alias; `web`
  may not import `cli`), and answer with what it said; active `GET` (`{active, current}`) and `POST
  {name}` write the sticky profile; persona `GET`/`PUT` (`PERSONA.md`), description `PUT`
  (`profile.yaml`), model `PUT {provider, model}` (that profile's `config.yaml` via the writer);
  export writes a zip of the profile's settings (no history, runtime state or secrets) and import
  restores one as a new profile. Every name is resolved against live profiles only: a deleted
  (tombstoned) profile is 404 for reads and writes and no route recreates its folder (R04.25).
- **Models** (scoped): `GET /api/model/info` (public: model id, provider, the configured context
  window, nothing else), options (the provider catalogue with `configured` = a key or sign-in is
  present, and the current choice), `POST /api/model/set {scope: main|auxiliary, task?, provider,
  model, base_url?}` through the config writer, auxiliary (each side task's provider/model and the
  main model). Provider login sessions: `POST /api/providers/login/start {provider}` for a
  provider that documents a device-code flow (D-16.11; none does today, so 400 `no_device_flow`),
  `GET /api/providers/login/{id}` polls (`pending | approved | denied | expired | failed |
  cancelled`), `DELETE` cancels. The approved tokens land in the credential pool exactly as
  `indus auth add --type oauth` stores them; no token ever appears in an answer.
- **Files** (machine-level): list, read (text preview up to 512 KiB, else `text: null`),
  download (attachment; built on W1's `?token=` exception, replaced at integration by a single-use
  download ticket, D-16.596), mkdir, upload
  (multipart `path`, `file`; 256 MiB cap, written to a temp file beside the target, renamed when
  complete), delete. With `INDUS_DASHBOARD_FILES_ROOT` set every path is confined to that root
  (relative paths are taken under it); without it browsing starts at the user's home and paths
  must be absolute. `..` is refused, the resolved path (symlinks followed) must stay under the
  root, and secret-holding names (`.env*`, `auth.json`, `secrets*.json`, `config.yaml`, pairing
  and token folders, …) are never listed, read or downloaded.
- **System** (scoped): host stats (CPU count and load, memory, disk; `psutil` when installed);
  update check (install method, behind count from git, cached 24 h, `force=1` re-checks) and apply
  (git installs only, a background `git pull --ff-only`; Docker/Nix/package installs get the
  command to run instead); memory provider get/set; credential pool list (redacted), add
  `{provider, api_key, label?}`, remove by 1-based index; operations as background actions:
  doctor, backup (+ download of the archive it wrote), import of an archive path or an upload,
  checkpoint list + prune, prompt-size breakdown; `GET /api/actions/{name}/status?lines=` tails an
  action's log and says `running` / `done` / `failed`; shell hooks list, create (only with
  `consent: true`, which also records the approval) and remove.
- **actions.py**: one registry per app; an action is a child process (`python -m indus …` with the
  profile's home) or an in-process job, its output in `<home>/logs/dashboard-actions/<name>.log`,
  one live run per name and profile (409 otherwise); status = log tail + exit code.

Inputs → outputs: JSON (multipart for uploads) → JSON / file downloads; errors
`{detail: {code, message}}`.

Files: `src/indus/web/routes/{profiles,models,files,system}.py`, `src/indus/web/actions.py`,
`tests/contract/web/test_profiles_models_routes.py`, `tests/contract/web/
test_files_system_routes.py`, this note. Shared: `web/routes/__init__.py` ROUTERS (+4), trap R04.25.

Edge cases / failure modes:
- Files: `../x` under a locked root → 400; an absolute path outside the root → 403; a symlink in
  the root pointing out → 403; upload over the cap → 413 and no partial file left; existing target
  without `overwrite` → 409; deleting the root → 400; a non-empty folder without `recursive` → 409.
- `?token=` on any route but the download → 401 (W1's gate; pinned here). Since integration:
  `?token=` → 401 on every route, the download included (D-16.596).
- Profiles: invalid id → 400; unknown/deleted → 404; `default` cannot be deleted or renamed (the
  CLI says so → 400 with its message); a read after delete stays 404 and the folder stays gone.
- Actions: unknown name → 404; a second start while running → 409; the log is tailed at most
  2,000 lines.
- Shell hook without `consent: true` → 400 `consent_required`; unknown event → 400.
- Credential pool: index out of range → 404; an entry inherited from the default profile → 409.
- Update apply on a non-git install → 409 with the command.

Our design differences / decisions:
- Profile writes go through the CLI process (one implementation of create/rename/delete).
- Persona file is `PERSONA.md` (our name); no "describe-auto", setup-command, open-terminal or
  desktop overlay routes (desktop surfaces are P20).
- Upload is multipart only (F1's contract); the reference also has a data-URL upload.
- Files has no `/api/fs/*` or `/api/media` family (chat attachments are P18-27's).
- Action log lines are plain text; the status answer adds `state` (`running`/`done`/`failed`).

Tests we will write:
- `test_files_system_routes.py`: forced root: `../` refused, outside 403, symlink escape 403,
  sensitive name hidden; list/read/mkdir/upload/delete round trip; upload cap 413 with no leftovers;
  `?token=` download 200, `?token=` on list 401; actions: fake doctor streams lines then `done`,
  second start 409; shell hook without consent 400, with consent listed + approved; credential pool
  add/list redacted/remove by index; update check cached and forced; route lists pinned.
- `test_profiles_models_routes.py`: list; create + delete through the real CLI; a later read 404s
  and the folder is not recreated (R04.25); persona/description/model round trips; active set;
  model info/set/auxiliary; login start for a provider with no device flow → 400; a fake device
  flow approves and lands in the pool; route lists pinned.

## Decisions for the lead
- Profile create/rename/delete run `indus profile …` as a child process because `web` may not
  import `cli`; moving those three functions into a lower layer (`service`) would let the route
  call them directly.
- Profile export/import: our own zip of the profile's settings (config, `.env` names stay, persona,
  skills, memories; no `state.db`, logs, cache, secrets). Lead may prefer a P07 CLI verb.
- The update check and install method load `indus.cli.version` by module name (stdlib-only module
  living in `cli`); it belongs in `foundation`.
- Profiles and files routes are machine-level writes (no `write_scope`); the ROUTE_LIST marks them
  `scoped: false` and the tests list them as the expected unscoped writes.
- Prompt-size breakdown is not built: no prompt-size breakdown exists below `cli` to run; it needs
  a P04 helper (a follow-up).
- Profile create writes no alias script unless the body says `alias: true` (a dashboard should not
  write into `~/.local/bin` by default).
- `GET /api/files` and friends treat a `~` path against the backend's own `HOME` from its scope;
  without `HOME` browsing starts at `/`.
- Secret-holding names are refused for writes and deletes too, not only reads.

Compare checklist (filled after the rewrite; reopened `files.py` + `web_server_files.py`
resolution and entries, `ops.py` pool/hooks/backup, `actions.py` status, `profiles.py` routes):
- [x] locked root: relative under root, `..` refused, resolved path must stay under the root, parent
  stops at the root (tests: `tests/contract/web/test_files_system_routes.py::test_forced_root_refuses_escapes`,
  `::test_listing_a_locked_root_hides_secrets_and_links_out`,
  `::test_an_absolute_path_outside_the_root_is_refused`)
- [x] sensitive names never listed/read/downloaded (same tests)
- [x] upload streamed to a temp file, cap enforced while streaming, temp removed on failure (test:
  `::test_upload_over_the_cap_leaves_nothing`)
- [x] downloads open in a new tab without a header — at integration by a single-use, path-bound,
  30-second ticket instead of the reference's `?token=` (D-16.596; tests:
  `::test_a_download_ticket_is_good_once_for_its_path`, `::test_an_expired_ticket_is_refused`,
  `::test_a_ticket_for_one_path_opens_no_other_and_is_spent`,
  `::test_another_profile_s_dashboard_does_not_take_the_ticket`,
  `::test_tickets_never_reach_a_log_or_the_audit_file`)
- [x] action status tails the log, lines clamped 1..2,000, unknown name 404 (tests:
  `::test_doctor_status_streams_lines_then_done`, `::test_action_status_unknown_and_idle`)
- [x] pool listing redacted, 1-based index remove, env-held key suppressed not deleted (test:
  `::test_credential_pool_add_list_remove_by_index`)
- [x] backup download confined to the backups folder (test: `::test_backup_runs_the_cli_and_download_is_confined`)
- [x] hook removal also revokes the approval (test: `::test_shell_hook_needs_consent_and_is_recorded`)
- [x] update check cached, `force` re-checks, non-git installs cannot apply (test:
  `::test_update_check_is_cached_and_forced`)
- [x] deleted profile not recreated (test: `tests/contract/web/test_profiles_models_routes.py::test_create_delete_and_a_deleted_profile_is_never_recreated`)
- Deliberate differences: hooks need `consent: true` (the reference makes approval optional); text
  preview instead of a data URL (F1's contract); dangling links are skipped in listings instead of
  shown as placeholders; download cap 256 MiB; memory provider set checks "installed", not a full
  readiness probe; action logs are masked with the profile's known secrets.
- Not taken: security audit action, memory reset, `/api/fs/*`, `/api/media`, profile
  setup-command / open-terminal / describe-auto / desktop overlay, MoA, recommended-default, the
  code-skew guard, update receipts and commit lists, prompt-size breakdown.

## Status

Done (2026-10-20) except the prompt-size breakdown (see Decisions). Shared: `web/routes/__init__.py`
ROUTERS (+4 of 7 lines), traps-ledger R04.25 row.

Integration (P18 REST integrator, 2026-10-20), lead decision 3 (security): no credential rides in
a URL. `POST /api/files/download-ticket {path}` (behind the gate) → `{ticket, expires_in}`; `GET
/api/files/download?path=&ticket=` — the ticket (`web/auth/download_ticket.py`) is good once, for
30 s, for the resolved path it was minted for, in the dashboard process that minted it, and is
checked before the path is looked at (no file-system answer without it); in gated mode the
session cookie is wanted as well. The gate's `QUERY_TOKEN_PATHS` exception is gone
(`TICKET_PATHS` instead) and `?token=` on any `/api` path is 401 `credential_in_url`. F1's Files
page mints the ticket on click (`downloadFile` in `apps/web/src/lib/api.ts`). The prompt-size
breakdown stays a follow-up (lead decision 6). Decisions numbered D-16.596 (download tickets),
D-16.620 (profiles), D-16.621 (files), D-16.622 (system, actions); the builders are one
`rest_app.py`.
