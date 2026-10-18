# 07 — Learning loop, skills, memory, plugins, cron, MCP client: behavioural specification (clean-room)

Scope: the agent's self-improvement loop (curated memory stores, user profile, periodic review nudges,
background review fork, explicit "learn"/"refine" commands, skill curator), the skill system (format,
discovery, loading, agent-managed writes, hub/catalog install, bundled-skill seeding, bundles), the
plugin system (kinds, manifest, lifecycle, hooks, capabilities, install/update, packs, isolation,
curated catalog), the cron scheduler (schedule grammar, storage, tick and run semantics, prompt
assembly, delivery targets as seen from cron, failure handling), and the MCP client (config,
transports, discovery, lifecycle, filtering, OAuth, sampling/elicitation, curated optional-MCP
catalog). Only the learning-loop semantics of memory/skill/cron/MCP code inside `agent/` and `tools/`
are covered.
Out of scope: turn loop, compaction and prompt assembly basics (`01-*.md`); generic tool registry,
toolsets and the tool-search bridge (`02-*.md`); session DB / FTS internals behind session search
(`03-*.md`); gateway adapters and the delivery transport itself (`06-*.md`).

Reference: `<ref-owner>/<ref-repo>` @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056` (licence MIT); shipped
build checked: none (REA not connected in this session — source reading only).
Sources studied: website docs (user-guide/features: memory, memory-providers, skills, curator, cron,
plugins, hooks, mcp; reference: mcp-config-reference; developer-guide: creating-skills), the
`plugin-catalog/README.md` admission policy, `optional-skills/DESCRIPTION.md`, and code under `agent/`,
`tools/`, `cron/`, `<cli-pkg>/` (the CLI package; real name in §2), `plugins/`, `optional-mcps/`, `skills/`,
`optional-skills/` as listed in §4. No tests were read in depth; test paths are listed for later.

Conventions:
- "Home" = the per-profile state directory (reference default lives under the user's home; rebuild:
  `~/.indus/`, overridable by `INDUS_HOME`). Every store below is per profile.
- "Notes store" = the agent's own durable memory file; "profile store" = the user-profile memory file.
- "Review fork" = a forked agent instance that replays a conversation snapshot after a turn to decide
  what to save. "Curator" = the periodic skill-maintenance pass.
- "Foreground" = a normal user-driven turn; "background review" = writes made by the review fork.
- "Gateway" = the long-lived daemon that hosts sessions and the cron ticker (area 06).
- MUST marks an invariant that the reference defends in code (usually with an issue-number comment).
- Evidence marks: [obs] read directly in source/docs; [inf] inferred from code shape; [unk] unknown.
  No REA Evidence IDs exist for this area yet.

---

## 1. Overview

The area is the agent's long-term adaptation layer. Five cooperating parts:

1. **Curated memory** — two small, bounded text stores (agent notes + user profile) that are
   rendered into the system prompt at session start as a frozen block, plus an optional single
   external memory provider plugin that runs alongside them.
2. **Self-improvement loop** — counters that fire a post-turn background review fork every N user
   turns (memory) or N tool iterations without a skill write (skills). The fork reads the transcript
   and writes memory entries and skill edits through the same tools the foreground agent uses, under
   a tool whitelist and stricter guards. A periodic, idle-triggered curator ages and archives
   agent-created skills and can optionally consolidate them with an LLM pass.
3. **Skills** — directories holding a `SKILL.md` (agentskills.io-compatible frontmatter + markdown
   procedure) and optional `references/ templates/ scripts/ assets/`. Listed compactly in the system
   prompt, loaded on demand (progressive disclosure), invokable as slash commands, writable by the
   agent, installable from a multi-source hub with a security scanner.
4. **Plugins** — Python packages with a manifest that register tools, hooks, slash/CLI commands,
   skills, providers and platform adapters through a context object; opt-in by allow-list, installed
   from git (optionally SHA-pinned via a curated catalog), statically scanned, optionally run in an
   out-of-process host.
5. **Cron + MCP** — a 60-second ticker inside the gateway that runs due jobs as fresh agent sessions
   (or script-only jobs) and delivers the final answer to chat/file targets; an MCP client that
   connects configured stdio/HTTP servers and registers their tools under a prefixed namespace.

```
             user turn ──► agent loop (01) ──► final answer delivered
                               │   ▲  (memory tool / skill-manage tool calls reset counters)
                               │   │
        system prompt ◄── frozen memory block + skill index (+ plugin sections, ≤8000 chars)
                               │
                 post-turn:  counters due? ──yes──► review fork (≤16 iterations, tool whitelist)
                               │                         │ writes: memory add (patch/remove staged),
                               │                         │ skill patch/create/write_file (ledgered)
                               ▼                         ▼
                      skills dir + .usage telemetry ◄── curator (idle ≥2h, every 168h):
                                                          active → stale(14d) → archived(30d)
                                                          [+ optional LLM consolidation]

  gateway ticker (60 s) ──► due jobs ──► preflight ──► fresh agent (cron hint + skills + context)
                                                     └► or script-only job (stdout = message)
                         ──► output doc on disk ──► delivery targets (origin/local/platform/all/…)

  config mcp servers ──► discovery (≤4 concurrent) ──► tools registered as prefixed names
  plugins (allow-list) ──► register(ctx): tools, hooks, commands, skills, providers
```

What the user sees: the agent "remembers" facts across sessions (visible next session, not this one),
grows a library of procedures it consults and edits, can be told to schedule work in plain language,
and gains tools from MCP servers and plugins. Guarantees the reference defends: memory never
auto-truncates; memory writes are threat-scanned; background review cannot silently delete memory
entries; the curator never hard-deletes (archive only); hub/plugin installs pass a scanner whose
`dangerous` verdict cannot be forced; cron runs cannot create cron jobs unless explicitly allowed;
a cron job whose run cannot be recorded is not fired.

## 2. Reference names (rename in rebuild)

| Kind | Names (reference only; they become banned identifiers) |
|---|---|
| Binary / CLI and flags | `hermes`; subcommands `hermes memory setup/status`, `hermes skills browse/search/inspect/install/list/check/update/audit/uninstall/reset/publish/snapshot/tap/trust/untrust/opt-out/opt-in/list-modified/diff`, `hermes bundles list/show/create/delete/reload`, `hermes curator status/run/backup/rollback/pause/resume/pin/unpin/adopt/list-unmanaged/restore/list-archived/archive/prune/ledger/purge`, `hermes journey` (aliases `learning`, `memory-graph`), `hermes plugins install/update/remove/enable/disable/list/search/browse/info/capabilities/check-updates/adopt/trust-update-url/validate/show/pack`, `hermes cron create/edit/list/pause/resume/run/remove/status/tick/runs/history/incidents/doctor/notepad`, `hermes pause/resume`, `hermes mcp/catalog/install/configure/login/add/test`, `hermes hooks list/test`, `hermes -p <profile>`, `hermes -s <skill>`, `hermes -z`; flags `--no-skills`, `--consolidate`, `--dry-run`, `--background`, `--pin/--unpin`, `--paused/--paused-reason`, `--no-agent`, `--script`, `--workdir`, `--continuity/--no-continuity`, `--interpreter`, `--ref`, `--enable/--no-enable`, `--force`, `--preset`; slash commands `/memory pending/approve/reject/approval`, `/skills …`, `/learn`, `/refine`, `/journey` (`/learning`, `/memory-graph`), `/reload-skills`, `/bundles`, `/curator`, `/cron …`, `/plugins`, `/reload-mcp`, `/suggestions`, `/blueprint`, `/plan`, `/new` |
| Env vars | `HERMES_HOME`, `HERMES_CRON_TIMEOUT`, `HERMES_CRON_SCRIPT_TIMEOUT`, `HERMES_CRON_MEDIA_SEND_TIMEOUT`, `HERMES_CRON_MAX_PARALLEL`, `HERMES_CRON_SESSION_DB_TIMEOUT`, `HERMES_CRON_SESSION`, `HERMES_INTERACTIVE`, `HERMES_GATEWAY_SESSION`, `HERMES_EXEC_ASK`, `HERMES_ENABLE_PROJECT_PLUGINS`, `HERMES_SAFE_MODE`, `HERMES_IGNORE_RULES`, `HERMES_WRITE_SAFE_ROOT`, `HERMES_SESSION_*`, `HERMES_LOCAL_STT_COMMAND`; skill template tokens `${HERMES_SKILL_DIR}`, `${HERMES_SESSION_ID}`; third-party but reference-documented: `GITHUB_TOKEN`/`GH_TOKEN`, `TELEGRAM_CRON_THREAD_ID`, `*_HOME_CHANNEL` |
| Files and folders | CLI package dir `hermes_cli/` (written `<cli-pkg>/` elsewhere), `~/.hermes/`, `~/.hermes/profiles/<p>/`, `memories/MEMORY.md`, `memories/USER.md` (+ `.lock`), `pending/memory/`, `pending/skills/<id>.json`, `skills/`, `skills/.hub/{lock.json,taps.json,quarantine/,audit.log}`, `skills/.bundled_manifest`, `skills/.usage.json`, `skills/.archive/`, `skills/.curator_ledger.jsonl`, `skills/.curator_backups/<utc>/skills.tar.gz`, `.curator_backups/blobs/`, `skills/.curator_state`, `skills/.locks/`, `.no-bundled-skills`, `skill-bundles/<slug>.yaml`, `logs/curator/<ts>/{run.json,REPORT.md}`, `cache/project_skill_scans/`, `<repo>/.hermes/skills/`, `<repo>/.agents/skills/`, `<repo>/.hermes/plugins/`, `plugins/`, `plugins/.install-metadata.json`, `plugins-backup/<name>-<sha>/`, `plugin-data/<name>/`, `hermes-pack.yaml`, `plugin.yaml`, `plugin.json`, `hooks/<name>/{HOOK.yaml,handler.py}`, `cron/jobs.json`, `cron/.tick.lock`, `cron/ticker_heartbeat`, `cron/ticker_last_success`, `cron/executions.db`, `cron/output/<job_id>/<ts>.md`, `cron/output/<job_id>/monitor_last_output.txt`, `scripts/`, `runtime/bot_live_delivery/<id>.json`, `state.db`, `config.yaml`, `.env`, `auth.json`, `SOUL.md`, `/etc/hermes/config.yaml`, repo dirs `skills/`, `optional-skills/`, `plugins/`, `plugin-catalog/` (+`removed.yaml`), `optional-mcps/<name>/manifest.yaml`, `skills/index-cache/` |
| HTTP routes / URL schemes | `hermes://skill/install?identifier=…`, `/docs/api/plugin-catalog.json`, gateway `POST /api/jobs`, dashboard `POST /api/cron/jobs`, `{callback_url}/api/cron/fire`, `https://portal.nousresearch.com` |
| RPC methods / events / commands | Model tools: `memory`, `skill_manage`, `skills_list`, `skill_view`, `cronjob_manage` (docs also say `cronjob`), `session_search`, `manage_connections`, `tool_search/tool_describe/tool_call`; MCP prefixed names `mcp__<server>__<tool>`, utilities `list_resources/read_resource/list_prompts/get_prompt`, toolset `mcp-<server>`; plugin context methods `register_tool/register_hook/register_command/register_cli_command/register_skill/register_platform/register_*_provider/register_context_engine/register_approval_transport/register_system_prompt_section/dispatch_tool/inject_message/post_message/call_mcp/llm.complete/has_capability/session_cwd/session_surface/platform_actions`; entry-point group `hermes_agent.plugins`; gateway hook events `gateway:startup`, `session:start/end/reset/compress`, `agent:start/step/end`, `reaction:added/removed`, `command:*`; the 46 plugin hook names in §3.13 |
| Settings and storage keys | `memory.{memory_enabled,user_profile_enabled,memory_char_limit,user_char_limit,write_approval,nudge_interval,provider,prefetch_spill_enabled}`, `skills.{external_dirs,create_dir,project_discovery,trusted_project_dirs,auto_load,template_vars,inline_shell,inline_shell_timeout,guard_agent_created,tier1_advisory,write_approval,ledger,ledger_max_bytes,creation_nudge_interval,disabled,config}`, `curator.{enabled,interval_hours,min_idle_hours,stale_after_days,archive_after_days,consolidate,prune_builtins,archive_ttl_days,backup.enabled,backup.keep}`, `auxiliary.{background_review,curator}.{provider,model,enabled,max_input_tokens,extra_tools,defer,defer_max_age_s,reasoning_effort,timeout}`, `display.memory_notifications`, `plugins.{enabled,disabled,entries.<id>.{granted_capabilities,mcp_allowlist,allow_*},clone_timeout_seconds,hook_callback_timeout,load_timeout_seconds,auto_update_check_hours,auto_apply,isolation,host.launcher,scan_on_install}`, `security.approval.{transport,transport_fallback}`, `cron.{catch_up_missed,allow_agent_scheduling,preflight,model,model_provider,provider,wrap_response,delivery.notify,mirror_delivery,max_parallel_jobs,output_retention,script_timeout_seconds,session_db_timeout_seconds,media_send_timeout_seconds,require_restart_safe_scope,failure_repeat_alert_hours,failure_nudge_threshold,retry_unreachable,cleanup_timeout_seconds,standalone_send_timeout_seconds,misfire_grace_minutes}`, `mcp_servers.<name>.{command,args,env,cwd,url,headers,auth,oauth,client_cert,client_key,identity_header,timeout,connect_timeout,lazy,idle_timeout_seconds,max_lifetime_seconds,enabled,supports_parallel_tool_calls,tools.{include,exclude,resources,prompts},trust,sampling.*,elicitation.*,transport}`, `mcp.{auto_reload_on_config_change,discovery_concurrency}`; job-record keys `last_status`, `blocked_config`, `delivery_failed`, `last_delivery_unverified`, `last_fire_error`, `failure_streak`, `quota_hold_until`, `pending_slot`, `monitor_state`, `context_from` reserved entry `self`; usage-record keys `created_by` (`agent`/`learn`), `pinned`, `state`; skill frontmatter namespace `metadata.hermes.*`; memory block headers "MEMORY (your personal notes)" / "USER PROFILE (who the user is)" |
| Other (scopes, error tags, IPC channels) | Write-origin values `foreground`/`background_review`; scanner id `skills-guard-v10`; trust levels `builtin/official/trusted/community/agent-created`; plugin kinds `standalone/backend/platform/exclusive`; catalog tiers `official/community`; plugin-section markers `<!-- hermes-plugin-sections:start/end -->`, heading prefix "## Plugin Context: "; event namespace `hermes`; cron model-facing tokens `[SILENT]`, `[CRON_FAILURE]`, `wakeAgent`, `## Run Context`, "Cronjob Response:"; mirror label `[Cron delivery: …]`; prompt sentinels "[IMPORTANT: The user has invoked the …"; skill names containing the product name: `hermes-agent`, `hermes-agent-skill-authoring`, `inspecting-hermes-desktop-dom`, optional `hermes-s6-container-supervision`; bundled plugin `hermes-achievements`; provider/auth plugin `nous`; cron scheduler provider `chronos`; desktop SDK `@hermes/plugin-sdk`; Nix option `services.hermes-agent.*` |

