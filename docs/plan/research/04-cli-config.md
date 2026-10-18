# 04 — CLI, configuration, auth and lifecycle: behavioural specification (clean-room)

Scope: the command-line program (`indus`) and everything it owns outside the agent loop: process
startup and dispatch, every top-level command group and its flags, the classic interactive chat
REPL surface (keys, slash-command registry and dispatch, bang shell, quick commands), the one-shot
modes, the data home and profile model, the config file (defaults, load/merge/save, migration,
`config get/set/unset`), the secrets file and external secret sources, provider auth (API keys,
OAuth device and loopback flows, the credential pool), the model picker and setup wizard, tools
enable/disable, approvals mode, checkpoints CLI, backup/import, doctor/dump/debug/status/logs,
update/self-update, uninstall, install scripts and packaging entry points.
Out of scope: agent loop, compression, provider wire adapters (`01-agent-loop.md`); tool
implementations, approval guard internals, checkpoint store internals (`02-tools.md`); session DB
schema and repair internals (`03-state-db.md`); full-screen TUI (`05-tui.md`); gateway, messaging
platforms, multiplexing, service install (`06-gateway.md`); skills, curator, cron, kanban
(`07-skills-cron.md`); desktop app and web dashboard (`08-desktop.md`).
Reference: reference repo @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056` (MIT); shipped build
checked: none (REA not connected this session — source reading only).
Sources studied: `website/docs/reference/cli-commands.md`, `slash-commands.md`,
`environment-variables.md`, `profile-commands.md` (skim), `getting-started/installation.md`,
`updating.md`, `user-guide/cli.md`, `checkpoints-and-rollback.md`, `which-file-does-what.md`;
code under the CLI package (parser, every subcommand builder, `main`, `config*`, `env_loader`,
`auth*`, `profiles`, `setup*`, `backup`, `doctor*`, `uninstall`, `bang_shell`, `commands`,
`approval_mode`, `checkpoints`, `gateway_chat*`), `cli.py`, `cli-config.yaml.example`,
`pyproject.toml`, `setup.py`, `setup-<name>.sh`, `scripts/install.sh` (header and flags), the
package `AGENTS.md` map. No tests were read in depth.

Conventions:
- "Home" = the active data directory (default `~/.indus`, see §3.9). "Root" = the default
  (pre-profile) home; named profiles live under `<root>/profiles/<name>/`.
- "Install dir" = the source checkout or package the code runs from (distinct from home).
- "Gateway" = the long-lived per-profile agent server process (spec 06). In the current reference
  the classic chat REPL is a client of it (§3.4.6).
- `INDUS_X` = the rebuild's name for a reference env var; the mapping rule is in §2.
- "MUST" marks an invariant the reference defends with code, a comment naming a past bug, or a
  test.
- Evidence tags: [obs] read in source/docs, [inf] inferred from source shape, [unk] not settled.

---

## 1. Overview

The CLI is one Python console script with ~70 top-level commands built on the standard argparse
library. Bare `indus` (no subcommand) means "chat". The program's job splits in four:

1. **Bootstrap and locate state.** Before any app module imports, resolve which profile/home this
   process serves (from `-p/--profile`, a sticky `active_profile` file, or `INDUS_HOME`), load
   `<home>/.env` into the process environment, bridge a few early config flags into env, set up
   file logging, then build the parser and dispatch.
2. **Talk to the user.** Chat (classic REPL, TUI hand-off, one-shot `-q`/`-z`), and interactive
   wizards (`setup`, `model`, `auth`, `tools`, `profile`, `config edit`) that use curses pickers
   and masked secret prompts.
3. **Own configuration and secrets.** `config.yaml` (non-secret settings, deep-merged over a large
   built-in default tree, version-stamped and migrated), `.env` (secrets and env-shaped settings),
   `auth.json` (OAuth tokens + credential pool), an optional administrator overlay directory, and
   optional external secret managers.
4. **Own the install lifecycle.** `update` (git/release-channel based, transactional, restarts
   running gateways), `uninstall`, `backup`/`import`, `doctor`, logs, version, completion.

```
argv ──► bootstrap (utf-8 stdio, import hardening)
        └► profile pre-parse (-p / active_profile / INDUS_HOME) ─► sets INDUS_HOME, strips -p
            └► load <home>/.env (+ project .env fallback, external secrets, managed env)
                └► early config bridges (redact flag, force_ipv4) ─► logging setup
                    └► fast paths (version, serve, plain chat) ──► or full parser build
                        └► parse (subcommand-routing workaround) ─► --yolo → env
                            └► plugin discovery (only for agent-running commands)
                                └► -z oneshot? ─► chat (no subcommand) ─► handler(args) → exit code
```

What the user sees: a single `indus` command; `indus` alone opens a chat; `indus setup` on first
run; `indus model` to add providers; `indus config set k v`; `indus -p work …` to act on another
profile; `indus update` to move to the next release. What the system guarantees: secrets never
land in `config.yaml` through `config set`; config writes are atomic, comment-preserving, refuse
to run on an unreadable file, and never persist defaults; credential files are 0600 under 0700
dirs; a broken edit to `config.yaml` falls back to the last good copy instead of silently losing
user rules.

## 2. Reference names (rename in rebuild)

| Kind | Names (reference only; they become banned identifiers) |
|---|---|
| Binary / CLI and flags | `hermes` (main CLI), `hermes-agent` (legacy single-query runner), `hermes-acp` (ACP entry), `hermes-canary`, `hermes-<short-sha>` (bundle CLIs); `hermes` launcher script at repo root; `HermesArgumentParser`; wrapper alias body `hermes -p <profile>`; product strings "Hermes Agent", "Hermes Setup", "Hermes Doctor", "Hermes Console" |
| Env vars | Every `HERMES_*` var. Rebuild maps `HERMES_<X>` → `INDUS_<X>` one-to-one (e.g. `HERMES_HOME`→`INDUS_HOME`, `HERMES_YOLO_MODE`→`INDUS_YOLO_MODE`, `HERMES_INFERENCE_MODEL`, `HERMES_INFERENCE_PROVIDER`, `HERMES_TUI`, `HERMES_ACCEPT_HOOKS`, `HERMES_IGNORE_USER_CONFIG`, `HERMES_IGNORE_RULES`, `HERMES_SAFE_MODE`, `HERMES_REDACT_SECRETS`, `HERMES_MANAGED`, `HERMES_MANAGED_DIR`, `HERMES_DATA_DIR_SUFFIX`, `HERMES_UID`/`HERMES_GID`, `HERMES_HOME_MODE`, `HERMES_SESSION_SOURCE`, `HERMES_INTERACTIVE`, `HERMES_CUSTOM_<SLUG>_API_KEY`, `HERMES_PORTAL_BASE_URL`, `HERMES_NOUS_MIN_KEY_TTL_SECONDS`, `HERMES_NOUS_TIMEOUT_SECONDS`, `HERMES_DASHBOARD_*`, `HERMES_INSTALL_CHANNEL`, `HERMES_INSTALL_DIR`, `HERMES_INSTALL_VERBOSE`, `HERMES_REPO_URL`, `HERMES_RUNTIME_DIR`, `HERMES_NIX_BUILD`, `HERMES_KANBAN_TASK`, `HERMES_GATEWAY_SESSION`, `HERMES_CRON_SESSION`, `HERMES_SESSION_PLATFORM`, `HERMES_AGENT` …). Also `NOUS_API_KEY`, `NOUS_INFERENCE_BASE_URL` → rebuild `INDUS_PORTAL_API_KEY`, `INDUS_PORTAL_INFERENCE_BASE_URL`. `AI_AGENT=hermes-agent` value → `indus-agent`. |
| Files and folders | `~/.hermes` (POSIX home), `%LOCALAPPDATA%\hermes` (Windows home), `/etc/hermes` (managed overlay dir), `hermes-agent/` (install dir under home), `hermes-backup-<ts>.zip`, `.hermes/environment.json` and `.hermes/plugins/` (project-local), `.hermes.md` / `HERMES.md` (project context file), `refs/hermes/...` git refs (checkpoints/worktree archives), `hermes_cli/`, `hermes_constants.py`, `hermes_bootstrap.py`, `hermes_logging.py`, `hermes_state*.py`, `setup-hermes.sh`/`.ps1`, `Hermes.app`, `Hermes-Setup` |
| HTTP routes / URL schemes | `hermes://` URL scheme; docs host `hermes-agent.nousresearch.com`; `portal.nousresearch.com`, `inference-api.nousresearch.com/v1`, `welcome-api.nousresearch.com/v1`, `telemetry.nousresearch.com`; OAuth client id `hermes-cli`; user agent `hermes-cli/<ver>`; image `nousresearch/hermes-sandbox:desktop` |
| RPC methods / events / commands | Provider ids `nous` and `nous-api` (first-party portal provider; rebuild: `portal`, `portal-api`); `hermes honcho` style plugin command names; skin name prefixes; "Hermes managed block" marker in a codex `config.toml` |
| Settings and storage keys | Config section `nous:` (`keepalive_interval_seconds`, `anthropic_wire`, `guest`) → rebuild `portal:`; `cron.chronos.portal_url`; `monitoring.gateway_health_export.resource_attributes service.name=hermes-gateway`; `wake_word.phrase "hey hermes"` and model `hey_hermes`; `moa` preset defaults; `HERMES_HOME` markers |
| Other (scopes, error tags, IPC channels) | Nous OAuth scopes `inference:invoke`, `billing:manage` (portal-specific; rebuild names its own); certificate name "Hermes Local Signing"; process titles |

Path aliases used in §4 and §8 (so reference names stay in this section): `C/` = `hermes_cli/`;
`C/sub/` = `hermes_cli/subcommands/`; `consts.py` = `hermes_constants.py`; `boot.py` =
`hermes_bootstrap.py`; `logsetup.py` = `hermes_logging.py`; `setup-X.sh/.ps1` =
`setup-hermes.sh/.ps1`; `C/auth_portal.py` = `hermes_cli/auth_nous.py`; `C/portal_*.py` =
`hermes_cli/nous_*.py`.

Third-party protocol names used by this area (keep exact, check against vendor docs):
OAuth 2.0 Device Authorization Grant (RFC 8628, grant type
`urn:ietf:params:oauth:grant-type:device_code`, errors `authorization_pending`, `slow_down`),
PKCE (RFC 7636, S256), OAuth refresh-token grant, OpenID Connect discovery
(`/.well-known/openid-configuration`), OpenAI-compatible `/v1/models`, dotenv file format, YAML
1.1 via a round-trip writer, POSIX flock / Windows `msvcrt` byte locks, SQLite online backup API,
zip archives, `tar.gz`, OSV.dev vulnerability API, ACP (Agent Client Protocol), MCP, OTLP,
systemd user/system units, launchd LaunchAgents, Windows Scheduled Tasks, MSIX / App Installer,
Bitwarden Secrets Manager CLI (`bws`), 1Password CLI (`op`, `op://` refs), `uv`, Nix/uv2nix.
Provider endpoints (third-party, keep exact): see the registry table in §3.16.

## 3. Behaviour

### 3.1 Process entry and startup sequence

- **Entry points** [obs]: three console scripts — the main CLI (`<pkg>.main:main`), a legacy
  single-query runner with a minimal flag set (input `--query`; endpoint `--model` and
  `--base-url`; limit `--max-turns`; tools `--enabled-toolsets`/`--disabled-toolsets` and
  `--list-tools`; output `--save-trajectories`; plus `--version`; without a query it prints help
  and exits), and an ACP server entry. A repo-root launcher script just
  imports and calls the main function.
- **Bootstrap module** imported first by every entry point [obs]: on Windows force UTF-8 stdio and
  ANSI console (POSIX locale is respected, not overridden); harden `sys.path` so a project-local
  file cannot shadow app modules; dual-stack "happy eyeballs" connects with a 0.25 s stagger.
  Stdlib-only.
- **Ordered startup** (main module import + `main()`) [obs]:
  1. Profile pre-parse (§3.10.2) — runs at module import, before any other app import. Sets
     `INDUS_HOME`, removes `-p NAME` from `sys.argv`.
  2. Re-point temp-dir env vars at the home's scratch dir (a user-set `TMPDIR` is left alone).
  3. A private package-manager subcommand (`pm`) is short-circuited here (rebuild: optional).
  4. Windows only: re-stage launcher copies in a managed bin dir outside the checkout.
  5. Load `<home>/.env` then the install dir's `.env` (dev fallback) — §3.14.1.
  6. One parse of `config.yaml` (effective loader, includes admin overlay) to bridge
     `security.redact_secrets` → `INDUS_REDACT_SECRETS` (only if not already in env) and remember
     `network.force_ipv4`. MUST happen before the redaction module is imported because it
     snapshots the flag once.
  7. File logging: mode "gui" when the first positional is `dashboard|serve|gui|desktop`, else
     "cli".
  8. Apply IPv4 preference before any HTTP client exists.
  9. `main()`: set process title; warn if running as PID 1 without a supervisor; export
     `AI_AGENT=indus-agent` (only if unset) and `INDUS_AGENT=true`; Windows UTF-8 again; install
     the OS trust store for TLS; sweep stale Windows exe quarantine files; sweep stale bytecode if
     the checkout changed; recover owed post-update work; first-boot bootstrap (skipped for
     `update`); a passive runtime-environment check that prints one warning line.
  10. **Fast paths** tried in order before building the full parser: two Termux-specific, then
      `serve`, then plain chat. Each returns early if it handled the invocation.
  11. Build full parser (§3.2); if a container-exec marker says this host routes everything into
      a managed container, `exec` there with the raw argv (before argparse so `--help` forwards).
  12. Parse with the routing workaround (§3.2.3). `--version` → print version block, return.
  13. `--yolo` → set `INDUS_YOLO_MODE=1` NOW (approval module freezes it at import).
  14. Plugin discovery + shell-hook registration, only for commands that actually run an agent
      (introspection commands like `hooks list`, `cron list`, `gateway status` skip it and never
      trigger consent prompts).
  15. `-z` set → run one-shot and hard-exit (§3.5).
  16. No subcommand → chat with top-level flags promoted (§3.4).
  17. Else call the handler; an integer non-zero return becomes the process exit code; `None`
      means success; missing handler prints help.
- **Errors**: a failure in the profile layer itself prints a warning and continues with the
  default profile ("a profile bug must never prevent startup") [obs]; an unknown/missing named
  profile exits 1 with a message; a sticky profile that no longer exists exits 1 with the hint to
  switch back to default, except for a small set of recovery commands that run in default.

### 3.2 Top-level parser

#### 3.2.1 Global flags [obs]

| Flag | Value | Meaning |
|---|---|---|
| `-V`, `--version` | — | Print version block, exit |
| `-p`, `--profile NAME` | name | Consumed before argparse (not on parser); works in any position |
| `-z`, `--oneshot PROMPT` | text | One-shot: only final text on stdout (§3.5) |
| `--usage-file PATH` | path | One-shot only: JSON usage report |
| `-m`, `--model` | id | Per-run model override (pairs with `-z`/`--tui`; env `INDUS_INFERENCE_MODEL`) |
| `--provider` | id | Per-run provider override |
| `--reasoning LEVEL` | none\|minimal\|low\|medium\|high\|xhigh\|max\|ultra | Per-run effort |
| `-t`, `--toolsets` | csv | Per-run toolsets |
| `-r`, `--resume SESSION` | id/title/`latest` | Resume |
| `--no-restore-cwd` | — | Do not chdir into the resumed session's recorded cwd |
| `--in DIR` | dir | chdir first; scopes `latest`/`-c`; pins session there |
| `-c`, `--continue [NAME]` | optional | Resume by title or most recent |
| `-w`, `--worktree` | — | Isolated git worktree |
| `--accept-hooks` | — | Auto-approve unseen shell hooks (env `INDUS_ACCEPT_HOOKS=1`, config `hooks_auto_accept`) |
| `-s`, `--skills` | repeatable/csv | Preload skills |
| `--yolo` | — | Bypass dangerous-command approvals |
| `--pass-session-id` | — | Put session id in system prompt |
| `--checkpoints` | — | Enable filesystem checkpoints |
| `--ignore-user-config` | — | Use built-in defaults; `.env` still loads |
| `--ignore-rules` | — | Skip AGENTS.md/SOUL.md/.cursorrules/memory/preloaded skills |
| `--safe-mode` | — | Implies both above plus no plugins/MCP/hooks |
| `--tui` / `--cli` | — | Force TUI / classic REPL |
| `--native` (`--tui-native`) | — | TUI native scrollback, no mouse |
| `--dev` | — | TUI from TypeScript sources |

