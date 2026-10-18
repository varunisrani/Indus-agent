# 03 — State and persistence: behavioural specification (clean-room)

Scope: the per-profile home directory and its layout, profile isolation, the SQLite session store
(open/close lifecycle, journal mode, locks, read pool, schema and migrations), sessions and
messages (transcripts), FTS search, compression records, branch/rewind/reset, titles, usage
accounting, media retirement, lineage, deletion fences, repair/reconcile/health/maintenance,
export/import and cross-profile moves, the gateway / Telegram-topic / terminal-tombstone state
slices, process logging, the timezone clock, the YAML policy, process bootstrap, the startup
watchdog, the Windows directory-ACL helper, registration leases, and where trajectories go.
Out of scope: the agent loop and how it decides to compress (`01-agent-loop.md`), tools
(`02-tools.md`), CLI/TUI command UX and flags (`04-cli.md`), memory and skills semantics
(`07-memory-skills.md`). The gateway's own in-memory session model, queueing and transport
belong to the gateway area; only its durable slices are described here.

Reference: `NousResearch/hermes-agent` @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056` (MIT);
shipped build checked: none (REA not connected in this session — source reading only).
Sources studied: developer docs (session storage, state DB recovery, gateway session
lifecycle), user docs (sessions, profiles, session storage recovery), every root state module
listed in §4, the home/profile resolver module, default config, the trajectory helper, and the
names of the 200 store tests (titles only, not bodies).

Conventions:
- "Store" = one profile's SQLite database file plus its `-wal`/`-shm` sidecars.
- "Home" = the resolved per-profile data directory (rebuild: `~/.indus` or `INDUS_HOME`).
- "Handle" = one in-process store object owning one writer connection and a read pool.
- "Lineage" = a chain of session rows linked by parent ids; a "compression chain" is the subset
  where each parent ended with reason `compression` and the child is a genuine continuation.
- "Tip" = the newest live segment of a compression chain; "root" = its first segment.
- Message row states: live (`active=1`), compaction-archived (`active=0, compacted=1`: hidden
  from the model, still displayed and searchable), rewound (`active=0, compacted=0`: hidden
  everywhere except audit reads).
- "MUST" marks an invariant the reference defends with dedicated code and a named regression.
- No REA evidence exists for this area; every claim is [obs] from source/docs, [inf], or [unk].
- Reference file paths appear only in §4, §8 and the header, for traceability; they are not names to reuse.

---

## 1. Overview

The area is the durable memory of the product. Every conversation on every surface (terminal
chat, TUI, desktop, messaging gateway, cron, API server, editor protocol adapter, subagents)
lands as rows in one SQLite file per profile. The store is large (≈30,700 lines across ≈60
root modules) because it is defended against a long list of field failures: multi-process
write contention, WAL sidecar loss, out-of-band file replacement, FTS index corruption,
descriptor exhaustion, crashes mid-compression, and profile cross-talk.

Parts and who calls whom [obs]:

```
 surfaces (CLI, TUI, desktop, gateway, cron, API, ACP, workers)
        |  (in-process callers share one handle per path via the registry;
        |   out-of-process workers go through the gateway's RPC, never the file)
        v
 +-----------------------------------------------------------------------+
 | Store facade (one class composed of ~15 mixins)                        |
 |  sessions | messages | compression | rewind | titles | search/FTS      |
 |  usage | maintenance | portability | gateway slice | telegram slice    |
 |  profile repair | coverage | schema                                    |
 +-----------------------------------------------------------------------+
        | write: one writer conn, BEGIN IMMEDIATE, jittered retry, guards
        | read : bounded pool of read-only conns (WAL only)
        v
 SQLite file  <home>/state.db  (+ -wal, -shm)   <-- also opened read-only by
        ^                                           dashboards / list commands
        | file-level helpers: header probes, holder scans (/proc, libproc,
        | Windows Restart Manager), lock guard, quarantine, repair, capture
```

Runtime-ledger modules (admissions, workers, receipts, mutation receipts, retirement fences)
sit beside the facade and run their logic inside the facade's write transactions. The gateway
is the only canonical writer of session transcripts for its profile; other in-profile clients
submit through it [obs, developer doc]. Non-session tables (heartbeats, routing, maintenance
meta) may still be written by other processes.

What the user sees: conversations persist and resume across restarts; `sessions list/search/
export/prune/repair/recover` style commands work; titles are unique; search finds old turns
including CJK; disk does not grow unbounded when auto-prune is on; a damaged store produces a
clear message and recovery steps instead of silent data loss. What the system guarantees:
canonical rows (sessions, messages) are never deleted to fix a derived index; a damaged or
replaced file stops receiving writes; started work is never replayed after an owner crash;
deleted sessions cannot be resurrected by an exact retry.

## 2. Reference names (rename in rebuild)

| Kind | Names (reference only; they become banned identifiers) |
|---|---|
| Binary / CLI and flags | `hermes`, `hermes-agent`, `hermes-acp`, `--hermes-home`, `hermes sessions …`, `hermes doctor`, `hermes backup`, `hermes update`, `hermes gateway …`, `hermes serve`, `hermes db optimize` |
| Env vars | `HERMES_HOME`, `HERMES_DATA_DIR_SUFFIX`, `HERMES_REAL_HOME`, `HERMES_HOME_MODE`, `HERMES_TIMEZONE`, `HERMES_STATE_DB_GUARD_BYPASS`, `HERMES_TEST_ISOLATION`, `HERMES_SEARCH_SLOW_MS`, `HERMES_CJK_FTS`, `HERMES_FTS5_CJK_SO`, `HERMES_STARTUP_WATCHDOG`, `HERMES_STARTUP_WATCHDOG_TIMEOUT_S`, `HERMES_SCRATCH_DIR`, `HERMES_MANAGED`, `HERMES_CONTAINER`, `HERMES_SKIP_CHMOD`, `HERMES_UID`, `HERMES_GID`, `HERMES_SESSION_ID`, `HERMES_SESSION_SOURCE`, `HERMES_OPTIONAL_SKILLS`, `HERMES_OPTIONAL_MCPS`, `HERMES_BUNDLED_SKILLS`, `HERMES_GATEWAY_HEARTBEAT_REFRESH_S` |
| Files and folders | `~/.hermes`, `%LOCALAPPDATA%/hermes`, `hermes_state*.py`, `hermes_time.py`, `hermes_yaml.py`, `hermes_logging.py`, `hermes_dir_acl.py`, `hermes_bootstrap.py`, `hermes_startup_watchdog.py`, `hermes_constants*.py`, `hermes-agent` (install dir), `shared-nous` |
| HTTP routes / URL schemes | `hermes-agent.nousresearch.com/docs/...` (recovery docs URL) |
| RPC methods / events / commands | hook name `on_session_delete` (generic; fine to keep), `RuntimeSessionStore` |
| Settings and storage keys | state_meta key prefixes beginning `gateway.` (generic, may keep); class/mixin names `SessionDB`, `AsyncSessionDB`, `Session*Mixin` |
| Other (scopes, error tags, IPC channels) | `Nous Portal` (provider label), log-record attribute `_hermes_session_injector`, logger name `hermes_logging`, issue/ticket ids (`OOF-298`, `NS-608`), executable sets naming the reference binaries |

Third-party protocol names used by this area (keep exact; check against vendor docs): SQLite
(WAL, DELETE/rollback journal, `BEGIN IMMEDIATE`, `wal_checkpoint` PASSIVE/TRUNCATE,
`application_id`, `locking_mode=EXCLUSIVE`, online backup API, `VACUUM`, `VACUUM INTO`,
`integrity_check`, `writable_schema`, `SQLITE_BUSY`/`LOCKED`/`CORRUPT`/`CORRUPT_VTAB`/`NOTADB`/
`IOERR`/`FULL`/`CONSTRAINT_FOREIGNKEY`, `SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE`), FTS5 (external
content tables, `unicode61` and `trigram` tokenizers, `rebuild`, `optimize`, `merge`,
`delete-all`, `integrity-check`, `usermerge`, bm25 `rank`, `snippet`), POSIX `fcntl` and OFD
locks, `/proc/<pid>/fd`, `/proc/locks`, `/proc/self/ns/pid`, `/proc/self/mountinfo`, macOS
`libproc` and `F_FULLFSYNC`, Windows Restart Manager, DACL/ACE and `icacls`, `psutil`,
`ruamel.yaml` (YAML 1.1), `concurrent-log-handler`/`portalocker`, Python `faulthandler`, RFC
8305 (Happy Eyeballs), s6/systemd service restart codes, Telegram Bot API, ShareGPT JSONL.

## 3. Behaviour

### 3.1 Home directory, profiles and layout

- **Resolution order** [obs]: (1) a context-local override (per task/thread; never mutates the
  process environment), (2) the `INDUS_HOME` env var (with `~`, `$VAR`, `${VAR}`, `%VAR%`
  expansion), (3) the platform default: `~/.indus` on POSIX, `%LOCALAPPDATA%/indus` on Windows,
  each with an optional suffix from a data-dir-suffix env var. Every path in the product derives
  from this resolver; the default store is `<home>/state.db`.
- **Process home vs routed home** [obs]: a multi-profile host (multiplexed gateway, desktop
  backend) can "pin" its launch home; routed work for another profile sets the context override.
  Process-level assets (themes, plugin manifests) read the process home, data reads the routed
  home. Log records carry the resolved home so the async log writer can route them later.
- **Profiles** [obs]: the default profile is the root itself; a named profile lives at
  `<root>/profiles/<name>/` with a full private layout (own store, config, env file, logs,
  memory, skills, cron, gateway state). Name rule: lowercase alnum start, then up to 63 of
  `[a-z0-9_-]`. A dir counts as a profile only if it has an identity marker (one of config file,
  env file, persona file, profile manifest, auth file, store) and is not tombstoned; an empty env
  file alone (left by a crashed create) is not enough to be "servable". Deletion leaves a
  tombstone under `profiles/.deleted`; creating dirs under a tombstoned or missing named profile
  is refused, so a stale route cannot re-scaffold a deleted profile.
- **Sticky selection** [obs]: an `active_profile` file under the default root names the sticky
  profile. CLI bootstrap applies an explicit `-p/--profile` by writing the resolved dir into the
  home env var before importing the rest; without a selector a profile-specific home env var is
  kept, else the sticky file applies. A warning fires once when the env var is unset but a
  non-default profile is sticky. Child processes MUST be given the home env var explicitly;
  changing `HOME` is not a profile selector. User-facing guidance strings include `-p <name>`
  whenever a named profile is active.
- **HOME for subprocesses** [obs]: modes `auto` (hosts keep the real user HOME, containers use
  `<home>/home`), `real`, `profile`. A "real home" env var is exported so scripts can find the
  account home in profile mode.
- **sudo** [obs]: when running as root under sudo, the invoking user's default home is
  computed so services and stores are not created under root's home.
- **Layout** (files and dirs seen in the code; not exhaustive) [obs]: `state.db` (+ sidecars),
  `config.yaml`, `.env`, `auth.json`, `active_profile`, `profiles/<name>/`, `profiles/.deleted/`,
  `sessions/` (legacy JSONL transcripts, divert files `<id>.jsonl`, `sessions.json` routing
  mirror, `saved/*.json` snapshots, request dumps), `logs/` (agent/errors/gateway/gui logs,
  `update_receipts/`, `process-results/`, `curator/`, startup-watchdog dump), `cache/scratch/`
  (TMPDIR for all processes), `memories/`, `skills/`, `plugins/`, `plugin-data/`, `hooks/`,
  `cron/jobs.json`, `cron/output/`, `checkpoints/`, `state-snapshots/`, `pending_messages/`
  (gateway spool), `gateway_state.json`, `processes.json`, `browser-profile/`, `workspace/`,
  `home/` (profile HOME), `lib/` (optional CJK tokenizer library), plus per-integration files.
  Lock/aux files next to the store: `.quarantine.lock`, `.repair.lock`,
  `.auto-maintenance.lock`, `.fts_rebuild.lock`, `.repair-attempts.json`; recovery artefacts
  `*.zeroed-<ts>-<pid>.bak`, `*.notadb-<ts>-<pid>.bak`, `*.malformed-backup-<ts>`,
  `*.retired-wal-<utc ts>-<pid>/` (with `manifest.json`), `*.pre-update-emergency-<ts>.bak`.
- **Permissions** [obs]: home dirs default to owner-only 0700 unless a managed install
  (systemd/NixOS marker or env) or explicit operator sharing says otherwise; container
  deployments chown new subdirs to configured uid/gid. The store file is created/tightened
  BEFORE the first SQLite connect so a permissive umask never exposes it; sidecars are
  re-tightened after WAL setup. `secure_parent_dir` refuses `/` and top-level dirs (a misresolved
  home).
- **Scratch dir** [obs]: `<home>/cache/scratch` becomes TMPDIR/TMP/TEMP for the process and its
  children unless the user set them; entries idle > 24 h are pruned at most once per 3600 s
  (stamp file), after reaping same-user processes whose cwd is inside the entry (TERM, 3 s
  grace, KILL; never the pruner's own ancestry or processes the app registered as its own) and
  pruning git worktree registrations pointing into it. Prune log rotates at 5 MiB × 3. Socket
  paths fall back to the OS temp root if the scratch path exceeds 50 chars.

### 3.2 Opening a store (writable)

- **Trigger:** constructing a handle with an optional path (default `<home>/state.db`) and a
  read-only flag. Long-lived in-process callers instead `acquire` from the registry (§3.6).
- **Flow** [obs]:
  1. Test-isolation guard (§3.26) runs before any mkdir/connect.
  2. Create the parent dir via the profile-safe mkdir (never materialise a deleted profile).
  3. If the file is absent or its first page is not SQLite (0 bytes, NUL header, clobbered page
     0): check the dir is writable, take the cross-process quarantine lock (5 s bounded wait;
     proceed with a warning if not acquired), re-check, move the bad file and its sidecars aside
     to `<name>.<zeroed|notadb>-<ts>-<pid>.bak` (numbered suffix on clash), log an error naming
     the recovery commands, and continue with a fresh file. If the move fails and the file is
     still invalid, raise instead of opening it.
  4. Writability preflight on the main file and sidecars (read-only files from a sudo run or a
     restored backup get an actionable message, not a late "readonly database" error).
  5. Refuse if any process holds a deleted `-wal`/`-shm` generation of this path (§3.23) —
     checked before connect so a second WAL inode is never minted.
  6. Connect with a 1 s busy timeout, autocommit mode, cross-thread use allowed; set journal
     mode (§3.3), confirm WAL from the on-disk header, apply pragmas, enable foreign keys, try
     to load the optional CJK tokenizer.
  7. Schema init (§3.7). Lock contention during init is retried with jittered sleeps
     (250 ms–1 s) for up to 20 s; then the lock holders are logged and the error propagates.
  8. If the very first statement reports a malformed schema, a one-shot per-process automatic
     schema repair runs (backup first) and the open is retried once.
  9. Mint a once-per-file generation stamp: a random token in meta key `db_file_generation`
     (first opener wins) and, if the header `application_id` is 0, a derived non-zero 31-bit
     value written there. Record the file's (device, inode), sidecar identities and app id.
  10. In WAL mode take the lock guard (§3.23) so stray opens/closes inside the process cannot
      cancel the WAL-protecting POSIX locks.
- **Read-only open** [obs]: `mode=ro` URI (path percent-encoded properly — a raw `?` or `#` in
  the home path would otherwise open the wrong DB), 5 s busy timeout, no schema init, FTS
  availability probed with SELECTs only, connection closed on any probe failure. A transient
  "disk I/O error" (a writer mid-checkpoint) is retried 3 times at 50 ms; lock errors are not
  retried (the 5 s wait already happened). Browsing opens never create a missing store, never
  quarantine, never reconcile, never auto-archive; uninitialised or incompatible stores answer
  HTTP 503 with guidance.
- **Init failure reporting** [obs]: the last init error text is kept per process so resume
  pickers can say why persistence is off; it is never cleared by a later success (another
  thread may be formatting it).
- **Errors:** failed construction closes any half-open connection; callers fall back to
  "store unavailable" mode with a banner (§3.25).

### 3.3 Journal mode and pragmas

- **Policy** [obs]: configured `database.journal_mode` is `wal` (default) or `delete` (for
  filesystems where WAL is unsafe: macOS virtiofs, NFS, SMB); invalid values fall back to
  `wal`.
- **Never live-downgrade** (MUST) [obs]: if the on-disk header says WAL, or the mode cannot be
  read (probe blocked by a concurrent opener), the open keeps whatever is there; a configured
  `delete` on an existing WAL file logs one ERROR per process and is ignored. Switching mode is
  an explicit offline command that refuses while any holder exists (§3.23) and verifies the
  header afterwards; `--force` only waives a failed holder scan, never a found holder.
- **Refuse to enable WAL** [obs] when: mountinfo shows the store on a filesystem shared across VMs
  (virtiofs or 9p), or the linked SQLite has the WAL-reset bug (versions 3.7.0 through 3.51.2,
  fixed in 3.51.3, backports 3.50.7 and 3.44.6). Existing WAL files on such hosts keep WAL with
  a warning. On WAL-incompatible filesystems (error text "locking protocol", "not authorized",
  "disk i/o error", or silent refusal) fall back to DELETE with one ERROR per process. A strict
  variant used by another store raises instead of falling back.
- **Mode switch mechanics** [obs]: any non-WAL switch is issued with busy timeout 0 so a
  concurrent opener makes SQLite refuse rather than race.
- **WAL companions** [obs]: journal size limit 64 MiB (so a once-huge WAL shrinks back), on
  macOS `checkpoint_fullfsync` and `synchronous=FULL` are forced (never below FULL there).
- **Config pragmas** [obs]: `database.cache_size`, `mmap_size`, `temp_store`,
  `wal_autocheckpoint`, `journal_size_limit` (ints; bad values warned and skipped),
  `synchronous` (OFF/NORMAL/FULL/EXTRA or 0–3; unknown values warned, level left alone). Applied
  to every connection kind. Repair/probe connections that bypass the normal open also get the
  macOS barriers and the configured synchronous level.

### 3.4 Write path

- **Shape** [obs]: every mutation is a callback run inside `BEGIN IMMEDIATE` on the single
  writer connection, under an in-process mutex; the helper commits (callbacks MUST NOT commit)
  and rolls back on any exception. Callbacks MUST be idempotent because the whole callback is
  re-run on lock retry.
- **Retry budgets (time-based, not attempt-counted)** [obs]:
  - routine writes 20 s, transcript writes 60 s (their failure aborts the turn), observation-only
    activity writes 0.5 s (skipped writes retry at the next heartbeat);
  - jitter: uniform 20–150 ms for the first 2 s, then 250 ms–1 s;
  - collision with a live compression lock: separate 5 s budget, then refuse (the lease is a
    correctness boundary);
  - transient "no more rows" errors on contended appends are retried like busy;
  - "disk I/O error" from `BEGIN IMMEDIATE` itself (callback not yet run) is retried once on the
    same connection; once the callback ran, an I/O error propagates (settlement unknown).
- **Lock exhaustion** [obs]: log the current write-lock holders (§3.23) then raise a "database
  is locked" error whose text explains it is contention, not damage (and deliberately avoids
  words that the error classifier maps to disk/corruption buckets; holder argv goes only to the
  log because a worktree name could flip the bucket).
- **Corruption on write** [obs]: on "not a database"/malformed/FTS-scoped corruption first check
  the file was not replaced (§3.23); then if the error is FTS-scoped, detach the FTS triggers
  atomically with a stale marker and retry (§3.13); otherwise quarantine the handle (§3.23).
- **Housekeeping after success** [obs]: PASSIVE checkpoint every 50 successful writes (never
  TRUNCATE on the hot path: TRUNCATE under load tore B-tree pages on large files); a bounded FTS
  `merge` every 1000 writes (up to 4 commands per index, ~500 pages each, stop on no progress).
- **Closed-handle race** [obs]: if a teardown closed the connection while a worker still had a
  write in flight, the write path reopens the writer (no schema init) with a WARNING, unless the
  handle is quarantined or the file generation changed.
- **Lease-holder strings** [obs]: holders are formatted like `pid=<n>[:pidns=<inode>]:...`; see
  §3.10 and §3.23 for how liveness is judged.

### 3.5 Read path and descriptor budget

- **Pool** [obs]: WAL-only. Reads borrow a read-only connection from a LIFO pool so they never
  queue behind the writer mutex; read-your-writes holds because reads are autocommit. Under
  DELETE mode or a read-only handle, reads use the writer connection; in DELETE mode the busy
  timeout is temporarily raised to 5 s for the read (a SHARED lock waits on every foreign
  commit).
- **Budgets** [obs]: at most 8 live read connections per database path (shared by every handle
  on that path in the process), at most 24 per process across all paths, and a headroom gate
  that refuses a new descriptor when fewer than 64 fds remain below the soft RLIMIT (fd count
  cached 0.25 s; unmeasurable → allow). A reader that cannot get a permit falls back to the
  writer connection rather than blocking. A permit miss first tries to close an idle pooled
  connection anywhere in the process. Failed read opens back off for 60 s (timestamp, not a
  sticky flag). More than 4 handles on one path triggers a diagnostic warning.
- **Close** [obs]: closing marks the pool closed so in-flight readers close their own
  connection instead of returning it.

### 3.6 Process-wide handle registry

- **Why** [obs]: each bare handle owns a writer, mutex, close-time checkpoint and token-writer
  thread; two handles on one file race checkpoints. Long-lived processes share one handle per
  resolved path, refcounted.
- **Behaviour** [obs]: `acquire(path)` returns the current generation and bumps the refcount;
  if the file's inode changed since that generation opened (snapshot restore, recovery swap), the
  old generation is retired (kept alive for holders) and a fresh one opened. A shared handle's
  `close()` releases one ref; the last release tears down outside the registry lock. `close_all`
  (gateway shutdown), `close_all_under(dir)` (profile delete/rename on Windows needs the file
  closed), `other_generations_for_path` (in-process half of "is this store quiet?"),
  `borrow_live` (pins refs for maintenance), `release_or_close` (drop-in for one-shot callers).
  Teardowns of one path are admitted through a barrier so a re-acquire waits for in-flight
  physical closes.

### 3.7 Schema, versions and migrations

Tables in neutral notation (field lists are behavioural; types: `text`, `int`, `real`,
`json` = JSON text, `flag` = 0/1 int) [obs]:

- `SchemaVersion { version: int }` — single row; current value 32 (docs still say 31).
- `SystemPrompt { hash (sha256 of text), prompt }` — content-addressed dedupe for prompts and
  for tool-list pins.
- `Session { id, runtime_revision, runtime_generation, source, created_source?, user_id?,
  session_key?, chat_id?, chat_type?, thread_id?, display_name?, origin: json?,
  expiry_finalized: flag, model?, model_config: json?, system_prompt? (legacy),
  system_prompt_hash?, parent_session_id?, started_at, ended_at?, end_reason?, message_count,
  tool_call_count, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
  reasoning_tokens, cwd?, git_branch?, git_repo_root?, git_metadata_generation,
  billing_provider?, billing_base_url?, billing_mode?, estimated_cost_usd?, actual_cost_usd?,
  cost_status?, cost_source?, pricing_version?, title?, title_source?: derived|llm|user,
  last_activity_at?, last_activity_description?, last_activity_provenance?, api_call_count,
  handoff_state?: pending|running|completed|failed, handoff_platform?, handoff_error?,
  compression_failure_cooldown_until?, compression_failure_error?,
  compression_fallback_streak, compression_ineffective_count, compression_recovery_deadline?,
  compression_overload_streak, profile_name?, transport_profile?, rewind_count, archived: flag,
  auto_archived: flag, pinned: flag, hidden: flag, last_read_at?, tool_names: hash? }` with FK
  parent → session and prompt hash → SystemPrompt.
- `Message { id: autoincrement, session_id, role, content?, tool_call_id?, tool_calls: json?,
  tool_name?, effect_disposition?, timestamp: real epoch, token_count?, finish_reason?,
  reasoning?, reasoning_content?, reasoning_details: json?, codex_reasoning_items: json?,
  codex_message_items: json?, platform_message_id?, observed: flag, compressed_summary: flag,
  active: flag (default 1), compacted: flag, api_content?, display_kind?, display_metadata: json?,
  display_identity: blob?, display_order?, message_uid?, absorbed_message_uids: json?,
  tool_call_uids: json?, tool_call_uid? }`.
- `ModelUsage { session_id (cascade delete), model, billing_provider, billing_base_url,
  billing_mode, task ('' = main loop), api_call_count, input/output/cache_read/cache_write/
  reasoning tokens, estimated_cost_usd, actual_cost_usd, cost_status?, cost_source?,
  first_seen?, last_seen? }` keyed by all six identity fields.
- Runtime ledger: `RuntimeEpoch { singleton=1, epoch>0, instance_id }`;
  `Admission { seq: autoincrement, admission_id unique, request_id, principal_id,
  target_session_id (restrict delete), lineage: json, payload: json, payload_digest,
  intent: queue|steer|redirect, status: queued|started|unknown|terminal, outcome?,
  owner_epoch?, generation? }` unique on (principal, target, request); started requires epoch and
  generation. `WorkerExecution { execution_id, session_id (restrict), kind:
  cron|child|compute|kanban, owner_epoch, generation, status: registered|running|unknown|terminal,
  adoption_digest, last_sequence>=0 }`. `WorkerReceipt { execution_id (restrict), sequence>0,
  payload_digest, result: json }` keyed (execution, sequence).
- `Meta { key, value }` — generic key/value; holds markers, timestamps, receipts and fences
  (see §3.17 for prefixes).
- `GatewayRouting { scope ('' default), session_key, entry: json, updated_at }` keyed (scope,
  key). `GatewayHygiene { session_key, failure_streak }`. `ConversationGeneration { source,
  session_key, generation }`. `GatewayHeartbeat { backend_id, pid, started_at, last_heartbeat,
  profile, host }`.
- `CompressionLock { session_id, holder, acquired_at, expires_at }`.
  `TurnLease { conversation_id, holder, acquired_at, expires_at }`.
- `AsyncDelegation { delegation_id, origin_session, origin_ui_session_id, parent_session_id?,
  state, dispatched_at, completed_at?, updated_at, event: json?, result: json?, delivery_state
  (default pending), delivery_attempts, delivered_at?, owner_pid?, owner_started_at?, task: json?,
  owner_execution_id?, delivery_claim?, delivery_claimed_at?, origin_session_id }`.
- Lazily created by other modules: delivery-obligations outbox (gateway), Telegram topic tables
  (§3.20).
- FTS virtual tables: base word index, trigram index, optional CJK-bigram index (§3.13), plus
  stable projection views feeding them.
- Indexes worth reproducing [obs]: sessions by source, (source,id), parent, started desc,
  session_key, gateway peer, handoff_state, prompt hash, tool_names, effective activity, local
  creator+started; messages by (session, timestamp), (session, id), (session) for assistant
  rows with tool calls, (session, platform_message_id) partial, active, display page/identity,
  (session, uid); locks/leases by expiry; usage by session and model; delegations by delivery;
  admissions by (target, status, seq) and a partial one for cron requests; unique non-NULL
  title.
- Triggers [obs]: FTS sync insert/update/delete per index (update narrowed to indexed columns);
  mint a `message_uid` for rows inserted without one (older builds writing a newer store);
  maintain display order/identity on insert/update/delete; a delete guard on sessions that
  raises a plain-language error if a pre-ledger build tries to delete a row with ledger rows.
- **Init sequence** [obs]: declare a 600 s startup-watchdog progress lease; run the full
  declared DDL idempotently; declaratively add any missing column by diffing live columns against
  the declared shape (no version needed for new columns or tables); heal two primary-key shapes
  ADD COLUMN cannot express (routing table PK, usage table PK lacking `task`) by atomic
  table rebuild; create indexes on reconciler-added columns afterwards; un-hide NULL `active`
  rows (read-probe first: an UPDATE takes the write lock even when matching nothing); detect FTS5
  support; honour a durable FTS stale marker by dropping triggers; then either stamp a fresh
  store (version, store instance uuid, creation time, uid backfill done) or run data migrations;
  ensure the unique title index; set up FTS; commit.
- **Data migrations (version-gated, data only)** [obs]: <16 tag subagent rows with a
  delegate marker (and orphaned tool-heavy short untitled rows with an orphan marker); <18
  backfill gateway display metadata from the legacy routing JSON; <20 seed per-model usage from
  aggregates; <22 rebuild usage with `task` in the key; <23 only flag that the FTS storage
  upgrade is available (opt-in, never automatic — it is hours long and ~2× disk on big stores);
  <25 dedupe stored system prompts into the content-addressed table; every open advances the
  message-uid backfill (2,000-id chunks, ~1 s budget after the first chunk, resume cursor in
  meta, done marker); <30 rebuild the trigram index once to drop cron and subagent rows. The
  version is advanced only if FTS5 is available and FTS migrations completed. A separate
  `fts_storage_version` (current 3) tracks the FTS layout independently so legacy-FTS installs
  still get every later migration.
- **Drift vs corruption** [obs]: a missing declared table/column is "drift", detected by a
  schema read probe, not by the version number. Browsing a lagging store that no gateway owns
  may catch up once with additive DDL only, in one transaction, under the gateway ownership
  lock, within a 3 s budget, after a full read-only integrity check outside the lock (a damaged
  store refuses with a corrupt-store error). If a gateway owns the store, browsing refuses with
  the owner's restart command. Explicit repair applies the same additive DDL on a protected
  snapshot.

### 3.8 Sessions

- **Ids** [obs]: `YYYYMMDD_HHMMSS_<hex>` with 6 hex chars for interactive surfaces (desktop's
  id regex depends on this width), 8 for gateway keys, 12 for portability imports. Other
  recognised shapes: cron runs `cron_<job>_<YYYYmmdd_HHMMSS>`, background `bg_[HHMMSS_]<6hex>`,
  rooms `room_<32hex>`, API `api_<epoch>_<8hex>` and `api-<16hex>`, runs `run_<32hex>`, plain
  uuid4. Salvage/recovery uses this recogniser list; any new derived shape MUST be added there.
- **Sources** [obs]: free text tags such as `cli`, `tui`, `desktop`, `gui`, `local`, `telegram`,
  `discord`, `slack`, `whatsapp`, `cron`, `kanban`, `subagent`, `tool`, `oneshot`, `acp`,
  `api_server`, `bot_room`, `recovered`. `local` rows also store the creating surface in
  `created_source` (`unknown` placeholder until stamped).
- **Create** [obs]: an upsert that never overwrites values an earlier writer already set (the
  gateway may create a bare row before the agent fills model/prompt). On create, a child row
  NULL-fills cwd/git/profile from its parent (profile only within the same routing namespace);
  routing columns are inherited only by compression continuations. `user_id` is the principal
  (platform sender id, or `<provider>:<user>` for authenticated desktop/dashboard logins; empty
  for anonymous/subagent/cron/kanban) set once at creation.
- **End and reopen** [obs]: ending stamps time and reason; the first end reason wins (a stale
  late end must not overwrite `compression`). Reopen clears both, but first freezes markerless
  legacy reset children. End reasons fall into families:
  - compression: `compression`, `orphaned_compression`;
  - reset boundaries (advance the peer's conversation generation): `session_reset`,
    `session_switch`, `idle`, `daily`, `suspended`, `resume_pending_expired`; plus
    `new_session` as a boundary;
  - recoverable accidents: `agent_close`, `ws_orphan_reap`, `superseded_by_resume`,
    `startup_orphan_reap`;
  - automatic (recoverable plus) `tui_shutdown`, `ws_disconnect`, `idle_timeout`, `lru_evict`;
  - explicit closes: `tui_close`, `cli_close`, `webhook_complete`, `user_exit`, `branched`.
  A host proving a session is still routed may clear an explicit-close stamp. A reset boundary
  can be promoted onto live or recoverably-ended rows, never over explicit boundaries.
- **Child kinds** (all via `parent_session_id`) [obs]: compression continuation (parent ended
  `compression`, no fork marker naming the parent), branch (`_branched_from` marker in
  model_config, or parent ended `branched`), reset fork (`_reset_from` marker, or legacy: same
  non-empty session key and parent ended at a reset boundary), delegate/subagent
  (`_delegate_from` marker; `__orphaned__` when the parent was deleted), tool session
  (`source=tool`). Markers only count when they name the actual parent (continuations copy
  model_config verbatim, so an inherited marker is not a fork).
- **Flags** [obs]: archive, pin, hide and read-state are applied to the whole compression
  lineage (listings project roots forward to tips, so flagging only the tip would let the root
  resurrect it). Deliberate archive clears `auto_archived`; the idle sweep sets it; resume or a
  compression continuation un-hides only sweep archives. Pinning clears hidden and exempts the
  lineage from sweeps. Read state is a watermark: unread when activity is newer than
  `last_read_at`; NULL means read; 0 means unread.
- **model_config** [obs]: tolerant JSON object; atomic merge-patch with `None` deleting a key;
  lineage markers always survive patches. Conversation-wide keys are read-modify-written at the
  chain tip in one transaction. A per-session "yolo" (approval bypass) flag lives there and reads
  as false on any parse problem. Runtime route locks are stored there too. The route used for
  resume is read with precedence: nested gateway runtime, then top-level provider/base_url;
  bare billing buckets `auto` and `custom` are not routable and fall back to config.
- **Workspace** [obs]: cwd and git metadata are written with a claimable generation so an
  async git probe only publishes while its cwd claim is current; probe failures never clobber
  stored values except on an explicit workspace move. Workspace key = repo root, else cwd
  (branch excluded). Resume-by-workspace filters by repo root or cwd prefix.
- **Activity** [obs]: mid-turn heartbeat writes `last_activity_at` (never backwards) plus a
  description and provenance label; cleared labels keep the timestamp. "Last active" everywhere
  is the freshest of activity time, latest message time, and start time, each clamped to a sane
  epoch window.
- **Status** [obs]: from the last message only: error finish reasons (`error`, `agent_error`,
  `content_filter`) → error; assistant with unanswered tool calls or trailing user/tool row →
  interrupted; none → empty; else complete.
- **Listing** [obs]: rich listing returns preview (first eligible user message, newlines
  folded, 63-char head, shaped to 60 chars, skipping hidden rows and summary scaffolds) and last
  active; default limit 20; filters for source(s), exclusions, cwd prefix, min message count,
  archived/hidden/pinned/subagents, id or text search across the forward chain; compression
  roots are replaced by their tip's fields (root start kept for ordering). Internal sources
  (`kanban`, `tool`, `oneshot`) are hidden from default listings. A latency-bounded recent list
  (default 20, 3 s budget) preselects candidates by the indexed activity column.
- **Lookups** [obs]: exact id, else unique prefix; find live session by origin (source + chat
  [+ thread]); with a user id an exact sender wins and an ambiguous multi-user chat returns
  nothing rather than another participant's session.

### 3.9 Messages and transcripts

- **Append** [obs]: one row per message; returns row id; bumps message/tool-call counters.
  Batch append is all-or-nothing with optional chunking for large copies. Timestamps outside a
  sane epoch window are replaced by "now". Content that is a list/dict (multimodal parts) is
  stored as a reserved-prefix JSON string; a literal string that happens to start with the
  reserved prefix is JSON-wrapped so imports cannot smuggle structure; lone UTF-16 surrogates
  are scrubbed (else SQLite binding aborts the write). Reasoning sidecars are stored verbatim as
  text (no re-encoding of already-serialised strings). `platform_message_id` falls back to a
  `message_id` key.
- **Write guards** (inside the transaction) [obs]: if a turn-lease holder is presented it must
  still own the conversation lease (expired-but-still-mine is renewed); destructive mutations
  may opt in to refuse an active compression lock or foreign turn lease (stale ones are reaped);
  writes to a parent already closed by compression are refused with a typed error (callers adopt
  the continuation). Ordinary appends do NOT check the compression lock (§3.10).
- **Durable identity** [obs]: `message_uid` (32 hex) minted at first insert and carried across
  every clone/re-issue/export/import, stripped from provider payloads; `absorbed_message_uids`
  records rows merged into this one; `tool_call_uids` maps provider tool-call ids to
  per-occurrence uids (lists when an id repeats), with the matching uid on tool-result rows
  (derived on restore if missing).
- **api_content** [obs]: the exact string sent to the provider when it differs from `content`
  (ephemeral injections), so replay is byte-identical for prompt caching. A reasoning-only clean
  stop stores empty content and keeps the text in reasoning fields plus `api_content`.
- **Replace** [obs]: atomic replacement for retry/undo/compress, destructive by default
  (rows deleted) or soft (dropped rows archived); a variant pins the expected active id set and
  fails if the transcript moved.
- **Display projection** [obs]: display reads include live and compaction-archived rows,
  collapse duplicate generations (a protected tail copied into each compaction generation) to one
  logical message preferring the live row, with a persisted display order/identity index and a
  one-time legacy backfill. Model reads include live rows only. Reads always order by id (clocks
  regress; tool-call adjacency depends on id order).
- **Resume assembly** [obs]: resolve the target forward to the tip that holds messages; one
  query yields both the model history (tip's live rows, alternation-repaired, summary marker
  kept) and the display history (verified lineage root→tip). Replayed user turns cloned during
  rotation are de-duplicated by exact (timestamp, canonical content) keys. Stale bare tool
  markers like `[memory]` and background-review harness messages are stripped on load.
- **Size guards** [obs]: resume and in-memory export each default to 20,000 messages
  (configurable, 0 disables). Interactive resume counts the whole lineage; desktop cold resume
  counts only the tip. Refusals carry codes 4130 (resume, with count and scope) and 4131
  (export/save) [obs, user doc].
- **Other per-row operations** [obs]: reactions (one per author per message; same emoji again
  clears; surfaced once on the author's next turn), presentation kind stamping, row-addressed
  api_content backfill, single user-row content rewrite (prompt expansion after submit), single
  row deactivation (queued-prompt drain), deactivate all rows of a display kind, latest Todo
  result lookup with bounded pages, window around an anchor id (default 5 each side), anchored
  view with 3-row bookends, timeline of prompts with cursor paging, PR-URL scan, gateway
  accepted-input owner probe, platform message id existence probe (gateway dedupe), detached
  results (delegation deliveries and completion notes recorded once as user rows, idempotent
  across rotation).
- **Legacy JSONL** [obs]: pre-store `sessions/*.jsonl` files are neither read nor written; when
  the store was replaced under a live process, pending messages are diverted (appended) to
  `sessions/<id>.jsonl` for later recovery.

### 3.10 Compression records

- **Compression lock** [obs]: per-session row with holder and expiry (default TTL 300 s).
  Acquire is a single transaction: delete a stale holder's row (expired, or provably dead local
  PID in the same PID namespace), insert-or-ignore, then read back to confirm. Refresh extends
  only while the holder matches (expiry not consulted, so a stalled refresher can revive its own
  lease). Release only if owned. Its purpose is to stop two compressions colliding.
- **Turn lease** [obs]: per-conversation (keyed by the lineage root, resolved on the writing
  connection) cross-process lease, TTL default 300 s; blocking acquire waits up to 1800 s
  polling every 1 s with a wait notice every 15 s and an abort hook; leases older than 86,400 s
  past expiry are swept. Writes presenting a lost lease fail fast (no retry).
- **Concurrent appends during compression** (MUST) [obs]: compression captures a watermark (max
  live id) at start. Rows above it that arrive during the slow summary are re-sequenced after
  the compacted set by a pure column clone (fresh ids, byte-exact payload), never summarised
  away.
- **Publish continuation (rotation)** [obs]: one transaction closes the parent with
  `compression`, inserts the child row copied from the parent (same profile, routing kept),
  inserts the handoff messages, then clones the foreign tail `(watermark, ceiling]`. The lease
  is verified (optionally refreshed first) inside the same transaction; a lost lease aborts.
  Readers see either the live parent or a complete child.
- **In-place compaction** [obs]: alternative that keeps one session id: archive live rows
  (`active=0, compacted=1`), insert the compacted set as fresh live rows, clone the concurrent
  tail, optionally merge a model_config patch. When the compressor can prove exactly which rows
  it held ("coverage", including rows an alternation repair merged), only those are archived and
  every other live row is cloned after the new set. Carried-forward originals and clone sources
  get rewind flags so search does not return duplicates. Archived generations MUST NOT be
  deleted as duplicates; diagnose duplicate live writes by `active=1` only.
- **Anti-thrash counters** (durable because the compressor is rebuilt often) [obs]: failure
  cooldown deadline + error (merge-max on write), deterministic-fallback streak, ineffective
  compaction count, recovery deadline, overload streak (atomic increment). Cooldown row snapshot
  and exact restore exist for cancellation rollback, and restore failures propagate.
- **Chains** [obs]: walk forward root→tip (cap 1000 steps per walk; callers continue past the
  cap), unique live child of a compression-ended parent (more than one → fail closed),
  reopen an orphaned compression parent only when no continuation exists and no lease is
  active, mark orphan continuations (no end, zero API calls, older than 7 days) as
  `orphaned_compression`.

### 3.11 Rewind, undo, retry, branch, reset

- **Rewind** [obs]: soft-delete every live row with id ≥ the target user row (target
  included; the caller re-offers it as the next prompt), returning the count, target, and new
  head; increments `rewind_count`. One implementation behind CLI, gateway and TUI. Target by
  user ordinal (0 = oldest; negative counts back from newest and clamps), validated against the
  caller's warm history (same user turns and same live text, else nothing changes). Optional
  requirements: the target must be losslessly replayable as text; the target must be a
  compaction "carrier" (a row combining a retained summary with a live ask), in which case the
  hidden handoff scaffold is re-inserted as the new head. A plan-only variant lets the gateway
  commit the rewind inside its re-send admission. Recent user messages for `/undo N` pickers:
  newest 20 by default, 80-char previews.
- **Branch** [obs]: inside one receipt transaction create an independent child with its own
  route, frozen policy (editor MCP selection re-prepared for the child), copied transcript up to
  a boundary row that must be live in the physical target, title either given (user) or next
  lineage title of the parent (derived), routing row and creation receipt.
- **Reset** [obs]: a canonical reset keeps the logical creation id as owner and moves the
  transcript to a child (one policy, FIFO and generation per logical conversation); listings
  show one row per local conversation.
- **Clear** [obs]: delete all messages and zero counters for a session.

### 3.12 Titles

- **Rules** [obs]: unique among non-NULL titles (unique index), max 100 chars after
  sanitising; control chars, zero-width, bidi overrides and lone surrogates stripped; whitespace
  collapsed; empty → NULL (clears).
- **Provenance** [obs]: `user` > `llm` > `derived`; NULL provenance ranks as user. User writes
  always land; automatic writes land only on untitled rows or rows with strictly lower
  authority (derived upgrades to llm once). Copying a title across rotation keeps its
  provenance.
- **Conflicts** [obs]: one place frees a title within the caller's transaction or raises:
  a compression ancestor of the requesting session yields its title.
- **Lineage numbering** [obs]: "name" → "name #2" → "name #3" (strip an existing `#N`, take the
  highest existing number + 1). Resolve-by-title prefers the newest `#N` continuation.
- Auto titles are produced by an auxiliary model once per session after the first exchange
  (3–7 words) [obs, user doc]; generation itself is out of scope.

### 3.13 Full-text search

- **Indexes** [obs]: (a) base word index (unicode61) over content, tool name, tool-call JSON,
  external-content over a stable projection view where tool rows contribute only their first
  8,192 chars; (b) trigram index for substring/CJK, excluding tool rows, tool-call JSON, and
  sessions from `cron` and `subagent` (or delegate-marked); (c) optional CJK-bigram index using a
  loadable tokenizer library (configurable on/off, default on when present). The projection MUST
  be stable (depend only on the row) so FTS integrity checks and trigger delete/update commands
  agree with what was indexed.
- **Query handling** [obs]: user input capped at 2,048 chars; sanitiser keeps paired quoted
  phrases, strips unmatched specials, quotes hyphenated/dotted terms, drops dangling boolean
  operators. Default limit 20, offset paging, sort by bm25 rank or newest/oldest (timestamp then
  rank). Filters: source include/exclude, role, session start window, include inactive. Live and
  compaction-archived rows are searchable; rewound rows are not (unless requested).
- **Routing** [obs]: explicit tool-role search → LIKE over canonical rows (full content). Stale
  FTS → LIKE fallback with a compiled boolean subset. CJK query → bigram index (unless the query
  wants rows the substring indexes exclude, or has a lone single CJK char) → trigram (each token
  ≥ 3 CJK chars) → LIKE with OR per token. Latin query → base index; on zero results try bigram
  then trigram (all tokens ≥ 3 chars) because unicode61 glues Latin to adjacent CJK; still zero
  and multi-term → retry with an OR-relaxed query. During a deferred rebuild, top up with a
  bounded LIKE scan over the not-yet-indexed id range.
- **Result shape** [obs]: `Hit { id, session_id, role, snippet (match markers), timestamp,
  tool_name, source, model, session_started, context? }`; context = one neighbour before/after,
  each truncated to 200 chars, fetched in batches only when requested. Slow searches (default
  ≥ 1000 ms, 0 = log all) log their route.
- **Corruption fail-open** (MUST) [obs]: an FTS-scoped corruption on write or search records a
  durable `fts_stale` marker and drops the FTS triggers in the same transaction, then canonical
  writes continue and searches use LIKE. The live operation never runs a full rebuild. A later
  open may rebuild under a cross-process admission lock (120 s timeout, 0.1 s poll; holder
  record with pid + start ticks; dead holders broken after 5 s reacquire window) and only when
  no foreign holder has the file open; otherwise it defers with backoff 60 s growing to 3600 s,
  escalating after 3 attempts/60 s and giving up as futile after 10 attempts/1800 s. Orphan
  FTS shadow tables (virtual table row missing) are dropped per family by exact name.
- **Maintenance verbs** [obs]: `merge` (bounded, automatic), `optimize` (manual, merges then
  VACUUM), `rebuild` (manual repair, admission-locked), and the opt-in storage upgrade: demote
  legacy virtual tables to plain tables (O(1)), create the new external-content tables, backfill
  in chunks claimed atomically (high-water + progress markers; concurrent processes interleave),
  tear down demoted tables in chunks, VACUUM, then stamp the layout version inside the write
  transaction. Re-running resumes; status reports pending/total/indexed/percent.

### 3.14 Usage accounting

- **Writer** [obs]: token/cost reports are queued to a background thread that coalesces adjacent
  incremental deltas with identical routes (absolute reports never merge) and applies them in
  order; it retires after 30 s idle; `get_session` drains first so readers see exact totals;
  flush has a timeout and never raises; after close, reports fall back to the synchronous path.
- **Semantics** [obs]: incremental mode adds to counters (per-API-call, CLI path); absolute mode
  sets them (gateway holds cumulative totals). Route/cost columns are COALESCE-filled; a
  separate unconditional route update reflects the latest model switch. Every main-loop delta
  also accumulates into the per-model usage table; auxiliary calls (vision, compression, title
  generation…) record only into the per-model table with a non-empty `task`, summed across the
  compression lineage on read. Store-wide totals prefer billed cost over estimate. "Recent model
  route" reads the most recently used coherent (model, provider, base url) tuple.

### 3.15 Media and spillover retirement

- [obs] When sessions are deleted, the media files and spillover archives their messages
  reference are recorded as durable candidates in the same transaction (the pointers vanish with
  the rows). A later collector unlinks only those no surviving admission or message still
  references (a sibling branch sharing the pointer keeps it); unknown reference sets keep files.
  Collection is synchronous SQLite + one message pass, so async callers run it off the loop.

### 3.16 Lineage queries

- [obs] Compression lineage (ancestors through tip), compression chain, tip, conversation
  root (stable id across segments and delegates, used for usage tagging), resume lineage ids
  (verified hops only), ancestor display prefix (rows whose session id is not the tip),
  workspace grouping, local-conversation grouping (reset segments collapsed to one row with
  every segment id annotated so client deletes/tombstones match).

### 3.17 Deletion and retirement fences

- **Delete** [obs]: removes the session, its messages, and recursively its delegate children;
  branch/compression children are orphaned (parent set NULL), not deleted. Optional expected-id
  and display-snapshot fences detect drift. Refused while a live turn lease or compression lock
  protects the row (expired/dead ones are reaped). Ledger rows are retired first in the same
  transaction; live or unknown work blocks the delete. Unreferenced prompts are garbage
  collected. After commit, an `on_session_delete` plugin hook fires under the store's own
  profile home (fail-open), and transcript side files (`<id>.json`, `<id>.jsonl`, legacy
  snapshot, request dumps) are removed best-effort. Bulk delete runs in one transaction and
  skips unknown ids. Empty-session deletion only applies to ended, untitled, childless,
  message-less rows.
- **Tombstones** [obs]: deleting writes durable markers so an exact retry of a settled request
  reports the original result instead of "not found", and a user-deleted id can never be
  recreated (derived ids move to a next generation). Retired receipts keep exact-retry outcomes
  but drop transcript copies and user content. Sweep-created markers expire after 7 days; user
  deletes are permanent. Imports refuse ids that were deleted here.
- **Meta key families** (generic prefixes, may be kept) [obs]: mutation receipts, deferred
  picks, local creation policy, API binding/declared conversation/settings, retired session,
  retired route, viewer close, history control, local legacy binding, queued replacement,
  admission result, terminal admission/identity/worker, media and spillover retired, legacy
  admissions import marker.

### 3.18 Runtime ledger (gateway authority data)

- [obs] Owner start bumps a single epoch row (caller must hold the profile ownership lock).
  Inputs are admitted per (principal, target, request id) with a payload digest; an exact
  retry returns the existing row, a different payload is `admission_conflict`; at most 32
  queued per session (`queue_full`). The owner claims the FIFO head (status started with epoch
  and generation), settles it terminal, cancels, steers queued input into the running claim,
  reverts steering, replaces a queued input atomically, rehomes queued inputs on an automatic
  reset. On owner start, anything left `started` becomes `unknown` and is never replayed; later
  inputs wait until an operator resolves it (discard). Worker executions register, are adopted
  only with the original producer proof, and report ordered, digest-checked receipts (exact
  retry returns the stored result). Terminal executions have their read receipts compacted.
  Metadata mutations (title, archived, hidden, pinned, unread) commit together with an
  exact-retry receipt. Refusals carry a bounded reason code plus field/detail with whitespace
  collapsed and non-printables dropped. Detailed protocol belongs to the gateway spec.

### 3.19 Gateway state slice

- **Routing index** [obs]: session key → entry JSON per scope (one scope per sessions dir);
  atomic replace-all for a scope returns keys skipped because their route is retired. A legacy
  `sessions.json` mirror is written when enabled (default on) and is NOT the session list.
- **Peers and orphans** [obs]: record routing peer on a row (display name/origin optional);
  newest row per key listing; find latest recoverable session for a peer (rows ended by known
  accidental reasons are recoverable); report message-bearing rows that lost their key and are
  adoptable only with exactly one keyed predecessor (by lineage, or contiguity within a window,
  default 900 s per user doc); adopt re-verifies inside the transaction. Never-active keyed rows
  (no messages, tokens, calls, activity or title) may be pruned with their routes.
- **Hygiene streak** [obs]: per-key atomic increment/reset of failure streak.
- **Heartbeats** [obs]: each backend upserts `{backend_id stable for the process, pid,
  started_at, last_heartbeat, profile, host}` (refresh default 60 s, from the reaper), clears its
  row at exit, and stale rows are pruned by age. Orphan sweeps only reap a row when no fresh
  backend could own it.
- **Handoffs** [obs]: `request` (fails if one is in flight) → `pending` → `claim` (atomic to
  `running`) → `completed` or `failed` with reason; failures may be compare-and-swap on state;
  at watcher start any `running` row belonged to a dead gateway and is failed.
- **Profile identity** [obs]: rename rewrites exact profile identity (routing keys
  `agent:<ns>:…`, profile_name columns, topic rows) atomically; delete purges it.
- **Conversation generation** [obs]: per (source, key) counter advanced only in the transaction
  that writes a reset boundary; used to tell peers a conversation boundary was crossed.
- **Desktop holder reap** [obs]: old PPID-1 desktop backends with no client may be terminated
  before repair; anything unprovable stays a blocking holder.

### 3.20 Telegram DM topic slice

- [obs] Two tables created only on explicit opt-in (never at startup), self-healed to the
  current shape (v3) on read: `TopicMode { profile_name (default 'default'), chat_id, user_id,
  enabled, activated_at, updated_at, has_topics_enabled?, allows_users_to_create_topics?,
  capability_checked_at?, intro_message_id?, pinned_message_id? }` keyed (profile, chat), and
  `TopicBinding { profile_name, chat_id, thread_id, user_id, session_key, session_id (cascade
  delete), managed_mode (default auto), linked_at, updated_at }` keyed (profile, chat, thread),
  unique on session. A session binds to at most one topic. Reads treat absent tables as empty.
  Multiplexed callers MUST pass the routed profile, never the process-global one.

### 3.21 Terminal tombstone slice

- [obs] Read-only identity lookups over retired admissions/workers for a deleted target: a
  retired target only answers the exact original digest (never fresh admission). Stored results
  keep the final reply once (the closing assistant message's content is replaced by a reference
  envelope) and drop cumulative history.

### 3.22 Maintenance

- **Auto-prune** (off by default) [obs]: at CLI/gateway/cron startup, under a non-blocking
  cross-process maintenance lock, at most once per 24 h (meta `last_auto_prune`, shared by all
  processes): delete ended sessions inactive for `retention_days` (default 90; negative or
  missing → skip with warning), keeping compression ancestors while any later segment is kept
  and sparing pinned and guarded rows; then close stale open rows from state-owned sources
  (`cli`, `cron`, `kanban`, `acp`, `api_server`, `subagent`, `tool`, `recovered`) older than the
  retention window with `startup_orphan_reap` (resumable; aged from the close); then VACUUM only
  if rows were pruned AND ≥ 30 days since last VACUUM (meta `last_vacuum`) AND freelist ratio
  > 25% AND no other process or in-process generation holds the file. Long steps declare 900 s
  watchdog leases. Never raises.
- **Low-disk prompt** [obs, user doc]: when config lacks the auto-prune key and the disk holding
  the store has < 2 GB free (or < 10% free with < 20 GB left), surfaces ask once.
- **Auto-archive** (off by default) [obs]: soft-hide lineages idle ≥ 3 days, at most once per
  24 h (meta `last_auto_archive`), pinned exempt, only tips qualify.
- **Ghost prune** [obs]: delete `tui` rows untitled, ended, older than 24 h, with no messages.
- **Orphan sweep** [obs]: close open rows of given sources (default `tui`, `desktop`,
  `subagent`) whose start AND last activity are older than the idle threshold, unless a fresh
  backend heartbeat could own them (staleness default 2× idle, ownership grace default =
  staleness), or a live lease/lock protects them; never for messaging sources.
- **Manual prune/archive** [obs]: shared filter surface (age as duration, absolute windows,
  source, title/cwd substrings, archived tri-state, pinned handling), dry-run candidate lists and
  counts. While a gateway runs, prune previews read-only and the gateway performs the delete.
- **VACUUM** [obs]: refuses on a quarantined handle; merges FTS first; PASSIVE checkpoint
  before, TRUNCATE after (else a 3 GB rewrite leaves a 3 GB WAL), then re-record file identity
  (TRUNCATE may replace the WAL inode). Logical size = page_count × page_size.
- **Stats** [obs]: strictly read-only snapshot with short timeout; each field independent.

### 3.23 Integrity, health and recovery

- **Replaced file** [obs]: before every write (inside the mutex) compare the path's (device,
  inode) and header `application_id` with what was opened. Mismatch → sticky
  `StoreReplaced` error, close-time checkpoint disabled; pending transcripts are diverted to
  JSONL and the gateway spool. In-file repair is never attempted on a new generation.
- **Lost WAL generation** [obs]: if the `-wal`/`-shm` this handle opened were unlinked (a
  sibling's close or a cleanup) the handle halts writes, durably captures the retired WAL (and
  the main image when ≤ 512 MiB) into `<name>.retired-wal-<utc>-<pid>/` with a versioned
  manifest (copied via pread of SQLite's own descriptor in 8 MiB chunks, fsynced, renamed), and
  refuses to settle without that capture. The check must run under the same mutex as `close()`
  or the process's own clean close looks like external deletion.
- **Lock guard** [obs]: SQLite protects a live WAL generation with a SHARED lock on the main
  file's lock range and a shared lock on the `-shm` DMS byte (offset 128). Any `open()`/`close()`
  of those files inside the process cancels POSIX locks, letting a sibling unlink the WAL. The
  guard re-takes those ranges as OFD locks on every descriptor the process has open on the file,
  refcounted per inode, released before the handle's own close. All byte-level header probes use
  a cached never-closed descriptor (pread) for the same reason.
- **Structural corruption quarantine** [obs]: bare corrupt/not-a-database errors without FTS
  provenance poison the handle for the process lifetime: the failing write raises, later writes
  fail immediately, no reopen after close, no explicit close-time checkpoint and (where the
  runtime allows) SQLite's own last-connection checkpoint disabled so the WAL survives for
  forensics. A process-wide health latch per path records "corrupt" for readiness probes and
  listing endpoints (so a damaged store is not shown as an empty sidebar).
- **Holder scans** [obs]: who has the file or a sidecar open — Linux `/proc/*/fd` (with a
  `(deleted)` pre-filter confirmed by identity, since some filesystems mislabel live files),
  macOS libproc (5 s scan timeout), Windows Restart Manager. Processes are classified by argv as
  belonging to this install/profile or another (explicit home flags, env assignments, profile
  flags, path tokens). A failed scan counts as an unknown holder (fail closed). Write-lock owners
  are named from `/proc/locks` byte offsets (main file PENDING/RESERVED range; `-shm` byte 120
  write, 121 checkpoint, 123–127 read slots). PID-namespace stamps on holder strings mean a
  holder from a sibling namespace is never probed by PID: TTL rows defer to expiry, persistent
  records follow a legacy-compatible rule.
- **Structural maintenance refusal** [obs]: VACUUM, index rebuild, bulk delete and journal mode
  switch refuse while any foreign or other in-process holder exists, listing each PID and
  command; dry-runs are never blocked; `--force` overrides.
- **Schema repair** [obs]: under a cross-process repair lock (120 s, 0.1 s poll), only when no
  live writer holds the file: check disk headroom (≥ 256 MiB or 2% free beyond the copy), take a
  raw forensic backup of file+sidecars (publish sidecars first, main last as commit marker; keep
  3 newest), snapshot via the online backup API onto a scratch copy (timeout scaled at
  ≥ 10 MiB/s), run escalating strategies on the scratch copy only — FTS rebuild, REINDEX,
  dedupe duplicate schema objects (lowest rowid wins, bump schema cookie), drop all FTS +
  VACUUM — verify the copy opens cleanly (journal probe, integrity check, sessions read, FTS
  probe), then copy back through the backup API and restore the journal mode on the exclusive
  guard connection. A persistent ledger caps attempts at 3 per content fingerprint (size plus
  sampled head/tail with volatile header bytes masked; mtime excluded); only deterministic
  corruption consumes an attempt (locks, timeouts, disk full do not). Canonical rows are never
  deleted to clear an index error; row counts must not decrease.
- **Recover** [obs, docs]: a salvage command inspects (read-only) and copies row-bearing tables
  into a new file, regenerating FTS and version, verifying counts, and excluding the four runtime
  ledger tables (a partial ledger could replay started effects). Full backup restore keeps the
  ledger.
- **Stale stored prompts / tool markers** [obs]: repair verbs to clear verified degraded
  system prompts and purge bare tool-call marker content, optionally with a `VACUUM INTO`
  backup.

### 3.24 Portability

- **Export** [obs]: single session, compression lineage as one logical document (with segment
  list), or all sessions; flags include compaction-archived display history (not for re-import)
  or every row with its flags (round-trippable). Each export carries text-free timing evidence.
  In-memory exports respect the 20,000-message guard; a streaming dashboard export does not.
  Output formats (JSONL default, HTML, prompts-only, trace, Markdown/QMD with redaction) are CLI
  UX (area 04).
- **Import** [obs]: limits 500 sessions, 10,000 messages per session, 50,000 total, 5 MiB per
  session, 25 MiB total; whole payload validated first and every error reported; NaN/Infinity
  anywhere rejected; ints clamped to SQLite range, floats must be finite; one transaction; existing
  ids skipped; deleted-here ids refused; parents re-attached only if present (cycles broken at
  the closing edge), otherwise detached; routing/handoff/rewind/activity fields reset (export
  includes activity, import clears it — deliberate). Foreign-history import (other agent tools)
  is idempotent per source provenance.
- **Cross-store moves** [obs]: crossed-profile repair finds mislabelled rows, parents keyed in
  another profile, profile-less topic rows; it relabels, severs, or moves whole rows (every
  message generation, prompt, usage) to the owning store; rows with ledger history refuse to
  move. Stranded lineages may be adopted from a donor store with a divergence re-check.

### 3.25 Error classification and user copy

- [obs] One classifier maps any persistence error (live exception or RPC string) to a bucket:
  `locked`, `disk`, `compression`, `compression_closed`, `turn_lease`, `corrupt`, `fts_index`,
  `replaced`, `deleted_wal`, `session_row_missing`, `unknown`. Order: typed errors first,
  FTS provenance before prose, lock result codes, FK constraint, phrases, disk-full. One table
  per bucket supplies a plain-language what-happened / what-to-do line plus a machine code so
  GUIs can show a "run doctor" button; a secondary details line carries the raw cause. The
  "unavailable" banner includes a network-drive hint where relevant. Transient vs damage also
  decides HTTP 503 vs 500.

### 3.26 Test-isolation guard

- [obs] Any process that looks like a test run (env markers or a test-runner ancestor process)
  refuses to open `<real default root>/state.db` or `<root>/profiles/<name>/state.db`; deeper
  scratch paths are allowed. The real root is computed without the patched home helpers. A
  bypass env var exists for a spawned child that must touch the live store, plus an in-process
  marker; neither should be exported in a user shell.

### 3.27 Logging

- **Files** [obs]: under `<home>/logs/`: `agent.log` (configured level, default INFO, default
  5 MiB × 3 backups), `errors.log` (WARNING+, 2 MiB × 2), `gateway.log` (gateway mode only,
  INFO+, 5 MiB × 3, gateway logger prefixes), `gui.log` (dashboard/TUI-gateway mode, INFO+,
  10 MiB × 5). Config keys `logging.level`, `max_size_mb`, `backup_count`.
- **Mechanics** [obs]: all file handlers sit behind one in-process queue and listener thread so
  emitters never block on file I/O or rotation locks; records are not pre-formatted. Every
  record gets a session tag (` [<session id>]` when set for the thread) via the record factory
  and the resolved home, so a multi-profile process routes records to the right profile's files
  (handlers created lazily per home; released when a profile is deleted). A redacting formatter
  scrubs secrets; detached processes route stdout/stderr through redaction too (their stdio is a
  persisted log). Rotation detects external rotation (inode change) and reopens. Managed installs
  get group-readable files. On Windows a cross-process rotating handler is used when its lock
  backend works; otherwise rotation is disabled with a one-time warning, and its lock timeouts
  are suppressed after one warning. Noisy third-party loggers are pinned at WARNING. A verbose
  flag adds DEBUG console logging. Flush (stop+restart listener) and a time-bounded drain for
  hard exits exist. Piped stdout is line-buffered.

### 3.28 Clock and YAML policy

- **Clock** [obs]: `now()` returns an aware datetime in the configured IANA zone: env var first
  (only when it may speak for the active profile — under a multiplexed gateway it carries the
  default profile's value, so routed profiles read their own config), then `timezone` in config,
  else server-local. Invalid zones warn and fall back. Cached per profile; reset hook. A safe
  strftime repairs locale zone names containing surrogates (Windows ANSI code pages).
- **YAML** [obs]: YAML 1.1 semantics (bare y/n booleans), duplicate keys rejected, pure-Python
  parser always (the C extension rejects documents the pure parser accepts and is missing on
  some platforms), fresh parser per operation (not thread-safe), dumps with readable Unicode and
  indented block lists, and a comment-preserving round-trip editor with unlimited width for
  user-authored files.

### 3.29 Process bootstrap, startup watchdog, directory ACL, registration leases

- **Bootstrap** (imported first by every entry point) [obs]: Windows UTF-8 stdio and ANSI
  console enablement; stub the Windows `ver` shell-out; make environment writes safe on glibc
  older than 2.41 (never free the old environ array); stop the cwd from shadowing first-party
  modules; export the scratch TMPDIR; settle an interrupted self-update before imports; pin a
  minted launcher's home; Happy Eyeballs connects (interleave address families, 0.25 s stagger),
  applied to urllib3 lazily via an import hook to avoid a ~50 ms import at start.
- **Startup watchdog** [obs]: a daemon thread armed at gateway process entry, disarmed once the
  event loop is live. Default deadline 300 s (env override, floor 30 s; opt-out env). When the
  deadline passes: honour a phase-declared progress lease (each call clamped to 900 s; schema
  init declares 600 s, maintenance steps 900 s); else extend if the process burned ≥ 1 s CPU in
  the window, at most 3 times; else fire: write a JSON record and all-thread stack dump to
  `logs/gateway-startup-watchdog.log`, record the exit in the lifecycle ledger (5 s bounded),
  and hard-exit with code 75 so the supervisor restarts; an escort thread hard-exits after 10 s
  if forensics wedge. Disarm and fire are mutually exclusive under a state lock. A `kick` pushes
  the deadline for intentional idle waits.
- **Directory ACL (Windows)** [obs]: an elevated process creating owner-only dirs on newer
  Python leaves dirs unusable to the same user unelevated; the helper grants the token user an
  inheriting full-control entry on each dir it creates (and on a tree after elevated updates),
  refusing junctions, symlinks, reparse points and hard-linked files, falling back to `icacls`.
  No-op elsewhere.
- **Registration leases** [obs]: a coordinator for replaceable registry slots models ownership
  generations; disposing a lease restores the nearest still-live predecessor regardless of
  unload order; registry snapshots compare by identity.

### 3.30 Trajectories

- [obs] Not stored in the session store. The agent appends ShareGPT-format JSONL entries to
  `trajectory_samples.jsonl` or `failed_trajectories.jsonl` (by completion), under an exclusive
  whole-file lock (flock on POSIX, 1-byte lock on Windows). Batch-runner/RL compression of
  trajectories is a separate tool.

## 4. File map (what to read in the Read step)

| Sub-area | Reference path | Lines | Read what | Docs / tests to read with it |
|---|---|---|---|---|
| Facade, open/close, write/read paths | `hermes_state.py` | 1730 | all of the class body; skip test-isolation details | `website/docs/developer-guide/session-storage.md` (604), `tests/hermes_state/test_write_lock_patience.py` |
| Shared constants, DDL, FTS DDL, locks | `hermes_state_common.py` | 1469 | declared tables and indexes, end-reason families, FTS projection, rebuild admission lock | `test_fts_rebuild_admission.py` |
| Schema init and migrations | `hermes_state_schema.py` | 1379 | init sequence, data migrations, uid backfill, PK heals, stale FTS recovery | `test_runtime_schema.py`, `test_schema_read_probe.py`, `test_message_uid.py` |
| Messages / transcripts | `hermes_state_messages.py` | 1972 | append, guards, archive_and_compact, display projection, resume assembly, rewind_to_message | `test_compression_watermark_commit.py`, `test_display_projection_parity.py` |
| Sessions | `hermes_state_sessions.py` | 1980 | upsert/inherit, flags, listing filters, delete paths | `test_delete_compression_chain.py`, `test_pinned_lineage.py` |
| Search and FTS maintenance | `hermes_state_search.py` | 1368 | `_search_messages_impl`, CJK routing, optimize/rebuild | `test_fts_cjk_bigram.py`, `test_search_or_relaxed_fallback.py` |
| FTS setup and fail-open | `hermes_state_fts.py` | 453 | all | `test_fts_index_fail_open.py`, `test_fts_orphan_shadow_repair.py` |
| Compression records | `hermes_state_compression.py` | 848 | locks, leases, publish, counters, chains | `test_hermes_state_compression_locks.py`, `test_session_turn_lease.py` |
| Coverage for compaction | `hermes_state_coverage.py` | 264 | skim | `test_repaired_history_commit_coverage.py` |
| Rewind | `hermes_state_rewind.py` | 169 | all | `test_rewind_surfaces_invariant.py`, `test_composite_carrier_rewind.py` |
| Titles | `hermes_state_titles.py` | 230 | all | `test_canonical_title_guard.py` |
| Usage | `hermes_state_usage.py` | 446 | all | `test_aux_usage_accounting.py` |
| Maintenance | `hermes_state_maintenance.py` | 586 | all | `test_sweep_orphaned_sessions.py`, `test_auto_vacuum_holder_gate.py` |
| Portability | `hermes_state_portability.py` | 840 | export/import/validation | `test_session_import_value_validation.py`, `test_session_export_timings.py` |
| Gateway slice | `hermes_state_gateway.py` | 1025 | routing, orphans, heartbeats, handoffs, rekey/purge | `test_orphan_gateway_session_repair.py`, `test_rekey_profile_state.py`, gateway-session-lifecycle doc (659) |
| Telegram slice | `hermes_state_telegram.py` | 370 | all | `test_telegram_topic_selfheal.py` |
| Terminal slice | `hermes_state_terminal.py` | 170 | skim | `test_terminal_retirement.py` |
| Runtime ledger | `hermes_state_runtime.py` | 933 | admission/claim/settle/recover, receipts | `test_runtime_admission.py`, `test_runtime_recovery.py` |
| Ledger helpers | `hermes_state_runtime_workers.py` (64), `hermes_state_worker_compression.py` (467), `hermes_state_worker_context.py` (86), `hermes_state_worker_lifecycle.py` (274) | 891 | skim; gateway spec owns detail | `test_runtime_worker*.py` |
| Mutation verbs | `hermes_state_mutations.py` (184), `_mutation_binding` (95), `_mutation_branch` (101), `_mutation_compress` (27), `_mutation_guards` (121), `_mutation_prepared` (137), `_mutation_reset` (43), `_mutation_retirement` (433), `_mutation_transcript` (38) | 1179 | retirement fences fully; others skim | `test_runtime_mutation*.py`, `test_retired_fence_outlives_horizon.py` |
| Local receipts | `hermes_state_local.py` (270), `_local_lineage` (193), `_local_migration` (102) | 565 | skim | `test_reset_lineage_delete_queued_work.py` |
| Media | `hermes_state_media.py` | 69 | all | — |
| Detached results | `hermes_state_detached_results.py` | 59 | all | — |
| Ids / keys / identity codec | `hermes_state_ids.py` (60), `_keys.py` (92), `_identity.py` (111) | 263 | all | `test_hermes_state_ids.py`, `test_session_id_recognition_recovery.py` |
| Timeline | `hermes_state_timeline.py` | 167 | skim | `test_get_messages_around.py` |
| Journal mode / pragmas | `hermes_state_wal.py` | 669 | `apply_wal_with_fallback`, synchronous, pragmas | `test_hermes_state_wal_fallback.py`, `test_cross_vm_fs_wal_refusal.py`, `test_sqlite_wal_reset_gate.py` |
| Read pool | `hermes_state_readpool.py` | 259 | all | `test_session_db_read_conn_pool.py` |
| File-level helpers | `hermes_state_dbfile.py` | 849 | header probe, quarantine, retired-WAL capture, holder enumeration | `test_zeroed_db_quarantine_race.py`, `test_retired_wal_generation_capture.py` |
| Holder authority | `hermes_state_holders.py` | 658 | foreign holders, argv classification, refusal text | `test_state_db_holders.py` |
| Lock guard / lock owners / PID ns | `hermes_state_lockguard.py` (197), `_lockowners.py` (124), `_pidns.py` (128) | 449 | all | `test_wal_lock_guard.py`, `test_write_lock_owner_attribution.py`, `test_lease_pid_namespace.py` |
| Errors / health / guard / user copy | `_errors.py` (305), `_health.py` (114), `_guard.py` (170), `_user_copy.py` (113) | 702 | all | `test_storage_failure_copy.py`, `test_storage_health_latch.py`, `test_live_db_isolation_guard.py` |
| Registry | `hermes_state_registry.py` | 478 | all | `test_shared_session_db_registry.py` |
| Repair | `hermes_state_repair.py` | 1161 | lock, ledger, backup, strategies | `test_state_db_malformed_repair.py`, `test_state_db_repair_loop_cap.py`, state-db-recovery doc (111) |
| Drift reconcile | `hermes_state_reconcile.py` | 514 | all | `test_schema_read_probe.py` |
| Profile repair | `hermes_state_profile_repair.py` | 388 | skim | `test_purge_profile_state.py` |
| Model-config helper | `hermes_state_sessions_model_config.py` | 42 | all | — |
| Home / profiles / scratch | `hermes_constants.py` (1467), `hermes_constants_scratch.py` (473), `hermes_constants_scratch_stuck.py` (192) | 2132 | home resolution, profile predicates, permissions, scratch | `website/docs/user-guide/profiles.md` (465) |
| Logging | `hermes_logging.py` | 1005 | setup, queue, profile routing | — |
| Clock / YAML | `hermes_time.py` (149), `hermes_yaml.py` (98) | 247 | all | — |
| Bootstrap / watchdog / ACL / leases | `hermes_bootstrap.py` (622), `hermes_startup_watchdog.py` (513), `hermes_dir_acl.py` (462), `registration_lifecycle.py` (95) | 1692 | watchdog fully; others skim | `test_startup_maintenance_lease.py` |
| Trajectories | `agent/trajectory.py` | 56 | all | — |
| Defaults | `hermes_cli/config_defaults.py` | 3156 | only the `sessions`, `database`, `logging` sections | `website/docs/user-guide/sessions.md` (1248) |

Giant files: `hermes_state_messages.py` [1972] — the parts that matter are the append/guard
block, `archive_and_compact` and its on-connection body, the display dedupe/order functions,
`get_resume_conversations`, `rewind_to_message`; the reaction/Todo/detached helpers are
peripheral. `hermes_state_sessions.py` [1980] — `_insert_session_row`, `end_session`/
`reopen_session`, the lineage flag setters, `list_sessions_rich` + `_project_compression_tips`,
`delete_session(s)`. `hermes_state.py` [1730] — `__init__`, `_open_writer`, `_execute_write`,
`_read_ctx`, the generation/replaced/corrupt guards, `close`. `hermes_state_common.py` [1469] —
the declared DDL string and end-reason constants; ignore the SQL-building helpers' text.
`hermes_state_schema.py` [1379] — `_init_schema` and `_run_data_migrations`. `hermes_state_search.py`
[1368] — `_search_messages_impl`, `_search_cjk`, `_sanitize_fts5_query`, `optimize_fts_storage`.
`hermes_constants.py` [1467] — only the first ~450 lines (home/profile) and the permission/scratch
block; the rest is unrelated (reasoning effort, Node paths, WSL).

## 5. Minimum viable subset

v1 needs [inf]:
1. Home resolution (override → env → platform default) and named profile dirs with the name
   rule; owner-only permissions; scratch TMPDIR.
2. One store per profile: writer connection with `BEGIN IMMEDIATE`, 1 s busy timeout and the
   time-budgeted jittered retry (20/60/0.5 s), WAL with "never live-downgrade", PASSIVE
   checkpoints every 50 writes and at close, read-only open for list commands.
3. Tables: schema version, meta, system prompts, sessions (core + title + counters + billing +
   cwd + archived/pinned + parent), messages (core + reasoning sidecars + active/compacted +
   message_uid + api_content), model usage, compression locks, turn leases. Declarative column
   reconcile from day one so later columns need no version bump.
4. Sessions CRUD with first-end-reason-wins, child kinds via markers, lineage-wide flags,
   titles with uniqueness/sanitising/provenance/`#N` numbering, rich listing with preview.
5. Messages: append (content codec, surrogate scrub), get in id order, conversation format,
   rewind (soft delete), replace, resume size guard.
6. Compression: lock + watermark + publish continuation in one transaction; in-place compaction
   can wait if v1 uses rotation only.
7. FTS5 base index with stable projection, sanitised queries, LIKE fallback, fail-open on FTS
   corruption. Trigram can follow; CJK bigram library is optional.
8. Usage accounting (synchronous first; background coalescing writer later).
9. Export/import JSONL with the validation limits; prune with retention and the VACUUM gates.
10. Logging to agent/errors files via a queue with redaction and session tags; timezone clock;
    YAML 1.1 loader.

Can wait [inf]: runtime ledger and mutation receipts (needed only with the multi-client
gateway authority), retirement tombstones, Telegram topic tables, profile repair/moves, the
retired-WAL capture and lock guard (critical for multi-process robustness, but only once several
processes share a store), schema-surgery repair (provide offline salvage first), drift
reconcile for browsing, storage-layout upgrade path (a fresh rebuild starts on the final
layout), startup watchdog, Windows ACL helper, registry generations (a simple per-path
singleton is enough until file swaps are supported).

## 6. Hard parts / traps

1. **R03.1 Never live-downgrade WAL.** Switching an existing WAL file to DELETE while another
   process has uncheckpointed commits destroys them. Treat "on-disk WAL" and "mode unreadable"
   identically: keep it. Mode changes only via an offline command that proves no holders.
2. **R03.2 Stray open/close cancels SQLite's POSIX locks.** Any `open()`+`close()` of the store
   or `-shm` inside a process that holds a WAL connection drops the locks protecting the WAL
   generation; a sibling's close may then checkpoint and unlink the WAL, splitting the store.
   Never read headers with a fresh descriptor (use a cached pread fd), and re-assert the locks as
   OFD locks. Refuse to open while any process holds a deleted sidecar.
3. **R03.3 Checkpoint mode.** TRUNCATE checkpoints on the hot path or at close under load tore
   B-tree pages on large files; use PASSIVE everywhere except right after VACUUM. A handle that
   saw corruption must not checkpoint at all on close.
4. **R03.4 Replaced file / new generation.** A backup restore or `cp` over the path while a
   process is live makes that process write stale WAL frames into a new file. Compare inode and a
   per-file `application_id` stamp before every write; on mismatch stop writing (sticky), divert
   pending transcript to JSONL, and never run in-file repair.
5. **R03.5 Structural corruption must stop writes.** A handle that kept writing ~50 minutes
   after the first malformed error turned a readable file into an unopenable one at shutdown.
   Quarantine the handle (no retries, no reopen, no close checkpoint) on non-FTS corruption.
6. **R03.6 FTS is derived; never rebuild inline.** An FTS corruption must not block canonical
   writes nor trigger a full `rebuild` in the live path. Detach triggers + durable stale marker
   in one transaction, search via LIKE, rebuild later under a cross-process admission lock only
   when no foreign holder exists. Two concurrent rebuilds corrupted production stores.
7. **R03.7 Stable FTS projection.** External-content FTS reads back the source on delete and
   integrity check; if what is indexed depends on mutable state (a moving high-water mark), the
   index drifts ("checksum mismatch"). The indexed projection (tool rows truncated to 8,192
   chars) must depend only on the row.
8. **R03.8 Retry budgets are time-based.** Attempt-counted retries lost turns on a healthy store
   while a sibling legitimately held the lock for seconds (VACUUM, checkpoint, recovery). Use
   deadlines, random jitter (avoid convoy), separate short budgets for observation-only writes
   and for compression-lock collisions, and make every write callback idempotent.
9. **R03.9 Opening must wait out locks too.** Schema init DDL on a 1 s-timeout connection failed
   the entire open when a sibling held the lock, and callers then disabled persistence for the
   whole run. Apply the same patience to the open.
10. **R03.10 Ordinary appends must not be blocked by compression.** Fencing appends on the
    compression lock killed turns during slow summaries and stranded writes behind dead PIDs for
    the full TTL. Use the watermark: rows newer than the start watermark are cloned after the
    summary in the same transaction.
11. **R03.11 Lineage-wide flags.** Archive/pin/hide/read applied only to the tip get undone
    because listings project roots forward to tips. Write the whole compression lineage, and
    keep sweep-archive provenance separate from deliberate archive.
12. **R03.12 First end reason wins.** A stale late "end" after compression must not replace
    `compression`, or chain walking breaks. Reopen is the only way to re-end.
13. **R03.13 Fork markers must name the actual parent.** Continuations copy `model_config`
    verbatim, so an inherited `_branched_from`/`_delegate_from`/`_reset_from` would make a
    continuation look like a fork. Check the marker value equals `parent_session_id`.
14. **R03.14 Order transcripts by id, never timestamp.** Clocks regress (NTP, imports); tool
    call/result adjacency breaks if ordered by time.
15. **R03.15 Long migrations hold the write lock.** A single full-table UPDATE held the lock for
    261 s on an 835k-row store and every sibling timed out. Chunk data migrations (2,000 ids),
    time-box per open (~1 s), keep a resume cursor; make heavy layout changes opt-in.
16. **R03.16 Read-only means read-only for browsing.** Dashboard/list reads must never create,
    quarantine, migrate, auto-archive, or checkpoint the store, and must return 503 on busy or
    uninitialised stores. WAL readers may still touch `-shm`; that is fine.
17. **R03.17 Descriptor exhaustion.** Unbounded per-thread or per-handle read connections drove
    processes to EMFILE (and the failure surfaced in unrelated tools). Bound per path and per
    process, gate on fd headroom, and degrade to the writer connection instead of blocking.
18. **R03.18 SQL variable limit.** Bulk deletes binding tens of thousands of ids failed ("too
    many SQL variables"). Chunk IN-lists at 900.
19. **R03.19 Surrogates and reserved prefixes.** Lone UTF-16 surrogates abort binding; scrub them
    on every text write (content, titles, api_content). A literal string beginning with the
    structured-content prefix must be escaped so imports cannot inject structure.
20. **R03.20 PID-based liveness across namespaces.** A holder PID from another container looks
    dead locally; reclaiming its lease splits work. Stamp holders with the PID-namespace id, never
    probe foreign/unstamped TTL holders (wait for expiry), and treat PID reuse conservatively.
21. **R03.21 Never replay started work.** After an owner crash, inputs that were `started` are
    `unknown` forever until a human resolves them; FIFO later inputs wait. Recovery copies must
    exclude ledger tables.
22. **R03.22 Deleted ids must stay dead.** Without retirement markers an exact retry of a
    settled request reports "not found" or recreates the session; derived ids must move to a new
    generation. Keep markers outside prunable transcript rows.
23. **R03.23 Profile cross-talk.** A subprocess that drops the home env var falls back to the
    default profile and writes there. Pass the home explicitly to every child; namespace shared
    tables (routing, topics) by profile; route log records by the record's home, not the
    process's.
24. **R03.24 Test runs touching the live store.** Fixture data leaked into real stores; the guard
    must detect tests by env and by ancestry (children with rebuilt env lose env markers).
25. **R03.25 Automatic maintenance must only skip, never refuse a turn.** VACUUM/TRUNCATE under
    a live sibling retired its WAL generation and every agent then refused turns. Auto-VACUUM
    requires no foreign and no other in-process holder; manual structural commands refuse with a
    holder list.
26. **R03.26 Repair loops.** Repeated automatic repair attempts filled disks with backups and
    never converged. Cap attempts per content fingerprint (mtime excluded because a malformed
    store still accepts writes), keep 3 backups, check free space first, operate on a scratch
    copy only.
27. **R03.27 macOS durability.** Plain fsync is not a barrier there; force `checkpoint_fullfsync`
    and `synchronous=FULL` on every connection, including repair/probe connections.
28. **R03.28 Startup watchdog vs slow I/O.** Multi-GB migrations are I/O-bound (near zero CPU)
    and look like deadlocks; long phases must declare progress leases (clamped) instead of
    relying on CPU-based extension.

## 7. Open questions

1. Final DB shape after all migrations on a long-lived install (exact column order, presence of
   lazily created tables, which FTS layout version a typical upgraded user has). Needed: needs
   REA — copy of a shipped-build `state.db` after upgrade plus a fresh one.
2. Whether `schema_version` reads 32 on shipped stores (docs say 31). Needed: needs REA — open
   a store created by the current release.
3. Exact FTS DDL semantics of the projection views and the CJK-bigram tokenizer library (built
   from a native dir; is it shipped by default on any platform?). Needed: needs REA — inspect
   installed package for the tokenizer library and `sqlite_master` of a fresh store.
4. Message content reserved prefix exact bytes (`\x00json:` per comment) and whether any
   shipped client depends on it. Needed: source confirm of the class constant, plus a product
   decision whether the rebuild keeps a compatible codec (only matters for importing reference
   stores).
5. Do we need read compatibility with reference stores/exports at all (import of their JSONL,
   opening their SQLite)? Needed: lead decision; affects whether we mirror column names.
6. Heartbeat staleness default used by gateway orphan sweeps (only the 60 s refresh and the
   "2× idle" fallback were seen). Needed: read gateway startup/housekeeping code (gateway area).
7. Exact `sessions.json` mirror shape and when it is written relative to the routing table.
   Needed: gateway spec (`gateway/session*.py`).
8. State snapshot (`state-snapshots/`) mechanics, `backup` command and `recover` command output
   format live in CLI modules not read here. Needed: area 04 research or a REA run of the
   commands.
9. Whether the in-place compaction path or the rotation path is the default in the shipped
   build (both are implemented). Needed: needs REA — run a long session and inspect rows, or
   read `agent/context_compressor` (area 01).
10. Windows behaviour of the lock guard and holder scans (Restart Manager) in practice. Needed:
    needs REA on Windows, or accept as not-in-v1.
11. Value of `pricing_version`/`cost_status`/`cost_source` enumerations. Needed: read the usage
    pricing module (area 01/providers).

## 8. What to read later

- Reference docs: `website/docs/developer-guide/session-storage.md`,
  `website/docs/developer-guide/state-db-recovery.md`,
  `website/docs/user-guide/session-storage-recovery.md`, `website/docs/user-guide/sessions.md`
  (storage, cleanup, oversized guards, journal-mode sections), `website/docs/user-guide/profiles.md`
  ("How it works"), `website/docs/developer-guide/gateway-session-lifecycle.md` (restart recovery,
  housekeeping), `website/docs/developer-guide/context-compression-and-caching.md`.
- Reference tests that list the edge cases: the whole `tests/hermes_state/` directory (200
  files); highest value first: `test_write_lock_patience.py`, `test_wal_checkpoint_strategy.py`,
  `test_state_db_corrupt_quarantine.py`, `test_state_db_file_identity.py`,
  `test_deleted_wal_generation_guard.py`, `test_wal_lock_guard.py`,
  `test_fts_index_fail_open.py`, `test_fts_projection_integrity.py`,
  `test_compression_watermark_commit.py`, `test_session_turn_lease.py`,
  `test_delete_compression_chain.py`, `test_pinned_lineage.py`,
  `test_auto_archived_lineage_reactivation.py`, `test_prune_activity_recency.py`,
  `test_session_import_value_validation.py`, `test_message_uid.py`,
  `test_session_db_read_conn_pool.py`, `test_live_db_isolation_guard.py`,
  `test_runtime_admission.py`, `test_runtime_recovery.py`, `test_retired_fence_outlives_horizon.py`;
  also `tests/test_state_config_contention.py`.
- REA Evidence (outside the repo): none yet; planned bundle `~/rea-lab/evidence/03-state/`
  (fresh and upgraded `state.db` copies, `sqlite_master` dump, CLI `sessions` command captures).
- Related specs: `01-agent-loop.md` (compression decisions, message metadata keys, persistence
  flush), `02-tools.md` (session search tool shapes), `04-cli.md` (`sessions` subcommands, backup,
  recover, doctor, profile commands), `07-memory-skills.md` (memory/skills dirs under home), and
  the gateway area spec (routing entries, admissions protocol, handoff watcher).