Third-party protocol names used by this area (keep exact; check against vendor docs): Model Context
Protocol (MCP) — `initialize`, `tools/list`, `notifications/tools/list_changed`,
`prompts/list_changed`, `resources/list_changed`, `sampling/createMessage`, `elicitation/create`
(form mode vs URL mode), tool annotation `readOnlyHint`, result `_meta`, `structuredContent`,
`ImageContent`; transports stdio, Streamable HTTP, SSE; OAuth 2.1 with PKCE, Dynamic Client
Registration (DCR), Client ID Metadata Document (CIMD), device-code login (RFC 8628); mTLS; the
agentskills.io SKILL.md open standard; skills.sh (and its `skills.sh.json` groupings schema);
`/.well-known/skills/index.json` discovery convention; ClawHub, LobeHub, browse.sh catalog APIs;
GitHub REST API rate limits (60/h unauthenticated, 5,000/h with token); croniter 5-field cron
syntax (0 = Sunday; named weekdays/months); OSV malware database; NVIDIA SkillEvaluator /
SkillSpector (optional advisory scanners); systemd `systemd-run --user --scope`, `loginctl
enable-linger`; Unicode TAG block U+E0000–U+E007F.

## 3. Behaviour

### 3.1 Built-in curated memory (notes store + profile store)

- **Trigger / inputs:** the agent calls the memory tool during any turn (foreground) or during a review
  fork (background). Users can also edit the files by hand or via the journey commands (§3.9).
- **Stores** [obs]:
  - Notes store: environment facts, project conventions, tool quirks, lessons, completed-work diary.
    Default limit **2,200 chars** (~800 tokens at 2.75 chars/token).
  - Profile store: who the user is — name/role/timezone, communication preferences, pet peeves, work
    habits, skill level. Default limit **1,375 chars** (~500 tokens).
  - Both are plain text files in `<home>/memories/`; entries are joined by the delimiter
    newline + `§` + newline. Entries may be multi-line. Character count = length of the joined text
    (delimiters count).
- **Rendering into the prompt** [obs]: at session start each store is read, exact-duplicate entries
  are dropped (first occurrence wins), and a block is rendered: a ruled header naming the store, a
  usage figure "<pct>% — <used>/<limit> chars", then the entries separated by `§` lines. The rendered
  text is captured once as a **frozen snapshot** and never changes for the rest of the session so
  the provider prefix cache stays valid. Writes during the session hit disk immediately and show up
  in tool results, but the prompt only sees them next session (or after a prompt rebuild — area 01).
  An entry that matches a threat pattern at load time is replaced in the snapshot only by a
  "[BLOCKED: …]" placeholder telling the agent to remove it; the in-memory entry list still holds the original so the
  user can see and delete it.
- **Tool operations** [obs]: every call carries a required top-level `target` (`memory` | `user`).
  Single-op shape `{ action: add|patch|remove, target, content?, old_text? }` (`new_text` is an alias
  of `content`) or batch shape `{ target, operations: Op[] }` where all ops use the one target.
  There is no read action (content is already in the prompt) and the old whole-entry `replace`
  action is refused with a pointer to `patch` (writes staged before that change still replay).
  - `add`: append; exact duplicate → success with a "no duplicate added" note.
  - `patch`: `old_text` selects one entry by unique substring; only that span inside the entry is
    replaced by `content`, the rest of the entry is kept. The span must occur exactly once within
    the entry; an exact whole-entry match beats substring matches; to drop text, patch a longer
    span. Matching folds curly quotes/backticks to a straight quote [obs].
  - `remove`: deletes the whole entry selected by unique substring.
  - Missing `old_text` → recoverable error that returns the current entries and usage.
  - No match → error carrying the most similar entries (`closest_entries`); multiple matches →
    error asking for a more specific substring.
- **Limits and overflow** [obs]: there is no automatic compaction. A write whose result would exceed
  the limit fails with a message stating current usage, the size of the new entry, the overflow in
  chars, and instructing a single batch that removes/shortens entries and adds the new one — the
  limit is checked only against the batch's final result. Over-budget failures include the live
  entries; batch-abort failures do not echo the inventory again. After **3** failed consolidation
  attempts in one turn the tool returns a terminal "stop retrying, continue your reply" result so a
  memory side effect can never consume the turn. Files that exceed their limit on load (hand edits,
  external writers) stay loaded with a warning; further adds are refused until under the limit.
- **Integrity** [obs]: writes take an exclusive lock on a sibling lock file (mode 0600, no-follow)
  and replace the store atomically. If a file holds an entry larger than the whole store limit
  (free text written without delimiters) the store refuses to rewrite it, saves a `.bak` snapshot
  and tells the agent how to repair — rewriting would silently lose text.
- **Security** [obs]: content is scanned with the strict threat-pattern set (prompt injection,
  credential exfiltration, SSH backdoors, invisible Unicode) before acceptance; matches are refused.
- **Enable flags** [obs]: notes and profile stores toggle independently. Both off → the tool and its
  guidance block are removed entirely (an external provider is unaffected). One off → the tool stays
  but its schema advertises only the remaining target and writes to the disabled store are refused;
  the prompt carries a narrower guidance block. Removing the memory toolset hides external provider
  tools too.
- **Write approval** [obs]: a single boolean gate (default off) covers both foreground and background
  writes. On: interactive CLI foreground writes prompt inline (entries are short enough to read);
  every other surface (messaging, scripts, review fork) stages the write as a pending record
  `<home>/pending/memory/<id>.json`. Users list/approve/reject staged writes (single id or all) and
  can flip the gate persistently. A staged patch/remove records the full entry it targeted at stage
  time; approval applies only if that entry is unchanged, otherwise the write is refused and stays
  pending. Approval output lists the full text of each entry overwritten or removed.
- **Unattended review delete gate** [obs]: independent of the approval gate, an unattended review
  fork may only `add`. Any `patch`/`remove` (alone or inside a batch) is staged as a proposal for
  approval instead of being applied; if staging fails the call is denied. An explicit user-requested
  review (`/refine`, §3.3) is "attended" and is not subject to this gate.
- **Concurrency note** [obs]: one agent process per home; two writers sharing a home compound each
  other's entries. Memory is scoped per profile.
- **Shapes:** `MemoryStoreState { notes: string[], profile: string[], notes_limit: int,
  profile_limit: int, snapshot: { notes: string, profile: string } }`;
  `MemoryOp { action: add|patch|remove, content?, old_text?, matched_entry? }`;
  `PendingWrite { id, subsystem: memory|skills, payload, summary, origin: foreground|background_review, created_at }`.

### 3.2 External memory providers

- **Model** [obs]: at most **one** external provider plugin is active (chosen by a single config key;
  empty = built-in only). It runs alongside, never instead of, the built-in stores. Bundled providers
  in the reference: two (a cloud memory API with hybrid search; a CLI-backed knowledge tree). Six more
  are catalog plugins (§3.14). On a name collision bundled providers win over user ones (first seen
  wins: bundled → user → project → entry points) — opposite of model-provider plugins.
- **Provider contract** (conceptual) [obs]: `MemoryProvider { name, is_available(), unavailable_reason(),
  initialize(session_id, …), system_prompt_block(), prefetch(query), queue_prefetch(query),
  recall_status(), sync_turn(…), get_tool_schemas(), handle_tool_call(name, args), on_turn_start(n, msg),
  on_session_end(messages), on_session_switch(…), on_pre_compress(messages) -> text,
  on_delegation(task, result, child_session_id), on_memory_write(action, target, content, meta),
  get_config_schema(), save_config(values, home), identity_signature(), backup_paths(), shutdown() }`.
- **Flow per turn** [obs]: inject provider context block into the system prompt; prefetch relevant
  memories in the background before each turn (bounded wait **8 s**); after the response, sync the
  turn and queue the next prefetch; extract at session end; mirror every built-in memory write to the
  provider; expose provider tools. Shutdown drains sync workers for at most **5 s** (daemon threads).