- Flags marked "inherited" (model, provider, reasoning, accept-hooks, skills, yolo,
  pass-session-id, checkpoints, ignore-*, safe-mode, tui/native/cli/dev, and `-p`) carry over when
  the process relaunches itself [obs].
- The set of value-taking top-level flags is **derived from the live parser** (with a hand-kept
  fallback) so argv scanners that find "the first positional" never mistake a flag value for a
  subcommand (a past bug: `--reasoning high chat` read `high` as the command) [obs].
- The `chat` subparser repeats most of these flags with a "suppress" default so a value given
  before the subcommand is not clobbered by the subparser's default (MUST; test-guarded) [obs].

#### 3.2.2 Unknown-command UX [obs]
An unknown first positional prints exactly three short lines instead of argparse's full choice
dump: "'X' is not a `indus` command.", then one of: an install hint (when X names a known
catalog plugin that registers that command once installed), a "moved" hint (command that left
core), or "Did you mean: a, b, c?" (up to 3 close matches, cutoff 0.6); then "Run `indus --help`
to see all commands." Exit 2. The same applies inside nested groups with the nested prog name.

#### 3.2.3 Subcommand routing workaround [obs]
Because `-c` takes an optional value, older Python argparse fails to route `indus -c model …`.
Approach: if any non-flag token equals a known subcommand, try parsing with subparsers required
(stdout/stderr captured); on failure fall back to a non-required parse (so `-c model` treats
`model` as a session name). Help output from the speculative parse is replayed when it exits 0.
A pre-pass also coalesces multi-word session names after `-c`.

#### 3.2.4 Help ordering
Registration order is the `--help` order and must stay stable [obs]. Order: chat, model, moa,
fallback, worktree, browser, secrets, egress, migrate, codex-runtime, gateway (+proxy), lsp,
setup, whatsapp, whatsapp-cloud, slack, send, login (hidden), logout, auth, status, pause/resume,
cron, webhook, peer, portal, kanban, project, hooks, doctor, verify, security, approvals, dump,
debug, backup, checkpoints, import, import-agent, config, skin, console, pairing, skills, bundles,
plugins, plugin-registered commands, curator, pets, journey, memory, tools, computer-use, mcp,
connectors, sessions, insights, usage, monitoring, claw, vault, update, uninstall, acp, profile,
completion, dashboard (+serve), desktop (alias gui), logs, prompt-size.

### 3.3 Command catalogue

In-scope groups are detailed in later subsections; out-of-scope groups get one line here.

| Command | Subcommands / key flags | Spec |
|---|---|---|
| `chat` | see §3.4 | here |
| `model` | `--refresh`, portal-login flags (`--portal-url`, `--inference-url`, `--client-id`, `--scope`, `--no-browser`, `--timeout` default 15, `--ca-bundle`, `--insecure`) | §3.17 |
| `fallback` | `list`(`ls`, default) / `add` / `remove`(`rm`) / `clear` | §3.17.4 |
| `moa` | `list`(`ls`) / `configure`(`config`) `[name]` / `delete`(`rm`) `name` | §3.17.5 |
| `setup` | `[model\|tts\|terminal\|gateway\|tools\|telemetry\|agent]`, `--non-interactive`, `--reset`, `--reconfigure`, `--quick`, `--portal` | §3.18 |
| `auth` | `add PROVIDER` (`--type oauth\|api-key\|api_key`, `--label`, `--priority N`, `--api-key`, `--portal-url`, `--inference-url`, `--client-id`, `--scope`, `--no-browser`, `--browser`, `--timeout`, `--insecure`, `--ca-bundle`), `list [provider]`, `remove P T`, `reset P [T]`, `priority P T N`, `refresh P [T]`, `status P`, `logout P`, `upgrade` (`--no-browser`, `--timeout`); bare = interactive | §3.16 |
| `login` | hidden, deprecated; any flags accepted; prints redirect | §3.16.8 |
| `logout` | `--provider portal\|openai-codex\|xai-oauth` (default active) | §3.16.8 |
| `config` | `show`, `edit`, `get [key] [--json] [--raw]`, `set [key] [value] [--force]`, `unset [key]`, `path`, `env-path`, `check`, `migrate` | §3.13 |
| `tools` | `--summary`; `list [--platform cli]`, `enable NAME… [--platform]`, `disable NAME…`, `post-setup KEY`; bare = interactive | §3.19 |
| `sessions` | list/export/delete/prune/archive/optimize/… | §3.20 (CLI surface) + 03 |
| `approvals` | `suggest` (`--apply N[,M]`, `--json`, `--days 90`, `--min-count 2`, `--limit 20`, `--db`), `test [--env-type local] [--json] -- CMD…` | §3.21 |
| `checkpoints` | `status`/`list` (`--limit 20`), `prune` (`--retention-days 7`, `--max-size-mb 500`, `--keep-orphans`, `-f`), `clear [-f]`, `clear-legacy [-f]` | §3.22 |
| `backup` | `-o PATH`, `-q/--quick`, `-l/--label`, `-k/--keep N` (default 3, 0 = keep all) | §3.23 |
| `import` | `ZIPFILE`, `-f/--force` | §3.23 |
| `profile` | list/use/create/delete/describe/show/alias/rename/purge-identity/migrate-identity/export/import/install/update/info | §3.10 |
| `doctor` | `--fix`, `--live`, `--ack ID` | §3.24.1 |
| `dump` | `--show-keys` | §3.24.2 |
| `debug` | `share` (`--lines 200`, `--expire 1`, `--local`, `-y`, `--no-redact`, `--private`), `delete URL…` | §3.24.3 |
| `status` | `--full`(`--all`), `--deep` | §3.24.4 |
| `logs` | `[agent\|errors\|gateway\|gui\|desktop\|mcp\|update\|handoff\|list]`, `-n 50`, `-f`, `--level`, `--session`, `--since`, `--component` | §3.24.5 |
| `usage` | `--provider`, `--json` | §3.24.6 |
| `prompt-size` | `--platform cli`, `--json` | §3.24.7 |
| `security audit` | `--json`, `--fail-on low\|moderate\|high\|critical` (default critical), `--skip-venv`, `--skip-plugins`, `--skip-mcp` | §3.24.8 |
| `insights` | `--days 30`, `--source` | 03 (reads DB) |
| `update` | `--check`, `--plan`, `--gateway`, `--list-venv-holders`, `--no-backup`, `--backup`, `-y`, `--keep-stash`, `--branch`, `--switch-branch`, `--force`, `--force-venv`, `--set-channel`, `--channel`, `--install-id`, `--no-gateway-restart` | §3.25 |
| `uninstall` | `--full` \| `--gui` \| `--data` (exclusive), `--gui-summary`, `-y`, `--dry-run` | §3.26 |
| `completion` | `[bash\|zsh\|fish]` (default bash) | §3.28 |
| `console` | — (curated safe command REPL) | §3.28 |
| `pause` / `resume` | `pause --reason` | §3.28 |
| `hooks` | `list`(`ls`), `test EVENT [--for-tool] [--payload-file]`, `revoke`(`remove`,`rm`) `CMD`, `doctor` | §3.28 |
| `import-agent` | `[claude-code\|codex]`, `--source`, `--dry-run`, `--overwrite`, `-y`, `--sync` | §3.28 |
| `claw` | `migrate` (`--source`, `--dry-run`, `--preset user-data\|full`, `--overwrite`, `--migrate-secrets`, `--no-backup`, `--workspace-target`, `--skill-conflict skip\|overwrite\|rename`, `-y`), `cleanup`(`clean`) | §3.28 |
| `secrets` | `bitwarden`(`bw`) / `onepassword`(`op`,`1password`) → `setup`, `status`, `sync`, `disable`, `install` | §3.14.4 |
| `vault` | `add [--kind login\|payment\|address]`, `list`, `rm HANDLE`, `sources [--disable\|--enable NAME]` | §3.14.5 |
| `migrate` | `xai [--apply] [--no-backup]`, `relay [--all-profiles] [--no-validate]` | §3.13.8 |
| `codex-runtime` | `migrate [--dry-run] [--json]` | 01 |
| `skin` | `list`, `use NAME`, `set KEY HEX [--skin]` | 05 (display) |
| `gateway`, `proxy`, `pairing`, `send`, `peer`, `webhook`, `slack`, `whatsapp(-cloud)`, `monitoring`, `egress`, `portal`, `connectors` | — | 06 |
| `cron`, `kanban`, `project`, `skills`, `bundles`, `curator`, `journey`, `memory`, `pets` | — | 07 |
| `dashboard`, `serve`, `desktop`/`gui`, `computer-use`, `browser`, `worktree`, `verify`, `lsp`, `acp`, `mcp`, `plugins` | — | 02/06/08 |

### 3.4 `chat` (and bare `indus`)

#### 3.4.1 Chat-only flags [obs]
`-q/--query TEXT` xor `--query-file PATH` (`-` = stdin; nothing shell-interpreted);
`--oneshot` (answer and exit); `--image PATH`; `-v/--verbose`; `-Q/--quiet`;
`--format text|stream-json`; `--create-if-missing` (with `-c NAME`); `--max-turns N` (default 500
or `agent.max_turns`); `--run-budget SECONDS` (wall-clock per run; at 80% elapsed a one-time
wrap-up notice; provider stale timeouts capped to remaining budget); `--base-url URL` +
`--api-key KEY` (launch-only, for `--provider custom`, key never persisted); `--source TAG`
(default `cli`; one-shot defaults to `oneshot`; `tool` hides from user lists). Plus all globals.

#### 3.4.2 Interface choice [obs]
Order: `--cli` → classic; `--tui`/`--native` → TUI; stdin or stdout not a TTY → classic (MUST:
ambient preferences never hijack a piped run; a TUI with no TTY exits 0 and breaks workers);
`INDUS_TUI=1` → TUI; `display.interface: tui` → TUI; default classic. The TUI owns its own
first-run "setup required" state; the classic path runs the first-run guard (§3.4.4).

#### 3.4.3 Session selection [obs]
- `--in DIR`: expand `~`, translate MSYS-style paths on Windows, must exist (else print error,
  exit 1), chdir, refresh `TERMINAL_CWD` only if it was already set, and imply
  `--no-restore-cwd`.
- `--resume @claude` / `@codex`: open a picker over the other agent's local sessions, import the
  chosen one, print the new id and resume it.
- `-c NAME`: resolve by title or id; miss → stderr "No session found matching 'NAME'." plus a
  hint, exit 1; with `--create-if-missing` create a session titled NAME and resume it.
- bare `-c` / `--resume latest`: this terminal's breadcrumb session if valid (parallel terminals
  resume separate conversations; disabled by `session.terminal_continue:
  false`), else the most recent session for the workspace (git root, else cwd). None → stderr
  message, exit 1. `--create-if-missing` without a name prints a warning (no-op).
- The literal `latest` beats a session titled "latest" (that one stays reachable by id or
  `-c latest`).

#### 3.4.4 Pre-launch guards [obs]
1. stream-json or a bypass launch (`--safe-mode`, `--ignore-user-config`, or the matching env vars)
   goes straight to the gateway-client launcher.
2. Non-interactive guard: `--ignore-user-config` etc. applied.
3. Retired-model warning.
4. **First-run guard** (classic only): if no provider is usable (config model differs from the
   default AND/OR any provider key env, `OPENAI_BASE_URL` alone counts for local servers, any
   logged-in OAuth provider, …) print a short "not configured yet" block with "Run: indus setup".
   Non-TTY: print non-interactive guidance and exit 1. TTY: ask "Run setup now? [Y/n]" (EOF/Ctrl-C
   = n); yes runs the setup wizard, no exits 1.
5. Background prefetch (model catalogs etc.).
6. Env bridges: `--yolo`→`INDUS_YOLO_MODE=1`; `--ignore-rules`→`INDUS_IGNORE_RULES=1`;
   `--source`→`INDUS_SESSION_SOURCE` + `INDUS_SESSION_SOURCE_EXPLICIT=1`.
7. Telemetry-consent offer (only once a provider exists).
8. **Expensive/surprising model guard**: when `-m` or `--provider` is passed explicitly, compute
   warnings (paid aggregator cost, data-training tier). TTY → confirm. Non-TTY → print to stderr
   and refuse, except data-policy warnings are allowed when
   `security.allow_data_training_tiers_noninteractive: true`. Independent of `--yolo`.

#### 3.4.5 Query seeding vs one-shot [obs]
`-q` on a real TTY (both stdin and stdout) without `--oneshot`/`-Q` **seeds** an interactive
session: the text is submitted literally as the first turn (no slash-command or `!` interpretation) and the REPL stays open. Any of `--oneshot`, `-Q` or non-TTY stdio switches to
answer-then-exit. `--format stream-json` requires a query, implies quiet, refuses `--tui`.

#### 3.4.6 Classic chat is a gateway client [obs]
In the reference, `indus chat` does not build an agent in-process. Instead it locates the
profile's gateway, launching it if needed (`gateway ensure`), then opens or resumes the session on that server, and talks to
it through a loopback WebSocket connection. A local adapter replays gateway events into the same callbacks the
REPL used for an in-process agent and returns a result dict of the same shape; the CLI never runs
tools or writes the session DB.
- **Frozen at creation**: once a session exists, none of these can change —
  - where and with what: working directory, model, provider, base URL and API key, reasoning;
  - what the agent may use: toolsets, skills, the turn cap (`--max-turns`);
  - safety switches: yolo, accept-hooks, checkpoints, ignore-rules, safe-mode, ignore-user-config,
    pass-session-id.

  A resume may repeat the same values; a different value is refused.
- **Refused flags** (exit 2, message lists where each capability lives now; nothing falls back
  to a local agent): `--image`, `--worktree/-w`, `--no-restore-cwd`, `--run-budget`, `--verbose`,
  `--compact`, `--create-if-missing` without `-c NAME`.
- Bypass launches read no profile default model: `--model` is mandatory (example printed).
- Only these slash commands work in this mode — session control: `/model <m> [--provider p]`,
  `/branch [title]`, `/compress …`, `/yolo [on|off]`, `/stop`; pending prompts:
  `/approve <id> <choice>`, `/answer <id> <text>`, `/discard <admission_id>`; plus `/help` and
  `/quit`. Anything else prints "unsupported; use /help".
- Closing the terminal detaches; the gateway finishes accepted work. If a finite query is queued
  behind a turn that lost its owner, exit code 3.
- Interrupt settle wait: 10 s.
This is a large architectural dependency on spec 06; see trap R04.1.

#### 3.4.7 stream-json output [obs]
One JSON object per stdout line; diagnostics and the `session_id:` line go to stderr. Every event
carries a `timestamp` in Unix milliseconds. Event types and their fields:

| type | fields |
|---|---|
| `system` | `subtype:"init"`, `model`, `session_id` |
| `text` | `delta` |
| `tool_use` | `name`, optional `input` |
| `tool_result` | `name`, `output` (at most 5000 chars), `is_error`, `duration_ms` |
| `result` | `exit_code`, `session_id`, `text`, `duration_ms`, optional `error`, and `tokens` with `input`/`output`/`total`/`cache_read`/`cache_write` |

The final record is always a `result` (Ctrl-C included, with exit_code 130); the process exits
with that record's exit_code.