- **Egress scrub** [obs]: everything handed to a provider (turns, tool output, queries, transcripts,
  mirrored writes, delegation results, the provider's own tool args) goes through the secret
  redactor first, even when the user disabled log redaction.
- **Recall size** [obs]: provider recall is returned whole up to a safety ceiling of 10× the hook
  output-spill threshold (min 100,000 chars); an opt-in flag replaces oversized recall with a
  head/tail preview plus a file path.
- **Missing provider** [obs]: if the configured provider is not installed (it moved to the catalog),
  startup attempts a one-time fetch/migration; if still unavailable a warning is emitted once.
- **Nudge interaction** [obs]: the documented advice is to set the memory nudge interval to 0 when
  an external provider auto-extracts.

### 3.3 Periodic nudges and the background review fork

- **Counters** [obs]:
  - Memory counter: +1 per user turn (only if the memory tool is available and a store exists). When
    it reaches `nudge_interval` (default **10**) it resets and a memory review is due. Resumed
    sessions hydrate the counter as (prior user turns mod interval).
  - Skill counter: +1 per tool-calling iteration (API call within the tool loop) while the
    skill-manage tool is available. At turn end, if ≥ `creation_nudge_interval` (default **10**), it
    resets and a skill review is due.
  - Any foreground call to the memory tool resets the memory counter; any call to the skill-manage
    tool resets the skill counter (the agent already did the work).
- **Trigger** [obs]: after the final answer is delivered, and only if the turn produced a final
  response, was not interrupted, the session is not marked to skip reviews (cron runs, safe-worker
  mode, one-shot finite turns), and automatic reviews are enabled (`enabled`, default true;
  config read fails open). Review kind = memory-only, skill-only, or combined.
- **Fork** [obs]: a daemon thread builds a forked agent that copies what the parent is running with
  (same model and provider route, credentials, cached prompt bytes, reasoning settings and tool list) so the
  first request is a warm prefix-cache read. It replays a structural clone of the message snapshot
  (sanitizers cannot touch the live transcript). Max **16** iterations. Its own nudge intervals are
  forced to 0 (no recursion). Approval prompts inside the fork are auto-denied.
- **Input budget** [obs]: the sum of replayed input tokens across the fork's requests is capped:
  explicit `max_input_tokens` (≤0 = unlimited) else 75% of the fork model's context window capped at
  **600,000**; unknown window → **120,000**. The loop stops before crossing the budget.
- **Routing to a cheaper model** [obs]: if the review is routed to a different model, the fork replays
  a digest instead: the last **24** messages verbatim (extended so the kept tail never starts on a
  tool result) plus one synthetic user message summarising older turns (user text clipped to 300
  chars, assistant text to 200, tool names listed). A same-model review always inherits the parent's
  reasoning effort (a configured review effort is ignored with a one-time warning); a routed review
  honours it.
- **Tool whitelist** [obs]: dispatch-side whitelist = the memory + skill toolsets' tools + read-only
  file read/search + any explicitly configured `extra_tools` that the parent already has. The fork
  may *see* the parent's full tool list (cache parity) but cannot dispatch outside the whitelist.
- **What the review is told** (paraphrased semantics, not text) [obs]:
  - Memory: route each fact to exactly one store (user identity/preferences → profile; environment
    facts → notes), save once, or answer that nothing is worth saving.
  - Skills: be active — most sessions warrant at least a small update. Prefer, in order: patch a
    skill that was loaded this session (whoever authored it), patch an existing class-level umbrella,
    add a topical support file under an umbrella (`references/` for depth, `templates/` for starter
    files, `scripts/` for re-runnable actions) with a pointer line in `SKILL.md`, and only then create
    a new class-level skill (never named after a PR, error string, codename or one-day task).
    Corrections of style/format/workflow are first-class skill signals and belong in the governing
    skill body (memory only for cross-cutting preferences, never both). Write lessons as rules with a
    one-clause mechanism, attached to the step they affect; no incident narration, PR numbers, dates
    or quotes; one lesson stated once; don't restate always-loaded context; fix wrong text in place.
    Do not capture environment-dependent failures, negative claims about tools, transient errors,
    one-off narratives, or unresolved failure sequences dressed as a workflow. Note overlaps for the
    curator; never delete skills in a review.
  - Read-before-write is enforced: in a review, patching an existing skill requires a fresh view of
    it during that review; overwriting/removing an existing support file requires viewing that file.
    On refusal, view once and retry once.
- **Write provenance** [obs]: the fork binds write-origin `background_review`. Skills it creates are
  marked curator-managed (§3.8). Memory patch/remove from an unattended fork are staged (§3.1).
- **Notifications** [obs]: after a review, gateway chats show a short line (default on; off | on |
  verbose, per platform). `on` = generic ("memory updated", "skill X patched"); `verbose` adds a
  compact preview/diff snippet. Staged and rolled-back writes are not reported as done. Usage is
  recorded against the session with task label "background review", plus one completion log line.
- **Cancellation** [obs]: a new live turn can cancel an in-flight review; the canceller waits up to
  **2.0 s** for acknowledgement then proceeds.
- **Local GPU deferral** [obs]: when the review would run on the managed local inference server,
  reviews are queued per session (newest snapshot replaces older), run once the machine is idle
  after a settle window, re-queued if pre-empted by a new prompt, and force-run after
  `defer_max_age_s` (default **1800 s**). Queue is in-memory (lost on exit). `never` restores
  immediate spawn. Explicit `/refine` never defers.
- **Manual review (`/refine [focus]`)** [obs]: runs the same review immediately in the background
  against a snapshot, with optional focus text; works even when automatic reviews are disabled;
  counts as attended.
- **Why session boundaries matter** [obs]: messaging chats are one long session, so fresh memories
  stay invisible (frozen snapshot) and compaction runs repeatedly; the docs recommend starting a new
  session at natural boundaries so the forget → recall → search-past-sessions loop can fire.

### 3.4 Explicit learning: "learn" command and foreground skill creation

- **Trigger:** `/learn <anything>` on any surface (CLI, gateway, TUI, dashboard panel with
  directory/URL/free-text fields) [obs].
- **Flow** [obs]: the command builds one standards-guided prompt and submits it as a normal turn; the
  live agent gathers material with its existing tools (file read/search for directories, web
  extraction for URLs, the current conversation, pasted notes) and saves the result through the
  skill-manage tool. No separate ingestion engine or model tool, so it works on every terminal
  backend. The write-approval gate applies.
- **Authoring standard enforced by the prompt** [obs]: description ≤ **60** chars, a standard section
  order (when to use → procedure → pitfalls → verification; optional quick reference), framing in
  terms of the agent's own tools, no invented commands.
- **Large sources** [obs]: books, paper stacks, specs or big doc folders become a knowledge-base
  skill — a lean `SKILL.md` with the core mental models and an index, plus a separate condensed note per chapter
  or topic under `references/`, optional glossary/cheatsheet. Distillation synthesises structure and
  must not reproduce source passages. Re-running on the same topic folds into the existing skill.
- **System-prompt guidance** [obs]: the base prompt asks the agent to save non-trivial workflows as
  skills (multi-step workflows worth repeating, dead ends that led to a working path, user
  corrections). Foreground-created skills are recorded as `learn`-origin: they appear in the
  learning journey but are **not** curator-managed.

### 3.5 Skill format

- **Layout** [obs]: `<skills-root>/[category/]<skill>/SKILL.md` plus optional `references/`,
  `templates/`, `scripts/`, `examples/`, `assets/`. Category directories may carry a
  `DESCRIPTION.md` whose frontmatter description labels the category in the index. Directories
  whose names start with `.` or `_` are ignored by taps.
- **Frontmatter fields** (YAML; conceptual) [obs]:
  `SkillFrontmatter { name, description, version?, author?, license?, platforms?: (macos|linux|windows)[],
  environments?: string[], requires_apps?: string[], required_environment_variables?: EnvReq[],
  metadata?: { <product>: { tags?, category?, fallback_for_toolsets?, requires_toolsets?,
  fallback_for_tools?, requires_tools?, session_platforms?, config?: ConfigVar[],
  blueprint?: { schedule, … } } } }`;
  `EnvReq { name, prompt?, help?, required_for? }`; `ConfigVar { key, description, default, prompt }`.
- **Hard validation on agent writes** [obs]: name ≤ **64** chars matching lowercase start
  `[a-z0-9]` then `[a-z0-9._-]*`; description ≤ **1,024** chars; `SKILL.md` content ≤ **100,000**
  chars; each support file ≤ **1 MiB**; support writes only under `references/ templates/ scripts/
  assets/`.
- **Advisory linter** [obs]: runs on create, `SKILL.md` patches and `references/` writes; returns
  findings in the tool result but never blocks. Three shape rules: body dense in PR/issue numbers
  (incident-log shape), more than **60** reference files (sprawl), body past ~**24k** chars (oversized).
- **Index description budget** [obs]: in the system-prompt index each description is clipped to
  **60** chars (truncation is detected so the agent knows to view).
- **Conditional visibility** [obs]: `platforms` hides a skill on other OSes (index, list, slash
  commands). `fallback_for_*` hides it when the listed toolsets/tools ARE available; `requires_*`
  hides it when they are NOT. `environments` hides unless any declared environment tag is active
  (unknown tags fail open). `requires_apps` hides unless every named app declaration is offerable
  (unknown name fails closed). `session_platforms` restricts to listed session surfaces [obs/inf].
- **Secrets and settings** [obs]: declared env vars do not hide the skill. When loaded in the local
  CLI and missing, the user is prompted securely (skippable); messaging surfaces never ask for
  secrets and point at local setup. Once set, declared vars pass through to sandboxed terminal/code
  execution. Non-secret config vars live under a `skills.config` subtree; when a skill loads its
  resolved values are injected as a "skill config" block.
- **Preprocessing** [obs]: optional template tokens (skill directory path, session id) substituted in
  skill content (default on); optional inline-shell snippets (backtick command prefixed with `!`)
  pre-executed with stdout inlined, **off** by default, **10 s** timeout per snippet, and never for
  community hub installs.
- **Media directives** [obs]: a response containing bare absolute media paths is turned into native
  attachments by the gateway; an "as document" directive forces file-style delivery for every media
  path in that response; an "audio as voice" directive promotes audio to voice bubbles (area 06).
- **Blueprints** [obs]: a skill whose metadata has a `blueprint` block (with a schedule) is also a
  runnable automation; installing it adds a *suggested* cron job (§3.15.11), never a scheduled one.

### 3.6 Skill discovery, resolution and loading

- **Roots and precedence** [obs]: tiers, highest first: **project** (trusted repo's
  `<root>/.<product>/skills/` and `<root>/.agents/skills/`) → **local** (`<home>/skills/`, the
  source of truth) → **create_dir** (optional redirect for agent-created skills) → **external_dirs**
  (list; `~` and `${VAR}` expanded; missing dirs silently skipped). Plugin skills use a
  `plugin:skill` namespace and never collide.
- **Duplicates** [obs]: across tiers the higher tier wins everywhere (index, list, view, slash
  commands, preload, cron); the shadowed copy is logged. Within one tier, two different skills with
  the same name are never guessed between: both are listed by relative path and a bare-name load
  fails with "Ambiguous skill name … use one of …". Byte-identical copies or symlink views resolve
  to the shallowest path.
- **Project skills** [obs]: project root = nearest ancestor containing `.git` (worktrees and
  submodules count; walk-up bound 64 levels). Not loaded until the user trusts the repo (trusted
  roots stored in config); the banner shows a count of found-but-unloaded project skills. Discovery
  can be disabled entirely. Every project skill is scanned with the hub scanner before indexing; a
  `dangerous` verdict quarantines it (hidden, refuses to load by name with an explanation). Scan
  results are cached by content hash outside the repo and re-run on change. Non-interactive
  surfaces (cron, API, ACP) inherit trust decisions and never prompt; project root follows the
  surface's working directory (cron `workdir`; TUI/desktop session workspace). Project skills are
  tagged "[project]" in the index; the curator never modifies them and agent-created skills never
  land there.
- **External dirs are writable** [obs]: existing skills are edited in place wherever they live,
  including external dirs — not a write-protection boundary.
- **Disabled list** [obs]: a config list (optionally per platform) of skill names that are hidden and
  refused by view.
- **System-prompt index** [obs]: a compact "Skills" block grouped by category with name + clipped
  description; computed once and frozen with the system prompt. "Coding posture" can demote some
  categories to a names-only line, but entries are never dropped (agent-created skills are the
  agent's project memory). Unloadable same-tier duplicates get a rename note instead of a row.
- **Mid-session changes** [obs]: on every turn the session rebuilds its skills index and diffs it
  against the copy already embedded in the prompt; added/removed skills are announced once in a short note attached to the
  next user message (names + descriptions of new, names of removed). The system prompt itself only
  picks up the change at the next compaction. A reload command still refreshes slash commands.
- **Progressive disclosure** [obs]: level 0 = list tool returns `{name, description, category}` rows
  (~3k tokens for the full set); level 1 = view tool returns full `SKILL.md` + metadata + linked file
  list; level 2 = view with a relative file path returns one support file. Listing is cached
  **30 s** [obs].
- **Slash commands** [obs]: every visible skill is `/skill-name [instruction]` on every surface. Up to
  **5** leading `/skill` tokens may be stacked in one message; parsing stops at the first token that
  is not an installed skill (so path arguments are safe). The expansion is a fresh user message
  (skill bodies + "user instruction" framing) — the system prompt is never mutated. A skill loaded
  into a conversation increments its `use_count`; a view increments `view_count`.
- **Auto-load list** [obs]: config list of skill names fully loaded into every new session (all
  surfaces, resolved once at first prompt build; missing names warn and skip; an ignore-rules env
  switch suppresses it).
- **Skill bundles** [obs]: tiny YAML files in `<home>/skill-bundles/<slug>.yaml`:
  `Bundle { name?: string (default file stem; slugified), description?, skills: string[] (non-empty),
  instruction? }`. `/<bundle>` loads all listed skills in one user message with the instruction
  prepended; missing skills are skipped with a note; a bundle beats a same-slug skill. CLI:
  list/show/create (interactive or flags, `--force` overwrite)/delete/reload.
- **Plan command** [obs]: `/plan [request]` is now a built-in command (writes a markdown plan under
  a workspace plans dir) rather than a skill.

### 3.7 Agent-managed skill writes (skill-manage tool)

- **Actions** [obs]:
  - `create { name, content (full SKILL.md), category? }` — new skill in the local dir (or
    create_dir; the tool description renders the configured path).
  - `patch { name, old_string, new_string, file_path?, replace_all? }` — targeted replace (preferred;
    empty `new_string` deletes the match); or `patch { name, content }` — full rewrite (legacy alias
    `edit`). Supplying both shapes is an error.
  - `delete { name, absorbed_into? }`.
  - `write_file { name, file_path, file_content }`, `remove_file { name, file_path }`.
  - Batch: `operations[]` applied atomically. Each op shape owns exactly one text slot; an op carrying
    another action's slot fails validation **with nothing in the batch applied**, naming the key to
    move [obs].
- **Guards (each returns a refusal or proceeds)** [obs]:
  - Pinned skill → `delete` refused (patches still allowed), with an unpin hint.
  - Review fork: read-before-write (§3.3); may archive/delete only curator-managed, unpinned skills;
    curator consolidation deletes route to the recoverable archive, not hard delete.
  - Path redirects / escapes out of the skills root refused; delete target validated.
  - Optional post-write security scan of agent-created skills (`guard_agent_created`, default
    **off** because the agent could run the same code through the terminal anyway): a dangerous
    verdict becomes a tool error so the agent retries without the flagged content.
  - Write-approval gate (default off): when on, **every** skill write stages regardless of origin
    (a `SKILL.md` is too big to approve inline). Review via list (one-line gist each), full unified
    diff (truncated on chat surfaces), approve/reject by id or all, toggle persistently. Pending
    records survive restarts.
- **Per-skill lock** [obs]: a lock file under a hidden locks dir keyed by a digest of the basename
  (never inside the skill dir so delete/recreate can't unlink it under a waiter).
- **Post-success side effects** (best effort, never fail the call) [obs]: append a ledger entry
  (§3.8), clear the cached skills system-prompt fragment, update usage telemetry (create records
  origin; patch-type actions bump `patch_count`), fire the `on_skill_lifecycle` plugin hook.
- **Modified bundled/hub skills** [obs]: a bundled or hub skill edited by the agent counts as locally
  modified, so bundled sync and hub update stop overwriting it.

### 3.8 Curator (skill lifecycle maintenance)

- **Trigger** [obs]: inactivity check, not a cron job. Evaluated on CLI session start, during gateway
  housekeeping, and on the serve/dashboard backend's hourly maintenance timer (first poll after
  **90 s**). Runs when enabled, not paused, `last_run_at` older than `interval_hours` (default
  **168** = 7 days), and the agent idle ≥ `min_idle_hours` (default **2**). First observation only
  seeds `last_run_at = now` (a full interval of grace before anything is touched). Serve backends
  skip while a turn in that profile runs; a running messaging gateway for the profile owns the
  chore instead. A claim older than **3,600 s** counts as stale [obs]. Multiple independent serve
  processes can still race the interval check [obs].
- **Jurisdiction** [obs]: only skills marked curator-managed in the usage sidecar: not in the bundled
  manifest, not in the hub lock, and the usage record carries the "agent-managed" marker (a legacy boolean form is also honoured). Only the
  background review fork writes that marker; foreground creates write `learn`. Hand-written,
  external, project, hub and bundled skills are untouched — except bundled ones when
  `prune_builtins` is on (deterministic archival only; never patched/consolidated/deleted). A
  hard-coded "protected built-ins" set (currently empty) is never archived or offered.
- **Phase 1 — deterministic transitions** (always on, no LLM) [obs]: unused ≥ `stale_after_days`
  (default **14**) → `stale`; unused ≥ `archive_after_days` (default **30**) → moved into
  `skills/.archive/`. Skipped: pinned skills; any skill named in any cron job's skill list (even
  paused/disabled jobs). Never-used skills (`use_count == 0`) are not archived until at least
  `stale_after_days` old.
- **Phase 2 — LLM consolidation** (default **off**; `--consolidate` forces once) [obs]: a forked agent
  (auxiliary "curator" model slot; default = main model; generous timeout ~600 s) with effectively
  unbounded iterations (9,999; a full sweep is typically 50–100 calls) surveys only skills it can
  read and write, and per skill keeps, patches, consolidates overlaps into class-level umbrellas, or
  archives via delete-with-`absorbed_into` (no terminal access). Must treat a skill with support
  files as a whole package (keep standalone, re-home files and rewrite paths, or archive intact).
  Consolidation rewrites cron skill references when it renames. The final summary prints an explicit
  old → new rename map.
- **Backups** [obs]: only before a consolidation pass, snapshot the live skill tree (excluding
  archive, ledger, hub state and backups) to a timestamped tar.gz with a manifest (reason, size);
  keep the newest **2** (configurable; can be disabled — then manual backup is also disabled).
  Whole-tree rollback (newest, by id, list, `-y`) first takes a "pre-rollback to <id>" snapshot.
- **Ledger** [obs]: every mutation by any actor appends one JSONL line:
  `LedgerEntry { id, ts, actor: curator|agent|user, action: create|edit|patch|delete|write_file|
  remove_file|archive|restore|purge|rollback, skill, evidence { absorbed_into?, archived?, session_id?,
  file_path? }, before: FileRef[], after: FileRef[] }`, `FileRef { path, sha256 }`; file contents are
  stored once per hash in a blob store. Single-entry rollback restores exactly the touched files
  (removing files the mutation created), takes a safety entry first and **fails closed** if that
  capture fails. Ledger writes are telemetry, never a gate. Size cap default **5 MiB**: past it the
  next append compacts via unchanged-file dedup, then drops oldest entries; the newest entry always
  survives; unreferenced blobs older than **1 hour** are swept; `0` = never maintain.
- **Usage sidecar** [obs]: `SkillUsage { use_count, view_count, patch_count, last_used_at,
  last_viewed_at, last_patched_at, created_at, state: active|stale|archived, pinned, archived_at,
  origin_marker?: agent|learn|null, last_activity_at }`, atomic writes under a cross-process lock,
  bundled and hub skills excluded from telemetry writes.
- **Other operations** [obs]: status (last run, counts, pinned, 5 least-recently-used, managed vs
  unmanaged with reasons "pre-dates marker" / "foreground-created"), run (blocking, `--background`,
  `--dry-run`), pause/resume (persistent), pin/unpin (agent-created only; refused for bundled/hub),
  adopt (declares curator management; does not reset the inactivity clock; refuses bundled, hub,
  external and protected), list-unmanaged, restore (refuses if a bundled/hub skill now owns the
  name), list-archived, archive one, bulk prune (idle ≥ N days, default archive_after_days), purge
  archives older than `archive_ttl_days` (default **0** = never; explicit only; ledgered with blobs).
- **Reports** [obs]: each run writes a timestamped directory with a machine-readable run file and a
  human-readable report. With no candidates the LLM phase is skipped and the report shows the model
  as "(not resolved)" with duration 0 s — not an error.

### 3.9 Learning journey (inspect / prune what was learned)

- [obs] A timeline graph of learned items: skills with a learning signal (created in this profile by
  learn or foreground create, created by the review, or used at least once) and memory chunks.
  Surfaces: CLI render (animated playback with fps/size/colour/json options), TUI overlay, desktop
  panel. Node ids: skill names, or `memory:<source>:<index>:<fingerprint>` for memory chunks.
  Commands: list, delete (skills are archived/restorable; memory chunks removed; confirmation
  unless `-y`), edit (opens the `SKILL.md` or memory chunk in `$EDITOR`).

### 3.10 Skills hub: sources, search, install, update

- **Sources** [obs]: `official` (optional skills shipped in-repo; built-in trust, no third-party
  warning), `skills-sh` (skills.sh directory; alias-style slug resolution), `well-known`
  (`/.well-known/skills/index.json` on a site), `url` (direct `SKILL.md` URL), `github` (repo/path and
  taps), `clawhub`, `lobehub` (agent entries converted to skills), `browse-sh` (site-automation
  skills, identifiers `browse-sh/<hostname>/<task-id>`, content resolved through the per-skill detail
  endpoint). Default GitHub taps include openai/skills, anthropics/skills, huggingface/skills,
  NVIDIA/skills, garrytan/gstack and two science repos (~480 skills grouped under "science").
- **Search** [obs]: unfiltered searches use a cached centralised index (rebuilt periodically; the
  repo ships index-cache snapshots). On no match, the hub also queries skills.sh, ClawHub, LobeHub and
  well-known endpoints live, with at most **8 s** of the search budget; custom taps and provider
  filters do not trigger that fallback. Taps may ship a `skills.sh.json` whose groupings become
  category labels.
- **Install flow** [obs]:
  1. Resolve identifier → source adapter (+ optional `--category`, `--name`).
  2. Fetch into a quarantine dir: `SKILL.md` plus only the files it explicitly references under the
     allow-listed support dirs (URL/GitHub installs don't copy unrelated repo files). `.skillignore`
     / `.clawhubignore` honoured by the scanner.
  3. Static scan of the whole bundle → verdict `safe | caution | dangerous` with findings
     (exfiltration, injection, destructive commands, supply-chain signals, invisible chars, path
     traversal, agent-config edits; documentation-context and comment-context step-downs).
  4. Policy by trust level (safe/caution/dangerous): builtin allow/allow/allow; trusted
     allow/allow/block; community allow/block/block; agent-created allow/allow/ask. `--force`
     overrides `caution` blocks only — never `dangerous`.
  5. Optional advisory second scan (NVIDIA SkillEvaluator tier 1) if the binary is on PATH; findings
     printed (credential-looking ones highlighted), never blocking.
  6. Install-target safety checks (symlink/junction, category bucket, nested skills), move into the
     skills dir, record a lock entry `HubLock { name, source, identifier, install_path, content_hash,
     scanner_version, findings, ts, fresh|cached }`, append to the audit log.
  7. Open conversations get the "new skill" note on their next message (§3.6).
- **URL name resolution** [obs]: frontmatter `name` → URL parent dir/slug if it matches
  `^[a-z][a-z0-9_-]*$` → interactive prompt (TTY) → error pointing at `--name` on non-interactive
  surfaces. URL installs are always `community` trust and update by re-fetching the same URL.
- **Update lifecycle** [obs]: check compares the stored identifier's current upstream bundle hash with
  the recorded one; skips network for orphaned (missing dir) and invalid (unsafe path) records and
  never auto-removes. Update reinstalls only changed skills; locally edited ones (disk hash ≠ install
  hash) are skipped unless `--force`. No overall deadline for a check (a slow source delays later
  entries). Audit re-scans all hub skills. Uninstall removes by lock entry.
- **Taps** [obs]: GitHub repos with skills under a configurable root (default `skills/`); each subdir
  holding `SKILL.md` is a skill; new taps are `community` trust; elevated trust requires adding the
  repo to a hard-coded trusted set (core change). Stored in a taps JSON file. Private repos need a
  token. GitHub API cost ≈ 2 calls per install (files come from raw content, not counted).
- **Desktop deep link** [obs]: a custom URL scheme opens a confirm dialog (source / install-to rows,
  cancel = no change, installing → installed, errors stay in dialog); links cannot bypass scanning
  or choose a different profile.
- **Publish / snapshot** [obs]: publish a local skill to GitHub; export/import skill configuration
  snapshots.

### 3.11 Bundled-skill seeding and sync

- [obs] On install and every update, bundled skills are copied into the local dir and a manifest
  records `name:origin_hash` (v2 lines; v1 bare names auto-migrate). New skills → copied and
  recorded. Existing → refreshed only when upstream changed AND the on-disk copy hashes equal to the
  recorded origin; any mismatch means the user edited it, and it is never touched again. A skill the
  user deleted stays deleted. A skill dropped upstream keeps its manifest row until neither a live
  nor an archived copy exists.
  Generated caches (`__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.pyc` next to
  `.py`) are excluded from hashing.
- [obs] Opt-out marker file in the profile dir stops all future seeding (installer, update, sync);
  `--remove` also deletes bundled skills that are byte-identical to the installed version (with
  confirmation); opt-in removes the marker and optionally re-seeds. Profiles can be created
  without skills.
- [obs] Reset clears one skill's manifest entry (next sync re-baselines on the current copy); with
  restore it deletes the local copy and re-copies the bundled one (confirmation unless `--yes`).
- [obs] Official optional skills are not seeded; installing one records hub-lock provenance
  (with backfill/restore helpers).

### 3.12 Bundled and optional skill catalogues (names + one-line purpose; content is NOT reusable)

The rebuild must author its own skill texts. Names below are inventory only. Every bundled skill
declares licence **MIT** in its frontmatter [obs]. Bundled count: **58** [obs].

| Category | Skill | Purpose (paraphrased) |
|---|---|---|
| apple | apple-notes | Manage macOS Notes through a CLI |
| apple | apple-reminders | Add/list/complete macOS Reminders via CLI |
| apple | findmy | Locate Apple devices/tags through the Find My app |
| apple | imessage | Send/receive iMessage/SMS via a CLI on macOS |
| autonomous-ai-agents | claude-code | Delegate coding work to the Claude Code CLI |
| autonomous-ai-agents | codex | Delegate coding work to the Codex CLI |
| autonomous-ai-agents | computer-use | Desktop control, background-first with escalation |
| autonomous-ai-agents | (self-guide; ref name in §2) | How to use/configure/extend the agent itself |
| autonomous-ai-agents | opencode | Delegate coding/PR review to the OpenCode CLI |
| creative | architecture-diagram | Dark-theme SVG infra diagrams as HTML |
| creative | ascii-video | Convert video/audio to coloured ASCII video |
| creative | baoyu-infographic | Infographic layouts × styles |
| creative | claude-design | One-off HTML artifacts (landing, deck, prototype) |
| creative | design-md | Author/validate/export DESIGN.md token files |
| creative | humanizer | Remove AI-style phrasing from text |
| creative | manim-video | Math/algorithm animations with Manim CE |
| creative | p5js | Generative/interactive p5.js sketches |
| creative | popular-web-designs | Recreate well-known design systems in HTML/CSS |
| creative | songwriting-and-ai-music | Song craft and AI-music prompting |
| devops | sdlc-review | Review kanban hand-offs, route verified outcomes |
| email | email-inbox-triage | Prioritise threads and draft replies safely |
| email | himalaya | IMAP/SMTP email via a terminal client |
| media | gif-search | Find/download GIFs via an API with curl/jq |
| media | songsee | Audio spectrogram/feature extraction CLI |
| media | youtube-content | Turn video transcripts into summaries/posts |
| note-taking | obsidian | Read/search/edit notes in an Obsidian vault |
| productivity | airtable | Airtable REST CRUD via curl |
| productivity | box | Box cloud files, sharing, metadata |
| productivity | document-to-action-items | Extract cited tasks/deadlines from documents |
| productivity | docx | Create/read/edit Word documents |
| productivity | google-workspace | Gmail/Calendar/Drive/Docs/Sheets via CLI or Python |
| productivity | maps | Geocoding, POIs, routing via OpenStreetMap/OSRM |
| productivity | meeting-action-items | Meeting notes → decisions, owners, tickets |
| productivity | notion | Notion API and CLI |
| productivity | pdf | Create/read/merge/fill/OCR PDFs |
| productivity | powerpoint | Create/edit .pptx decks |
| productivity | product-price-monitor | Watch prices and alert at a target |
| productivity | teams-meeting-pipeline | Teams meeting summaries and Graph subscriptions |
| productivity | weekly-review-planning | Weekly reset and next-week plan |
| productivity | xlsx | Create/edit Excel workbooks and CSVs |
| research | arxiv | Search arXiv papers |
| research | competitor-news-monitor | Cited digests of named companies' news |
| research | grounded-citations | Ground answers in verifiable sources |
| research | llm-wiki | Build/query an interlinked markdown knowledge base |
| social-media | xurl | X/Twitter API via a CLI |
| software-development | codebase-inspection | LOC/language stats with a counter tool |
| software-development | dogfood | Exploratory QA of web apps with evidence |
| software-development | github | GitHub via the gh CLI |
| software-development | (skill-authoring; ref name in §2) | Write in-repo `SKILL.md` files correctly |
| software-development | (desktop-DOM inspection; ref name in §2) | Read the desktop app's DOM/CSS over CDP |
| software-development | node-inspect-debugger | Debug Node via inspector protocol |
| software-development | python-debugpy | Debug Python via pdb/debugpy |
| software-development | requesting-code-review | Pre-commit review gates and auto-fix |
| software-development | simplify-code | Parallel clean-up of recent changes |
| software-development | spike | Throwaway experiments before building |
| software-development | systematic-debugging | Four-phase root-cause debugging |
| software-development | test-driven-development | Red-green-refactor discipline |
| web | blocked-page-recovery | Recover when a fetch hits 403/429/paywall/WAF |

Optional (hub `official` source, not seeded) — **153** skills [obs], grouped by category with counts:
autonomous-ai-agents 7 (merge-conflict arbiter, CLI delegation to several coding agents, a dynamic
fan-out workflow, a memory-provider setup guide), blockchain 3 (EVM, Hyperliquid, Solana read-only
clients), communication 1 (1-3-1 decision briefs), creative 30 (presenter video, diagrams, ASCII
art, music/audio generation, comics, fonts, pixel art, Excalidraw/tldraw, video rendering, design
guidance, mockups, Unreal automation, etc.), data-science 1 (live Jupyter kernel), devops 7 (inference
setup, Docker, container supervision, tunnels, setup-wizard generator, feed/API watchers, an AI-app
CLI), dogfood 1 (adversarial UX test), email 1 (agent inboxes), finance 9 (statement/comps/DCF/LBO/
merger workbooks, spreadsheet and deck authoring, prediction markets, stock quotes), gaming 2,
health 2, mcp 3 (build Python MCP servers, manual OAuth for headless gateways, CLI MCP caller),
migration 1 (import another agent's memories/skills), mlops 37 (training, fine-tuning, inference
servers, vector DBs, evaluation, interpretability), payments 3, productivity 10, research 15
(including keyless web search fallbacks, feed monitoring, OSINT, paper writing), security 6
(secret manager CLI, forensics, username search, data-broker removal, authorised pentest, and one
jailbreak toolkit), smart-home 1, social-media 1, software-development 6, web-development 5,
a chat-platform helper 1.
Licences on optional skills [obs]: **9** declare **Apache-2.0** (hyperframes, impeccable, and the
finance set 3-statement-model, comps-analysis, dcf-model, excel-author, lbo-model, merger-model,
pptx-author); the other 144 declare MIT. Several wrap third-party tools with their own licences
(docs flag GPL and a commercial-licence database among science taps) — record per skill if any are
ever ported.

### 3.13 Plugin system

#### 3.13.1 Kinds and discovery

- **Kinds** [obs]: general (`standalone` — tools, hooks, commands, skills; multi-select), `backend`
  (image/video gen, web search, browser, TTS/STT, computer-use drivers; selection by a per-category
  config key), `platform` (gateway channel adapters), memory providers (`exclusive`, single active,
  own loader), context engines (single active, own loader), model providers (many registered, user
  picks one; own lazy loader; user copies override bundled — last writer wins).
- **Sources** [obs]: bundled (repo `plugins/`), user (`<home>/plugins/`), project
  (`./.<product>/plugins/` — **not loaded** by the gateway that hosts sessions; enabling it only
  produces a warning), pip entry points (group in §2), Nix declarative installs (symlinked with a
  managed prefix). Later sources override earlier ones on name collision (user beats bundled) —
  except memory providers (§3.2). Sub-category dirs (`platforms/`, `image_gen/`, `memory/`,
  `context_engine/`, `model-providers/`, `web/`, `browser/`, `video_gen/`, `computer_use/`,
  `cron_providers/`, `dashboard_auth/`) route to specialised discovery one level deeper.
- **Opt-in gate** [obs]: general plugins and user-installed backends/platforms are discovered (listed)
  but not loaded until named in `plugins.enabled`; `plugins.disabled` always wins. Three states:
  enabled / disabled / not-enabled (default for new and bundled general plugins). Bundled platforms,
  bundled backends, memory providers, context engines and model providers bypass the gate (selection
  happens by their own config). An upgrade that introduced the gate grandfathered already-installed
  user plugins into the allow-list (not bundled standalone ones). Toggling a plugin also adds/removes
  its toolset from saved per-platform tool selections when such a selection exists.
- **Manifest** (`plugin.yaml` v1/v2, or a portable `plugin.json`) [obs]:
  `PluginManifest { name, version?, description?, author?, license?, homepage?, tags?, kind?:
  standalone|backend|platform|exclusive, requires_env?: (string|{name,prompt,secret,…})[],
  provides_tools?, provides_hooks?, capabilities?: string[], manifest_version: 1|2 (supported max 2),
  api_version?, requires_plugins?: {name, …}[], requires_<product>?: semver-floor,
  python_dependencies?: string[], config_schema?: map, emits?, listens?, provides_locales?,
  locale_metadata?, portable?, skill_namespace?, update_url? }`. Validation warns and never fails a
  load [obs]. A semver floor newer than the running version makes the loader skip the plugin [obs].
- **Load** [obs]: import module, call `register(ctx)`; per-plugin deadline `load_timeout_seconds`
  (default **10**, 0 = off, max **600**); an overrun is skipped with "load timed out after Ns",
  the stuck thread abandoned, others continue. Load order respects `requires_plugins`. A
  mid-run activation summary says what is live now (slash commands, transform hooks, platform
  callbacks) versus next session; plugin skills and portable MCP servers go live in open chats.

#### 3.13.2 Context API (what `register(ctx)` can do) [obs]

Register tools (schema description is model-facing; registry description is separate metadata),
hooks, slash commands (run in the gateway process: must return text instead of printing; receive
optional session keywords; the session cwd comes from a context helper, not the process cwd),
dispatch a registered tool from a command, register CLI subcommands, inject a message into a
session (by session key or origin), post-and-edit a live status message, register skills
(namespaced `plugin:skill`), register gateway platforms, image/video/TTS/STT/web/browser/computer-use
providers, context engines, terminal environment providers, approval transports, cache-safe system
prompt sections, call MCP tools (§3.16.9), make host-owned one-shot LLM calls (optionally
schema-validated), probe granted capabilities, and act on chat platforms through a capability-gated
verb set (v1: add reaction, set thread title; Telegram and Discord; structured `{ok, error, detail}`
results with stable error codes; every action logged). Per-plugin data dir `<home>/plugin-data/<name>/`
(never the install dir, which update/remove replace). Secrets come only through the host's secret
scope.

#### 3.13.3 Hooks [obs]

46 hook names are accepted (the user doc still says 27 — stale). Categories and semantics:

- **Directive / control:** `pre_tool_call` (per tool call; return block {message} > approve {message?,
  rule_key?} > none; or modify {args} shallow-merged and accumulated across hooks, later wins on the
  same key; timeout or exception **fails closed** = tool blocked); `pre_llm_call` (once per user
  turn after compaction, before the loop; returned `{context}` or plain string is appended to the
  *user message*, never the system prompt; multiple plugins joined by blank lines in discovery order;
  multimodal turns get an extra text part persisted with the turn); `pre_verify` (once per turn when
  code was edited and the agent is about to finish; can return continue+message, bounded by a max
  nudges setting); `pre_gateway_dispatch` (per inbound message before auth: skip/rewrite/allow);
  `transform_api_error_classification` (run all, first valid wins).
- **Transform:** `transform_tool_result`, `transform_terminal_output`, `transform_llm_output` (first
  non-None replacement wins), `pre_transcription` (may mutate prompt/language/model; last writer wins).
- **Observer** (returns ignored): `post_tool_call` (result + duration_ms), `post_llm_call`,
  `pre_api_request`, `post_api_request`, `api_request_error`, `pre_auxiliary_call`,
  `post_auxiliary_call` (aux traffic never reaches the turn-scoped API hooks),
  `on_stream_start/delta/end`, `on_interim_message`, `on_session_start/end/finalize/reset/delete`,
  `post_compaction`, `agent_loop_stopped`, `on_skill_lifecycle`, `subagent_start/stop`,
  `pre_approval_request`, `post_approval_response`, `on_human_input_request/resolved`,
  `pre_command`, `post_gateway_admission`, `gateway_platform_event`, `on_room_member_activity`,
  kanban task/worker hooks (8 names).
- **Rules:** keyword arguments, accept `**kwargs`; exceptions logged and skipped (a repeating
  signature mismatch warned once, then debug); hot-path observers and `pre_tool_call` bounded by
  `hook_callback_timeout` (default **30 s**, 0 = off, max **600**); a hung callback is suppressed for
  **60 s** and at most **3** abandoned workers per callback; caller-thread hooks (`subagent_stop`)
  never moved to a worker. Plugin command handlers awaited up to **30 s** [obs].
- **System-prompt sections:** id `^[a-z0-9][a-z0-9._-]{0,127}$`, unique; only anchor `after_memory`;
  sorted by id; each ≤ **4,000** chars; all sections ≤ **8,000** chars and ≤ **32** sections;
  callable receives `{session_id, model, provider, platform, profile_name, cwd}` once per new
  session; rendered bytes frozen through compaction and recovered from the persisted prompt on resume;
  bad sections skipped with a warning; each accepted section logged.
- **Event bus:** plugins can emit/listen to namespaced events; emit depth cap **8**, pending cap
  **64** [obs].
- **Non-plugin hook systems** (for completeness; trust by placement, not by allow-list) [obs]:
  gateway hook dirs (`HOOK.yaml` with a non-empty `events` list + `handler.py` exposing `handle(event,
  context)`, sync or async, loaded once at gateway start; events listed in §2 with `command:*`
  wildcard); shell hooks declared in config (block/modify/inject, Claude-Code-compatible JSON
  shapes, per-entry timeouts); signed outbound webhooks.

#### 3.13.4 Capabilities and consent [obs]

Declared capabilities (e.g. tool override, LLM provider/model/agent-id/profile/task override, platform
actions) are shown with one-line risk notes at install/enable and asked once; grants are stored per
plugin together with a hash of what was consented and when. A refusal keeps the plugin enabled minus those surfaces
(plugins should probe and degrade). Updates that add capabilities ask again; new ones stay off until
granted. Non-interactive installs/updates never grant. Legacy per-feature `allow_*` keys still open
the same gates (either grants). Capabilities are a consent/audit layer, **not** a sandbox.

#### 3.13.5 Install, update, remove [obs]

- Identifier: catalog name (resolves to repo @ pinned SHA, provenance recorded) | `owner/repo[/subdir]`
  | git URL (flagged custom/unreviewed). `--ref` must be a full 40-char commit SHA (tags, branches,
  short SHAs rejected); checkout is detached and HEAD verified; pinned installs refuse `update`.
- Clone deadline `clone_timeout_seconds` default **300** (max **3600**); subdirectory installs fetch
  only that folder. Anonymous first; on refusal, credentials from a token env var, then the gh CLI
  login, then the git credential helper — sent as a one-shot header, never written to disk config.
- Static security scan before activation (same engine as skills plus plugin-aware exemptions such as a
  provider reading its own declared key): safe → install; caution → findings + confirm (or `--force`);
  dangerous → blocked, `--force` does not override; a dangerous update disables the plugin.
  Documentation prose, test trees/fixtures, comments, changelogs and benign base64 media are scored as
  context (step down a severity, never deleted from the report); agent-facing shapes keep full
  severity. Scan can be turned off by config.
- After install: prompt "enable now?" default **no** (`--enable/--no-enable` for scripts); a memory
  provider instead asks whether to make it the active provider. `requires_env` prompted. Python
  dependency installation is a separate consent step (declining it, or having no TTY, may leave the
  plugin on disk yet switched off); Node sidecar deps prompted separately and stay plugin-local.
- Install metadata (source, revision, pin status; never config values, secrets or grants) recorded
  per profile. Update: pulls latest, local edits autostashed and re-applied; forced reinstall from
  the same source keeps untracked/ignored files and copies tracked edits to a backup dir.
  Update checks run every `auto_update_check_hours` (default **24**, 0 = off); unattended apply is
  opt-in (`auto_apply`, default false). A changed `update_url` must be explicitly trusted.
- Remove drops the plugin from enabled/disabled/entries and resets the memory provider if it was the
  live one.
- **Packs** (`<pack>.yaml`): `Pack { name, description?, author?, version?, plugins: ({name, ref} |
  {repo, ref, subdir?})[], config?: map (non-secret seeds), skills?: string[] (display only) }`. Every
  `ref` must be a 40-char SHA; mandatory review screen then one confirmation, then per-plugin
  capability consent; no `--yes`; non-interactive refused; secret-shaped seed keys, capability grants
  and `allow_*` keys rejected/stripped; existing user values win; per-plugin failures reported,
  non-zero exit if any failed; export includes only git-provenanced plugins.

#### 3.13.6 Isolation [obs]

`isolation: in_process` (default) imports plugins into the agent process. `host` runs user-installed
general plugins in one separate Python process per profile (optional launcher argv prefix for a
sandbox), talking over a private pipe; plugins are unchanged (same ctx). Crashes kill only the host,
the in-flight call returns a tool error, host restarts with bounded retries. Surfaces that need
in-process code (platform adapters, approval transports, SDK-building model providers, streaming /
websocket dashboard endpoints, monkey-patching plugins) refuse with a reason. On a multi-profile
gateway, non-launch profiles always run dashboard plugins in their own host; un-answered hosted HTTP
requests time out after **60 s** (HTTP 504); websocket routes refused (501). A machine-wide managed
config can pin isolation. Reference claims 299 of 348 catalog entries run unchanged in the host.

### 3.14 Plugin catalog (curated)

- [obs] One YAML per entry in an in-repo catalog dir (**688** YAML files including a removal list;
  tiers: **672** community, **14** official among those carrying a tier). Entry shape:
  `CatalogEntry { name, repo, sha (40 hex), description, maintainer, tier: official|community,
  category: desktop|memory|platform|web|tools|voice|automation|models|general, docs_url?, platforms?,
  capabilities: { provides_tools[], provides_hooks[], provides_middleware[], requires_env[] } }`.
  Removal list = kill list: trying to install a name or repo on it fails, and the refusal quotes the
  reason stored with that entry.
- [obs] Live refresh: the documentation site build emits the whole catalog as a single JSON file; clients fetch it,
  fall back to the last valid cached copy regardless of age, then to the in-tree copy.
- [obs] Admission policy (paraphrased, 17 rules): human-merged PRs only; exact SHA pins; no
  self-updating code; SHA bumps are new reviewed PRs; owner/major-contributor submissions or
  maintainer sweeps; declared capabilities must match what is registered at the pin; the install
  scanner runs in CI (dangerous fails, caution reviewed; pinned catalog installs then accept caution
  without prompting); desktop UI plugins limited to the SDK surface (no prototype patching, eval,
  dynamic imports, DOM reach-ins); no runtime overrides of core (validator refuses, warns on private
  imports); dependency policy is the plugin's own within core constraints; credentials only via host
  routes; approvals and unattended runs respected; risky behaviour disclosed; truthful compatibility
  metadata; no skins/forks of bundled plugins; one listing per lineage; no unscannable binaries
  without build attestation. "Cataloged" ≠ audited.
- [obs] CLI: search (names, descriptions, declared tools), browse, info, install by bare name; re-pin
  on catalog moves asks y/N when the new pin adds tools, hooks, deps, capabilities, provider sign-in or
  a desktop half.
- [obs] Bundled plugins (reference repo): browser backends (3), computer-use driver (1), cron
  scheduler provider (1), dashboard auth providers (4), disk cleanup of ephemeral files, a video-call
  joiner, image-gen backends (8), memory providers (2), model providers (~41), platform adapters
  (27), a security-guidance tool-result annotator, a Teams meeting pipeline, video-gen backends (4),
  web search backends (11), plus kanban, achievements and context-engine dirs.

### 3.15 Cron scheduler

#### 3.15.1 Job model and storage

- **Storage** [obs]: one JSON file per profile (a jobs JSON file under `<home>/cron/`), atomic writes, guarded by a
  file lock (wait up to **30 s**); private dir/file permissions; ownership preserved. Per-run output
  documents `<home>/cron/output/<job_id>/<timestamp>.md` (keep newest **50** per job; ≤0 disables
  pruning). An execution ledger (SQLite) and incident ledger live next to it. A malformed hand-edited
  file is repaired on load (non-object entries dropped, invalid completed-counts reset) with a
  type-only warning. Docs advise editing jobs only through the tool/CLI.
- **Record** (conceptual) [obs]: `CronJob { id: 12 hex chars (random), name?, prompt?, skills: string[],
  schedule: Schedule, schedule_display, repeat: { times: int|null (null = forever), completed: int },
  enabled: bool, state: scheduled|paused|completed|error, paused_at?, paused_reason?,
  next_run_at?, last_run_at?, last_status?: ok|error|blocked_config|delivery_failed|delivery_queued|…,
  last_error?, last_delivery_error?, last_delivery_unverified?, last_delivery_queued?,
  last_fire_error?, last_dispatch?, failure_streak, deliver: string, failure_deliver?, origin?:
  { platform, chat_id, thread_id?, … }, enabled_toolsets?: string[], workdir?: abs path,
  model?, provider?, base_url?, pinned?, reasoning_effort?: none|minimal|low|medium|high|xhigh|max|ultra,
  script?, interpreter?, no_agent: bool, monitor_script? | monitor_url?, monitor_state?,
  context_from?: string[] (reserved "self" = continuity), attach_to_session?: bool,
  quota_hold_until?, pending_slot?, fire_claim?, created_at }`.
  `Schedule = { kind: once, run_at } | { kind: interval, minutes } | { kind: cron, expr }` (+ display).
  An authored-field set (name, prompt, skills, model/provider/base_url, script, no_agent, monitor,
  context_from, schedule, deliver, origin, toolsets, workdir, attach, reasoning, failure_deliver) is
  distinguished from scheduler-owned state so imported definitions merge without clobbering run state
  [obs].
- **States** [obs]: `enabled` is authoritative — an enabled job never displays as paused; pause
  markers are a second gate so a half-paused record never fires. `completed` (one-shot done or repeat
  limit reached; record retained for inspection, later pruned) and `error` are terminal, except a
  recurring job in `error` (next run could not be computed, e.g. missing cron library) which stays
  recoverable — a recurring job must never silently become "completed".
- **Defaults at create** [obs]: `deliver` = `origin` when created from a chat with an origin, else
  `local`; one-shot → repeat 1; recurring → forever; repeat ≤0 means forever. A job needs a non-empty
  payload (prompt, script or skills). `--paused` creates `enabled=false`, `state=paused`,
  `next_run_at=null`, timestamp and reason (default reason "created paused; awaiting operator
  approval") in one locked write; reason requires paused.
- **Job reference** [obs]: every mutating verb accepts id or name (case-insensitive); exact id wins;
  ambiguous names are refused with candidate ids.

#### 3.15.2 Schedule grammar (natural language) [obs]

Parsed in this order:
1. Natural day/time phrase, with or without a leading "every": day spec (`day|daily|everyday` → all,
   `weekday(s)` → Mon–Fri, `weekend(s)` → Sat+Sun, or a list of weekday names/abbrevs joined by
   spaces/commas/"and"), optional `at`, then a time: `9am`, `9:30pm`, `14:00`, bare 24h hour `7`,
   `noon`/`midday`, `midnight`. Compiled to a 5-field cron expression (0 = Sunday). Requires the cron
   library.
2. Any other `every <duration>` → recurring interval. Durations: `<n><unit>` with units
   m/min/mins/minute(s), h/hr/hrs/hour(s), d/day(s); a bare unit means 1.
3. Five or more whitespace fields of `[A-Za-z0-9*,-/]` → cron expression (named months/weekdays ok).
4. ISO timestamp (contains `T` or starts `YYYY-MM-DD`) → one-shot; naive times take the **configured
   agent timezone**, not the server's.
5. `in <duration>` → one-shot at now + duration (computed in UTC so DST doesn't stretch it).
6. A bare duration (`30m`) → **recurring** interval.
7. Otherwise a multi-line error listing the accepted forms.

Next-run computation [obs]: intervals anchor on `last_run_at` (else now) and add minutes in UTC;
cron expressions are evaluated on the configured zone's wall clock and re-zoned, with a guard for the
DST fall-back hour (try both folds, pick the earliest instant strictly after the base). A one-shot
whose time passed beyond a **120 s** grace is rejected at create.

#### 3.15.3 Tick, due detection, catch-up

- [obs] The gateway runs a ticker every **60 s** (heartbeat and last-success marker files; status
  reports liveness). A process-wide tick lock prevents overlapping ticks. A standalone `tick`
  command runs one pass for external schedulers, but agent jobs execute only inside a running
  gateway — without one they are skipped with a logged reason and not penalised; script-only jobs
  can run from a bare tick.
- [obs] Due = runnable and `next_run_at` ≤ now. Due jobs run in a per-profile parallel pool
  (`max_parallel_jobs`: unset/0 = unbounded, 1 = serial; env override). A job already in flight in
  this process is not re-dispatched.
- [obs] Fire claims: a claim younger than **300 s** is live (heartbeat **60 s**; heartbeat grace 3×);
  externally delivered fires up to **60 s** early still own the slot. Claim TTLs use 3× the inactivity
  timeout as headroom because the timeout is an inactivity limit, not wall clock.
- [obs] Lateness: grace = half the schedule period clamped to **[120 s, 7,200 s]**. Dispatch within
  **300 s** of schedule = on time; within grace = late; beyond grace = catch-up (all missed slots
  collapse into one run). Catch-up after a gateway outage runs once; a slot already run is never
  re-run; paused jobs never catch up. With catch-up disabled, a job past grace skips ahead
  to the following slot and does not run now (the skip is logged). Resume keeps a slot that came due while
  paused (one catch-up run, or a logged skip).
- [obs] Global emergency pause: no scheduled fire starts through any path (ticker, hosted fire
  webhook answers 503 with Retry-After 60, misfire sweep idle); in-flight runs continue; manual runs
  are an operator override and still execute.
- [obs] Hosted scheduler provider (optional plugin): fires arrive by authenticated callback; a
  misfire sweep runs a job locally if its fire never arrived after `misfire_grace_minutes`
  (default 10; 0 disables). Missed hand-offs stamp `last_fire_error` (cleared on next success).
- [obs] Store not writable (ENOSPC/EROFS/EACCES): a job whose run cannot be recorded is **not** run
  (prevents double fire after recovery); the ticker keeps running and re-probes about once a minute;
  status/list/doctor show it; the gateway posts one notice when it becomes unwritable and one after an
  hour writable again; on recovery each still-due job fires once.

#### 3.15.4 Run flow (agent job) [obs]

1. Claim the fire; record an attempt in the execution ledger (`claimed → running → completed|failed|
   unknown`; terminal immutable) with the exact scheduled instant (so a stale jobs file cannot replay a
   completed occurrence; manual runs don't consume a slot identity).
2. **Preflight** (default on): provider key resolves (skipped for unpinned jobs when a fallback chain
   exists), attached skills ready (required env/commands/credential files), delivery targets known and
   configured (`local`/`origin` never checked), each MCP server named in the job's toolsets resolved
   at least one tool (a server that was connected earlier and is merely reconnecting does not block).
   Failure → `last_status = blocked_config`, **one** alert (not per tick), **no LLM call**; next
   healthy run clears it. Missing-credential messages name the profile and home path read.
3. Optional pre-run **script** (always a file under `<home>/scripts/`, resolved path must stay inside;
   `.sh/.bash` run under bash, else under the agent's Python or a user-pinned interpreter; sanitised
   env without provider secrets; declared pass-through vars resolved through the owning profile's
   secret scope; timeout default **3,600 s**, env → config → default). Its last stdout line may be a
   JSON wake-gate `{wake: false}` (skip this tick silently) or `{wake: true, context: {...}}`.
4. Optional **monitor** source (script or URL; mutually exclusive; incompatible with script-only):
   run first, hash exact output bytes; unchanged → run suppressed (`no_change`); changed/first →
   inject a change block (diff ≤ **4,000** chars, new output ≤ **8,000** chars); source failure is an
   error, never a change, and leaves the stored hash. URL fetch: **30 s** timeout, ≤ **256 KiB**.
5. **Prompt assembly**: a fixed cron hint (paraphrased: you are a scheduled run; your final answer is
   delivered automatically — don't send it yourself; reply with only the silence token when nothing is
   new and never mix it with content; put the failure token alone on line 1 to record a failed run;
   never create or update cron jobs because the task text mentions recurrence), then attached skills
   loaded exactly as slash commands do (with config blocks), then `context_from` outputs (each
   upstream job's most recent completed output; ≤ **8,000** chars each; upstream framing vs
   continuity framing for `self`; silent/no-change/wake-false records skipped when choosing; error
   documents remain eligible), then the per-job notepad (§3.15.10), then the job prompt, then an
   optional per-run `## Run Context` block from a manual run (never persisted). The assembled prompt
   passes the prompt-injection/credential-exfil scan.
6. **Agent**: fresh session, no memory review (background review is skipped for cron), memory stores
   loaded normally; toolsets = job `enabled_toolsets` > the configured cron-platform toolsets >
   built-in defaults (an unreadable toolset config fails the run rather than granting all tools);
   cron-management tools disabled unless `allow_agent_scheduling` (default false); interactive tools
   (clarify/messaging) always denied. `workdir` (absolute existing dir) loads project context files
   from that dir and binds tool cwd to the run's task identity (safe under parallel runs). Model:
   per-job pin > cron fleet model > main model *at fire time*; per-job reasoning effort overrides
   global. Credential-pool rotation applies to all jobs; provider fallback chain only to unpinned
   jobs (a pinned route fails rather than silently switching model). Inactivity timeout default
   **600 s** (env-configurable); progress stamps keep long healthy runs alive.
7. **Result classification**: runtime exception/timeout/unreachable model = failed; first line equal
   to the failure token = failed with the rest as evidence (strict: elsewhere it's content); response
   that is the silence token (entire reply, or alone on the opening or closing line; case-insensitive; bracketless
   "SILENT"/"NO_REPLY"/"NO REPLY" variants accepted) = success with delivery suppressed (output still
   saved). Failed runs always deliver regardless of silence.
8. **Output document** written (prompt + response with length frames; failures include a redacted
   chained traceback section).
9. **Delivery** (§3.15.6) and bookkeeping: `repeat.completed` +1 (not for retry-ladder re-runs),
   next run computed, terminal completion when limits reached. Post-run cleanup bounded at **10 s**
   per operation (0 = legacy unbounded) so a hung finaliser can't wedge the job's in-flight guard.
- **Self-removal** [obs]: a recurring run that removes its own job and then answers still delivers
  that final answer and records `completed`; its output dir is deleted afterwards.

#### 3.15.5 Script-only jobs (no agent) [obs]

`no_agent` requires a script. Trimmed stdout is delivered verbatim; empty stdout = silent tick;
non-zero exit or timeout = error alert delivered; wake-false last line = silent; failure token
ignored (exit code is the signal); no model, no tokens, no fallback. The agent is expected to pick
script-only mode itself when the message is fully determined by the script (watchdogs, thresholds,
heartbeats), writing the script into the scripts dir first. A script argument that looks like a
command line (not a file) is refused at create.

#### 3.15.6 Delivery from cron (transport itself is area 06) [obs]

- Target tokens (comma-combinable, deduplicated by platform/chat/thread): `origin` (creating chat),
  `local` (files only), `<platform>` (that platform's home channel), `<platform>:<chat_id>[:<thread_id>]`,
  named channels where supported, `all` (every platform with a configured home channel, resolved at
  fire time; zero → recorded as a delivery failure), and a "bot-chat[:<profile>]" target that delivers
  into a profile's own canonical bot conversation as an incoming message (costs a full agent turn;
  same-machine profiles only, validated at create; never part of `all`).
- `origin` from inside a cron-run-created job gets pinned when the job is created to the parent job.s own
  concrete target (or `local`) — no dangling origins.
- Delivered text is secret-redacted on every lane even if log redaction is off; a redactor failure
  replaces the payload instead of sending it unscanned. Default wrapper: a header naming the job and a
  footer noting the agent can't see the message (config toggle). Push notification flag set by
  default. Empty payloads are never sent. Media attachments per-file timeout **300 s**; standalone
  (non-live-adapter) send timeout **60 s**.
- Status: execution and delivery tracked separately — `delivery_failed` (with reason) never counts as
  a run failure; adapter acks without message id/raw response are accepted but flagged "unverified";
  bot-chat delivery records a queued receipt (`delivery_queued`) and settles later; a durable delivery
  queue lets a restart-safe worker hand sends to the gateway — a claimed-then-lost send is marked
  unknown and never retried (duplicates are worse than losses).
- Continuable deliveries (opt-in globally or per job): thread-capable platforms open a thread per
  delivery seeded with the brief; DM-only platforms mirror the brief into the DM session as a labelled
  user turn; only the job's own conversation (origin, home fallback, or an explicitly opted-in single
  target) is ever touched; broadcasts never become continuable.

#### 3.15.7 Failure handling [obs]

- `failure_streak` counts consecutive failed runs (delivery failures excluded; pre-agent crashes
  included). For recurring jobs, reaching the threshold (default **3**; 0 disables) appends a review
  nudge (fix / pause / remove) to the failure message. Success resets. One-shots never nudge.
- **Unreachable-model re-runs**: a recurring job that failed with a transient network/DNS error before
  any model call re-runs after **5, 15 and 30 minutes**, then returns to schedule; interim failure
  notices suppressed; re-runs don't count toward repeat; never past the next natural occurrence;
  one-shots excluded; disable via config.
- **Quota hold**: when a subscription provider reports its usage window closed with a retry-after and
  the whole fallback chain is unavailable, the job is parked until that instant (one alert, missed
  occurrences coalesced; sparse schedules retry once at the reopening boundary if it is at least half
  a period early). Mid-run 429s are not held. One-shots not held.
- **Incidents**: failures grouped by (job, normalised error signature — case, whitespace and measured
  durations ignored; signature from first **200** chars; stored error redacted and truncated to
  **500**). States `detected → alerted → resolved | closed(ack)`. First failure of a signature alerts;
  repeats silent while alerted, with one reminder after `failure_repeat_alert_hours` (default **6**;
  0 = alert every run). A different error alerts at once; success resolves open incidents (a repeat
  re-opens); acknowledged incidents stay silent. Ledger unreadable → deliver the ping.
- **Execution ledger reaping**: an abandoned attempt becomes `unknown` only when the owner PID +
  process-start fingerprint prove it dead, or a live owner has been silent longer than
  max(3 × inactivity timeout, script timeout, 2 h). Unknown attempts are never re-run. Checked every
  tick and before every manual run. History bounded; active attempts never pruned.
- **Doctor**: read-only health report per active job (last run failed, delivery failed or unverified,
  late/catch-up dispatch, fire hand-off error, missing or >15-minute-overdue next run, missing/escaping
  script, script-only without script, vanished workdir); exit 1 while any finding stands.
- **Restart-safe workers**: under a service manager, each run executes in a separate worker process
  launched in a transient user scope so gateway restarts don't kill it; without a user session it
  degrades to an unscoped subprocess (warn once) unless configured to fail closed. A worker that dies
  before acknowledging records its stderr tail.

#### 3.15.8 Agent-facing cron tool [obs]

One tool with `action ∈ {create, list, update, pause, resume, remove, run}`. Model-visible fields: job
id, prompt, schedule, name, repeat, deliver, failure deliver, skill/skills, script, monitor, no-agent,
context_from, continuity, enabled toolsets, workdir, attach-to-session, paused/paused reason, pinned.
Model/provider/base_url and reasoning effort are **not** agent-settable (inference pins are user-owned;
`pinned=true` only locks the current main model and only when the user asks). `update` with
`skills=[]` clears skills. `run` is asynchronous: returns a handle immediately; the outcome re-enters
the conversation when done; a running job is refused "already running"; surfaces that can't receive
detached results fall back to synchronous. Tool availability: interactive CLI, gateway sessions, or a
cron session (then gated by `allow_agent_scheduling`). Creation paths reject specs that would restart
or stop the gateway from inside the gateway (restart-loop guard) and prompts matching injection/
exfil/invisible-Unicode patterns (create and update). Per-job `base_url` overrides need an explicit
provider and must share the configured endpoint's scheme/host/port so a stored key is never sent
elsewhere.

#### 3.15.9 Human-facing cron surfaces [obs]

Slash `/cron add|list|edit|pause|resume|run|remove` and CLI create/edit (`--skill` replaces list,
`--add-skill`, `--remove-skill`, `--clear-skills`, `--workdir ""` clears), list (overdue labelling,
streaks, delivery warnings), status (scheduler alive, heartbeat, last success, soonest run ordered by
instant), tick, runs/history, incidents (+ack), doctor, notepad. Next runs more than **15 min** in the
past display as overdue.

#### 3.15.10 Notepad [obs]

Per-job durable key-value store (cursors, watermarks) in profile-local SQLite, injected into each run's
prompt; ≤ **16 KB** per value and ≤ **64 KB** per job (key+value); oversize writes raise and leave the
store untouched; written via a CLI the agent can call through the terminal (no model tool).

#### 3.15.11 Suggestions and blueprints [obs]

A unified "suggested job" surface: proposals from a curated starter catalog, skill `blueprint` blocks,
the self-improvement review ("usage"), and connected integrations. Accepting calls the normal job
creation with the stored spec; dismissing latches by a dedup key so it is never re-offered; nothing
auto-schedules. Blueprints define slot schemas (human parts like time and weekdays only; the
recurrence is a fixed template) rendered as dashboard forms, pre-filled slash commands and agent seed
prompts.

### 3.16 MCP client

#### 3.16.1 Configuration [obs]

`mcp_servers` map in profile config: `McpServer { command?, args?, env?, cwd?, url?, transport?: http|sse,
headers?, auth?: oauth, oauth?: { client_id?, client_secret?, client_name?, redirect_host?,
redirect_port?, timeout? }, client_cert?: path | [cert, key] | [cert, key, password], client_key?,
identity_header?: { name, value_from: static|profile, value? }, timeout? (tool call, default 300 s),
connect_timeout? (default 60 s; also bounds initialize), lazy?: bool, idle_timeout_seconds?: 0=never,
max_lifetime_seconds?: 0=never, enabled?: bool, supports_parallel_tool_calls?: bool,
tools?: { include?: (name|glob)[], exclude?: (name|glob)[], resources?: bool, prompts?: bool },
trust?: full|untrusted, sampling?: SamplingCfg, elicitation?: { enabled, timeout } }`.
`${VAR}` placeholders in command/args/url/headers are resolved at connect time from the environment
(including the profile's env file); context variables `${userHome}`, `${workspaceFolder}`,
`${workspaceFolderBasename}`, `${pathSeparator}`/`${/}` are substituted case-sensitively. Stdio `cwd`
defaults to the pinned session working directory when one exists.

#### 3.16.2 Transports and security baseline [obs]

- Stdio servers get a filtered environment: explicit `env` plus a safe baseline (PATH, HOME, USER,
  LANG, LC_ALL, TERM, SHELL, TMPDIR, `XDG_*` and a few case-insensitive keys) — never the full shell env.
- Before spawning a stdio package command, an OSV malware check runs off-loop with a **12 s** wall
  bound, fail-open on timeout.
- HTTP/SSE honour standard proxy env vars (socks alias normalised), OS proxy settings, and `NO_PROXY`
  (CIDR and wildcard). mTLS supported (missing files → server-scoped clear error). Identity header:
  static value or the active profile name; an explicit same-name header wins; invalid blocks warned
  and ignored; ignored on stdio.

#### 3.16.3 Discovery and registration [obs]

- At startup / reload / config-watch reconcile, servers connect at most **4** at a time (`0` =
  unlimited); every wave gets **120 s**, and the full pass is bounded by **300 s**. List calls page at most
  **50** pages. A startup summary names every server that failed with its error, and marks those
  skipped while in retry cooldown.
- Tool names: `<prefix><server>__<tool>` where each component has every char outside `[A-Za-z0-9_]`
  replaced by `_`, prefix is the literal `mcp` plus two underscores; names over **64** chars are
  truncated with an `_` + 8-hex-char sha256 suffix (warn once). Descriptions have Unicode TAG chars
  stripped; missing description → generic text. The reference docs show a single-underscore form —
  the code uses double underscores.
- Each server with ≥1 registered tool forms a runtime toolset `mcp-<server>`; a fully filtered server
  creates none.
- Filters: `include` wins over `exclude` when both set; globs (`*`, `?`, `[`) are case-sensitive
  fnmatch, plain entries exact.
- Utility wrappers (list/read resources, list/get prompts) registered only if the config allows them
  **and** the session advertises the capability.
- Schema cache written on every live connect; `lazy` servers register from cache and connect on first
  call (first run of a new/changed server is always eager). Status shows "lazy, starts on first use".
- MCP and plugin tools may be hidden behind the generic tool-search bridge (area 02).

#### 3.16.4 Runtime behaviour [obs]

- `notifications/tools/list_changed` → lock-protected re-fetch and registry update;
  prompts/resources change notifications received but ignored.
- `/reload-mcp` re-reads config and refreshes tools (also the way to re-probe availability-gated
  tools); it invalidates the provider prompt cache. CLI watcher auto-reload (default on) with a 30 s
  timeout; the gateway watches config and connects/tears down servers within about a minute.
- Reconnect: first connect up to **3** retries; reconnect up to **5** retries with exponential backoff
  capped at **60 s** (±20% jitter); failed servers retried on a cooldown starting **30 s**, doubling to
  **10 min**; parked servers re-probed every **300 s**; recycled reconnect timeout **15 s**; dead stdio
  respawn wait **15 s**; keepalive RPC timeout **30 s**; graceful termination grace **3 s**.
- Per-server circuit breaker: **3** consecutive failures → open for **60 s** (calls short-circuit with
  "server unreachable"/open-breaker wording), then half-open probe. Keyed per consuming profile.
- Trust tier: `untrusted` → every call to a tool whose discovered `readOnlyHint` is not exactly true
  needs human approval before the RPC (malformed hint fails closed; missing trust = full; an
  unrecognised value = untrusted).
- Tool results: Unicode TAG chars stripped (flag emoji sequences preserved); vendor `_meta` passed
  through, protocol-reserved key prefixes dropped; `structuredContent` kept; hard cap **2,000,000**
  chars; resources ≤ **50 MiB**; images saved to a cache and returned as a media path, with up to **4**
  attached natively when the main model accepts images (≤16 candidates prepared), downscaled to a byte
  target.
- Parallel tool calls from one server only when `supports_parallel_tool_calls: true`.
- Stdio recycling after idle/lifetime limits, transparent restart on next call.
- Expired OAuth in background contexts never opens a browser: the server parks with a log warning
  until the user re-authorises; only the timed self-probe retries a rejected token.

#### 3.16.5 OAuth [obs]

`auth: oauth` → discovery, client identification (CIMD where supported, else DCR; pre-registered client
when configured), PKCE, token exchange, refresh, step-up — via the MCP SDK. Interactive login waits
**5 minutes** by default (configurable `oauth.timeout`; waits timeout + 15 s or connect timeout,
whichever longer). Device-code login supported. Pre-registered clients pin a loopback redirect port, so
the approving browser must be on the same host (SSH port-forward otherwise). One vendor-specific quirk:
a DCR allow-list keyed on client name is auto-handled for one host. Token refresh is fenced (60 s).

#### 3.16.6 Sampling and elicitation [obs]

- Sampling (server asks the client for an LLM completion): enabled by default per server;
  `SamplingCfg { enabled: true, model?, max_tokens_cap: 4096, timeout: 30 s, max_rpm: 10,
  max_tool_rounds: 5, allowed_models: [] (any), log_level: info }`; sliding-window rate limiter,
  per-request timeout, tool-loop depth limit, per-server metrics.
- Elicitation: form-mode requests routed through the existing approval surface (CLI prompt or chat
  buttons); URL-mode declined; enabled by default; timeout **300 s**.

#### 3.16.7 CLI management [obs]

Interactive picker (catalog + configured servers with status available / enabled / installed
(disabled) / custom), plain catalog listing, install by name, configure (tool checklist), login,
add (with presets, e.g. one wiring a third-party coding CLI's MCP server mode), test (reports HTTP
status, URL and start of body on opaque SDK errors), enable/disable/remove. In chat, the agent's
connection-management tool can trigger a setup card (fields → connect/cancel → authorise link, never
auto-opening a browser → save only after the server accepted the token and first connect returned →
tools registered same turn; "Authorized. Tools unavailable." if listing fails).

#### 3.16.8 Optional MCP catalog [obs]

- **65** curated manifests (presence = approval; no community tier; MCPs are never auto-updated —
  reinstall to refresh). All are remote HTTP; **10** need no auth (alltrails, aws-knowledge, context7,
  deepwiki, kiwi, microsoft-learn, trivago, twilio-docs, unreal-engine, wolfram), the rest use OAuth.
  Names: airtable, algolia, alltrails, amplitude, asana, atlassian, attio, aws-knowledge, betterstack,
  buildkite, calendly, canva, circleci, clickup, close, cloudflare, cloudinary, comfy-cloud, context7,
  craft, datadog, deepwiki, dropbox, figma, fireflies, gamma, gitlab, globalping, grafana, hugging_face,
  indeed, intercom, kiwi, klaviyo, linear, microsoft-learn, miro, mixpanel, monday, motherduck,
  n8n-official, neon, netlify, notion, paypal, plaid, postman, prisma-postgres, railway, robinhood,
  semgrep, sentry, square, strava, stripe, supabase, todoist, trivago, twelve-data, twilio-docs,
  unreal-engine, vercel, webflow, wolfram, wordpress-com. GitHub deliberately excluded (requires each
  client's own OAuth app; the bundled gh-CLI skill is preferred).
- **Manifest shape**: `McpManifest { manifest_version: 1, name, connector_slug?, description, source (URL),
  transport: { type: http|stdio, url? | command?, args? }, auth: { type: none|oauth|api_key, provider?,
  env?: EnvReq[], oauth?: pinned client }, install?: { git source/ref, bootstrap: string[] },
  tools?: { default_enabled?: string[], default_excluded?: (name|glob)[] }, suggest?: { keywords[],
  hosts[], applications? (≤16), requires_app?, examples? (≤6) }, post_install?: text }`.
  `${INSTALL_DIR}` is an install-time placeholder distinct from runtime `${VAR}`.
- **Install flow**: show source URL → prompt for API keys / non-secret values (written to the profile env
  file; config holds only `${VAR}` references) or set `auth: oauth` → probe the server's tool list →
  checklist pre-checked from (1) the user's prior selection, (2) manifest `default_enabled`, (3) all;
  only checked tools are written as `include` (all checked → no filter). Manifests with
  `default_excluded` (huge surfaces, e.g. ~3,300 endpoint tools) skip the checklist and write
  `exclude`. Probe failure still installs (manifest default or no filter); `configure` re-runs the
  checklist later. Newer `manifest_version` than supported → warning row, not hidden.
- **Suggestions**: `suggest` keywords/hosts drive one-click "add server" pills in the desktop composer
  (advisory only).

#### 3.16.9 Plugin access to MCP [obs]

A plugin may call a configured server's tool synchronously through the same client (same connections,
breaker, trust gates): `call_mcp(server, tool, args?, timeout=30 s clamped 1–600) → { ok, result,
structuredContent? } | { ok: false, error }`; results over ~64 KB truncated and flagged. Default-off:
each plugin needs an explicit per-server allow-list (no wildcard); calling an unlisted server raises a
permission error naming the config key.

#### 3.16.10 Serving the agent as an MCP server

Out of scope here beyond noting it exists (the agent can expose its own tools/events over MCP); see
area 02 or 06 [obs].

## 4. File map (what to read in the Read step)

Paths are relative to the reference root. "CLI pkg" = `<cli-pkg>/`, the reference CLI package directory (name in §2).

| Sub-area | Reference path | Lines | Read what | Docs / tests to read with it |
|---|---|---|---|---|
| Memory tool entry + gates | `tools/memory_tool.py` | 485 | all: gate/stage, unattended delete gate, schema overrides per enabled target | `website/docs/user-guide/features/memory.md`; `tests/agent/test_skip_memory_store.py` |
| Memory store | `tools/memory_tool_store.py` | 700 | all: delimiter, rendering, matching, batch budget, drift guard, lock, retry cap | same |
| Write-approval store | `tools/write_approval.py` | 413 | gate decision, staging, approve/replay | memory.md §write approval |
| Threat patterns | `tools/threat_patterns.py` | 162 | scopes (strict vs others) only | security docs |
| External memory manager | `agent/memory_manager.py` | 890 | fan-out, one-provider rule, prefetch 8 s, drain 5 s, scrub | `features/memory-providers.md`, `developer-guide/memory-provider-plugin.md` |
| Provider contract | `agent/memory_provider.py` | 225 | the abstract method list | same |
| Memory/agent init | `agent/agent_init.py` | (giant) | only `_init_memory` and `_apply_agent_section` (nudge defaults) | — |
| Nudge counters | `agent/turn_context.py`, `agent/turn_iteration_prep.py`, `agent/turn_finalizer.py`, `agent/tool_executor.py` | (large) | search `_turns_since_memory`, `_iters_since_skill`, `_spawn_background_review` | `tests/agent/test_skip_background_review.py` |
| Background review | `agent/background_review.py` | 1349 | constants, digest, whitelist, fork build, prompt semantics (do not copy text) | `tests/agent/test_background_review*.py` (12 files) |
| Review engine / idle queue | `agent/review_engine.py`, `agent/review_idle_queue.py` | 195, 213 | skim | memory.md §defer |
| Write provenance | `tools/skill_provenance.py` | 44 | all | — |
| Learn prompt | `agent/learn_prompt.py` | 197 | structure only (don't copy prompt) | skills.md §learn |
| Journey graph | `agent/learning_graph.py`, `agent/learning_mutations.py` | 255, 207 | node selection rules, delete/edit | memory.md §journey |
| Curator | `agent/curator.py` | 1212 | defaults, `should_run_now`, transitions, candidate filter, fork setup | `features/curator.md`; `tests/agent/test_curator*.py`, `tests/<cli-pkg>/test_curator_*.py` |
| Curator backups | `agent/curator_backup.py` | 492 | snapshot scope, keep, pre-rollback | same |
| Curator CLI | `CLI pkg/curator.py` | 730 | command semantics | same |
| Usage telemetry | `tools/skill_usage.py` | 748 | record shape, states, protected set, eligibility | curator.md §telemetry |
| Skill ledger | `tools/skill_ledger.py` | 633 | entry shape, blobs, rollback fail-closed, size cap | curator.md §ledger |
| Skill manage tool | `tools/skill_manager_tool.py` | 928 | limits, actions, side effects, staging | skills.md §agent-managed; `tests/tools/test_skill_manage_schema_diet.py` |
| Skill manage guards / batch | `tools/skill_manager_guards.py`, `tools/skill_manager_batch.py` | 252, 308 | each guard; batch atomicity | — |
| Skill linter | `tools/skill_linter.py` | 215 | rule ids + thresholds | CONTRIBUTING skill standards |
| Skill list/view | `tools/skills_tool.py` (+`skills_tool_setup/plugin/dedup.py`) | 765 (+151/173/56) | progressive disclosure, linked files, env setup | skills.md |
| Skill utils | `agent/skill_utils.py` | 871 | tiers, precedence, project trust, conditions, config vars, desc limit 60, stack 5 | skills.md §external/project |
| Skill commands | `agent/skill_commands.py` | 1001 | slash expansion, stacking, auto-load | skills.md §using |
| Bundles / preprocessing / index delta | `agent/skill_bundles.py`, `agent/skill_preprocessing.py`, `agent/skills_index_delta.py` | 213, 156, 136 | all | skills.md §bundles |
| Skills index in prompt | `agent/prompt_builder.py` | 1779 (giant) | only `build_skills_system_prompt` and helpers below it | — |
| Hub guard | `tools/skills_guard.py` | 958 | policy matrix, verdict logic, context step-downs; skip the regex corpus | skills.md §security |
| Bundled sync | `tools/skills_sync.py`, `tools/skills_sync_bundled_ops.py`, `tools/skills_sync_optional.py` | 494, 260, 235 | manifest rules | skills.md §reset |
| Hub core + adapters | `tools/skills_hub*.py` | 454/325/270/340/430/618/400/382/577 | install pipeline, lock, taps, search fallback; adapters skim | skills.md §hub |
| Hub CLI | `CLI pkg/skills_hub.py` | 1731 | command surface | — |
| Plugin manager | `CLI pkg/plugins.py` | 2235 (giant) | `VALID_HOOKS`, `PluginContext` method list | `features/plugins.md`, `features/hooks.md`, `developer-guide/plugins/index.md` |
| Manifest / discovery / loader / dispatch | `CLI pkg/plugins_manifest.py`, `plugins_discovery.py`, `plugins_loader.py`, `plugins_dispatch.py` | 560, 313, 762, 687 | manifest fields; gate; load timeout; hook timeouts & prompt sections | hooks.md |
| Capabilities / host / packs | `CLI pkg/plugin_capabilities.py`, `plugin_host.py`, `plugin_packs.py`, `plugin_isolation.py` | 210, 580, 584, 138 | consent, isolation refusals, pack rules | plugins.md |
| Install | `CLI pkg/plugins_cmd_install.py` | 863 | clone-scan-publish, consent order | plugins.md §install |
| Catalog | `CLI pkg/plugin_catalog.py`, `plugin-catalog/README.md` | 534, — | entry schema, refresh, removed list; admission rules | `features/plugin-catalog.md` |
| Plugin storage / bundled loader | `plugins/plugin_storage.py`, `plugins/plugin_loader.py` | 46, 278 | conventions | — |
| Shell / gateway hooks | `agent/shell_hooks.py`, `gateway/hooks.py`, `CLI pkg/hooks.py` | 842, 181, 353 | event list, return shapes | hooks.md |
| Cron store | `cron/jobs.py` | 3484 (giant) | `parse_schedule`, `compute_next_run`, `create_job`, `_advance_after_run`, grace/lateness, claims, states | `features/cron.md`, `developer-guide/cron-internals.md`; `tests/cron/` (165 files: start with catchup, scheduled_occurrence, incidents, monitor, continuity) |
| Cron scheduler | `cron/scheduler.py` | 4425 (giant) | markers, tick/run flow, script runner; search `SILENT_MARKER`, `run_job`, `tick(` | same |
| Cron prompt / preflight / delivery | `cron/scheduler_prompt.py`, `scheduler_preflight.py`, `scheduler_delivery.py` | 450, 412, 1923 | hint semantics, context caps; checks; target tokens | cron.md §delivery |
| Cron ledgers | `cron/executions.py`, `cron/incidents.py`, `cron/occurrences.py` | 653, 293, 121 | states, reaping rule, signatures | cron.md §history/incidents |
| Cron extras | `cron/monitor.py`, `unreachable_retry.py`, `quota_hold.py`, `notepad.py`, `suggestions.py`, `blueprint_catalog.py`, `lifecycle_guard.py`, `delivery_queue.py`, `constants.py` | 186/166/163/166/202/791/1293/428/22 | caps and state machines; lifecycle_guard only its purpose | cron.md |
| Cron tool | `tools/cronjob_tools.py`, `tools/cronjob_job_args.py`, `tools/cronjob_prompt_scan.py` | 1200, 552, 144 | schema fields, availability, forwarded args | — |
| Cron CLI | `CLI pkg/cron.py` | 1005 | verbs | — |
| Config defaults | `CLI pkg/config_defaults.py` | (giant) | sections `memory`, `skills`, `curator`, `plugins`, `cron`, `mcp` | `reference/` config docs |
| MCP core | `tools/mcp_tool.py` | 742 | timeouts, breaker, trust, OSV preflight | `features/mcp.md`, `reference/mcp-config-reference.md` |
| MCP config / discovery / registration | `tools/mcp_tool_config.py`, `mcp_tool_discovery.py`, `mcp_tool_registration.py` | 654, 899, — | safe env, concurrency, filters, cache | same; `tests/<cli-pkg>/test_mcp_tools_config.py` |
| MCP handlers / schema / content | `tools/mcp_tool_handlers.py`, `mcp_tool_schema.py`, `mcp_tool_content.py` | 740, 265, 332 | retry ladder, naming, result sanitisation | same |
| MCP sampling / lifecycle / OAuth | `tools/mcp_tool_sampling.py`, `mcp_tool_lifecycle.py`, `mcp_oauth.py`, `mcp_oauth_manager.py`, `mcp_schema_cache.py` | 323, 428, 1269, 466, 109 | defaults and flows | `tests/tui_gateway/test_mcp_oauth_*.py`, `tests/gateway/test_gateway_mcp_oauth_unattended.py` |
| MCP CLI / catalog | `CLI pkg/mcp_config.py`, `mcp_catalog.py`, `mcp_startup.py`, `optional-mcps/*/manifest.yaml` | 1175, 858, 319, — | install flow, checklist precedence | mcp.md §catalog |
| Skill inventories | `skills/`, `optional-skills/` | 58 + 153 skills | names/licences only — never content | `reference/skills-catalog.md`, `reference/optional-skills-catalog.md` |

Giant files: `cron/scheduler.py` [4425] — read the run-classification helpers near the
`SILENT_MARKER`/`CRON_FAILURE_MARKER` constants and the job run function; skip worker/systemd plumbing
on first pass. `cron/jobs.py` [3484] — read lines ~670–960 (grammar, grace) and ~1174–1240 (next run),
then `create_job`. `CLI pkg/plugins.py` [2235] — read the `VALID_HOOKS` block and the context class's
public methods only. `agent/prompt_builder.py` [1779] — the skills index builder only.
`agent/background_review.py` [1349] — constants at the top, `_digest_history`, `_review_tool_whitelist`,
`_run_review_fork`; the prompt constants for semantics only.

## 5. Minimum viable subset

v1 needs:
1. Built-in notes + profile stores with the tool (add/patch/remove/batch), char limits, frozen
   snapshot, threat scan, exact-dup rejection, file lock + atomic write, per-turn retry cap.
2. Skill format, local + external dirs, index in system prompt with mid-session delta note, list/view
   progressive disclosure, slash-command invocation (incl. stacking), skill-manage tool with limits,
   linter (advisory) and usage telemetry.
3. Post-turn background review (memory + skill) with counters, whitelist, iteration cap, input budget,
   unattended memory-delete staging, write provenance; `/refine` and `/learn`.
4. Curator phase 1 (deterministic stale/archive) with pin, cron-reference protection, restore, ledger.
5. Bundled-skill seeding with origin-hash manifest (rebuild authors its own small starter set).
6. Cron: grammar, JSON store with lock, 60 s ticker in the gateway, agent and script-only jobs,
   cron hint, skills attach, `context_from`/continuity, silence/failure tokens, local + origin +
   platform delivery via area 06, failure streak, preflight, execution ledger with unknown state,
   recursion guard, the agent cron tool.
7. MCP client: stdio + HTTP, env filtering, prefixed naming with 64-char clamp, include/exclude,
   discovery concurrency, reconnect/backoff, breaker, `/reload-mcp`, OAuth via the SDK.
8. Plugin system: manifest, user dir discovery, allow-list gate, `register(ctx)` for tools, hooks
   (pre/post tool, pre/post LLM, session start/end at minimum), commands, skills; load timeout; hook
   timeout with pre-tool fail-closed.

Can wait (and why): external memory providers (additive, plugin-shaped); hub multi-source install and
taps (needs scanner + network adapters; start with `official` + direct URL/GitHub); curator LLM
consolidation and backups (off by default in the reference); plugin catalog, packs, capabilities
consent, out-of-process host (supply-chain features; layer once plugins work); optional-MCP catalog
and setup cards; sampling/elicitation; cron incidents, unreachable ladder, quota hold, monitor mode,
notepad, suggestions/blueprints, continuable deliveries, bot-chat target, hosted scheduler provider,
restart-safe worker scopes; project-local skills with trust + quarantine; learning journey UI;
local-GPU review deferral.

## 6. Hard parts / traps

1. **R07.1 Frozen memory snapshot vs live writes.** Writes land on disk immediately but the prompt
   block must not change mid-session (prefix cache). Rebuild must render once at session start, keep
   live lists separately for tool results, and re-render only on new session/prompt rebuild;
   otherwise every memory write busts the cache. Load-time threat hits replace the entry in the
   snapshot only.
2. **R07.2 No auto-compaction; budget is checked on the batch result.** Overflow must fail with usage,
   overflow size and the current entries, and a batch must be validated against its final total —
   checking op-by-op makes a remove+add consolidation impossible at capacity. Cap failed consolidation
   attempts at 3 per turn with a terminal "stop and answer" result, or a weak model loops until the
   iteration budget is gone and the user never gets a reply.
3. **R07.3 Whole-entry replace destroyed multi-fact entries.** The reference removed `replace` because
   models used it as find-and-swap. Patch must replace only the matched span inside the entry, require
   the span to be unique within the entry, and prefer an exact whole-entry match.
4. **R07.4 Background review must not silently delete or rewrite memory.** Unattended forks may only
   add; patch/remove become staged proposals pinned to the full matched entry, and approval refuses if
   the entry changed since staging. Treat `/refine` as attended. Without pinning, approve-time
   re-matching can hit a newer entry.
5. **R07.5 Review fork recursion and cost.** The fork must have nudge intervals forced to 0, a
   dispatch-side whitelist (advertising tools ≠ permission), a 16-iteration cap and an aggregate
   input-token budget (75% of window, ≤600k, fallback 120k). Skip reviews for cron, one-shot and
   safe-worker runs; a cron fleet would otherwise spend ~30k tokens per event with nobody to benefit.
6. **R07.6 Curator jurisdiction is an explicit marker, never inferred.** Only skills the background
   review created (agent-managed marker) or the user adopted may be aged/archived. Foreground/learn,
   hand-written, hub, external and project skills must be left alone; inferring "looks agent-made"
   from telemetry will eventually archive a user's hand-written skill. Never hard-delete: archive only.
7. **R07.7 Curator defaults are inconsistent in the reference docs.** Code defaults are stale after
   14 days, archive after 30, interval 168 h, idle 2 h; one doc paragraph says 30/90 days. Use the code
   values and flag the doc text as stale. Also: first observation only seeds the clock (no run on a
   fresh install), never-used skills get a grace floor, and skills referenced by any cron job (even
   paused) are protected.
8. **R07.8 Cron skills must not be archived from under a job.** The curator must check every cron
   job's skill list, and consolidation that renames skills must rewrite cron references.
9. **R07.9 Bare duration means recurring.** `30m` and `every 30m` are recurring intervals; only
   `in 30m` is a one-shot. Getting this backwards creates surprise recurring spend.
10. **R07.10 Natural phrases with interval + time fail.** Per the grammar, "every 1d at 09:00" is
    neither a day spec nor a pure duration and should raise — yet a reference doc example uses it
    [inf]. Rebuild should either accept it (as daily at 09:00) or document the error; decide
    explicitly. Needs REA to confirm reference behaviour.
11. **R07.11 Timezones and DST in cron.** Naive ISO timestamps use the configured agent timezone, not
    the server's; cron expressions must be evaluated on the configured zone's wall clock; the DST
    fall-back hour needs the both-folds "strictly after base" guard or a job re-fires in a loop;
    intervals add minutes in UTC. Interval jobs anchor on last run (drift accumulates by run
    duration) — keep that or document a change.
12. **R07.12 Recurring jobs must never silently complete.** If next-run computation fails (missing
    cron library, bad expression) a recurring job goes to a recoverable `error` state, not
    `completed`, and self-heals. One-shots past their limit are retained as terminal records.
13. **R07.13 Exactly-once is not available; prefer at-most-once.** Record the attempt (with exact
    scheduled instant) before dispatch; never fire when the store is unwritable; mark interrupted
    attempts `unknown` only on proven owner death or prolonged silence (max(3×timeout, script timeout,
    2 h)); never auto-rerun unknown attempts; a claimed-then-lost delivery is unknown and not retried.
    Catch-up collapses many missed slots into one run.
14. **R07.14 Silence and failure tokens are model-facing and fragile.** The silence token is accepted
    as the entire reply or alone on the opening/closing line (bracketless variants too) but not mid-sentence;
    the failure token must be alone on line 1. Failed runs must deliver even if "silent". The token
    must be a literal ASCII control word the prompt tells the model never to translate.
15. **R07.15 Cron recursion.** A scheduled run reading "every Monday…" in its own prompt will try to
    schedule another job. Disable cron tools in cron runs by default and say so in the cron hint;
    when opted in, resolve `origin` at create time to a concrete target.
16. **R07.16 Preflight alerts once, spends nothing.** A misconfigured job (missing key, unready skill,
    unknown target, MCP server never connected) must block before any LLM call and alert once, not
    every tick; a reconnecting MCP server must not block.
17. **R07.17 Delivery success ≠ run success.** Keep execution status and delivery status separate;
    delivery failures don't feed the failure streak; adapter acks without evidence are "unverified";
    empty payloads never sent; redaction failure replaces rather than leaks.
18. **R07.18 Plugin hook timeouts and fail-closed policy.** A hung `pre_tool_call` must block the tool
    (fail closed), other bounded hooks fail open; abandon (don't join) timed-out workers, suppress a
    hung callback for 60 s and cap abandoned workers at 3. Plugin context goes into the user message,
    never the system prompt, to preserve caching.
19. **R07.19 Plugins are not a sandbox.** Allow-list, capability consent and static scanning are
    consent/audit layers; in-process plugins can do anything. Non-interactive installs must not grant
    capabilities; `dangerous` scan verdicts cannot be forced; SHA pins must be full 40-char and
    verified after checkout; credentials for private clones must never be persisted in git config.
20. **R07.20 Plugin system-prompt sections must be frozen.** Render once per new session, cap sizes
    (4,000 each, 8,000 total, 32 max), freeze through compaction and recover from the persisted prompt
    on resume; recomputing on resume silently changes a session's cached prompt.
21. **R07.21 MCP tool name length.** OpenAI-compatible providers reject names > 64 chars or with
    chars outside `[A-Za-z0-9_-]`, failing the whole request. Sanitize components and clamp with a
    deterministic hash suffix; dispatch must close over the original tool name. Docs show a
    single-underscore prefix; code uses the double-underscore convention shared with other agent CLIs.
22. **R07.22 MCP stdio env leakage and spawn storms.** Pass only explicit env + a safe baseline; cap
    connect concurrency (4) because each stdio server spawns a process tree; give each wave its own
    budget. Run a malware preflight on package commands (fail-open on timeout).
23. **R07.23 MCP reload busts the prompt cache.** Any tool-surface change invalidates the provider
    prefix; the config watcher and `/reload-mcp` must be explicit, and dynamic `list_changed` refreshes
    must be lock-protected against storms.
24. **R07.24 Background OAuth must never open a browser.** Gateway/reload/probe contexts park the server
    and wait for an interactive re-login; new sessions must not retry a rejected token (only the timed
    probe), or a dead grant hammers the token endpoint.
25. **R07.25 Untrusted MCP servers.** Gate write-capable calls on `readOnlyHint` captured at discovery;
    malformed hints fail closed; unknown trust values mean untrusted (a typo must not disable the gate).
    Strip Unicode TAG chars from results and descriptions.
26. **R07.26 Project skills are an injection vector.** Never auto-load repo skills; require explicit
    trust per root, re-scan on every content change (hash-cached outside the repo), quarantine
    `dangerous`, and inherit (never create) trust on non-interactive surfaces.
27. **R07.27 Same-name skills.** Different skills sharing a name inside one tier must never be guessed
    between (ambiguity error with paths); cross-tier the higher tier wins consistently on *every*
    surface (index, list, view, slash, preload, cron), or cron and chat load different skills.
28. **R07.28 Bundled skill updates vs user edits.** Use an origin hash per skill; skip forever once
    user-modified; exclude generated caches from hashing (running a helper script must not mark a
    skill modified); provide a reset escape hatch; honour an opt-out marker on every seeding path.
29. **R07.29 Skill index changes mid-session.** The index lives in the frozen system prompt; announce
    additions/removals once via a note on the next user message instead of rebuilding the prompt.
30. **R07.30 Reference doc drift.** Several doc numbers disagree with code (hook count 27 vs 46,
    curator 30/90 vs 14/30, MCP name prefix). Treat code as truth and REA as tiebreaker.

## 7. Open questions

1. Does "every 1d at 09:00" (doc example) actually parse in the shipped build, or error? Needed: REA
   runtime check of the schedule parser on the shipped package (needs REA).
2. Exact rendered form of the memory block header rule lines and usage line in the live system prompt
   (widths, glyphs). Needed: REA capture of a session's system prompt (needs REA).
3. Which threat-pattern scope ("strict") categories exactly block memory writes vs cron prompts vs
   skill installs — and false-positive behaviour on benign text. Needed: REA run feeding sample
   entries; decision on whether the rebuild shares one pattern set (needs REA + lead decision).
4. Is the skill-nudge counter per tool *iteration* also incremented by the provider-projection /
   codex runtime paths in the shipped build exactly as in the main loop? Needed: REA trace of counter
   values over a scripted session (needs REA).
5. What does the review fork do when the parent model cannot emit tool calls (a guard exists)?
   Needed: source read of `_parent_can_emit_tool_calls` call sites + REA (needs REA).
6. Exact content and order of the skill-list tool rows (category field, truncation) and of the skill
   view payload (linked-file list, setup notes). Needed: REA tool-call capture (needs REA).
7. Hub centralized index URL, rebuild cadence and cache TTL. Needed: source read of
   `tools/skills_hub_search.py` + REA network capture (needs REA).
8. Plugin catalog live-refresh fetch cadence and cache TTL. Needed: read `plugin_catalog.py` refresh
   path + REA (needs REA).
9. Cron default inactivity timeout: code constant 600 s — is it overridden by config in shipped
   defaults? Needed: REA of effective config on a fresh install (needs REA).
10. Cron job id format: 12 hex chars observed in code; do older stores contain other formats the
    rebuild must import? Needed: REA of a long-lived install's jobs file (needs REA).
11. Product decision: should the rebuild keep the reference's "bare duration = recurring" grammar or
    require explicit "every"? Needed: lead decision.
12. Product decision: ship plugin out-of-process host in v1 or later? Needed: lead decision (security
    posture).
13. Product decision: which bundled skills the rebuild authors for v1 (the reference ships 58; all
    content must be newly written). Needed: lead decision.
14. Product decision: whether to run a curated plugin catalog / optional-MCP catalog at all (they are
    governance processes, not just code). Needed: lead decision.
15. Exact MCP setup-card UI states and copy across CLI/TUI/desktop. Needed: REA UI capture (needs REA).
16. Default `failure_nudge_threshold` and `retry_unreachable` are in docs (3, true) but were not seen
    in the config-defaults section read; confirm where they default. Needed: source grep in
    `cron/scheduler.py` + REA effective config (needs REA).

## 8. What to read later

- Reference docs: `website/docs/user-guide/features/{memory,memory-providers,skills,curator,cron,plugins,hooks,mcp,plugin-catalog,built-in-plugins}.md`;
  `website/docs/reference/{mcp-config-reference,skills-catalog,optional-skills-catalog}.md`;
  `website/docs/developer-guide/{creating-skills,cron-internals,memory-provider-plugin,plugin-llm-access,model-provider-plugin,plugins/index,plugins/catalog-submission}.md`;
  `website/docs/guides/cron-script-only.md`; `plugin-catalog/README.md`; `optional-skills/DESCRIPTION.md`.
- Reference tests that list the edge cases: `tests/agent/test_background_review*.py`,
  `tests/agent/test_curator*.py`, `tests/<cli-pkg>/test_curator_*.py`,
  `tests/<cli-pkg>/test_skills_hub.py`, `tests/tools/test_skill_manage_schema_diet.py`,
  `tests/cron/` (catch-up, scheduled occurrence, incidents, monitor, continuity, silent, timezone,
  cross-process lock), `tests/gateway/test_gateway_mcp_oauth_unattended.py`,
  `tests/tui_gateway/test_mcp_oauth_*.py`, `tests/<cli-pkg>/test_mcp_tools_config.py`,
  `tests/pm/test_plugin*.py`, `tests/gateway/test_plugin_*.py`, `tests/skills/`.
- REA Evidence (outside the repo): `~/rea-lab/evidence/07/<bundle>.json` — none yet (REA not connected).
- Related specs: `01-*.md` (system prompt assembly, compaction rebuild of frozen blocks, turn
  finalisation), `02-*.md` (tool registry, toolsets, tool-search bridge, approval gate), `03-*.md`
  (session DB and FTS behind session search; usage accounting rows), `06-*.md` (delivery adapters,
  home channels, threads, continuable mirrors, gateway hooks host).