#### 3.4.8 Exit codes for finite chat runs [obs]
0 completed; 1 failed / partial / iteration budget hit / never ran (credentials, init); 130
interrupted; 75 (EX_TEMPFAIL) only for a dispatcher-spawned board worker whose failure was a
provider rate-limit/overload/5xx/timeout/billing wall; 3 lost-owner queue case (gateway client).

### 3.5 One-shot `-z`

- One prompt goes in and only the final reply text comes out; both streams are otherwise
  silent (no banner, spinner, tool previews or session line) [obs]. Approvals auto-bypassed (sets yolo env). Tools: explicit
  `--toolsets` (`all`/`*` allowed) else the user's `cli` platform toolsets; rules, memory,
  AGENTS.md, preloaded skills load as normal. Model/provider: both optional; only `--model` →
  provider auto-detected; only `--provider` → error (ambiguous) [obs].
- Exit codes (intentionally different from `chat -q`) [obs]: 0 completed; 2 failed/partial/
  budget/`completed:false` (even if text printed) and usage errors; 130 interrupted; 1 completed
  but produced no text at all.
- `--usage-file PATH`: JSON written even on failure: `estimated_cost_usd`, `cost_status`,
  `cost_source`, token counters (input/output/cache_read/cache_write/reasoning/total),
  `api_calls`, `model`, `provider`, `session_id`, `service_tier`, `completed`, `failed`,
  `partial`, `interrupted`, `turn_exit_reason`; an `auxiliary` block (same counters + `by_task`
  map) and `total_including_auxiliary` (cost, tokens, api_calls). A failed usage write never
  changes the run's exit code [obs].
- Shutdown: run a fixed list of process-global cleanups (terminal environments, async
  delegation, browser sessions, MCP servers, aux clients, local model runtime, metrics), flush
  streams, shut down logging, then hard-exit with `os._exit(rc)` to dodge native finalizer aborts
  that once changed the exit code after output was printed [obs].

### 3.6 Classic REPL surface (display details belong to 05)

- **Layout** [obs]: banner (model, terminal backend, cwd, tool and skill counts), scrollback,
  persistent status bar above a fixed input line, optional "Previous Conversation" recap on
  resume (`display.resume_display`, `resume_exchanges` 10, `resume_max_user_chars` 300,
  `resume_max_assistant_chars` 200, `resume_max_assistant_lines` 3).
- **Status bar** [obs]: model (truncated beyond 26 chars) │ context used/max (`~` prefix =
  includes a local estimate) │ fill bar │ cost (`n/a` if unknown) │ compression count │ active
  background tasks │ duration │ title badge │ yolo warning. Width tiers: ≥76 cols full, 52–75
  compact, <52 minimal. Context colours: <50% green, 50–80 yellow, 80–95 orange, ≥95 red.
- **Keys** [obs]: Enter send; Alt+Enter / Ctrl+J / Shift+Enter (when the terminal reports it via
  kitty protocol or modifyOtherKeys) newline; trailing `\` continues a line; Alt+V paste image;
  Ctrl+V paste text (+opportunistic image); Ctrl+B voice record (configurable); Ctrl+G or
  Ctrl+X Ctrl+E edit buffer in `$EDITOR`; Ctrl+S stash draft (stack; ≥2 opens a browser panel;
  in-memory only, never on disk); Ctrl+C clears a draft first, else interrupts, double press
  within 2 s force-exits; Ctrl+T/F6 live work monitor; Ctrl+R/F7 dock toggle; Ctrl+D exit on
  empty prompt (detaches, turn keeps running at the gateway); Ctrl+Z suspend (Unix); Tab accept
  suggestion/complete slash. Multi-line paste echoes a one-line preview. `display.
  cli_multiline_shortcuts: false` restores legacy Ctrl+J-submits.
- **Busy input** (`display.busy_input_mode`: `interrupt` default \| `queue` \| `steer`; unknown →
  interrupt) [obs]; classic gateway-client chat always queues at the gateway regardless. A
  one-time hint on first busy Enter, remembered in `onboarding.seen.busy_input_prompt`.
- **Final reply markdown**: verbose fences and bold/italic wrappers stripped in the CLI only
  (`display.final_response_markdown: strip`) [obs].
- **Output history ring**: last 200 lines kept for redraw replay [obs].

### 3.7 Slash commands

#### 3.7.1 Registry [obs]
One ordered registry is the single source for CLI dispatch, autocomplete, help-by-category,
gateway known-commands, messaging menus. Entry shape:
`CommandDef { name, description, category: Session|Configuration|Tools & Skills|Info|Exit,
aliases[], args_hint, subcommands[], cli_only, gateway_only, gateway_config_gate?: dotpath,
busy_policy: dispatch|reject|interrupt_then_dispatch (default reject), busy_handler?,
execute?: executor key, argument_mode?: options|text|mixed, desktop?: null|"hidden"|reason,
desktop_subcommands?[] }`. Descriptions are localised by key with English fallback.
Commands are case-insensitive; aliases resolve through the registry so adding an alias updates
every surface.

#### 3.7.2 Dispatch precedence in the classic REPL [obs]
1. Lowercase the first word for matching only (arguments keep case); resolve alias → canonical.
2. Record a usage metric once (not on internal re-dispatch).
3. Any command other than resume/sessions disarms a pending "pick a number" resume list.
4. Built-in handler lookup: explicit dispatch table, else a method named by convention from the
   canonical name. A per-session-setting command may be applied by the gateway instead.
5. Fire an observer-only `pre_command` plugin hook (return ignored, never raises).
6. No built-in handler → user `quick_commands` → plugin commands → skill bundles → skill
   commands → unique-prefix expansion of built-ins/skills.
7. Handler returning `False` exits the REPL.
- **Quick commands** (`quick_commands.<name>` in config): `type: exec` runs `command` through a
  shell with a sanitised environment (provider keys stripped), 30 s timeout, output redacted then
  printed, "no output"/"timed out" messages; `type: alias` re-dispatches `/<target> <args>`.
- **Skill commands**: each installed skill is `/skill-name [instruction]`; it is injected as a
  user turn; the system prompt stays untouched so the prompt cache holds.

#### 3.7.3 Built-in commands (classic CLI view) [obs]
Scope column: C = CLI only, G = gateway only, blank = both.

| Command (aliases) | Args | Scope | Notes |
|---|---|---|---|
| new (reset) | [name]; `now`/`--yes`/`-y` skips confirm | | interrupt-then-dispatch |
| clear | | C | clear screen + new session |
| redraw | | C | full repaint |
| history | | C | honours timestamps |
| save | json\|md\|html [file] [redact] | | |
| retry / undo [N] / title [name] | | | |
| prompt (compose) | [initial text] | C | compose in `$EDITOR` |
| handoff | platform | C | move session to a messaging platform |
| branch (fork) | [--here] [name] | | |
| worktree | new [name]\|list\|prune [--dry-run] | C | |
| compress (compact) | here [N] \| focus topic \| --preview\|--dry-run | | |
| rollback | [N] [--all], diff N, N file | | checkpoints |
| snapshot (snap) | create\|restore id\|prune | C | state snapshots |
| export / import | profile archive | C | profile tar.gz |
| stop | | | kill bg processes + interrupt |
| bg | prompt | | separate background session |
| btw | question | | side question, no interrupt |
| agents (tasks) | | | |
| journey (learning, memory-graph) | list\|delete\|edit | C | |
| queue (q) | prompt\|list\|edit N\|rm N\|move A B\|clear | | |
| steer (s) | prompt | | |
| goal, subgoal, heartbeat (hb), loop (proactive), plan, refine, review, moa | various | | agent features (01/07) |
| status, context (ctx) [all], egress | | | |
| whoami, profile, resume [name], sessions | | | |
| config | | C | show config |
| model | [model] [--provider] [--reasoning] [--global\|--session] [--refresh] | | reject while busy; switches only configured providers |
| codex-runtime | auto\|codex_app_server | | |
| personality | [name\|none] | | session overlay |
| statusbar (sb), battery, timestamps (ts), focus, indicator, skin | on\|off\|status etc. | C | |
| diff | staged\|all\|session [--stat] [path…] | | |
| verbose | | C (gateway via `display.tool_progress_command`) | cycles off→new→all→verbose |
| footer | on\|off\|status | | |
| yolo | | | toggle |
| approvals | manual\|smart\|off | | persistent (§3.21) |
| reasoning | level\|show\|hide\|on\|off\|full\|clamp [--global] | | levels none…ultra |
| fast | normal\|fast\|auto\|cold\|ultrafast\|status [--global] | | |
| voice, wake, busy, autoprune | on/off/status variants | | |
| tools | list\|disable\|enable [name…] | C | session-scoped; triggers session reset |
| toolsets, skills, memory, bundles, pet, hatch (generate-pet), learn, init, cron, suggestions (suggest), blueprint (bp), curator, kanban | | mostly C | 02/07 |
| reload | | C | reload `.env` into the session |
| reload-mcp (reload_mcp), reload-skills (reload_skills) | | | |
| browser | connect\|disconnect\|status\|use | C | |
| plugins | | C | |
| help | [skills\|filter] | | |
| palette | | C | |
| usage | [reset [--force]] | | |
| subscription (upgrade), login, topup | | | portal account |
| insights [days], platforms (gateway) | | | |
| copy [N], paste, image path | | C | clipboard / attachments |
| update, version (v), debug [portal\|local] | | | |
| quit (exit) | [--delete] | C | |
| start, topic, pause, approve, deny, sethome (set-home), commands, restart, platform | | G | messaging only |

### 3.8 Bang shell (`!command`) [obs]
- Only a leading `!` (after whitespace trim) counts; "fix the bug!" is a normal prompt. `!!`
  passes a literal `!` to the shell. Bare `!` prints a one-line usage reminder.
- Enabled only for interactive local CLI sessions: disabled when the process is a gateway session,
  cron session, or has a session-platform marker (no human at the keyboard there).
- Runs on the host where the CLI runs (never in a remote/sandbox terminal backend), in the
  session's recorded cwd, else the configured terminal cwd; a missing dir falls back to process
  cwd.
- Approval: goes through the same guard chain as the agent's terminal tool, evaluated as a local
  command without host-access escalation (hardline blocklist, user deny rules, dangerous-pattern
  prompt). If the guard module cannot load, it allows (treated as a broken install).
- Environment: the sanitised subprocess env (provider secrets removed); if the sanitiser is
  unavailable the launch fails rather than leaking the full env.
- Streams merged stdout/stderr line by line; timeout 120 s → kill, "timed out", code 124;
  Ctrl+C → kill, "interrupted", 130 (the session survives); launch failure → 127. Non-zero exit
  is shown after output. Nothing enters conversation history; zero tokens.

### 3.9 Home directory and file layout

- **Resolution** [obs]: context-local override (used by multi-profile servers) → `INDUS_HOME`
  env → platform default. Default: POSIX `~/.indus`, Windows `%LOCALAPPDATA%\indus` (fallback
  `~/AppData/Local`). `INDUS_DATA_DIR_SUFFIX` is appended literally to the default dir name
  (channel/test bundles). `~` and `$VARS` in `INDUS_HOME` are expanded. A one-time warning fires
  when code asks for the home without `INDUS_HOME` set in a context that should have a profile.
- **Root vs profile**: if `INDUS_HOME` points at `<root>/profiles/<name>`, the root is the
  grandparent; otherwise root = the home itself (so Docker's `/opt/data` is its own root) [obs].
- **Skeleton** created on every config load (memoised per path, ~14 syscalls avoided): `cron`,
  `sessions`, `logs`, `logs/curator`, `memories`, `pairing`, `hooks`, `image_cache`,
  `audio_cache`, `skills`. A named profile must already exist (tombstone and existence check
  before the memo) — MUST NOT mkdir a deleted/missing named profile [obs].
- **Permissions** [obs]: dirs 0700 (override `INDUS_HOME_MODE`, e.g. 0701), files 0600; optional
  chown from `INDUS_UID`/`INDUS_GID`; skipped in managed installs (they use 0640) and, for files,
  inside containers.
- **SOUL.md** seeded with a default identity on first run; an existing customised file is never
  overwritten; a legacy comment-only scaffold is upgraded in place; a broken symlink is replaced
  via temp file + rename, never written through [obs].
- **Notable home files** [obs]: `config.yaml`, `.env`, `.op.env` (1Password bootstrap token),
  `auth.json` (+ `auth.json.corrupt` on parse failure, `.lock`), `.env.lock`, `SOUL.md`,
  `memories/{MEMORY,USER}.md`, `state.db`, `logs/*.log`, `backups/config/`, `backups/`
  (pre-update/pre-migration zips), `state-snapshots/`, `checkpoints/store/`, `cache/`,
  `provider_models_cache.json`, `active_profile` (root only), `profiles/<name>/`,
  `profiles/.deleted/` (tombstones), `.managed` marker, `shell-hooks-allowlist.json`,
  `import-sync.json`, `ESTOP` sentinel, `skins/*.yaml`, `session-exports/`.
- **Install layout** [obs]: POSIX source install code at `<home>/<install-dir>/`, launcher at
  `~/.local/bin/indus`; Windows code at `%LOCALAPPDATA%\indus\<install-dir>\`, launchers in
  `%LOCALAPPDATA%\indus\bin\`; Docker code `/opt/<app>/`, data mounted at `/opt/data/`.

### 3.10 Profiles

#### 3.10.1 Model [obs]
A profile is an independent home under `<root>/profiles/<id>/` (the root itself is the profile
named `default`). No live inheritance between profiles. Per-profile dirs created on create:
`memories, sessions, skills, skins, logs, plans, workspace, cron, home`.
- **Id rule**: regex `^[a-z0-9][a-z0-9_-]{0,63}$` (lowercase, digits, `-`, `_`, ≤64, starts
  alnum). Input is normalised first: trimmed, lowercased, `Default` (any case) → `default`;
  non-string ids rejected (a numeric id once created `profiles/0/`). Reserved: `indus` (the
  binary name), `default`, `test`, `tmp`, `root`, `sudo`. Names colliding with CLI subcommands
  (`chat, model, gateway, setup, …, profile, plugins, acp`) are refused as aliases. Invalid input
  gets a suggested slug (spaces → `-`, else `my-work`).
- **Live profile** = directory holding any one of six identity markers (`state.db`, `auth.json`,
  `profile.yaml`, `config.yaml`, `SOUL.md` or `.env`; a dangling symlinked marker counts) and not tombstoned.
  Marker-less dirs are never listed, served or resolvable via `-p`.

#### 3.10.2 Selection (pre-argparse) [obs]
1. Scan argv for `-p NAME`/`--profile NAME`/`--profile=NAME` (skipping values of value-taking
   flags and anything inside `mcp add --args …`).
2. If none and `INDUS_HOME` is set whose parent dir is named `profiles` → trust it, done.
3. If none and not under a gateway supervisor / desktop SSH backend → read `<root>/active_profile`
   (sticky default, UTF-8 with BOM tolerated); non-`default` value selects that profile.
4. Resolve to a path; set `INDUS_HOME`; strip the flag tokens from argv. Under `sudo`, a missing
   profile may resolve against the invoking user's home.
`profile use NAME` writes `active_profile`; `use default` clears it [inf].

#### 3.10.3 Subcommands [obs]
- `list`; `use NAME`; `show NAME`; `info NAME` (distribution manifest).
- `create NAME [--clone] [--clone-all] [--clone-from SRC] [--clone-channels] [--sync-imports]
  [--no-alias] [--no-skills] [--description TEXT]`. `--clone` copies `config.yaml`, `.env`,
  `SOUL.md`, `memories/MEMORY.md`, `memories/USER.md` and skills from the active (or `--clone-from`)
  profile, but leaves messaging channel credentials behind (bot tokens, allowlists, platform sections),
  since one bot token used by two profiles conflicts; `--clone-channels` opts in (refused while a
  running multi-profile gateway serves the source). `--clone-all` copies all state except per-profile history
  and channels. Clones are built in a hidden staging dir and published by one rename; symlinked
  `.env`/`config.yaml` are materialised first so the clone never writes through to the source.
  `--no-skills` writes a marker that opts out of bundled-skill sync. A model config is seeded.
  Unless `--no-alias`, a wrapper script is created.
- **Wrapper alias**: `~/.local/bin/<alias>` containing an `exec <abs path of indus> -p <profile>
  "$@"` shell script (chmod +x); on Windows `<alias>.bat` with `indus -p <profile> %*`. Alias name
  validated with the same id regex (no path traversal). `alias NAME [--remove] [--name ALIAS]`.
  Warn if the wrapper dir is not on PATH [inf].
- `delete NAME [-y]`: stops services, writes a tombstone in `profiles/.deleted/` (beside, not
  inside, so a stray mkdir/rmtree cannot erase it), purges routing identity;
  `purge-identity NAME` retries that purge (idempotent).
- `rename OLD NEW`: for `default`, sets a display name only; otherwise moves dir and re-keys
  session/routing identity; `migrate-identity OLD NEW` retries (idempotent).
- `describe [NAME] [--text T] [--auto] [--overwrite] [--all]` (description used by task routing;
  `--auto` uses an auxiliary model).
- `export NAME [-o FILE]` → `tar.gz` (default a managed `profile-exports/<name>-<ts>.tar.gz` under
  the root); credential stores excluded; `import ARCHIVE [--name N]`.
- Distributions: `install SOURCE` (git URL or local dir with a `distribution.yaml` manifest;
  `--name`, `--alias`, `--force` overwrite keeping user data, `-y`), `update NAME
  [--force-config] [-y]` (re-pull; overwrite distribution-owned files such as `SOUL.md`,
  `mcp.json`, shipped skills and cron jobs; user data, memories, sessions, auth and `.env`
  untouched; `config.yaml` preserved unless `--force-config`).

### 3.11 The config file: load, merge, save

#### 3.11.1 Three loaders (MUST pick the right one) [obs]
- **Full** (`load_config`): built-in defaults deep-merged with the user file, canonicalised,
  `${VAR}` expanded, admin overlay applied. Used by most subcommands and wizards.
- **CLI REPL loader**: CLI defaults + user YAML + env mirroring (classic REPL).
- **Effective** (no defaults): user file + admin overlay + `${VAR}` + model-key canonicalisation.
  Used by gateway runtime, cron, send, doctor, logging, time. Presence-sensitive readers need to
  know whether the user set a key at all.
A key visible to one surface and not another usually means the wrong loader or a missing default.

#### 3.11.2 Load algorithm (full loader) [obs]
1. Lock-free cache hit check keyed on path with signature (user file mtime_ns + size, admin file
   mtime_ns + size) plus a snapshot of every `${VAR}` value used in the expansion; a hit returns a
   deep copy (readonly variant returns the shared object).
2. Under a process-wide re-entrant lock: ensure home skeleton; deep-copy defaults; parse the user
   file (UTF-8 with BOM tolerated) with a safe YAML loader; legacy root `max_turns` moves to
   `agent.max_turns`; deep-merge user over defaults (dicts merge recursively, everything else
   replaces); user mixture-of-agents presets merged specially.
3. On successful parse, keep a "good" backup copy (§3.11.5).
4. On parse failure: serve the **last-known-good** config (in-memory, then newest good backup on
   disk) so a mid-edit broken file never silently drops user rules (e.g. approval deny rules). If
   none exists, return defaults wrapped in a "failed read" marker object that save refuses to
   persist.
5. Canonicalise: model keys (`api_base` → `base_url`, scalar `model: x` ↔ `model.default`,
   `model.model` alias), max-turns normalisation.
6. Expand `${NAME}` and `${env:NAME}` in string values (keys untouched). Other SecretRef sources
   (`file:`, `bitwarden:`, `vault:` …) are left verbatim with a warning — external backends inject
   env at startup instead. Unset names stay as the literal placeholder. Inside a profile secret
   scope, lookups resolve against that profile's `.env`, never another profile's process env.
7. Merge the **admin overlay**: `<managed dir>/config.yaml` values deep-merge on top per leaf
   (siblings stay user-controlled); `${VAR}` in it expands against the process env only (a user
   cannot shadow a managed literal); a bare managed `model: x` is promoted to `model.default`;
   malformed/absent → ignored (fail-open).

#### 3.11.3 Save algorithm [obs]
1. Refuse when the install is package-manager managed (`.managed` marker or `INDUS_MANAGED`).
2. Refuse to save a "failed read" object.
3. Strip leaves pinned by the admin overlay and tell the user ("N managed setting(s) were not
   saved").
4. **Fail-closed re-read** of the raw on-disk file: unreadable → error, never treated as `{}`
   (a cached/swallowed empty dict once made the strip pass delete every user section that matched
   a default).
5. Optional merge-with-existing for partial callers.
6. Preserve `${VAR}` templates: if the in-memory value equals the expansion of a template in the
   raw file, write the template back, not the secret.
7. **Strip defaults**: remove any path whose value equals the default, unless the user explicitly
   had that path in the raw file (or it is in a preserve list). `_config_version` always kept. So
   the file never fills with defaults that would mask future default changes.
8. Write through the single writer seam: a round-trip YAML writer that preserves comments, key
   order, quoting and blank lines; atomic replace; chmod 0600. On **create only**, append
   commented example blocks for features left unconfigured (a security/redaction block and a
   fallback-model block). CI lints that nothing else writes config YAML [obs].
9. Invalidate the raw cache; remember the expanded form for template preservation.
- Additive writers use an "atomic write" variant that refuses deletion-by-omission; deliberate
  removals use the explicit full "replace" variant [obs].

#### 3.11.4 Seeding a missing config [obs]
`config edit` and `doctor --fix` create a missing file by copying `cli-config.yaml.example` (its
display keys are commented out), else writing the default-stripped tree. Never write the full
default tree: the gateway reads the file without defaults, so each written display key would act
as a global value overriding per-platform defaults.

#### 3.11.5 Config backups [obs]
`<home>/backups/config/config.yaml.<reason>.<YYYYMMDD-HHMMSS>`; reasons include `good`,
`pre-setup`, corrupt-file snapshots, migrations. Skip when the source is missing/empty or
byte-identical to the newest backup for that reason (byte compare, not a size/mtime cache); keep
newest 5 per reason; never write through a planted symlink; legacy sibling backup files are swept
into this dir; failures never block the write they precede.

#### 3.11.6 Versioning and migration [obs]
- `_config_version` stamp; latest is 51. Support floor 12: an explicit stamp below 12 is not
  migrated and not rewritten — a stderr warning tells the user to regenerate or hand-set the
  stamp; defaults still deep-merge at read time. A file with no stamp gets only legacy-key steps
  and a stamp.
- `migrate_config(interactive, quiet)`: refuse on malformed YAML before any side effect;
  normalise `.env` formatting; run ordered migration steps from the current version; disable
  suspicious MCP server entries; warn about invalid per-platform toolsets; print "Config version:
  A → B"; prompt for required env vars (currently none are universally required — the provider is
  chosen in the wizard); offer env vars introduced since the user's version (a per-version list);
  new default keys are NOT written to disk (only reported); stamp the new version; offer missing
  skill config vars.
- `update` runs config migration automatically (prompts accepted with `-y`).

### 3.12 Config keys and defaults (abridged; full tree in reference defaults module)

Top-level shape: `Config { model, providers, custom_providers, fallback_providers, fallback,
credential_pool_strategies, toolsets, database, runtime, session, sessions, attachments, agent,
terminal, web, browser, worktrees, checkpoints, mcp, mcp_servers, tool_output,
tool_loop_guardrails, compression, prompt_caching, openrouter, bedrock, vertex, auxiliary,
display, dashboard, privacy, tts, stt, voice, vision, wake_word, human_delay, context, memory,
delegation, goals, loops, moa, skills, curator, timezone, <platform sections>, approvals,
command_allowlist, quick_commands, platform_hints, platform_toolsets, plugins, hooks,
hooks_auto_accept, personalities, auth, security, cron, kanban, bot_mode, code_execution, tools,
logging, model_catalog, model_overrides, models_dev, network, monitoring, gateway, streaming,
onboarding, telemetry, doctor, updates, lsp, x_search, vault, secrets, paste_collapse_*,
bot_desktop, computer_use, proxy, desktop, portal, local_runtime, _config_version }` [obs].

Defaults most relevant to this area [obs]:

| Key | Default | Note |
|---|---|---|
| `model` | `""` (string or `{default, provider, base_url, api_key, api_mode, context_length, …}`) | example file shows provider `auto` |
| `toolsets` | `[<cli toolset>]` | |
| `fallback_providers` | `[]` | legacy `fallback_model` still read |
| `fallback.min_switch_reset_seconds` | 0 | |
| `database.journal_mode` | `wal` (`wal`\|`delete`) | |
| `runtime.nofile_soft_limit` | 4096 | |
| `max_live_sessions` | 16 | |
| `session.terminal_continue` | true | per-terminal `-c` breadcrumbs |
| `agent.max_turns` | null (CLI default 500) | |
| `agent.api_max_retries` | 3 | |
| `agent.gateway_timeout` | 1800 | |
| `agent.clarify_timeout` | 3600 | |
| `terminal.backend` | `local` | `cwd "."`, `timeout 180`, container cpu 1, mem 5120, disk 51200 |
| `checkpoints.enabled` | false | max_snapshots 20, max_total_size_mb 500, max_file_size_mb 10, retention_days 7, min_interval_hours 24 |
| `approvals.mode` | `smart` (`manual`\|`smart`\|`off`) | timeout 300, cron_mode/single_query_mode/unattended_mode `deny`, denial_breaker_threshold 3, `deny []`, mcp_reload_confirm true, destructive_slash_confirm true |
| `command_allowlist` | `[]` | |
| `quick_commands` | `{}` | |
| `hooks_auto_accept` | false | |
| `auth.adopt_external_logins` | true | borrow Claude Code / Codex CLI logins |
| `auth.codex_login_flow` | `device_code` | or browser PKCE |
| `security.redact_secrets` | true | |
| `security.allow_private_urls` | false | |
| `security.allow_data_training_tiers_noninteractive` | false | |
| `security.approval.transport` / `transport_fallback` | `builtin` / `deny` | |
| `security.acked_advisories` | `[]` | `doctor --ack` |
| `security.allow_lazy_installs` | true | |
| `display.interface` | `cli` | `tui` alternative |
| `display.busy_input_mode` | `interrupt` | |
| `display.skin` / `language` | `default` / `en` | |
| `display.persistent_output_max_lines` | 200 | |
| `display.cli_refresh_interval` | 1.0 | |
| `logging.level` / `max_size_mb` / `backup_count` | `INFO` / 5 / 3 | rotation |
| `updates.check` | true | passive update notices |
| `updates.pre_update_backup` | `quick` (`quick`\|`full`\|`off`) | |
| `updates.backup_keep` | 5 | |
| `updates.non_interactive_local_changes` | `stash` | |
| `updates.parked_branch_strategy` | `switch` (or `update_in_place`) | |
| `updates.auto_switch_parked_branch` | true | |
| `model_catalog.ttl_minutes` | 20 | remote catalog JSON |
| `network.force_ipv4` | false | early bridge |
| `doctor.live_probe_timeout` | 10 | |
| `telemetry.shared_metrics.enabled/send` | false/false | opt-in |
| `secrets.bitwarden.*` | enabled false, `access_token_env BWS_ACCESS_TOKEN`, cache_ttl 300, override_existing true, auto_install true | |
| `secrets.onepassword.*` | enabled false, `env {}`, `service_account_token_env OP_SERVICE_ACCOUNT_TOKEN`, cache_ttl 300 | |
| `vault.onepassword/bitwarden.enabled` | true | autofill vault sources |
| `onboarding.profile_build` | `ask` | |
| `paste_collapse_threshold` / `_char_threshold` | 5 lines / 2000 chars | |
| `portal.keepalive_interval_seconds` | 900 | first-party portal |
| `_config_version` | 51 | |

### 3.13 `config` subcommands

#### 3.13.1 `show` / `path` / `env-path` / `edit` [obs]
`show` prints the effective config with credentials redacted. `path`/`env-path` print paths.
`edit` seeds a missing file (§3.11.4) then opens `$EDITOR` [inf: falls back to a platform
default editor].

#### 3.13.2 `get KEY [--json] [--raw]` [obs]
- Env-shaped keys read from `.env`/process env; others from the full merged config (with
  per-platform display-key redirection and a legacy fallback path).
- Missing → "Config key not set: KEY", exit non-zero.
- Masking unless `--raw` or redaction disabled: credential-shaped string keys masked (e.g. first
  and last 4 chars), unless the value is a `${…}` placeholder; dict/list values redacted
  recursively. Reason: `print` bypasses log redaction and the agent itself runs this command in
  persisted transcripts.
- Output: bool → `true/false`, None → `null`, dict/list → YAML block, else string; `--json` →
  JSON. If the path sits inside a recognised section but has no schema entry, the value is still
  printed, with a stderr note that the runtime may ignore it (exit 0, stdout untouched).

#### 3.13.3 `set KEY VALUE [--force]` routing [obs]
1. Refuse in managed installs; refuse empty/whitespace keys and empty segments (`a..b`, `a.`).
2. Refuse admin-pinned keys.
3. **Credential-shaped bare name** (no dot, and in the known env list or ending `_API_KEY`,
   `_TOKEN`, `_SECRET`, or starting `TERMINAL_SSH`) → save to `.env` through the provider
   credential lifecycle (which also replaces a stale copy of the old value kept in `config.yaml`). Print "Set K in
   <env path>".
4. **Any other UPPER_SNAKE name** → `.env` as a plain setting (same file setup flows use).
   `config.yaml` never receives an UPPER_SNAKE key from here, even with `--force`. The env writer
   denylist (§3.14.2) applies.
5. Redirect per-platform display keys to their canonical path; validate the key against the
   schema. A key known under a different prefix (e.g. `gateway.discord.foo` when `discord.foo`
   exists) is refused with a did-you-mean unless `--force`; other unknown paths are written then a
   did-you-mean notice is printed; unknown top-level keys written with a notice.
6. `display.language` validated against available locales.
7. Read raw user file fail-closed; **coerce** the value: if the default at that path is a string,
   keep verbatim (so `approvals.mode off` is the string, not false) — except bare `model` with a
   structured literal; else `true/false/null`-style words, int, float; values that look like a
   list/mapping are parsed as YAML and refused (nothing written) if invalid; a quoted literal
   stores a string.
8. A scalar `model:` is converted to `{default: …}` before writing `model.*` subkeys.
9. Section guard: refuse replacing a mapping section with a scalar unless `--force`; container
   type guard for known list/mapping slots (`custom_providers` list, `providers` mapping,
   `model.aliases` mapping, `plugins.enabled/disabled` list,
   `model_catalog.excluded_providers` list, any key already holding one). Lenient one-item-list
   slots: `agent.disabled_toolsets`, `skills.disabled`.
10. **Dotted key names**: when navigating an existing mapping, a literal key matching the dotted
    remainder is preferred (`providers.p.models.grok-4.6.x`); to create a new key containing dots
    escape them with `\.`; an unescaped write that would create a phantom sibling shadowing an
    existing dotted key fails.
11. `model.provider` change: drop `model.base_url`/`model.api_mode` that belong to the old provider
    (printed), keep URLs that are the new provider's own, a named custom provider's, or local;
    unknown hosts kept with a yellow warning.
12. `model.api_base` / `api_base` saved as `model.base_url` with a note.
13. Write; if the key maps to a terminal env var (except `terminal.cwd`) mirror it into `.env`;
    touch skin file for skin keys; reset language cache for `display.language`.
14. Echo "Set K = V in <path>" with the value masked when credential-shaped.

#### 3.13.4 `unset KEY` [obs]
Credential keys: remove from `.env` and also prune env-seeded pool entries and model-cache rows
for that provider. Env settings: remove from `.env` and drop a stale top-level `config.yaml` copy.
Else remove the path (plus legacy alias path and mirrored terminal env var). Nothing removed →
"Config key not set", non-zero.

#### 3.13.5 `check` / `migrate` [obs]
`check` reports missing options, stale/deprecated keys and env vars, structural issues
(`custom_providers` entries, fallback model shape, web backends, voice, timezone, quoted
container values). `migrate` runs §3.11.6 interactively.

#### 3.13.6 Write-command error handling [obs]
Validation failures print to stderr and exit 1 ("nothing was written"). Usage errors print a
usage line plus examples.

#### 3.13.7 Approval mode via config [obs]
See §3.21.

#### 3.13.8 `migrate` (retired settings) [obs]
`migrate xai [--apply] [--no-backup]`: dry-run by default; rewrite references to models retired
on 2026-05-15 to their replacements; back up first. `migrate relay`: convert a legacy exporter env
var family into a TOML file, point an env var at it, comment out legacy lines; `--all-profiles`;
`--no-validate`. (Rebuild: keep the pattern "dry-run by default, backup, rewrite" — the specific
migrations are reference history.)

### 3.14 Secrets

#### 3.14.1 Loading order [obs]
1. Sanitise `<home>/.env` (and project `.env`) formatting / strip NULs if needed.
2. `<home>/.env` loaded with **override** (user file beats stale shell exports); keys known to
   the app but absent from the file are cleared from the inherited env so removed keys don't leak.
3. `<home>/.op.env` loaded **without** override, only if `OP_SERVICE_ACCOUNT_TOKEN` is unset (so
   a service manager's env file wins).
4. Install-dir `.env` (dev fallback): fills gaps only when the user file exists; overrides when
   it does not.
5. Re-assert values an external secret source resolved earlier (a re-load once clobbered them
   with a placeholder).
6. External secret sources (skipped for `update`, which must not import native crypto libs on
   Windows).
7. Admin-managed `.env` (from the managed dir) applied last with override.
8. Re-apply `terminal.*` config → env bridge so a leftover `TERMINAL_ENV` in `.env` cannot switch
   the terminal backend of a long-running process on a later reload.
- Multi-profile servers skip copying a routed profile's `.env` into the shared process env
  (scoped lookups instead).
- Loaded credentials with non-ASCII characters are sanitised/warned.

#### 3.14.2 Writing `.env` [obs]
- Name must match `^[A-Za-z_][A-Za-z0-9_]*$`. **Writer denylist** (refused with a message telling
  the user to edit the file by hand): prefixes `LD_`, `DYLD_`, `GIT_CONFIG_`; names `PATH`,
  `SHELL`, `BROWSER`, `EDITOR`, `VISUAL`, `PAGER`, `MANPAGER`, Python/Node/Perl/Ruby/Java/Go/Rust
  init vars (`PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP`, `NODE_OPTIONS`, `NODE_PATH`, `PERL5OPT`,
  `RUBYOPT`, `CLASSPATH`, `JAVA_TOOL_OPTIONS`, `GOFLAGS`, `RUSTFLAGS`, …), git helper/redirect vars
  (`GIT_SSH_COMMAND`, `GIT_ASKPASS`, `GIT_EDITOR`, `GIT_DIR`, …), `SSH_ASKPASS`, `SUDO_ASKPASS`,
  shell init (`BASH_ENV`, `ENV`, `ZDOTDIR`, `PROMPT_COMMAND`, `VIMINIT`, `EXINIT`), and app
  location/policy vars (`INDUS_HOME`, `INDUS_PROFILE`, `INDUS_CONFIG(_PATH)`, `INDUS_ENV(_PATH)`,
  `INDUS_OPTIONAL_MCPS`, ACP command/args, `INDUS_YOLO_MODE`, `INDUS_ACCEPT_HOOKS`,
  `INDUS_REDACT_SECRETS`, `INDUS_INTERACTIVE`, `INDUS_EXEC_ASK`, `INDUS_GATEWAY_SESSION`,
  `INDUS_CRON_SESSION`, `INDUS_SINGLE_QUERY_SESSION`, `INDUS_SESSION_KEY`,
  `INDUS_SESSION_PLATFORM`). The prefix `INDUS_` as a whole is NOT blocked (integration keys use
  it). Enforced on write only. Windows compares names case-insensitively.
- Value: newlines stripped; non-ASCII stripped with a stderr warning listing up to 5 positions.
- Quoting: empty stays empty; values containing `#`, `"`, `'` or any whitespace are double-quoted
  with `\` and `"` escaped.
- Existing line detection accepts `KEY=…`, `export KEY=…`, and `KEY = …` (exactly what the reader
  parses) so saves replace instead of appending duplicates and removes don't leave resurrecting
  lines.
- Whole read-modify-write under a cross-process lock on `.env.lock` (15 s timeout, reentrant per
  thread; degrades to depth-only on filesystems without flock).
- Atomic: temp file beside, fsync, rename. Existing mode preserved (Docker 0640 mounts); a new
  file is 0600.
- Publish the change to the live process env (or, under multi-profile scoping, only to the scope).
- Refused when managed install or the key is admin-pinned.
- Custom endpoint keys get generated names `INDUS_CUSTOM_<SLUG>_API_KEY` (slug = uppercase,
  non-alnum → `_`; fallback `INDUS_CUSTOM_API_KEY`).

#### 3.14.3 Policy: what goes where [obs]
`.env` = secrets and env-shaped settings only; non-secret settings belong in `config.yaml`, with
code bridging to env where needed (`terminal.cwd` → `TERMINAL_CWD`). `TERMINAL_CWD` in `.env` is
deprecated (loader warns).

#### 3.14.4 External secret managers (`secrets`) [obs]
Bitwarden Secrets Manager (`bws`) and 1Password (`op` with `op://` references) pull API keys at
process start instead of storing them in `.env`. Each: `setup` (install/verify binary, store access
token or account, pick project / references, enable), `status` (config, binary, token
validation), `sync` (fetch now, report changes), `disable`, `install` (pinned binary). Config under
`secrets.bitwarden` / `secrets.onepassword` (cache TTL 300 s, `override_existing` true). Values
injected into env; a config `${env:NAME}` ref then picks them up.

#### 3.14.5 Local vault (`vault`) [obs]
Encrypted local store of logins/cards/addresses for browser autofill; agent sees handles and
login identifiers only; passwords injected on the exact saved origin and never enter the
conversation. `add [--kind]`, `list` (metadata only), `rm HANDLE`, `sources` (detected password
managers, `--disable/--enable NAME`). Files `vault.key`, `vault.json.enc` (0600, included in
backups).

#### 3.14.6 Admin overlay ("managed scope") [obs]
Directory `$INDUS_MANAGED_DIR` (only if non-empty and existing) else `/etc/indus` if present.
Contains `config.yaml` and `.env`. Values are user-immutable: overlaid on load, stripped on save,
`config set/unset` refused, `.env` writes refused. Distinct from "managed install" (package
manager owns the whole install: `INDUS_MANAGED` env or `<home>/.managed` marker; empty/unreadable
marker still counts; certain false-y values ignored) which blocks all config mutation and
redirects `update` to the package manager (Nix message, Docker message).

### 3.15 Environment variables owned by this area (rebuild names) [obs]

| Var | Meaning |
|---|---|
| `INDUS_HOME` | Data home (profile path selects that profile) |
| `INDUS_DATA_DIR_SUFFIX` | Suffix for the default home dir name (bundles) |
| `INDUS_HOME_MODE`, `INDUS_UID`, `INDUS_GID` | Home dir mode / ownership |
| `INDUS_MANAGED`, `INDUS_MANAGED_DIR` | Managed install / admin overlay dir |
| `INDUS_INFERENCE_MODEL` | Per-process model override |
| `INDUS_INFERENCE_PROVIDER` | Provider when config says auto |
| `INDUS_TUI` (`=1`), `INDUS_TUI_DIR`, `INDUS_TUI_RESUME`, `INDUS_TUI_THEME` | Interface (05) |
| `INDUS_YOLO_MODE` | Bypass approvals |
| `INDUS_ACCEPT_HOOKS` | Auto-approve hooks |
| `INDUS_IGNORE_USER_CONFIG`, `INDUS_IGNORE_RULES`, `INDUS_SAFE_MODE` | Isolation |
| `INDUS_REDACT_SECRETS` | Redaction on/off (bridged from config) |
| `INDUS_SESSION_SOURCE` (+`_EXPLICIT`) | Session source tag |
| `INDUS_INTERACTIVE` | Set by REPL/doctor; enables interactive sudo prompts |
| `INDUS_QUIET` | Suppress non-essential output |
| `INDUS_MAX_ITERATIONS` | Per-turn iteration cap (default 500) |
| `INDUS_PORTAL_BASE_URL`, `INDUS_PORTAL_INFERENCE_BASE_URL`, portal min key TTL (1800), portal timeout | Portal auth |
| `INDUS_OAUTH_TRACE` | Log OAuth exchanges (redacted) |
| `INDUS_INSTALL_CHANNEL`, `INDUS_INSTALL_DIR`, `INDUS_INSTALL_VERBOSE`, `INDUS_REPO_URL`, `INDUS_RUNTIME_DIR` | Installer |
| `INDUS_CUSTOM_<SLUG>_API_KEY` | Generated custom endpoint key slots |
| `INDUS_AGENT=true`, `AI_AGENT=indus-agent` | Exported to children |
| `INDUS_SESSION_ID` | Exported to tool subprocesses (01/02) |
| Provider keys | `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `ANTHROPIC_API_KEY`, `ANTHROPIC_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, plus registry vars (§3.16.1) |
| `BROWSER`, `DISPLAY`, `WAYLAND_DISPLAY`, `SSH_CLIENT`, `SSH_TTY`, `CLOUD_SHELL`, `CODESPACES`, `CODESPACE_NAME`, `GITPOD_WORKSPACE_ID`, `REPL_ID`, `STACKBLITZ` | Read by OAuth browser/remote detection |
| `EDITOR`/`VISUAL` | `config edit`, Ctrl+G |
| `OP_SERVICE_ACCOUNT_TOKEN`, `BWS_ACCESS_TOKEN` | External secret managers |

### 3.16 Providers and auth

#### 3.16.1 Provider registry (priority order for auto-detection) [obs]
`ProviderConfig { id, name, auth_type: oauth_device_code|oauth_external|oauth_minimax|api_key|
external_process|aws_sdk|vertex, portal_base_url?, inference_base_url, client_id?, scope?,
extra{}, api_key_env_vars[] (priority order), base_url_env_var? }`.

| id | Auth | Key env vars (in order) | Base URL (base env) |
|---|---|---|---|
| portal (first-party) | device code | — | portal + inference URLs |
| openai-codex | external OAuth | — | `https://chatgpt.com/backend-api/codex` |
| openai-api | key | `OPENAI_API_KEY` | `https://api.openai.com/v1` (`OPENAI_BASE_URL`) |
| xai-oauth | external OAuth | — | `https://api.x.ai/v1` |
| qwen-oauth | external OAuth | — | `https://portal.qwen.ai/v1` |
| lmstudio | key (optional) | `LM_API_KEY` | `http://127.0.0.1:1234/v1` (`LM_BASE_URL`) |
| copilot | key | `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN` | `https://api.githubcopilot.com` (`COPILOT_API_BASE_URL`) |
| copilot-acp | external process | — | `acp://copilot` (`COPILOT_ACP_BASE_URL`) |
| gemini | key | `GOOGLE_API_KEY`, `GEMINI_API_KEY` | `https://generativelanguage.googleapis.com/v1beta` (`GEMINI_BASE_URL`) |
| zai | key | `GLM_API_KEY`, `ZAI_API_KEY`, `Z_AI_API_KEY` | `https://api.z.ai/api/paas/v4` (`GLM_BASE_URL`) |
| kimi-coding | key | `KIMI_API_KEY`, `KIMI_CODING_API_KEY` | `https://api.moonshot.ai/v1` (`KIMI_BASE_URL`); `sk-kimi-` keys redirected to the coding endpoint |
| kimi-coding-cn | key | `KIMI_CN_API_KEY` | `https://api.moonshot.cn/v1` |
| stepfun | key | `STEPFUN_API_KEY` | `https://api.stepfun.ai/step_plan/v1` (`STEPFUN_BASE_URL`) |
| arcee | key | `ARCEEAI_API_KEY` | `https://api.arcee.ai/api/v1` |
| gmi | key | `GMI_API_KEY` | `https://api.gmi-serving.com/v1` |
| actual | key | `ACTUAL_API_KEY` | `https://api.actual.inc/v1` (local `http://127.0.0.1:8080/v1`) |
| minimax | key | `MINIMAX_API_KEY` | `https://api.minimax.io/anthropic` |
| minimax-oauth | user-code OAuth | — | global `https://api.minimax.io` / CN `https://api.minimaxi.com` |
| anthropic | key (+OAuth) | `ANTHROPIC_API_KEY`, `ANTHROPIC_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN` | `https://api.anthropic.com` (`ANTHROPIC_BASE_URL`) |
| alibaba | key | `DASHSCOPE_API_KEY` | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` |
| alibaba-coding-plan | key | `ALIBABA_CODING_PLAN_API_KEY`, `DASHSCOPE_API_KEY` | `https://coding-intl.dashscope.aliyuncs.com/v1` |
| minimax-cn | key | `MINIMAX_CN_API_KEY` | `https://api.minimaxi.com/anthropic` |
| deepseek | key | `DEEPSEEK_API_KEY` | `https://api.deepseek.com/v1` |
| xai | key | `XAI_API_KEY` | `https://api.x.ai/v1` |
| nvidia | key | `NVIDIA_API_KEY` | `https://integrate.api.nvidia.com/v1` |
| ai-gateway | key | `AI_GATEWAY_API_KEY` | `https://ai-gateway.vercel.sh/v1` |
| opencode-zen / opencode-go | key | `OPENCODE_ZEN_API_KEY` / `OPENCODE_GO_API_KEY` | `https://opencode.ai/zen/v1` / `…/zen/go/v1` |
| kilocode | key | `KILOCODE_API_KEY` | `https://api.kilo.ai/api/gateway` |
| huggingface | key | `HF_TOKEN` | `https://router.huggingface.co/v1` |
| xiaomi | key | `XIAOMI_API_KEY` | `https://api.xiaomimimo.com/v1` |
| tencent-tokenhub / tokenplan | key | `TOKENHUB_API_KEY` / `TOKENPLAN_API_KEY` | (see reference) |
| ollama-cloud | key | `OLLAMA_API_KEY` | `https://ollama.com/v1` |
| bedrock | AWS SDK | — | `https://bedrock-runtime.us-east-1.amazonaws.com` |
| vertex | Vertex | — | computed per request from project + region |
| azure-foundry | key or Entra ID | `AZURE_FOUNDRY_API_KEY` | user-supplied |
Plus: `openrouter` (key `OPENROUTER_API_KEY`, also OAuth PKCE that mints a key), `custom` and
named custom providers (aliases `ollama`, `vllm`, `llamacpp` map to custom), plugin-registered
providers mirrored into the registry, and others listed in docs (novita, upstage, commandcode,
router, nebius, alibaba-cn variants). Providers excluded from env auto-detection: copilot,
lmstudio. Placeholder-shaped secrets (e.g. bare `sk-`, `ghp_`, `hf_` prefixes, known dummy
strings) do not count as configured.

#### 3.16.2 Auth store [obs]
- File `<home>/auth.json`: `AuthStore { version: 1, providers: { <id>: ProviderState },
  active_provider?, credential_pool: { <id>: PooledCredential[] }, suppressed_sources? }`.
  Legacy `systems` shape migrated on read.
- Profiles read the root store as a read-only fallback when their own lacks a provider.
- Read failures: OS error (EACCES, EMFILE, stalled mount) → raise and leave the file untouched
  (MUST: an empty store here is one save away from erasing every credential); corrupt JSON →
  copy to `auth.json.corrupt`, warn, start empty.
- Writes: atomic JSON, 0600, parent 0700 (refuses `/`, top-level dirs, the install tree), under a
  cross-process lock (15 s timeout) with a holder-PID hint on contention.
- Tests MUST never touch the real user store (a guard raises when the path is the real home under
  a test runner) [obs].

#### 3.16.3 Credential pool and `auth` subcommands [obs]
`PooledCredential { id, label, auth_type: oauth|api_key, source: manual|device_code|loopback_pkce|
env|…, priority, tokens/key, status: ok|exhausted, cooldown_until?, last_error? }`. Per-provider
strategy `credential_pool_strategies.<id>`: `fill_first` (default) | `round_robin` | `random` |
`least_used`.
- `add PROVIDER`: unknown provider → exit with hint (custom providers addressed by name). `--type`
  defaults to OAuth for oauth-capable providers (anthropic, portal, openai-codex, xai-oauth,
  qwen-oauth, minimax-oauth) except OpenRouter which stays key-first. API key prompted with a
  masked prompt if not given. `--priority N` inserts (0 = tried first), else appended. Warn when a
  new Codex token belongs to an account already pooled. Adding un-suppresses that provider's
  sources.
- `list [provider]`: index, label, type, source, status (exhausted with reason class and time
  left).
- `remove P T` (index, id or exact label); `reset P [T]` clears exhaustion/cooldown; `priority P T
  N` renumbers others; `refresh P [T]` refreshes OAuth tokens and clears cooldown (target required
  when >1); `status P`; `logout P` clears stored state (and resets config provider if it was the
  logged-out one).
- Bare `auth`: interactive menu (add, remove, reset, choose strategy).
- `upgrade`: sign in with a portal account keeping connectors (free-tier promotion; optional).

#### 3.16.4 Device-code flow (RFC 8628) [obs]
1. POST device authorization; show verification URL + user code; open browser only if a
   graphical browser is likely (see 3.16.6), unless `--no-browser`.
2. Poll token endpoint: `authorization_pending` → sleep interval; `slow_down` → interval +1 s
   (cap 30 s); non-JSON 408/429/5xx or a 403 edge-mitigation header → back off honouring
   `Retry-After`, else exponential from max(5, 2×interval), capped 60 s and at the deadline;
   any other error → provider-specific exception; deadline = `expires_in`. Portal polling interval
   capped at 1 s. A timeout while the endpoint was failing reports a service outage, not "you did
   not approve".
3. Store tokens with `obtained_at`/`expires_at`; mark provider active if none.
- Refresh skews [obs]: access tokens refreshed 120 s before expiry (portal, Codex, Qwen); MiniMax
  60 s; xAI 3600 s. Dead-grant errors (`invalid_grant`, `invalid_token`, `refresh_token_reused`)
  are terminal (re-login needed).
- TLS: `--insecure` disables verification, `--ca-bundle` sets a PEM; default verification uses the
  OS trust store.

#### 3.16.5 Loopback PKCE flow [obs]
Bind the callback server first (port 0 = OS-assigned, or a fixed port such as 1455 for Codex) so
the redirect URI names a port already owned; serve until code/error or deadline (min 5 s); always
shut down. On a remote session print an SSH port-forward hint (`ssh -L <port>:127.0.0.1:<port>
user@host`). Codex: device code by default; `--browser` or `auth.codex_login_flow` selects PKCE
on `http://localhost:1455/auth/callback`, falling back to device code if the port is busy.
OpenRouter OAuth PKCE mints an API key.

#### 3.16.6 Browser/remote heuristics [obs]
Remote session = `SSH_CLIENT`/`SSH_TTY` or a browser-IDE marker (`CLOUD_SHELL`, `CODESPACES`,
`CODESPACE_NAME`, `GITPOD_WORKSPACE_ID`, `REPL_ID`, `STACKBLITZ`). Graphical browser likely
unless `$BROWSER` names a console browser, or Linux with no `DISPLAY`/`WAYLAND_DISPLAY` and no
`$BROWSER`, or the resolved browser controller is a console one.

#### 3.16.7 External login adoption [obs]
With `auth.adopt_external_logins: true`, existing Claude Code and Codex CLI credential files may
be borrowed (Claude Code files preferred because they self-refresh). Claude Code OAuth tokens
(`sk-ant-oat01…`) arrive in a key slot but are routed as OAuth by prefix. Anthropic OAuth only
works for specific paid plans — rebuild must not promise it [obs: docs caveat].

#### 3.16.8 `logout` and deprecated `login` [obs]
`logout [--provider portal|openai-codex|xai-oauth]` (default active) removes stored credentials
and resets provider config. `login` is registered without help text (hidden) and accepts any
`--provider` so old scripts reach a deprecation message pointing to `auth`, `model`, `setup`.

### 3.17 Model picker (`model`) and related

#### 3.17.1 Flow [obs]
1. Require a TTY. `--refresh` wipes the picker disk cache first.
2. Show "Current model" and "Active provider". Active = the saved custom endpoint with the same base URL as
   `model.base_url` → configured provider (config > `INDUS_INFERENCE_PROVIDER` env > auto)
   → auto detection; unknown provider warns and falls back; "no provider configured" prints a
   friendly line (not a circular "run `model`" hint); OpenRouter + `OPENAI_BASE_URL` shows as
   custom.
3. Provider picker (curses) with group rows that drill into a member sub-picker; special rows:
   auxiliary-model config menu, reasoning-effort only, remove custom provider, cancel.
4. Per-provider flow: OAuth login if needed, key prompt if missing, model list (live
   `/v1/models` where possible, curated lists otherwise), custom endpoint entry (base URL, key,
   model auto-detect), local servers, Bedrock/Vertex/Azure specifics.
5. Persist `model.default`, `model.provider`, base URL/api mode as needed; offer reasoning effort
   after a pick.
6. Leaving custom for a named provider clears a stale `OPENAI_BASE_URL` from `.env`.
- Left-arrow at the first prompt goes back (setup navigation). Cancel prints "No change."
- **Caches/TTLs** [obs]: provider model lists in `<home>/provider_models_cache.json`, TTL 3600 s
  (fallback rows 60 s, stale-while-revalidate); curated catalog under `<home>/cache/`; free-tier
  check 180 s; portal recommendations 600 s; GitHub model catalog 300 s; negative probe TTL 60 s;
  remote model catalog JSON TTL 20 min (`model_catalog.ttl_minutes`).

#### 3.17.2 `/model` vs `indus model` [obs]
`/model` only switches between already-configured providers/models (no OAuth, no key prompts);
syntax `model`, `provider:model`, `custom:name:model`; session-only unless `--global` (or
`model.persist_switch_by_default`); on a global switch away from custom the stale base URL is
cleared.

#### 3.17.3 Reasoning levels [obs]
`none, minimal, low, medium, high, xhigh, max, ultra`; persistent `agent.reasoning_effort`, per
model `agent.reasoning_overrides`; per-run `--reasoning`.

#### 3.17.4 `fallback` [obs]
`list` (default), `add` (same picker as `model`, appends `{provider, model}` to
`fallback_providers`), `remove` (pick), `clear`. Chain tried on rate-limit/overload/connection
errors (01).

#### 3.17.5 `moa` [obs]
Named presets of reference models + aggregator under `moa.presets`; `list`, `configure [name]`,
`delete name`. Agent behaviour in 01.

### 3.18 Setup wizard (`setup`)

- **Guards** [obs]: managed install → refuse; ensure home; back up config (`pre-setup`) BEFORE
  `--reset` (which writes the default tree); non-TTY or `--non-interactive` → print guidance
  (example `config set` lines for a custom endpoint, or set an OpenRouter/OpenAI key) and return.
- **`--portal`**: one-shot first-party portal OAuth, model pick, set provider, opt into its tool
  gateway; skips the rest.
- **`setup SECTION`** (`model`, `tts`, `terminal`, `gateway`, `tools`, `telemetry`, `agent`):
  banner, run that section, save, success line.
- **Existing install** (an OpenRouter key, `OPENAI_BASE_URL`, or an active auth provider):
  `--quick` → only missing items; else full reconfigure where every prompt defaults to the
  current value (`--reconfigure` kept as a no-op alias).
- **First install**: offer OpenClaw migration first (§3.28); then choose mode: Quick (portal
  login, no keys), Full (every section), Blank Slate (everything off except the minimum; opt into
  each capability).
- **Full sequence**: print config/secrets/data/install paths; apply default agent settings on a
  first install (not prompted); sections Model & Provider → Terminal Backend (local, docker,
  singularity, modal, daytona, vercel, ssh, plugin backends) → Messaging Platforms (offers gateway
  service install) → Tools; after an OpenClaw import, already-configured sections can be skipped.
  Save; print backup path with a `cp` restore hint; print a capability summary; record completion
  (and offer opt-in telemetry once).
- **Navigation**: left-arrow on the first prompt of a section goes back one section; on a later
  prompt, prior answers are silently replayed and the preceding prompt is shown again. Ctrl+C →
  "Setup cancelled. Remaining sections were not changed."
- **Prompt helpers**: text prompt with default; masked secret prompt (one `*` per char, Backspace,
  Ctrl+C raises, Ctrl+D/EOF ends); yes/no; numbered/curses choice; checklist; pasted-input
  sanitising.

### 3.19 `tools` [obs]
Per-platform enablement stored in `platform_toolsets.<platform>` (platforms: cli, telegram,
discord, …). Built-in toolsets by plain name (`web`, `memory`); MCP tools as `server:tool`.
`list [--platform cli]`, `enable NAME… [--platform]`, `disable NAME…`, `--summary`, bare =
curses configuration UI (pick provider for tool backends, prompt keys, run post-setup installers).
`post-setup KEY` runs a backend's dependency installer non-interactively (keys like
`agent_browser`, `camofox`, `cua_driver`, `kittentts`, `piper`, `ddgs`, `xai_grok`,
`openai_codex`).

### 3.20 `sessions` CLI surface (storage semantics in 03) [obs]
`list [--source] [--limit 20] [--workspace NEEDLE]`; `browse [--source] [--limit 500]` picker;
`rename ID TITLE…`; `delete ID [-y]`; `export [OUTPUT] [--format jsonl|md|qmd|html|trace]
[--only user-prompts] [--session-id] [--lineage single|logical] [--redact] [--upload] [--public]
[--no-redact] [--delete-after-verified] [--force] + filters`; `prune` (bare = older than 90 days;
`--include-archived`, `--include-pinned`, `--never-active` default 30 days, `--force`); `archive
[--include-open]`; `pin/unpin IDS…`, `pinned`; `stats`; `optimize`, `optimize-storage
[--no-vacuum]`, `repair [--check-only] [--no-backup]`, `set-journal-mode delete|wal [--db]`,
`clean-markers`, `repair-routing [--max-gap-seconds]`, `repair-profiles [--legacy-main
report|rekey|move]`, `repair-prompts [ID]`, `recover --source --output --work-dir --chunk-size
1000 --report`, `retitle-skills [--limit 200]`, `import --from claude|codex [path]`. Shared filters:
`--older-than`, `--newer-than`, `--before`, `--after` (durations `5h`/`2d`/`1w` or ISO), `--source`,
`--title`, `--end-reason`, `--cwd`, `--min/max-messages`, `--model`, `--provider`, `--user`,
`--chat-id`, `--chat-type`, `--branch`, `--min/max-tokens`, `--min/max-cost`,
`--min/max-tool-calls`, `--dry-run`, `-y`.

### 3.21 Approvals mode [obs]
- Modes: `manual` (always prompt), `smart` (default; auxiliary-model assisted), `off` (no prompts).
  Stored in `approvals.mode` per profile; the guard re-reads config on every check, so a change
  takes effect immediately without rebuilding the agent or touching its system prompt.
- `/approvals [mode]`: no arg → print effective mode; invalid → usage; set goes through the same
  config writer (managed refusal and unreadable-config errors are captured and shown, never kill
  the REPL); re-read and report if the requested value did not take effect.
- `--yolo` / `INDUS_YOLO_MODE=1` / `/yolo` bypass prompts for the run/session; must be set before
  the approval module is imported.
- Non-interactive contexts use `cron_mode`, `single_query_mode`, `unattended_mode` (all `deny`).
- `approvals suggest`: mine approved dangerous commands from the session DB over `--days` (90; 0 =
  all), rank recurring patterns with count ≥ `--min-count` (2), show ≤ `--limit` (20); never
  propose destructive classes (recursive delete, sudo, disk writes, credential edits); `--apply
  N[,M]` merges the numbered proposals into `command_allowlist`.
- `approvals test [--env-type local] [--json] -- CMD…`: evaluate against the real guards without
  executing or persisting; print verdict, matching rule, normalised trace. Exit 0 allow, 2 would
  ask, 3 deny. Isolated container backends skip the guards.

### 3.22 `checkpoints` CLI [obs]
Store at `<home>/checkpoints/` (shared shadow git store; internals in 02). `status`/`list`
(`--limit 20`): total size, project count, per-project rows with age (`Ns/Nm/Nh/Nd ago`).
`prune`: delete projects whose workdir is gone (confirm unless `-f`, skip with `--keep-orphans`)
or untouched > `--retention-days` (7), git GC, then drop oldest commits until ≤ `--max-size-mb`
(500); ignores the 24 h idempotency marker. `clear [-f]` wipes the base (irreversible).
`clear-legacy [-f]` removes v1 migration archives; exit 2 if any could not be deleted. In-session
`/rollback` listed in §3.7.3.

### 3.23 Backup, import, snapshots [obs]
- **Full backup**: zip of the root home (all profiles), excluding: the install dir, `__pycache__`,
  `.git`, `node_modules`, `backups/`, `state-snapshots/`, `checkpoints/`, live browser profile dirs
  (credential stores), venvs and tool caches, regenerable runtime downloads at profile roots
  (`models/`, `runtimes/`, `node/`), `cache/` except `images, audio, videos, documents,
  screenshots, citations` and generated media; SQLite sidecars (`-wal`, `-shm`, `-journal`);
  `.pyc`; `.backup.lock`, `gateway.pid`, `cron.pid`; sockets, devices, symlinks. Databases are
  copied with the SQLite online backup API (safe while running). Memory-provider state outside the
  home stored under `_external/` (paths relative to user home). Default output
  `~/indus-backup-<ts>.zip`; written to a temp then published. `--keep N` (3) prunes older
  `indus-backup-*.zip` in the output dir (custom names never touched; skipped if incomplete).
  Exit: 0 all files landed; 1 incomplete (zip kept); 2 another backup running (lock wait 0.25 s).
- **Quick snapshot** (`--quick [--label]`): per-profile copy of critical files: `state.db`,
  `config.yaml`, `.env`, `auth.json`, `cron/jobs.json`, `cron/executions.db`, gateway state json,
  channel directory/alias json, `processes.json`, other per-profile stores and pairing dirs, into
  `<home>/state-snapshots/`; keep 20; files > 1 GiB skipped in automatic snapshots. Restorable via
  `/snapshot restore`.
- **Import** (`import ZIP [-f]`): file must exist and be a zip (else exit 1); validate; detect and
  strip an archive prefix; target = the CURRENT home (not mapped back to root — MUST, a past bug
  restored a profile backup over the live root); confirm if `config.yaml`/`.env` exist (unless
  `-f`); decompress every member and CRC-check BEFORE writing anything (damaged → exit 1, home
  untouched); never overwrite source-machine runtime files (`gateway_state.json`, `gateway.pid`,
  `cron.pid`, `gateway.lock`, `processes.json`); restore secret files with 0600; write `.db`
  members page-by-page INTO the existing DB file (so open connections converge) rather than
  renaming over it; if that is impossible while another process holds it, skip and warn; report
  when the imported `state.db` has fewer sessions/messages than the replaced one; exit 1 if any
  file was skipped. Advise stopping the gateway first.
- Pre-update (`backups/pre-update-*.zip`, keep 5) and pre-migration zips (keep 5); `.incomplete.zip`
  leftovers pruned.

### 3.24 Diagnostics

#### 3.24.1 `doctor [--fix] [--live] [--ack ID]` [obs]
Loads `.env` first; sets the interactive flag. `--ack ID` records an advisory acknowledgement in
`security.acked_advisories` and exits. Ordered checks (each prints rows, returns a finding):
security advisories; MCP server security; Python environment; SSL/CA certificates; required
packages; web dashboard import; `.env` file; `config.yaml` validity, model/auxiliary config,
drift (version stamp, stale root keys, ghost max-iterations, deprecations, structure, legacy custom
providers, channel record hygiene); retired-model notices; auth providers; directory structure;
state DB health/stats/WAL/retired-WAL holders; cron store; checkpoint store; gateway supervision;
command installation (launcher on PATH); git and ripgrep; terminal backend (docker/ssh/daytona/
vercel/plugin specifics); node and browser tooling; npm audit; API connectivity (probes per
configured provider); tool availability; skills hub; memory provider; exporter plugins; profiles.
`--fix` repairs what it can (seed config, create dirs, repair DB…). `--live` then runs one bounded
read-only real call per configured tool backend (timeout `doctor.live_probe_timeout` 10 s).
Summary: "Fixed N issue(s)", or numbered remaining issues with a `--fix` tip, or "All checks
passed". Exit 1 if any issue remains.

#### 3.24.2 `dump [--show-keys]` [obs]
Compact plain-text setup summary for pasting into support channels; keys shown only as set/not
set, or with `--show-keys` first/last 4 chars.

#### 3.24.3 `debug` [obs]
`share`: build a report (system info + last `--lines` (200) lines per log); confirm before upload
(`-y` required without a TTY — refuses silently uploading); force-redact secrets unless
`--no-redact`; upload to a public paste service (primary deletes after 6 h; fallback retention
`--expire` days, min 1, default 1, not deletable); `--local` prints instead; a private upload
option stores in vendor storage auto-deleted after 14 days. `delete URL…` deletes pastes.

#### 3.24.4 `status [--full|--all] [--deep]` [obs]
Default one screen: model, active provider, providers with credentials (same list the `/model`
picker offers), gateway state, platforms the gateway would start, scheduled jobs; no key values.
`--full` every section (keys redacted); `--deep` slower checks, implies full.

#### 3.24.5 `logs` [obs]
Logs in `<home>/logs/`: `agent.log` (INFO+), `errors.log` (WARNING+ subset), `gateway.log`,
`gui.log`, `desktop.log`, `mcp-stderr.log`, `update.log` (append-only mirror of updates),
`desktop-update-handoff.log`. Rotation: `logging.max_size_mb` 5 MB × `backup_count` 3 (a
concurrent-safe handler on Windows). `logs [name|list] -n 50 -f --level --session --since
(s/m/h/d) --component (gateway|agent|tools|cli|cron|gui)`; filters AND together; continuation
lines (tracebacks) follow their timestamped line; follow polls every 0.3 s; `list` shows files
with sizes.

#### 3.24.6 `usage [--provider] [--json]` [obs]
Account limit windows without a session (Codex 5 h/weekly + banked resets, Anthropic OAuth
windows, OpenRouter credits). JSON: `{provider, source, title, plan?, fetched_at, windows:
[{label, used_percent?, resets_at?, detail?}], details[], unavailable_reason?}` (stable keys,
additive only). Exit 1 + one stderr line when no credential, no usage endpoint, or fetch fails.

#### 3.24.7 `prompt-size [--platform cli] [--json]` [obs]
Offline byte breakdown of a fresh session's fixed prompt: system prompt total, skills index,
memory, user profile, tool-schema JSON.

#### 3.24.8 `security audit` [obs]
Query OSV.dev for the app's venv packages, plugin requirement files, and pinned npx/uvx MCP
servers; `--fail-on` severity (critical default) sets non-zero exit; `--skip-*`; `--json`.

### 3.25 `update`

- **Install kinds** [obs]: source checkout (git) — the only kind `update` moves; Docker, Nix,
  Termux APT, desktop bundles, Store packages update through their own channels (`update` prints
  the right instruction; managed installs are refused with the package-manager message).
- **Channels** [obs]: official checkouts default to `stable` = latest published `vX.Y.Z` release
  tag (never draft/prerelease/canary), checked out at its exact commit; `main` follows the branch
  tip; `canary` exists for releases. Forks/mirrors track `main`. `--set-channel C` persists per
  install under `update.installs.<install-id>` in config; `--channel C` is one run only;
  `--install-id` prints id and path. With no explicit channel choice, an install is never moved backwards: a
  checkout ahead of the newest release is left where it is. `--set-channel stable` may pin to an
  older commit (reported as a rollback). Updates refuse a release older than session stores the
  checkout already uses.
- **Read-only modes**: `--check` (is an update available; exits without installing), `--plan`
  (install kind, every running service across profiles with supervisor and running code version,
  how each would restart), `--list-venv-holders` (Windows JSON list of PIDs holding the venv; exit
  0 free / 3 holders; `[]` elsewhere).
- **Phases** (transactional: plan → snapshot → apply → restart → verify → report) [obs]:
  1. Pre-update snapshot per profile (`updates.pre_update_backup`: quick default, full, off;
     `--backup` forces full, `--no-backup` skips all); failure warns and continues; recorded in a
     receipt.
  2. Code move: stash local changes (`updates.non_interactive_local_changes: stash`), switch
     branch / fetch tag, merge or switch per parked-branch strategy (`--switch-branch` override),
     submodules. On Linux/macOS, gateways from this install are paused just before the first
     checkout change (none if nothing to apply).
  3. Post-pull syntax check of the critical startup files; on failure roll back to the pre-pull
     commit (hard reset, or checkout the prior detached sha/branch) so the shell stays bootable.
  4. Dependency preparation in a fresh environment validated before switching; incompatible
     plugins are disabled per profile (one retry for network failures) — a plugin never fails the
     update; only an unbuildable core fails.
  5. Config migration (prompts; `-y` accepts migration and stash-restore prompts, skips key
     entry).
  6. Optional desktop/web/TUI rebuild (stage-and-swap; failure leaves the old build and becomes an
     owed follow-up with exit 0).
  7. Restart paused gateways on the new code (before product builds on POSIX, after on Windows);
     service-managed via systemd/launchd; manual gateways relaunched when mappable; manually
     started dashboard/serve backends are asked to restart, not killed. `--no-gateway-restart`
     defers.
  8. Multi-profile consolidation of per-profile gateways when unblocked (06).
- **Exit codes** [obs]: post-commit failures are follow-ups with exit 0; a stash that could not be
  re-applied → 1; Ctrl-C → 130. Receipts under root `logs/update_receipts/`. Full output mirrored
  to `update.log`.
- **Windows specifics** [obs]: refuse while other processes hold the venv (`--force-venv`
  overrides, risky), `--force` ignores other running CLI exes; ZIP fallback only when git itself
  failed and the tree is clean; launcher copies live outside the checkout because the update's
  stash once deleted them.
- **Compat surface** [obs]: code that the OLD updater process imports after the checkout swap must
  keep the same names ("frozen surface") or mid-update upgrades break; the old interpreter must
  never execute freshly pulled modules.
- `updates.check: false` suppresses passive update notices and their network calls (explicit
  `update` still works).
- `--gateway` mode: prompts via file-based IPC for the in-chat `/update`.

### 3.26 `uninstall` [obs]
- Refuse when another manager owns the install. `--dry-run` prints what would be removed.
- Interactive: show code dir, config, secrets, data dirs, desktop user-data dir, other named
  profiles (with gateway-running marker). Options: 1 Keep data (remove code only, recommended),
  2 Full uninstall (everything), 3 Cancel. Full from the default profile asks whether to also
  stop/remove named profiles (default no). Final confirmation requires typing `yes` exactly;
  Ctrl-C/EOF → "Cancelled.".
- `-y`: no prompts; `--full` = full wipe; named profiles never auto-removed unattended.
- `--gui`: remove only the desktop app; `--gui-summary` prints JSON of installed artifacts;
  `--data`: remove only the active home's user data (works on Nix/bundle/Docker).
- Steps: remove gateway services (systemd user/system, launchd plists, Windows tasks), dashboard
  launchd jobs, PATH lines from shell rc files (`.bashrc`, `.zshrc`, `.profile`, fish …),
  Windows PATH/registry env entries, launcher wrapper(s), node symlinks, legacy runtime trees,
  desktop leftovers (XDG/macOS caches), the code dir, and (full) the home.

### 3.27 Install scripts and packaging [obs]
- **Supported distribution**: shell installer (`curl … | bash`), PowerShell installer, Docker
  image, Nix, Termux APT, desktop bundles. Building wheels/sdists is deliberately blocked outside
  a Nix build (guard env var); editable installs work. Python `>=3.11,<3.15`.
- **POSIX installer flags**: `--branch`, `--commit`, `--channel`, `--dir` (code), `--indus-home`
  (data), `--stage NAME [--json]`, `--manifest` (stage list JSON), `--non-interactive` (alias
  `--skip-setup`), `--skip-browser` (alias `--no-playwright`), `--skip-computer-use`,
  `--include-desktop`, `--verbose`. Env: `INDUS_REPO_URL`, `INDUS_INSTALL_CHANNEL`,
  `INDUS_INSTALL_DIR`, `INDUS_INSTALL_VERBOSE`, `INDUS_HOME`. Stages: prerequisites (git),
  repository (clone, stable tag resolution), venv/python bootstrap via a pinned `uv`, Python deps,
  products (launcher publish, shell PATH wiring, builds), desktop (opt-in), config (seed
  `config.yaml` from the example), setup (interactive wizard), gateway (configuration), complete
  (print reload-shell hint). One status line per step; child output to `<home>/logs/install.log`
  (streamed in CI, redirected output, or verbose); failures print last lines and the log path;
  an install receipt is written. Skip choices are remembered so later updates do not re-add them.
- **Dev setup script**: stage the pinned `uv` (sha256-verified from a lock file, mirror fallback on
  network curl exit codes 5/6/7/18/22/28/52/55/56), install pinned Python, run the internal
  package manager install, create `.env` from `.env.example` (0600) if missing, publish launchers
  to `~/.local/bin` (Windows: `<home>/bin`), print "source ./activate". Flags `--runtime-only`,
  `--test-environment[=…]`; unknown option exits 2.
- **Launcher**: POSIX `~/.local/bin/indus` wrapper; Windows copies in a managed bin dir outside the
  checkout; self-healed at startup.

### 3.28 Smaller commands [obs]
- `--version` / `/version`: banner label with version (derived from git release tags; build
  stamp file for packaged builds), release date, install directory, install method
  (stamp → managed → nix → git → pip), optional update availability.
- `completion [bash|zsh|fish]` prints a completion script generated from the live parser
  (default bash) [inf: includes profile names].
- `console`: curated "safe" command REPL exposing a subset of commands (not a shell); statuses
  ok/error/confirm_required/exit/clear.
- `pause [--reason]` / `resume`: write/remove an `ESTOP` sentinel in the home; new cron/board
  dispatch and new gateway turns halt at next check; in-flight work continues; `resume` says "not
  paused" if absent.
- `hooks`: inspect shell hooks declared in config; first-use consent allowlist at
  `<home>/shell-hooks-allowlist.json`; `test EVENT` fires matching hooks with a synthetic payload
  (`--for-tool`, `--payload-file` merged); `revoke CMD`; `doctor` (exec bit, allowlist, mtime
  drift, JSON validity, timing).
- `import-agent [claude-code|codex]`: map another coding agent's instructions files, permission
  allowlists, MCP servers, skills and memories; always preview; never imports credentials;
  `--sync` re-imports changed sources registered in `<home>/import-sync.json` without prompts.
- `claw migrate`: import settings, memories, skills (and allowlisted API keys only with
  `--migrate-secrets`) from an OpenClaw dir (default `~/.openclaw`), preview first, pre-migration
  zip unless `--no-backup`, refuse conflicts unless `--overwrite`; `cleanup` archives leftovers.

## 4. File map (what to read in the Read step)

Path aliases per §2. Line counts from `wc -l`.

| Sub-area | Reference path | Lines | Read what | Docs / tests to read with it |
|---|---|---|---|---|
| Top-level parser | `C/_parser.py` | 394 | all | `docs/reference/cli-commands.md` §Global |
| Main / dispatch / startup | `C/main.py` | 3602 | `_apply_profile_override`, `_resolve_continue_arg`, `_first_run_setup_guard`, `cmd_chat`, `_build_cli_parser`, `_parse_cli_args`, `main`; skip updater re-exports | `tests/C/test_argparse_flag_propagation.py` |
| Subcommand builders | `C/sub/*.py` (~60 files) | 4,990 total (`sessions` 337, `peer` 476, `gateway` 255, `computer_use` 205, `cron` 205, `plugins` 191, `skills` 177, `profile` 164) | all in-scope builders | `cli-commands.md` per command |
| Fast paths | `C/_startup_fast.py` | 260 | version printer, serve/chat fast checks | |
| Classic REPL | `cli.py` | 1793 | `process_command`, unregistered slash, quick commands, `main()` | `docs/user-guide/cli.md` |
| REPL mixins | `C/cli_*_mixin.py` + helpers | ~25,600 with gateway client | skim; read `cli_commands_mixin` (2416), `cli_single_query` (586), `cli_config_load` (318) | |
| Gateway chat client | `C/gateway_chat.py`, `gateway_agent.py`, `gateway_client.py`, `gateway_connection.py` | 397 / 1090 / 240 / 395 | refused flags, frozen policy, event replay | spec 06 |
| Slash registry | `C/commands.py` | 568 | all | `docs/reference/slash-commands.md` |
| Bang shell | `C/bang_shell.py` | 141 | all | `cli.md` §Shell Mode |
| Approval mode | `C/approval_mode.py` | 66 | all | |
| Config core | `C/config.py` | 4055 | env denylist; `_secure_*`; `ensure_*home`; load/save; set/get/unset; `.env` writers | `cli-commands.md` §config |
| Config defaults | `C/config_defaults.py` | 3156 | the default tree (dump keys; don't read linearly) and `OPTIONAL_ENV_VARS` (from line ~2742) | `cli-config.yaml.example` (2361) |
| Config helpers | `C/config_backups.py` 101, `managed_scope.py` 175, `config_migrations.py` 898, `config_version_stamp.py` 75, `config_section_guard.py` 61, `config_env_routing.py` 87, `config_effective.py` 116 | | all except migrations (skim step list) | |
| Env loading | `C/env_loader.py` | 833 | `load_*_dotenv` and secret-source parts | `docs/reference/environment-variables.md` |
| Home/paths | `consts.py` | 1467 | home resolution, profile predicates, display path, managed marker | `docs/user-guide/which-file-does-what.md` |
| Bootstrap | `boot.py` | 622 | skim | |
| Logging | `logsetup.py` | 1005 | rotation setup only | `cli-commands.md` §logs |
| Auth core | `C/auth.py` | 2490 | registry, store, locks, `resolve_provider`, status | `docs/user-guide/features/credential-pools.md` |
| Auth flows | `C/auth_device_flow.py` 452, `auth_codex.py` 1131, `auth_xai.py` 584, `auth_minimax.py` 363, `auth_qwen.py` 148, `auth_openrouter.py` 110, `auth_oauth_grants.py` 814, `auth_constants.py` 170, `auth_commands.py` 913, `copilot_auth.py` 511 | | flows + pool commands | `cli-commands.md` §auth |
| Portal auth (optional) | `C/auth_portal.py` 1680, `anon_auth.py` 1257, `C/portal_*.py` ~2,000 | | skip for MVP | |
| Credential pool | `agent/credential_pool.py` | 3097 | pool shape, strategies, exhaustion | 01 |
| Model picker | `C/main_provider_setup.py` 979, `model_setup_flows*.py` ~2,600, `models.py` 3002, `model_switch.py` 1898, `providers.py` 586, `runtime_provider.py` 1198 | | picker rows, flows, cache TTLs | `cli-commands.md` §model |
| Setup wizard | `C/setup.py` 765, `setup_quick.py` 405, `setup_terminal.py` 328, `setup_summary.py` 258, `setup_migration.py` 326, `setup_platforms.py` 327, `setup_tts.py` 273 | | flow + navigation | `cli-commands.md` §setup |
| Secret prompt | `C/secret_prompt.py` | — | masked input | |
| Tools config | `C/tools_config.py` 1137 (+`tools_config_*`) | | enable/disable, platform toolsets | |
| Profiles | `C/profiles.py` 2460, `profile_cmd.py` 636, `profile_*.py` | | naming, wrapper, create/clone, delete/tombstone | `docs/reference/profile-commands.md` (590), `user-guide/profiles.md` (465) |
| Backup/import | `C/backup.py` 2181, `backup_restore.py` 658 | | exclusions, run_backup, run_import, quick snapshots | `cli-commands.md` §backup/import |
| Checkpoints CLI | `C/checkpoints.py` | 221 | all | `user-guide/checkpoints-and-rollback.md` |
| Doctor | `C/doctor.py` 194 + `doctor_*.py` ~3,100 | | check order; individual checks as needed | |
| Update | `C/update_cmd.py` 1968 + `update*`/`_update*` (~20,600 total) | | phase dispatch only; treat rest as reference for edge cases | `getting-started/updating.md` (635), `developer-guide/cli-internals.md` |
| Uninstall | `C/uninstall.py` | 1211 | `run_uninstall`, `_perform_uninstall` | `updating.md` §Uninstall |
| Logs / dump / status / completion | `C/logs.py` 350, `dump.py` 344, `status.py` 424, `completion.py` 249 | | all | |
| One-shot | `C/oneshot.py` 665, `stream_json.py` 108 | | exit codes, usage report | `cli-commands.md` §-z |
| Installers | `scripts/install.sh` 1298, `scripts/install.ps1` 1670, `setup-X.sh` 306, `setup-X.ps1` 156 | | flags and stage list | `getting-started/installation.md` |
| Packaging | `pyproject.toml` 787, `setup.py` 99 | | `[project.scripts]`, python range | |
| Package map | `C/AGENTS.md` | 217 | all (architecture rules) | |

Giant files:
- `C/config.py` [4055]: the parts that matter are the env-name denylist (~78–228), home skeleton
  (~482–630), nested key helpers (~643–870), the load implementation (`_load_config_impl`), save
  (`save_config`), `.env` writers (`save_env_value`, `_quote_env_value`), and `set_config_value` /
  `get_config_value` / `unset_config_value` (~3406–3655). Platform-manifest env injection at the end
  is gateway territory.
- `C/config_defaults.py` [3156]: data only; extract with a script, not by reading.
- `C/main.py` [3602]: ~40% is re-exports kept for the frozen updater surface; ignore those.
- `C/models.py` [3002] and `C/model_switch.py` [1898]: only cache paths/TTLs and picker data are
  CLI concerns; resolution logic is shared with 01.
- `C/auth.py` [2490]: registry rows (~145–250), store/locks (~490–900), `resolve_provider`
  (~1629). Portal-specific token minting can be skipped.
- `C/backup.py` [2181]: exclusion constants at the top, `_run_backup_locked`, `run_import`, quick
  snapshot functions.
- Update family [~20,600 across ~45 files]: read the docs' phase list and `update_cmd.py`
  dispatch; Windows and fleet modules are edge-case archives.
- `C/kanban*`, `C/gateway*`, `C/web_server*`, `C/plugins*` (together >100k lines) are other areas.

## 5. Minimum viable subset

v1 needs:
1. Console script `indus` with argparse tree; bare = chat; global flags `-p`, `-m`, `--provider`,
   `-t`, `-r/--resume`, `-c`, `--yolo`, `--ignore-user-config`, `--ignore-rules`, `-z`,
   `--version`; unknown-command 3-line error.
2. Home resolution (`INDUS_HOME`, platform default), skeleton dirs, 0700/0600 perms, SOUL seed.
3. Profiles: id rule, `-p` pre-parse, `active_profile`, `profile list/use/create/delete/show`
   (clone of config/.env/SOUL; tombstone delete), wrapper alias.
4. Config: defaults tree (the subset the agent reads), deep merge, `${VAR}`/`${env:VAR}`
   expansion, round-trip comment-preserving atomic save with default stripping and fail-closed
   re-read, last-known-good fallback, version stamp + migration hook, `config
   show/get/set/unset/path/env-path/edit/check`, `set` routing (UPPER_SNAKE → `.env`) and value
   coercion.
5. `.env`: load precedence (home file overrides shell), writer with lock, denylist, quoting,
   atomic replace.
6. Auth: registry of API-key providers + OpenRouter + custom OpenAI-compatible; `auth.json` store
   with lock and corrupt handling; credential pool (`auth add/list/remove/reset`, fill_first);
   one OAuth device-code flow implementation (generic RFC 8628 poller) used by at least one
   provider; loopback PKCE helper.
7. `model` picker (provider → key → model list with cache) and a minimal `setup` (Full mode:
   model, terminal backend choice, tools) with non-TTY guidance and first-run guard.
8. Classic REPL hooks: slash registry + dispatch precedence (built-in → quick → skill → prefix),
   core commands (`help, new, clear, model, reasoning, yolo, approvals, tools, config, status,
   usage, title, resume, retry, undo, save, compress, stop, quit`), bang shell, quick commands.
9. One-shot `chat -q --oneshot` and `-z` with the documented exit codes; stream-json can wait.
10. `tools list/enable/disable`, `doctor` (config, env, auth, dirs, git/rg, connectivity),
    `logs`, `backup`/`import` (full zip with SQLite backup API and CRC pre-check), `checkpoints
    status/prune/clear`, `uninstall` (keep-data/full), a simple `update` (git fetch + tag/branch,
    syntax check + rollback, dependency sync, config migrate).

Can wait: gateway-client chat mode (decide in planning; see trap R04.1), TUI hand-off, desktop
and dashboard commands, multi-profile gateway concerns, admin overlay (`/etc/indus`), external
secret managers and vault, portal free tier and its account commands, release channels and
fleet restarts, Windows venv-holder logic, `approvals suggest`, `import-agent`/`claw`,
`security audit`, `debug share`, `usage`, `prompt-size`, telemetry consent, profile
distributions/export, setup Quick/Blank-Slate modes, back-navigation in wizards.

## 6. Hard parts / traps

1. **R04.1 Classic chat is a gateway client.** In the reference the REPL never runs the agent;
   it ensures a gateway, freezes launch policy into the session, and refuses some flags (exit 2).
   Copying that requires spec 06 to exist first. Decide in planning whether v1 runs in-process
   (simpler) behind an interface that a later gateway client can replace; keep the frozen-policy
   and "resume may repeat but not change creation flags" rule either way.
2. **R04.2 Profile must be resolved before any import.** Any module that reads the home at import
   time freezes the wrong profile. The `-p` scan must skip flag values (derived from the live
   parser) and nested argument tails, strip the tokens, and set `INDUS_HOME` before importing app
   code. Long-running multi-profile servers need a context-local override instead of env.
3. **R04.3 Fail-closed config writes.** Save must re-read the raw file and refuse on read errors;
   treating an unreadable file as `{}` lets the default-stripping pass delete user sections. Also
   refuse to save the "failed read" fallback object.
4. **R04.4 Never write defaults.** Save strips values equal to defaults unless the user had that
   path; otherwise later default changes are masked. Seeding a new config must use the commented
   example, not the full default tree (the gateway reads presence, not merged defaults).
5. **R04.5 Comment-preserving round trip, single writer seam.** Users hand-edit `config.yaml`;
   every writer must go through one round-trip YAML path with atomic replace; lint that nothing
   else dumps YAML to a config path.
6. **R04.6 Last-known-good on broken YAML.** A mid-edit syntax error must not silently drop
   approval deny rules or allowlists; serve the last good parse (memory, then newest good backup).
7. **R04.7 `config set` type coercion.** String-typed schema slots keep the literal (`off` stays
   `"off"`, not false); list/mapping slots refuse scalars; malformed structured literals refuse
   rather than store a string nobody reads. UPPER_SNAKE names never go to YAML.
8. **R04.8 Dotted key names.** Model ids contain dots; navigation must prefer existing literal
   keys and require `\.` escaping to create one; refuse writes that create a phantom sibling.
9. **R04.9 Provider switch leaves a stale route.** Changing `model.provider` without dropping the
   old `base_url`/`api_mode` posts the new key to the old endpoint. Same for a stale
   `OPENAI_BASE_URL` in `.env` after leaving a custom endpoint.
10. **R04.10 `.env` read/write symmetry.** Writers must recognise every line shape the reader
    accepts (`export K=`, `K = v`) or saves duplicate and removes resurrect. Whole RMW under a
    cross-process lock; atomic replace; preserve an existing file's mode.
11. **R04.11 `.env` precedence.** Home `.env` overrides inherited shell vars (stale exports),
    keys removed from the file must be cleared from env, but a service manager's token for the
    1Password bootstrap must win; terminal settings must be re-asserted from config after every
    reload.
12. **R04.12 Env writer denylist.** Any UI that writes env vars is an escalation path
    (`LD_PRELOAD`, `PATH`, `EDITOR`, git helpers, app policy flags like yolo). Enforce on write;
    compare case-insensitively on Windows.
13. **R04.13 Redaction bridge timing.** The redaction flag is snapshotted once at import; bridge
    `security.redact_secrets` into env before that import, or redaction ignores config.
14. **R04.14 `--yolo` timing.** The approval module freezes yolo state at import; set the env var
    before plugin discovery/tool import, not inside the chat handler.
15. **R04.15 Masking in `config get`.** Output bypasses log redaction and is captured in agent
    transcripts; mask credential-shaped values by default, never mask `${VAR}` placeholders.
16. **R04.16 Auth store read errors.** OS read errors must raise and leave the file; only genuine
    parse corruption may start empty (after preserving a copy). Never let tests touch the real
    user store.
17. **R04.17 Device-code polling edge cases.** Handle `slow_down` (+1 s, cap 30), edge/WAF 403/429
    /5xx with Retry-After (cap 60), and report outage vs "not approved" correctly at the deadline.
18. **R04.18 Browser opening on headless/remote hosts.** `webbrowser.open` may launch a text
    browser that takes over the terminal; detect remote sessions and console browsers; print an
    SSH tunnel hint for loopback flows; bind the callback port before building the redirect URI.
19. **R04.19 Non-TTY behaviour.** Wizards, first-run prompt, TUI selection and expensive-model
    confirmation must all degrade deterministically without a TTY (guidance + exit 1, classic
    REPL, stderr refusal). A TUI launched without a TTY exits 0 and silently breaks callers.
20. **R04.20 Two different exit-code tables.** `chat -q` (0/1/130/75/3) and `-z` (0/2/130/1) are
    intentionally different; scripts depend on them. `-z` also needs a hard exit after flush to
    avoid native finalizer aborts changing the code.
21. **R04.21 Subcommand routing with optional-value flags.** `-c [NAME]` breaks argparse routing
    on older Pythons; need the speculative-parse workaround and multi-word name coalescing; the
    chat subparser must use suppress defaults so pre-subcommand values survive.
22. **R04.22 Import target.** Restore into the current home, not the root; CRC-check every member
    before writing; write DB pages into the live file rather than renaming (open connections keep
    the old inode); skip source-machine runtime files.
23. **R04.23 Backup exclusions are security-relevant.** Browser profile dirs contain cookie and
    login databases and must never enter an archive; SQLite sidecars must be excluded and DBs
    copied with the online backup API.
24. **R04.24 Update self-modification.** The running updater imports code that is being replaced;
    keep a stable "old updater" import surface, never run pulled code in the old interpreter, run
    a post-pull syntax check with automatic rollback, and treat post-commit failures as owed
    follow-ups (exit 0).
25. **R04.25 Profile deletion and marker-less dirs.** Never recreate a deleted named profile by a
    stray mkdir; tombstones live beside the profile dir; directories without identity markers are
    not profiles and must not be served or `rmtree`d.
26. **R04.26 Clone must not share channels.** Copying messaging bot tokens to a second profile
    causes two consumers on one token; clone strips channel credentials by default and never
    writes through symlinked config/env.
27. **R04.27 Bang shell is not a bypass.** It runs on the host (not the sandbox backend), so it
    must pass the same approval guards and use a sanitised environment; fail closed if the
    sanitiser is missing.
28. **R04.28 Quick commands hold secrets.** They run with `shell=True`; strip provider keys from
    the env and redact output before printing.

## 7. Open questions

1. Does the shipped build's classic `chat` actually require a running gateway, and what does the
   user see when the gateway cannot start (timing, messages)? Needed: needs REA (run
   `chat -q` on a fresh install with the gateway stopped).
2. Exact text and layout of the first-run banner, setup wizard screens, curses pickers, and the
   status bar in a real terminal. Needed: needs REA (terminal capture of the shipped build).
3. Default model on a fresh install: the default tree has `model: ""` but the example file seeds
   a specific model with provider `auto` and an OpenRouter base URL. Which wins after the
   installer's config stage? Needed: needs REA (fresh install, read `config.yaml`).
4. Exact `--version` output format (calver vs semver, "upstream sha" suffix, update notice).
   Needed: needs REA, plus product decision for the rebuild's versioning.
5. Completion script content (does it include profile names and dynamic skill/session names?).
   Needed: read `C/completion.py` fully, or needs REA.
6. Masked value format for `config get` (first/last 4 vs other) and for `dump --show-keys`.
   Needed: read the masking helper in the agent redaction module (area 01/02) or needs REA.
7. Which `update` phases are worth rebuilding in v1 (release channels, fleet restart, receipts)?
   Needed: product decision by the lead.
8. Whether the rebuild ships a first-party portal provider at all (device-code OAuth, free tier,
   `auth upgrade`, `/topup`, `/subscription`). Needed: product decision.
9. Whether to keep argparse or adopt another CLI framework; argparse-specific workarounds
   (§3.2.3) disappear with a different parser. Needed: planning decision.
10. Windows parity scope (launcher copies, venv-holder guard, msvcrt locks, `%LOCALAPPDATA%`
    home). Needed: product decision; behaviour needs REA on Windows.
11. Exact shell-rc lines the installer adds and that uninstall removes. Needed: read
    `scripts/install.sh` `wire_shell_path` and uninstall `remove_path_from_shell_configs`.
12. Exact JSON shape of `uninstall --gui-summary`, `update --plan`, and install receipts. Needed:
    read source or needs REA.

## 8. What to read later

- Reference docs: `website/docs/reference/cli-commands.md` (2060), `slash-commands.md` (340),
  `environment-variables.md` (981), `profile-commands.md` (590), `faq.md` (backup vs export);
  `getting-started/installation.md`, `updating.md`, `quickstart.md`, `platform-support.md`;
  `user-guide/cli.md`, `configuration.md` (3121, per-key reference), `profiles.md`,
  `managed-scope.md`, `security.md` (approval and file write safety), `secrets/`,
  `checkpoints-and-rollback.md`, `which-file-does-what.md`;
  `developer-guide/cli-internals.md` (update pipeline, free-tier sign-in).
- Reference tests that list edge cases (names under `tests/<C>/`): argparse flag propagation,
  top-level value-flag parity, config YAML comment preservation, config set coercion/section
  guard, env denylist, auth store corruption, device-flow polling, profile name validation and
  tombstones, backup exclusions and import CRC, bang shell, approvals test exit codes. Locate
  with `rg -l` on the function names in §4.
- REA Evidence: none yet (REA not connected). Planned bundle `~/rea-lab/evidence/04-cli-config/`
  covering open questions 1–4, 10, 12.
- Related specs: `01-agent-loop.md` (provider resolution, credential pool runtime, reasoning,
  fallback chain), `02-tools.md` (approval guards, checkpoint store, terminal backends, sanitised
  subprocess env), `03-state-db.md` (sessions commands' storage), `05-tui.md` (TUI launch,
  skins, status bar rendering), `06-gateway.md` (gateway ensure, chat client protocol, service
  install, multiplex), `07-skills-cron.md` (skill slash commands, cron/curator CLIs),
  `08-desktop.md` (dashboard/serve/desktop commands).
