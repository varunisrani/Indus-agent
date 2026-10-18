# 10 — Research tooling, evals, packaging, CI, docs site, i18n: behavioural specification (clean-room)

Scope: offline trajectory generation for training data (batch runner, toolset distributions, the
single-tool SWE runner), trajectory compression, the dataset sampling helper, the eval harness
folder, the package manager/installer helper (PM) as a distribution mechanism, every install and
distribution channel (POSIX shell installer, PowerShell installer, Windows cmd shim, Termux APT,
Docker/Compose, Nix flake and modules, MSIX/Store, desktop bundles as release artefacts), the CI
pipelines and their gates, the release process (stable, canary, commit/one-off, dynamic channels),
lint/format/hook configuration, Python dependencies and extras, the lockfile, the locale catalogues
(as a packaged resource), the docs website (structure only), the contributor-mapping folder, and
the test strategy (layout, counts, fixtures, markers, runners).
Out of scope: runtime product behaviour — the agent loop, tools, gateway, state DB, CLI/TUI,
desktop app, updater runtime, i18n lookup at runtime (covered by `01`…`09`). Where this area only
*packages* or *tests* a runtime part, this file says so and points away.

Reference: `<ref-org>/<ref-repo>` @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056` (MIT).
Shipped build checked: none — REA was not connected in this session. Every claim below is from
source reading; anything only a shipped build can settle is in §7 marked "needs REA".
Sources studied: root research scripts, `evals/` (all READMEs, harness CLIs), `pm/` (facade,
lock shape, CLI surface, policy doc), `scripts/` (installers, release tooling, CI helpers, Termux,
MSIX, code-health), `.github/` (all 57 workflows summarised, 11 composite actions, gate scripts),
`Dockerfile`, compose files, `docker/`, `flake.nix`, `nix/`, `pyproject.toml`, `uv.lock` (header and
counts only), `ruff*.toml`, `lefthook.yml`, `justfile`, `locales/`, `website/` (tree and config),
`contributors/`, `tests/` (tree counts, root conftest fixture list, test-writing guide).

Conventions used below:
- "Trajectory" = one recorded agent conversation turned into a training sample.
- "Turn" = one `{from, value}` element of a trajectory.
- "PM" = the rebuild's own package/environment manager (the reference has one under `pm/`).
- "Ladder" = the ordered list of installer stages run when no single stage is requested.
- "Lane" = a named, independently switchable slice of CI or release work.
- "Receipt" = an immutable record (file or annotated tag) written only after something succeeded.
- `<ref>` inside a path stands for the reference product's name (see §2); the rebuild uses `indus`.
- Rebuild names: CLI `indus`, env prefix `INDUS_`, home `~/.indus`.
- "MUST" marks an invariant the reference defends with code, a comment about a past bug, or a test.

---

## 1. Overview

This area is everything *around* the product: how training data is mass-produced from the agent,
how that data is squeezed into a token budget, how ad-hoc A/B evals are organised, how the product
is pinned, built, installed, updated as an artefact, tested and released, and how its docs site and
translation catalogues are laid out.

Parts and who calls whom:

```
                 RESEARCH / DATA                                   SHIP
 ┌──────────────────────────────┐            ┌───────────────────────────────────────────┐
 │ dataset.jsonl {prompt,...}   │            │ pyproject + uv.lock  ──►  PM lock.json     │
 │        │                     │            │      (python deps)       (tool binaries)   │
 │        ▼                     │            │            │                 │             │
 │ batch runner ──► agent core  │            │            ▼                 ▼             │
 │  (process pool,  (one per    │            │   install.sh / install.ps1 ──► PM install  │
 │   checkpoints)    prompt)    │            │   Termux .deb (APT)  Docker image  Nix     │
 │        │                     │            │   MSIX / DMG bundles (release artefacts)   │
 │        ▼                     │            └───────────────────────────────────────────┘
 │ batch_N.jsonl ─► trajectories│                         ▲
 │        │          .jsonl     │            ┌────────────┴──────────────────────────────┐
 │        ▼                     │            │ CI: detect lanes ─► reusable workflows ─►   │
 │ trajectory compressor        │            │     one aggregate gate (all-checks-pass)   │
 │  (tokenizer + summariser LLM)│            │ Release: attempt ref ─► lanes ─► gate ─►   │
 │        │                     │            │     final tag receipt ─► channel heads     │
 │        ▼                     │            └───────────────────────────────────────────┘
 │ *_compressed.jsonl + metrics │
 └──────────────────────────────┘     evals/: standalone A/B harnesses, resume-safe, not in CI
```

What a user of this area sees:
- A researcher runs a batch CLI over a JSONL of prompts, gets one merged `trajectories.jsonl`, a
  `statistics.json`, per-batch files and a checkpoint; can resume after a crash without re-paying
  for finished prompts. Then runs the compressor to fit each trajectory under ~15k tokens.
- An end user runs a one-line installer (shell or PowerShell), a Docker image, a Nix flake, a
  Termux APT package, or a signed desktop package.
- A contributor runs one test command that mirrors CI, one lint command that mirrors CI, and gets
  a PR checked by a change-classifier that decides which suites run, then one aggregate gate.
- A maintainer cuts releases through a script that pushes lock refs, dispatches a gated pipeline,
  and publishes only receipt-backed bytes.

What the system guarantees (as defended by the reference):
- An acknowledged batch result is durable (appended and fsynced) before it counts as done. [obs]
- Resume never re-runs a prompt that already has a row, including deliberately discarded rows. [obs]
- Compression never splits a tool-call turn from its tool-response turn. [obs]
- Every downloaded bootstrap binary is sha256-verified against an in-repo pin. [obs]
- Every direct dependency is pinned or bounded; a new unbounded PyPI spec fails CI. [obs]
- A release publishes only the bytes that were tested; aliases move only after the receipt. [obs]

## 2. Reference names (rename in rebuild)

| Kind | Names (reference only; they become banned identifiers) |
|---|---|
| Product / org | `Hermes`, `Hermes Agent`, `hermes-agent`, `Hermes-Agent`, `Nous Research`, `NousResearch`, `nousresearch`, `Nous Portal`, `Hermes Desktop`, `Hermes.exe`, `Hermes-Setup.dmg`, the caduceus glyph `☤` used as a banner mark, the term "Hermes format" for the trajectory/tool-call XML convention |
| Binary / CLI and flags | `hermes` (main CLI), `hermes-agent` (legacy CLI script), `hermes-acp`, `hermes-canary` (canary app command), `hermes-<7-char-sha>` (one-off build command), `hermes pm …`, `hermes update --set-channel`, `hermes setup`, `hermes gateway install --if-missing`, PowerShell params `-HermesHome`, installer flag `--hermes-home` |
| Env vars | `HERMES_HOME`, `HERMES_INSTALL_CHANNEL`, `HERMES_INSTALL_DIR`, `HERMES_REPO_URL`, `HERMES_RUNTIME_DIR`, `HERMES_PYTHON`, `HERMES_INSTALL_VERBOSE`, `HERMES_TEST_WORKERS`, `HERMES_TEST_PATHS`, `HERMES_TEST_FILE_TIMEOUT`, `HERMES_TEST_FILE_RETRIES`, `HERMES_TEST_IMAGE`, `HERMES_NIX_BUILD`, `HERMES_UID`, `HERMES_GID`, `HERMES_LANGUAGE`, `HERMES_WEB_DIST`, `HERMES_TUI_DIR`, `HERMES_WRITE_SAFE_ROOT`, `HERMES_DASHBOARD_HOST`, `HERMES_BOT_DESKTOP`, `HERMES_DESKTOP_VARIANT`, `HERMES_LIVE_USAGE_FILE`, `__HERMES_ACTIVATED`, `__HERMES_TEST_PYTHON`, `__HERMES_TESTS_REEXEC` |
| Files and folders | `~/.hermes`, `%LOCALAPPDATA%\hermes`, `<home>/hermes-agent` (default checkout), `.hermes/bin/hermes` (published launcher inside checkout), `.hermes-bootstrap-complete`, `.hermes-clone-XXXXXX`, `hermes-uv-bootstrap.*`, `hermes-git-bootstrap-<pid>`, `refs/hermes-update-backups/…`, stash label `hermes-install-autostash-<stamp>`, `setup-hermes.sh`, `setup-hermes.ps1`, root launcher file `hermes`, `hermes_bootstrap.py`, `hermes_constants*.py`, `hermes_state_wal.py`, `hermes_yaml.py`, `hermes_cli/`, `hermes_platform/`, `docker/hermes-exec-shim.sh`, `scripts/hermes-gateway`, `scripts/run-in-hermes-env`, `nix/hermes-agent.nix`, `SOUL.md` (persona file), `pytest_live_guard.py` under the home dir, `/opt/hermes`, `/opt/data` as the image home, Unix user/group `hermes` (build UID 10000) |
| HTTP routes / URL schemes / hosts | `hermes-agent.nousresearch.com` (site, docs at `/docs/`, `install.ps1`), `hermes-assets.nousresearch.com` (asset mirror, `upstream/sha256/<hex>`, `releases/{stable,canary,commit/<sha>,tag/<tag>}/index.html`, `releases/termux/stable/key.asc`), `github.com/NousResearch/hermes-agent`, Docker image `nousresearch/hermes-agent` (tags `:main`, `:main-desktop`, `:latest`, `:stable`, `:test`), `discord.gg/NousResearch`, HF datasets `NousResearch/swe-terminus-agent-glm-kimi-minimax`, `NousResearch/hermes-agent-megascience-sft1`, `NousResearch/Hermes-Agent-Thinking-GLM-4.7-SFT1`/`SFT2`, `NousResearch/terminal-tasks-glm-hermes-agent`, git fork `NousResearch/misaki`, npm alias `@nous-research/image-size` |
| RPC methods / events / commands | Nix module `services.hermes-agent`, nix packages `.#pm-<tool>`, s6 service `main-hermes`, compose services `hermes` / `hermes-dashboard`, APT suites `hermes-stable` / `hermes-canary`, APT package `hermes-agent`, keyring `hermes-agent.asc`, PM runtime project `hermes-pm-runtime`, git hook mark "hermes-agent scripts/check hook" |
| Settings and storage keys | `[tool.hermes]` (`opt-in-extras`), `[tool.hermes.extras-platforms]`, config path `update.installs.<id16>.channel`, `display.language`, receipt JSON field `installer: install_sh` |
| Other (scopes, error tags, IPC channels) | tag `<REASONING_SCRATCHPAD>` (reference-specific in-content reasoning tag), completion sentinel `MINI_SWE_AGENT_FINAL_OUTPUT`, log prefix `[hermes]`, platform label "batch" is neutral; package-name `hermes-agent[...]` in self-referencing extras; persona/maintainer names in comments; email files under `contributors/emails/` (personal data — never copy) |

`<ref>` in any path elsewhere in this file stands for the reference product token (`hermes`).

Third-party protocol and format names used by this area (keep exact; check against vendor docs):
ShareGPT `conversations` with `from` ∈ `system|human|gpt|tool` and `value`; XML-tagged tool calling
tags `<tools>`, `<tool_call>`, `<tool_response>`, `<think>` (the open function-calling convention
used by several open-weight models); OpenAI Chat Completions (`choices[0].finish_reason`,
`tool_calls`, `role: tool`, `tool_call_id`); OpenRouter provider-routing options
(`providers_allowed`/`ignored`/`order`, `sort` ∈ price|throughput|latency); Hugging Face
`AutoTokenizer.from_pretrained(trust_remote_code)`, `datasets.load_dataset(split="train")`;
PEP 517/621/735 (dependency groups), PEP 508 markers, PEP 738 Android platform tags
(`android_24_arm64_v8a`), PEP 615 tzdata; uv (`exclude-newer`, `override-dependencies`,
`environments`), Debian/APT repository layout (`dists/<suite>/{Packages,Packages.gz,Release,InRelease,Release.gpg}`,
`pool/`), dpkg version ordering with `~`; s6-overlay (`/init`, `cont-init.d`, `s6-rc.d`,
`s6-setuidgid`, `with-contenv`); Docker buildx/imagetools, OCI manifests; Nix flakes, flake-parts,
uv2nix, pyproject.nix, home-manager, NixOS modules; MSIX, `.msixbundle`, App Installer `.appinstaller`;
GitHub Actions (`workflow_call`, `workflow_run`, `needs`, `concurrency`, `GITHUB_OUTPUT`);
Docusaurus 3 i18n (`i18n/<locale>/`); Algolia DocSearch; `llms.txt`; Cloudflare R2 (S3 API, ETag
conditional writes); OSV-Scanner; hadolint rule ids (DL3002, DL3003, DL3008, DL3059); ruff rule ids.

## 3. Behaviour

### 3.1 Batch trajectory generation (batch runner)

- **Trigger / inputs:** a researcher runs a script-style CLI (flags as `--name=value`, built on a
  "fire"-style auto-CLI). [obs] Required: dataset file, batch size (≥1), run name. Optional:
  distribution name (default `default`), model, API key, base URL (default OpenRouter's v1 URL),
  max turns (default 10), workers (default 4), `resume`, `verbose`, `list_distributions`,
  ephemeral system prompt, log preview chars (default 100), OpenRouter routing lists (comma
  strings split and trimmed), provider sort, reasoning effort, reasoning-disabled switch, prefill
  messages file, max samples. [obs]
- **Dataset shape:** JSONL; each kept line is an object with a `prompt` key; optional `image` or
  `docker_image` (per-prompt container image) and `cwd`. Blank lines skipped; invalid JSON and
  lines without `prompt` are warned about and skipped; an empty result is a fatal error; a missing
  file is fatal. [obs] `max_samples` truncates to the first N rows. [obs]
- **Reasoning switch resolution:** disabled wins → `{effort: none}`; else an explicit effort must
  be one of `none, minimal, low, medium, high, xhigh, max, ultra` → `{enabled: true, effort}`;
  else nothing is sent (the agent's own default applies). [obs] The help text claims the default is
  "medium" but no value is sent when unset — see R10.4. [obs]
- **Prefill file:** must parse as a JSON array, else exit 1. Prefill is injected at request time
  only; it is never written into the trajectory. [obs] Some newer Anthropic models reject a
  trailing assistant-role prefill with HTTP 400; use user-role priming for them. [obs]
- **Flow:**
  1. Validate the distribution name against the table (§3.2); unknown → error listing names. [obs]
  2. Create `data/<run_name>/` (relative to CWD). Paths: `checkpoint.json`, `statistics.json`,
     `batch_<N>.jsonl`, `trajectories.jsonl`. [obs]
  3. Split `(original_index, row)` pairs into consecutive chunks of `batch_size`. [obs]
  4. If `resume`: scan every `batch_*.jsonl`, collect the human prompt text of each row (flat
     `prompt`, or the first `human`/`user` turn of `conversations`/`messages`, using `value` or
     `content`), and drop dataset rows whose trimmed prompt text is in that set; re-chunk the rest
     (original indices preserved). Nothing left → print "all done" and return. [obs]
  5. Load the checkpoint; if its `run_name` differs, start an empty one. [obs] The checkpoint's
     `completed_prompts` (original indices) is then used to skip prompts **even without
     `resume`** — see R10.3. [obs]
  6. Build a picklable worker config. If the API key is a callable credential provider (e.g. a
     cloud identity bearer-token callback), drop it and let each worker rebuild credentials from
     config. [obs]
  7. Start a process pool of `num_workers`; each task = one whole batch; results arrive unordered.
     A progress bar (batches done / total, ETA) is drawn; root log level is lowered to WARNING for
     the duration so logs do not tear the bar. [obs]
  8. Worker, per batch, per prompt not already completed (sequential inside the batch):
     a. If the row names a container image and the sandbox backend env says Docker: check the image
        locally (10 s timeout), pull if absent (600 s timeout); a failed pull produces a failure
        result **before any model tokens are spent**. A missing Docker CLI is ignored (cloud
        sandbox backends pull server-side). Register per-task sandbox overrides: same image for
        docker/modal/daytona, `docker://<image>` for singularity, plus `cwd` when given. [obs]
     b. Sample toolsets from the distribution (§3.2). [obs]
     c. Build a fresh agent with: model, max iterations, sampled toolsets, its own trajectory saving
        **off**, context files **skipped** (persona/agents files would pollute samples), persistent
        memory **off**, log prefix `[B<batch>:P<index>]`, the pass-through fields (base URL, key,
        ephemeral system prompt, routing, reasoning, prefill) and execution-surface label `batch`
        (used for telemetry attribution). [obs]
     d. Run one conversation with task id `task_<index>` (each task gets an isolated sandbox). [obs]
     e. Compute tool stats and reasoning stats from the raw messages **before** conversion. [obs]
     f. Convert to the trajectory format (§3.3). Always close the agent afterwards, even on error
        (one agent per prompt otherwise leaks terminals/VMs/HTTP clients for the whole run). [obs]
     g. If the run produced a trajectory but **no assistant turn had any reasoning**, discard it:
        append a tombstone row `{prompt_index, discarded: "no_reasoning", prompt}` and count it as
        completed. [obs]
     h. Otherwise append the result row (§ shapes) to `batch_<N>.jsonl` and fsync. Only then mark
        the index completed. Failed prompts write nothing and are retried on the next run. [obs]
  9. Parent, for each finished batch: merge its completed indices into the set, record per-batch
     `{processed, skipped, discarded_no_reasoning}` under `batch_stats[<N>]`, atomically rewrite the
     checkpoint (stamped `last_updated`). A checkpoint write error only warns. [obs]
  10. Ctrl-C or a worker exception → terminate and join the pool, re-raise. [obs]
  11. After the pool: final checkpoint (best effort), per-tool success/failure rates (percent, 2 dp,
      0.0 when no calls), merge **all** `batch_*.jsonl` (old runs included) into
      `trajectories.jsonl`, dropping tombstones, invalid JSON lines, non-objects, and rows whose
      `tool_stats` names a tool not in the registry (a hallucinated tool name marks the sample as
      corrupt). Write `statistics.json`; print a summary table (tools sorted by call count) and
      reasoning coverage. [obs]
- **Tool-call success judgement (for stats only):** a tool response is a failure if it parses as a
  JSON object with a non-null `error`, or a nested `content` object with a non-null `error`, or
  `success: false`. Non-JSON text fails only when empty or when it begins with `error:`
  (case-insensitive). A non-zero exit code is **not** a failure (the model is expected to self-
  correct). Non-object JSON is success. [obs]
- **Reasoning detection:** an assistant message "has reasoning" if its content contains the
  reference's scratchpad open tag, or it carries a non-blank native `reasoning` field. [obs]
- **Data shapes:**
  - `DatasetRow { prompt, image?, docker_image?, cwd?, …ignored }`
  - `BatchResultRow { prompt_index, conversations: Turn[], metadata: { batch_num, timestamp, model }, completed: bool, partial: bool, api_calls: int, toolsets_used: string[], tool_stats: map<tool, {count, success, failure}>, tool_error_counts: map<tool, int> }`
  - `TombstoneRow { prompt_index, discarded: "no_reasoning", prompt }`
  - `Checkpoint { run_name, completed_prompts: int[] (sorted), batch_stats: map<string(batch), {processed, skipped, discarded_no_reasoning}>, last_updated: iso8601|null }`
  - `RunStatistics { run_name, distribution, total_prompts, total_batches, batch_size, model, completed_at, duration_seconds, tool_statistics: map<tool,{count,success,failure,success_rate,failure_rate}>, reasoning_statistics: {total_assistant_turns, turns_with_reasoning, turns_without_reasoning}, discarded_no_reasoning }`
  - `tool_stats` and `tool_error_counts` are **normalised**: every registered tool appears with zero
    counts, plus any unexpected names — so every row has the same columns for columnar (Arrow/
    Parquet) dataset export. [obs]
- **Limits and timings:** image probe 10 s; image pull 600 s; error preview of pull stderr 500 chars;
  ephemeral prompt preview 60 chars; invalid-tool preview 50 chars. [obs]
- **Errors and recovery:** per-prompt exceptions become failure results (printed, traceback when
  verbose); resume re-runs them. Unreadable checkpoint → empty checkpoint with a warning. [obs]
- **Defaults conflict:** CLI default model is a Sonnet-class OpenRouter slug while the class
  default is a dated Opus id and the docstring names yet another — the CLI value is what runs. [obs]
- **Evidence:** `batch_runner.py`; `tests/test_batch_runner_checkpoint.py`,
  `tests/test_batch_runner_discard_resume.py`, `tests/test_batch_runner_durability.py`.

### 3.2 Toolset distributions

- **What it is:** a named table mapping toolset entries to an independent inclusion probability
  in percent. Each entry is rolled separately, so several toolsets can be active. An entry may be a
  `+`-joined group that is rolled **once** for all members (keeps co-occurrence exact; two separate
  97 % rolls co-occur only ~94 % of the time). [obs]
- **Flow:** for each entry: if any member is not a valid toolset → warn and skip; else include all
  members when `random()*100 < p`. If nothing was selected, fall back to the single highest-
  probability entry (if valid). Unknown distribution → error. [obs]
- **The 17 distributions (exact percentages)** [obs]:

| name | entries |
|---|---|
| default | web 100, vision 100, image_gen 100, terminal 100, file 100, browser 100 |
| image_gen | image_gen 90, vision 90, web 55, terminal 45 |
| research | web 90, browser 70, vision 50, terminal 10 |
| science | web 94, terminal 94, file 94, vision 65, browser 50, image_gen 15 |
| development | terminal 80, file 80, web 30, vision 10 |
| safe | web 80, browser 70, vision 60, image_gen 60 (no terminal) |
| balanced | web/vision/image_gen/terminal/file/browser all 50 |
| minimal | web 100 |
| terminal_only | terminal 100, file 100 |
| terminal_web | terminal 100, file 100, web 100 |
| creative | image_gen 90, vision 90, web 30 |
| reasoning | web 90, file 60, terminal 20 |
| browser_use | browser 100, web 80, vision 70 |
| browser_only | browser 100 |
| browser_tasks | browser+search 97 (grouped), vision 12, terminal 15 |
| terminal_tasks | terminal 97, file 97, web 97, browser 75, vision 50, image_gen 10 |
| mixed_tasks | browser 92, terminal 92, file 92, web 35, vision 15, image_gen 15 |

- **`--list_distributions`** prints each with description and probabilities sorted high→low. [obs]
- Toolset names are owned by the runtime toolset registry (area `0x` tools); this table only refers
  to them. [inf]

### 3.3 Trajectory format (training sample)

- **Shape:** `Trajectory = Turn[]`, `Turn { from: system|human|gpt|tool, value: string }`. [obs]
- **Conversion rules** (from the agent's chat-format message list) [obs]:
  1. Turn 0 `system`: a fixed function-calling preamble that embeds the JSON list of available tool
     schemas inside `<tools>…</tools>` and explains the `<tool_call>` / `<tool_response>` convention.
     The rebuild writes its own preamble text; only the tags are protocol.
  2. Turn 1 `human`: the original user query (the dataset prompt).
  3. The agent's message 0 is skipped (already emitted as turn 1). Prefill messages never appear.
  4. Assistant message **with** tool calls → one `gpt` turn: optional native reasoning wrapped as a
     `<think>` block, then the message text (scratchpad tag rewritten to `<think>`), then one
     `<tool_call>` block per call containing compact JSON `{name, arguments}` where arguments are
     parsed back to an object (unparseable → `{}` with a warning). If no `<think>` occurs, an empty
     `<think>` block is prepended so **every gpt turn starts with a think block**. Trailing
     whitespace trimmed.
  5. The run of consecutive `tool` messages that follows → **one** `tool` turn whose value joins
     one `<tool_response>` block per message with newlines. Each block holds JSON
     `{tool_call_id, name, content}`; `content` is parsed to an object when it looks like JSON
     (starts with `{` or `[`); `name` is taken **by position** from the preceding call list,
     `unknown` if there are more responses than calls (see R10.6).
  6. Assistant message **without** tool calls → `gpt` turn with think prefix + content, guaranteed
     think block, stripped.
  7. Additional `user` messages → `human` turns.
  8. Image-bearing tool messages are replaced by their text summary first, so base64 payloads
     (~1 MB) never enter a trajectory.
- **Interactive saver** (not batch): appends `{conversations, timestamp, model, completed}` to
  `trajectory_samples.jsonl` (completed) or `failed_trajectories.jsonl` (not), in CWD, under an
  exclusive whole-file lock (POSIX `flock`; Windows 1-byte range lock at offset 0) around
  write+flush because gateway sessions and batch workers may append to the same file. [obs]

### 3.4 Single-tool SWE runner

- **Purpose:** a minimal tool-calling loop with only a `terminal` tool, run against a local,
  Docker or Modal execution environment, producing trajectories in the same format so the
  compressor and dataset tooling accept them. [obs]
- **Inputs:** `--task` (single) or `--prompts_file` (JSONL; each line `{prompt}` or `{task}`, or a raw
  text line), `--output_file` (default a fixed test filename), model, base URL, key, env
  `local|docker|modal`, image (default a slim Python 3.11 image), cwd, max iterations (default 15),
  command timeout (default 60 s). [obs]
- **Flow:** create the environment per task (local cwd = host temp dir; container cwd = `/tmp`
  inside the container); loop up to max iterations: one chat-completions call (300 s request
  timeout) with an internal system prompt and the single tool; no tool calls → finished
  (completed); otherwise execute each call's `command` (missing → a harmless echo), with per-call
  timeout from args or the default; if any output contains the completion sentinel string, stop as
  completed. Environment cleaned up in `finally`. API error → loop ends (not completed). [obs]
- **Tool result shape:** `{content: {output, exit_code, error|null}}` serialised as the tool message
  content. [obs]
- **Client selection:** explicit key or base URL → direct OpenAI-compatible client (key falls back
  through three provider env vars); else the shared provider router (OpenRouter first, then
  "auto"), else a bare OpenRouter client. Temperature: only fixed per-model contracts are applied
  (some models require omitting it, some force a value). [obs]
- **Differences from the batch format** (R10.7): the think block is emitted **without** newlines
  and only when reasoning exists (no empty think padding); `run_batch` opens the output file in
  **write** mode (truncates) while the batch runner appends. [obs]
- **Batch mode:** sequential; each result line written and flushed immediately; an exception
  becomes `{conversations: [], completed: false, api_calls: 0, error, metadata}`. [obs]

### 3.5 Trajectory compression

- **Trigger / inputs:** CLI `--input` (a JSONL file or a directory of `*.jsonl`), `--output`,
  `--config` (YAML; default path does not exist in the repo → defaults with a warning, R10.9),
  `--target_max_tokens`, `--tokenizer`, `--sample_percent` (0 < p ≤ 100, else error), `--seed`
  (default 42), `--dry_run`. [obs]
- **Config shape (defaults exact)** [obs]:
  `CompressionConfig { tokenizer_name = "moonshotai/Kimi-K2-Thinking", trust_remote_code = true,
  target_max_tokens = 15250, summary_target_tokens = 750, protect_first_system = true,
  protect_first_human = true, protect_first_gpt = true, protect_first_tool = true,
  protect_last_n_turns = 4, summarization_model = "google/gemini-3-flash-preview",
  base_url = OpenRouter v1, api_key_env = "OPENROUTER_API_KEY", temperature = 0.3,
  max_retries = 3, retry_delay = 2, add_summary_notice = true, summary_notice_text = <one sentence>,
  output_suffix = "_compressed", num_workers = 4, max_concurrent_requests = 50,
  skip_under_target = true, save_over_limit = true, per_trajectory_timeout = 300,
  metrics_enabled = true, metrics_per_trajectory = true, metrics_output_file = "compression_metrics.json" }`
- **YAML sections → keys:** `tokenizer{name, trust_remote_code}`, `compression{target_max_tokens,
  summary_target_tokens}`, `protected_turns{first_system, first_human, first_gpt, first_tool,
  last_n_turns}`, `summarization{model, base_url, api_key_env, temperature, max_retries,
  retry_delay}`, `output{add_summary_notice, summary_notice_text, output_suffix}`,
  `processing{num_workers, max_concurrent_requests, skip_under_target, save_over_limit}`,
  `metrics{enabled, per_trajectory, output_file}`. Missing keys keep defaults; `base_url: null`
  keeps the default. `per_trajectory_timeout` is **not** loadable from YAML. [obs]
  `num_workers`, `skip_under_target`, `save_over_limit` are loaded but have no effect in the
  current flow (concurrency comes only from the semaphore). [inf]
- **Token counting:** Hugging Face tokenizer `encode` length per turn `value`; on tokenizer error
  fall back to `len(text) // 4`. Tokenizer load failure is fatal. [obs]
- **Summariser routing:** detect the provider from the base URL host against an ordered host table
  (OpenRouter, the reference vendor's API host, z.ai, Moonshot hosts, Kimi API host, Arcee, two
  MiniMax hosts) plus a special ChatGPT Codex backend path; a known provider goes through the shared
  provider client (fatal if not configured); otherwise a raw OpenAI-compatible client using the key
  from `api_key_env` (fatal if unset). The async client is created **per event loop** (a shared one
  fails with "event loop is closed" across runs). [obs]
- **Algorithm per trajectory** [obs]:
  1. Count tokens per turn; total ≤ target → unchanged, `skipped_under_target`.
  2. Protected set = first index of each role whose protect flag is on, plus the last N turns.
     Compressible window = after the highest protected index in the first half (`< n//2`) and before
     the lowest protected index in the second half.
  3. Snap the window start forward off any `tool` turn (never begin on a response whose call is in
     the protected head); if start ≥ end → nothing to do (still over limit).
  4. Need = total − target + summary_target (the summary itself costs tokens). Accumulate turns from
     start until need is met; if never met, take the whole window.
  5. Snap the end boundary: prefer moving forward past `tool` turns (folds an orphan response into
     the summarised region), only backward if nothing clean lies ahead within the window.
  6. Abort if the region is empty or its tokens ≤ summary_target (cannot shrink).
  7. Summarise: render turns as `[Turn i - ROLE]:` blocks, eliding values over 3000 chars to head
     1500 + tail 500; prompt asks for a neutral factual summary of actions, results, decisions and
     concrete values at ≈ summary_target tokens, prefixed with a fixed `[CONTEXT SUMMARY]:` marker;
     `max_tokens = 2 × summary_target`. A `finish_reason` of `length` is treated as a failure (a
     truncated summary silently corrupts the sample). Retries: `max_retries` attempts with jittered
     backoff (base `retry_delay`, cap 30 s); after the last failure use a fixed fallback summary
     that says compression happened. Prefix normalised to appear exactly once.
  8. Assemble: head turns (system turn gets the notice sentence appended when enabled) + one
     **`human`** turn holding the summary + the untouched tail. Recount; `still_over_limit` if still
     above target.
- **Directory processing:** load all entries from all files, run them concurrently under a
  semaphore of `max_concurrent_requests`, each under `per_trajectory_timeout`; a timed-out entry is
  **dropped from the output** and counted failed (R10.8); any other exception keeps the original
  entry and counts failed. Output preserves original order per file, same file names, into the
  output dir. Aggregate metrics JSON written next to outputs. Progress shows compressed / skipped /
  timeouts / API calls / in-flight. [obs]
- **File mode:** load (bad lines warned), optionally sample `max(1, floor(n·p/100))` with the seed,
  write to a temp dir, run directory mode, concatenate to `<stem>_compressed.jsonl` (or `--output`),
  copy metrics to `<stem>_metrics.json`. **Directory mode:** default output `<dir>_compressed`;
  sampling is per file via a temp dir. Dry run prints what would happen and writes nothing. [obs]
- **Data shapes:**
  - `TrajectoryMetrics { original_tokens, compressed_tokens, tokens_saved, compression_ratio (4 dp), original_turns, compressed_turns, turns_removed, compression_region { start_idx, end_idx, turns_count }, was_compressed, still_over_limit, skipped_under_target, summarization_api_calls, summarization_errors }` — attached per entry as `compression_metrics` only when compressed. [obs]
  - `AggregateMetrics { summary { total_trajectories, trajectories_compressed, trajectories_skipped_under_target, trajectories_still_over_limit, trajectories_failed, compression_rate }, tokens { total_before, total_after, total_saved, overall_compression_ratio }, turns { total_before, total_after, total_removed }, averages { avg_compression_ratio, avg_tokens_saved_per_compressed, avg_turns_removed_per_compressed }, summarization { total_api_calls, total_errors, success_rate }, processing { start_time, end_time, duration_seconds } }` [obs]
- **Report:** a boxed console report (70-column frame) plus min/max/median of ratios and savings. [obs]

### 3.6 Dataset sample-and-compress helper

- Pulls five default Hugging Face datasets (the reference vendor's own published SFT sets), tags
  each row with its source, counts tokens with a worker pool (default 8 procs), keeps rows with
  ≥ 16000 tokens (default), randomly samples `total_samples` (default 2500) from the pooled set
  (all if fewer), writes them for the compressor, runs it with the compression config, then merges
  the output into one JSONL named by `output_name` (default "compressed_agentic"). [obs]
- Depends on `datasets` and `transformers`, which are **not declared** in any dependency group
  (R10.10). [obs]
- Rebuild: keep the shape, make the dataset list mandatory or config-driven; never default to the
  reference vendor's datasets. [inf]

### 3.7 Eval harness folder

- **What it is:** a package-shaped folder holding ~39 independent harnesses (one per question
  investigated), not a single framework: 236 files, 22,217 Python lines; each sub-folder has its
  own README and CLI. Topics span compaction recall, tool-search ranking, tool deferral, browser
  modes, provider wire formats, reasoning replay, gateway delivery, update pipelines, postmortem
  forensics, token accounting and more. [obs]
- **Shared design patterns (the part worth rebuilding)** [obs]:
  1. **A/B arms differ by one variable** — usually the code tree on `PYTHONPATH` (baseline checkout
     vs branch) or a config switch; same home dir (or a throwaway one per cell), same model, tasks
     and repetition count.
  2. **Tasks are traps/oracles** — each task is built to trigger one known failure class and is
     scored programmatically (marker strings, regex oracle over the final answer, on-disk checks),
     not by model self-report.
  3. **Scoring from traces** — metrics (LLM turns, tool calls, errors, retries, bytes, wall clock)
     come from structured run traces, not logs.
  4. **Resume-safe** — completed run ids/cells in a results JSONL are skipped on rerun; startup
     crashes (non-zero exit, empty output) are deliberately **not** recorded so they retry.
  5. **Observed vs modelled** — forensic figures recomputed from a *copy* of the state DB and
     rotated logs are labelled observed; replays are labelled modelled and never summed together.
  6. **Never touch live data** — every DB-reading harness insists on a copy of the database.
  7. Private transcripts are never committed; a synthetic-transcript generator feeds CI smoke.
- **Example — compaction recall harness:** input `{messages:[…]}` transcript; generate N recall
  questions (default 15, cached per transcript) from the region compaction will drop; run each
  compaction policy from a policy matrix; ask a fresh model the questions with only the compacted
  context; judge vs gold; emit recall-vs-tokens scorecard. CLI: transcript, cap tokens (default
  500,000), policies (comma list), questions, out dir, optional uncompacted control. [obs]
- **Example — browser mode benchmark:** tasks JSON (easy 5, hard 6), models list, arms
  (`base,pr,prns`), reps (default 3), results JSONL, per-run timeout 1200 s. [obs]
- **CI relationship:** only 3 small tests under `tests/evals/` import harness pieces; the harnesses
  themselves are run by hand. [obs]

### 3.8 Python packaging, dependencies and extras

- **Project metadata:** version is permanently `0.0.0` in source; release jobs stamp the real
  version into isolated build trees (never commit version bumps). [obs] Licence MIT with licence
  file. `requires-python >=3.11,<3.15`, but **every** core dependency carries the marker
  `python_version >= '3.14'`: older interpreters install with no deps, purely so an old install can
  run its updater far enough to move itself to 3.14 (R10.12). [obs]
- **Console scripts:** main CLI, a legacy agent CLI, and an ACP server entry. [obs]
- **Wheel/sdist guard:** building a wheel or sdist raises an error unless a Nix-build env flag is
  set; editable installs are unaffected. Rationale: assets (locales, skills, web/TUI dist, plugin
  manifests) are resolved from the checkout layout or Nix wrapper, so a wheel would be broken; pip/
  PyPI and Homebrew are explicitly unsupported channels. [obs]
- **Pinning policy (as enforced):** direct deps exact-pinned (`==`) after a 2026 supply-chain
  incident; a few use bounded ranges (`>=floor,<next_major`); git sources pinned to 40-char SHAs;
  a uv `exclude-newer = "3 days"` release-age window applies when *locking* (not on user machines);
  `override-dependencies` force patched `cryptography` and a `pynacl` range and disable a
  transitive spaCy package via an impossible marker. [obs] The PM policy doc describes the same rule
  differently (bounded ranges; mentions a "14 day" figure) — R10.11. [obs]
- **Scope rule:** only packages every session needs are core; provider/platform-specific ones live
  in extras prepared lazily by PM when a feature is chosen. [obs]
- **Extras (names):** `uvloop, anthropic, exa, firecrawl, parallel-web, ddgs, fal, edge-tts, neutts,
  kittentts, piper, modal, daytona, vercel, google-meet, messaging, cron (empty, compat), slack,
  matrix (Linux-only), wecom, tts-premium, voice, wake, telegram, discord, stt-whisper, audio-io,
  silk, wake-openwakeword, wake-sherpa, wake-porcupine, google-chat, doc-extract, trace-upload,
  vision (empty), pty (empty), mcp, nemo-relay (empty), sms, teams, computer-use, acp, mistral,
  otlp, bedrock, vertex, azure-identity, termux, termux-all, dingtalk, feishu, google, youtube, web,
  all`. Self-referencing extras compose (`all` = cron, pty, mcp, uvloop, sms, acp, google, web,
  youtube). One extra (`kittentts`) is opt-in only; seven extras carry platform markers in a custom
  table so PM can skip them where wheels do not exist. [obs]
- **Dependency groups:** `dev` (debugger, pytest, pytest-asyncio, MCP SDK + its HTTP/ASGI deps,
  type checker `ty`, ruff, setuptools) and `test` (Windows distlib, anthropic SDK). [obs]
- **Package discovery** includes the agent, tools, CLI, gateway, TUI gateway, cron, ACP adapter,
  plugins, providers, platform layer and PM packages; package data includes JSON schemas, plugin
  manifests (`plugin.yaml|yml`), PM lock/mirror/pyproject/uv.lock, and desktop-bot assets. [obs]
- **uv.lock summary:** lock format v1 rev 3, 7,261 lines, 327 packages, resolution markers split
  only on `android` vs not (both 3.14+), supported marker `python >= 3.14`, relative
  `exclude-newer-span = P3D`, manifest overrides as above; three non-index sources (a psutil git
  SHA for Android, a forked `misaki` git SHA, a KittenTTS release wheel URL). [obs] A second tiny
  lock (`pm/uv.lock`, 58 lines, 5 packages) covers PM's own runtime (packaging, tomli-w,
  ruamel.yaml, truststore) so repairing the app never requires resolving the app. [obs]
- **JS workspace (root `package.json`):** private npm workspaces for apps, TUI, web, JS tests; dev
  tooling only (eslint stack, lefthook, TypeScript); strict `overrides` for vulnerable transitive
  packages; `engines` node `^22.22.0 || ^24.11.0 || >=26.0.0`; an `allowScripts` allow-list for
  install scripts; a `prepare` hook installs lefthook when `.git` exists. `.nvmrc` = 26,
  `.python-version` = 3.14. [obs] For a Python-stack rebuild this is mostly out of scope. [inf]

### 3.9 PM — the package/environment manager (distribution role only)

- **Role:** a single dependency graph covering every external piece the app relies on: helper
  executables, the Python environment, Node module trees and plugins. Package *definitions* say what a package is; a machine-written
  lockfile says exact versions and hashes; a per-install facts file records what is actually on the
  machine. `ensure(name)` reconciles to the lock and returns a runner with a composed environment.
  The facade resolves lazily so boot does not import the downloader. [obs]
- **CLI subcommands:** `lock`, `install` (default: all required + optional defaults; `--without
  <pkg>` to skip), `env`, `doctor` (state vs lock), `repair` (rebuild the recorded environment
  without changing its graph), `gc` (unreferenced store entries), `bundle` (stage repo + store +
  facts + relocatable venv into `--out`), `status` (machine-readable last sync receipt), `update`
  (resolve latest and re-pin). [obs]
- **Lock shape:** `ToolLock { schema, packages: map<name, { version, artifacts: map<target, { url, sha256 }> }> }`.
  Targets: `any`, `darwin-arm64`, `darwin-x64`, `linux-arm64`, `linux-arm64-bionic`,
  `linux-arm64-musl`, `linux-x64`, `linux-x64-musl`, `win32-arm64`, `win32-x64`. [obs]
- **Pinned tool packages (21):** agent-browser 0.26.0, bws 2.0.0, chromium 1208+145.0.7632.6,
  cua-driver 0.21.0, dmgbuild 1.2.5+75c8a6c, ffmpeg 9.0.1, gh 2.97.0, git 2.53.0+3 (Windows only),
  iron-proxy 0.39.0, llamacpp-{cpu,cuda,hip,metal,vulkan} 11671, node 26.7.0, npm 12.0.2, python
  3.14.7+20260901, ripgrep 15.2.0, termux-docker (image digest), uv 0.12.3, whispercpp-cpu b5130
  (win32-arm64 only). [obs]
- **Artifact mirror:** a content-addressed fallback origin keyed by `<prefix>sha256/<hex>`; every
  download is verified against the lock hash whichever origin served it. [obs]
- **Boundary rule:** ruff `banned-api` forbids importing PM internals (raw uv bridge, environment
  engine, runtime stager) outside `pm/`; consumers ask PM for an interpreter or tool path, never
  run pip/uv themselves. [obs]
- Runtime details of PM (store layout, recovery, plugin eviction) belong to the updater/runtime
  area; this file only needs the lock/mirror/CLI contract. [inf]

### 3.10 POSIX shell installer

- **Trigger:** `curl … | bash` or `bash install.sh [flags]`. Linux and macOS only; Termux is
  refused with a pointer to the APT channel; other OSes are told to use the PowerShell script. [obs]
- **Flags:** `--channel stable|main` (also env), `--branch NAME` (default main), `--commit SHA`,
  `--dir PATH`, `--<home> PATH`, `--manifest`, `--stage NAME`, `--json`, `--non-interactive`
  (alias `--skip-setup`), `--include-desktop`, `--verbose`, `--skip-browser` (alias
  `--no-playwright`), `--skip-computer-use`, `-h`. Flags accept PowerShell-style spellings too
  (`-Branch`, `-Channel`, …) so one command line works for both scripts. A value flag missing its
  value (or followed by another dash-arg) exits 2; an unknown flag exits 1; a bad channel exits 2.
  [obs] Skip choices are remembered by later installs and updates. [obs]
- **Defaults:** home `~/.indus`; install dir `<home>/<repo-name>`; log `<home>/logs/install.log`;
  colour only on a TTY and when `NO_COLOR` is unset; uv config files ignored (`UV_NO_CONFIG=1`). [obs]
- **Stage ladder (manifest order):** `prerequisites`, `repository`, `venv`, `python-deps`, `config`,
  `products`, `setup`, `gateway`, `complete`. `desktop` is a valid `--stage` but not a ladder
  stage — `--include-desktop` makes `products` also build the desktop. [obs]
- **Manifest (`--manifest`):** prints `InstallManifest { protocol_version: 1, stages: { name, title,
  category: runtime|configuration, needs_user_input: bool }[] }`; `setup` and `gateway` need input.
  The script can be **sourced** for its functions without running main (test harness uses this). [obs]
- **Single stage (`--stage X [--json]`):** runs one stage in a subshell with errexit; with `--json`
  prints one frame `StageFrame { ok, stage, skipped, reason? }`. In non-interactive mode, `setup`
  and `gateway` are skipped with reason "needs user input". [obs]
- **Stage behaviour:**
  1. *prerequisites*: on macOS, if `git` exists but cannot run (a Command Line Tools stub), try
     Homebrew git, else trigger the CLT installer and poll every 5 s up to 900 s (log every minute);
     non-interactive → fail. Require git and curl. On musl hosts require a system `libstdc++`. On
     glibc hosts lacking `libatomic.so.1`, interactively pre-authorise sudo. [obs]
  2. *repository*: (details R10.14) an interrupted clone (no commits) is moved aside to
     `<dir>.broken-<UTC stamp>`; an existing checkout keeps its **saved channel** (read from the
     config file key keyed by the first 16 hex of sha256 of the checkout's real path); stable
     channel resolves the latest release tag via three fallbacks (GitHub API JSON, then the
     `releases/latest` redirect header, then `git ls-remote` tags sorted by version) — all requiring
     `v<major≤3 digits>.<minor>.<patch>`; unreachable stable either keeps the checkout (existing,
     non-explicit) or fails with a hint to use `--channel main`. Local changes are auto-stashed
     with a timestamped label; unmerged index entries cleared first; non-fast-forward main resets
     hard **after** saving diverged/orphan commits to a backup ref. A fresh install clones into a
     sibling temp dir with `--filter=blob:none`, 3 attempts (sleep 5 s, 10 s), then a deferred
     `--no-checkout` attempt, verifies `--commit` is an ancestor of the branch, checks out (2
     attempts), then **renames** into place (never a half-published checkout). Commit-graph writes
     are disabled in partial clones; a treeless clone is converted to blobless once. [obs]
  3. *venv*: ensure pinned uv (below); find a managed or system Python matching the major.minor of
     the PM-locked python (fallback 3.14), else download it via uv. [obs]
  4. *python-deps*: run PM `install` with the skip flags via the bootstrap interpreter; refresh sudo
     credentials non-interactively first. [obs]
  5. *config*: create home subdirs (`cron`, `sessions`, `logs`, `pairing`, `hooks`, `image_cache`,
     `audio_cache`, `memories`, `skills`); seed `.env` from the example (or empty) and chmod 600;
     seed `config.yaml` from the example if absent. [obs]
  6. *products*: run the shared "source completion" step (the same one the updater uses) to build
     the command launcher and app frontends, adding desktop when asked or already present; then add
     `~/.local/bin` to PATH in rc files (zsh: `.zshrc`+`.zprofile`; fish: `fish_add_path`; others:
     `.bashrc`, `.profile`, and existing `.bash_profile`/`.bash_login`), idempotently by regex. [obs]
  7. *setup* / *gateway*: interactive only (`/dev/tty` readable), run the product's setup wizard and
     service installer with stdin from the TTY; otherwise log how to run them later. [obs]
  8. *complete*: write a bootstrap-complete marker JSON `{schemaVersion: 1, pinnedCommit,
     pinnedBranch, completedAt}` atomically; persist an explicit `--channel` via the product's
     update command. [obs]
- **Pinned uv bootstrap:** version 0.12.3; six targets (linux x64/arm64 glibc/musl, darwin
  x64/arm64), each with upstream URL, mirror URL and sha256. libc detection: read the first 8 KiB
  of `/bin/sh` or `/bin/ls` for the ELF interpreter name, else `ldd --version`, else probe for
  `ld-musl-*.so.1`. Download to a temp dir; fall back to the mirror **only** on network-class curl
  exit codes (5, 6, 7, 18, 22, 28, 52, 55, 56); verify sha256 (sha256sum or shasum); extract;
  install to `<runtime dir or home/tools>/uv-<ver>-<target>/`; verify it runs. Failure classes:
  `unsupported_platform`, `download_failed`, `download_digest_mismatch`, `uv_unusable`. [obs]
- **Install receipt (telemetry):** `InstallReceipt { id (32 hex), installer, outcome:
  success|failed, failed_stage, failure_class, started_at, finished_at }` written to
  `<home>/telemetry/shared_metrics/pending_installs/<id>.json` via temp+rename; **first writer
  wins** (never overwritten); no clock → no receipt. Failure classes seen: `git_missing`,
  `curl_missing`, `libstdcxx_missing`, `unsupported_platform`, `download_failed`,
  `download_digest_mismatch`, `uv_unusable`, `python_install_failed`, `deps_install_failed`,
  `products_build_failed`, `filesystem_error`, `dir_not_checkout`, `git_clone_failed`,
  `git_fetch_failed`, `git_checkout_failed`, `git_reset_failed`, `local_changes_blocked`,
  `commit_not_on_branch`, `setup_failed`, `gateway_failed`, `interrupted`, `other`. [obs]
- **Signals:** INT/TERM/HUP are only *noted* while a stage runs in the foreground; the stage's own
  exit status is what counts — if the child handled the interrupt itself and exited 0, the ladder
  moves on; a
  failed stage after a signal is recorded `interrupted`, then the script re-raises the signal so its
  exit status is the signal's (129/130/143). Stages stay in the foreground because macOS's bash 3.2
  makes background children ignore Ctrl-C. [obs]
- **Exit:** success prints a reload hint only if `~/.local/bin` is not already on the parent PATH
  (installer is a child and cannot change it), naming the right rc for the login shell. [obs]

### 3.11 PowerShell installer and cmd shim

- **Parameters:** `-Branch`, `-Commit`, `-Channel` (env default), `-<Home>` (default
  `%LOCALAPPDATA%\<ref>`), `-InstallDir`, `-Manifest`, `-Stage`, `-ProtocolVersion` (prints 1),
  `-NonInteractive`, `-SkipSetup`, `-Json`, `-IncludeDesktop`, `-SkipBrowser`, `-SkipComputerUse`,
  `-ShowResolvedPaths` (JSON of resolved long/short profile paths). Dot-sourcing defines functions
  only. [obs]
- **Same ladder and manifest** as the shell script (same names, titles, categories). [obs]
- **Windows specifics:** git is not assumed — a pinned portable Git self-extractor per arch is
  verified and extracted (600 s timeout, killed on overrun; extract failure class
  `git_extract_failed`) into the PM store; long-path handling expands 8.3 short profile roots;
  desktop stage checks for the built exe under three `*-unpacked` dirs, grants AppContainer
  read/execute ACLs to the app dir (SID S-1-15-2-2), and creates shortcuts. [obs]
- **Receipt quirk:** the `finally` block always attempts an `interrupted` receipt; first-writer-wins
  makes it a no-op after success/failure was recorded — the rebuild must keep first-writer-wins or
  this pattern corrupts outcomes (R10.15). [obs]
- **cmd shim:** a 28-line batch file that launches PowerShell with execution-policy bypass to
  download and run the hosted `.ps1`; on failure prints the manual command and pauses. [obs]
- **Developer setup scripts** (`setup-<ref>.sh/.ps1`): from an existing checkout, fetch the
  PM-pinned uv using the lock + mirror (supports Git-Bash on Windows), with `--runtime-only` and
  `--test-environment[=…]` options. [obs]

### 3.12 Termux (Android) APT channel

- **User path:** install `curl gnupg`, fetch the repository public key into the Termux keyring dir,
  verify the published fingerprint, add a `deb [signed-by=…]` line for suite `<ref>-stable`
  component `main`, `pkg install <pkg>`. Only the standard Termux prefix and aarch64 are
  supported; wheels target Android API 24. The generic shell installer refuses Termux. [obs]
- **Build pipeline:** a builder Docker image (pinned termux-docker digest from the PM lock) builds
  CPython, Node and uv for bionic; build/retag wheels to the PEP 738 tag `android_24_arm64_v8a`
  with ABI `cp314`; a toolchain pin set (setuptools 83.0.0, cython 3.3.0, pybind11 3.1.0) is used
  only for the uvloop source build; stage runtime shared libs; assemble the `.deb`; stage a static
  APT repo (`dists/<suite>/{Packages, Packages.gz, Release, InRelease, Release.gpg}`, `pool/<first
  char>/`), arch `aarch64`; exit 3 if Release is emitted unsigned (CI treats 3 as failure). [obs]
- **Version mapping:** tag `v1.2.3` → deb `1.2.3-1`; canary tag
  `v1.2.3+canary.<YYYYMMDDTHHMMSSZ>` → `1.2.3~canary.<ts>-1` (`~` sorts the canary below its
  stable). Channel derived from the same regex (`canary` or `stable`); malformed tags rejected.
  [obs]
- **Release default:** not built unless the release includes the `termux` lane; the APT channel
  stays on the previous release otherwise. [obs]

### 3.13 Docker image and Compose

- **Image stages:** (1) build a pinned SQLite amalgamation (version id 3530400, sha256-checked) on
  a digest-pinned Debian 13.4; (2) runtime base (unbuffered Python, no bytecode, Playwright browsers
  in the tools dir, s6-overlay 3.2.3.0 with per-arch sha256 for noarch/x86_64/aarch64/symlinks);
  (3) Python deps; (4) icons; (5) frontend build; (6) final runtime. A build arg toggles the
  "desktop" variant (adds a virtual-screen package set and distro Chromium fallback). [obs]
- **Runtime contract:** working dir `/opt/<ref>`; home and write-safe root `/opt/data` (a VOLUME);
  PATH puts app bin, venv bin and the data dir's `.local/bin` first; ENTRYPOINT is a dispatcher;
  CMD empty. [obs]
- **Dispatcher:** if PID 1 → exec s6 `/init` with the main wrapper as the main program (on a TTY,
  enable s6 terminal logging); not PID 1 → warn, run the stage-2 hook directly, then exec the
  wrapper (no supervised services). A deprecated old entrypoint shim only runs the hook and warns.
  [obs]
- **Stage-2 hook (root, before services):** UID/GID remap from `<PREFIX>_UID/_GID` (or PUID/PGID
  aliases; default build UID 10000) via usermod/groupmod plus targeted chown; refuse symlinked path
  components; create a custom home path while still root; seed config/skills; sync routing
  overrides into the env file; reject `docker run --user <uid>` starts with actionable guidance
  (bootstrap needs root; the baked tree is root-owned and read-only). [obs]
- **Main wrapper routing:** no args → run the product CLI; first arg is an executable on PATH →
  exec it (e.g. `bash`); a leading `-flag` is always a CLI global option (never probed as an
  executable — a past restart loop); anything else → `<cli> <args>`. Restores the user's `-w`
  working dir; sets HOME to the app user's home; drops privileges via `s6-setuidgid`. [obs]
- **s6 services:** `main-<ref>` and `dashboard` in the user bundle; two cont-init scripts
  (supervise perms, profile reconcile). [obs]
- **Compose (Linux):** two services from one image — gateway (`gateway run`) and dashboard
  (`dashboard --host 127.0.0.1 --no-open`), host networking, `~/.<ref>` mounted at `/opt/data`,
  `restart: unless-stopped`, UID/GID default 10000. **Compose (Windows):** published image, no host
  networking, dashboard on `0.0.0.0:9119` with `--insecure`, port bound to `127.0.0.1:9119`. [obs]
- **hadolint:** fail at warning; ignore DL3002, DL3003, DL3008, DL3059; trusted registries docker.io
  and ghcr.io. [obs]
- **Docker installs never self-update** — update = pull a new image. [obs]
- **Publishing:** matrix arch {amd64 (x64 runner), arm64 (native arm runner)} × variant {slim,
  desktop}; PRs build and test all four; pushes to main publish `:main` and `:main-desktop` only;
  `:latest` / `:stable` move only through the ordered stable publication controller from the
  receipt-bound digest; release images are pushed by digest and tagged by attempt ref first. [obs]
- **Image tests:** a dedicated job runs `tests/docker/` against the freshly built `:test` image via
  an env var that makes the session fixture skip its own 3–7 min rebuild. [obs]

### 3.14 Nix flake and modules

- **Flake inputs:** nixpkgs (unstable), flake-parts, pyproject-nix, uv2nix, pyproject build-system
  pkgs, an npm-lockfile-fix tool, home-manager (all `follows` nixpkgs). Systems: x86_64-linux,
  aarch64-linux, aarch64-darwin. [obs]
- **Packages:** `default` (full: a fixed extras list incl. messaging, voice, modal, firecrawl, …,
  plus `matrix` on Linux), `minimal`, `messaging` (minimal + messaging group, because lazy install
  cannot write to the read-only store), `sandbox`, `tui`, `web`, `desktop`, `node-gyp`, an npm
  lockfile updater, and one `pm-<tool>` derivation per PM-locked tool. [obs]
- **Modules:** NixOS module (`services.<ref>`) with native systemd mode or OCI container mode
  (docker/podman backend, image, extra volumes/options, host users; user lingering on in native
  mode for cron's transient units; an assertion forbids backend mode together with container
  mode); a home-manager module; a shared option set (package, working dir, config file, settings,
  env files, environment, auth file, documents, home files, MCP servers with command/args/env or
  url/headers/auth + tool include/exclude + sampling, extra packages/plugins/python packages/
  dependency groups, restart policy, backend mode/host/port/session-token file). [obs]
- **Checks:** cross-system evaluation, PM package unpack, package build, frontend icons present,
  devshell build, and many module-evaluation assertion cases (e.g. "documents need a directory").
  [obs] `.envrc` uses the flake devshell and watches manifests/locks. [obs]
- **Updates:** Nix owns installation and updates; the in-app updater must not act. [obs]

### 3.15 Desktop/MSIX/Store artefacts (packaging view only)

- Windows: per-arch MSIX packages bundled into a signed universal `.msixbundle` plus an App
  Installer feed for updates; a separate **unsigned** Store bundle (the Store re-signs on
  ingestion) is submitted with the fixed official identity; non-stable packages must never use it.
  MSIX requires Windows 11 22H2+. [obs]
- macOS: Apple Silicon DMG + ZIP by default; Intel only with an opt-in lane. A bootstrap-installer
  DMG exists for Apple Silicon only. Linux desktop packaging is disabled in release. [obs]
- Canary and one-off builds have distinct app identities and CLI command names, distinct icon
  branding (canary yellow; one-off red with short SHA), and one-off builds refuse self-update. [obs]
- Desktop runtime/update behaviour belongs to the desktop area. [inf]

### 3.16 CI pipelines and gates

- **Inventory:** 57 workflow files, 11 composite actions, 1 workspace-check script; runners are a
  third-party fast-runner vendor (4/12/32 vCPU Ubuntu, macOS, Windows, arm). All third-party actions
  are pinned by commit SHA with a version comment; Dependabot updates Actions weekly (Monday),
  grouping minor/patch, max 5 PRs. [obs]
- **Entry workflow (CI):** triggers on PR, push to main, manual, and `workflow_call` (release reuse).
  Concurrency group per PR number (or ref; release runs get a unique run id); cancel-in-progress
  only for PRs. [obs]
- **Change classifier:** a `detect` job pipes changed paths into a classifier that emits boolean
  lanes: `python`, `python_prod`, `docker_meta`, `docker`, `nix`, `e2e`, `e2e_upgrade`,
  `e2e_desktop_core`, `e2e_desktop_update`, `frontend`, `site`, `scan`, `deps`, `uv_lock`,
  `npm_lock`, `bootstrap`, `desktop_updater`, `rust`, `catalog`, `remote_backends`, plus
  `event_name`. Contract: **fail open** — when unsure, run the lane. Pushes to main carry no PR diff,
  so all lanes switch on there; E2E lanes are forced off on PRs and main pushes (release/manual only); a
  catalog-data-only diff runs the catalog tests *instead of* the full Python suite. [obs]
- **Jobs (26 children + gate + timings):** tests, OS-specific tests, lint, JS tests, Rust tests,
  catalog tests, remote backends (real sshd container + runner Docker), bootstrap installer,
  desktop E2E (deferred/disabled), desktop core E2E, desktop update E2E, docs site, history check
  (PR has a common ancestor with main), contributor attribution, uv.lock freshness, infographic
  guard (no committed PR-infographic images, matched by image path in any spelling), profile
  artifact boundary, icon freshness, case-collision check, blob-size check, lazy-deps import guard,
  npm lockfile semantic diff (PR comment), prompt-surface diff (PR), docker/shell lint, supply-chain
  audit (PR). [obs]
- **Aggregate gate (`all-checks-pass`):** runs `always()`, feeds `toJSON(needs)` to a gate script.
  Any failure fails. A skipped or missing job fails when the detect lanes selected it (tests,
  OS tests, desktop update E2E, remote backends, catalog). In release mode every other skip fails
  too, except the deferred desktop E2E and the PR-only jobs (history, lockfile diff,
  prompt-surface diff, supply chain). A timings report job follows. [obs]
- **Tests job:** 2 slices on 32-vCPU runners, 30 min timeout; installs a sha256-pinned ripgrep;
  sets up the locked PM toolchain + test environment; asserts the runtime SQLite is not vulnerable
  to the WAL-reset bug; restores a per-file duration cache; runs the canonical test script; saves
  the cache on main only; asserts a clean worktree after. E2E job: builds the TUI, installs tmux,
  runs messaging-gated gateway cases, then all `tests/e2e` except upgrade. Upgrade E2E: planned
  shards from sub-dirs, 60 min, needs bubblewrap with unprivileged userns. [obs]
- **OS tests:** macOS (2 slices) and Windows lanes import only files whose `platforms(...)` marker
  specs reach that OS, then select with `-m platforms`; Windows E2E checks no leftover Python/app
  processes within 20 s. [obs]
- **Lint job:** (a) *lint-diff*: ruff + ty on HEAD and on the merge base (via worktree), JSON/
  gitlab outputs, a diff summary — only new findings matter; (b) *ruff-blocking*: `ruff check .`;
  (c) *code-health ratchet* vs `HEAD^1`; (d) footgun guards: Windows footguns, portable bash
  shebangs, no literal `/tmp` outside a baseline, no config writers bypassing the comment-preserving
  writer, no unmarked macOS fakes in tests, every AGENTS.md under its truncation cap, plus advisory
  profile-scope and public-surface diffs. [obs]
- **Code-health ratchet:** per-unit caps that only go down: cyclomatic complexity 20, function
  lines 300, nesting depth 6, file lines 2000; units already over target keep their measured base
  value as cap. Rule table HX001–HX013 (hard-coded home path, new env var, argv identity, unscoped
  secret fallback, import-time capture of home/config/env, missing timeout, sync config read in
  async, `get_event_loop`, unchecked gather exceptions, bool of env string, elif ladder, raw thread,
  wall-clock sleep). The enforcement switch (blocking/advisory/off) is read **from main**, never
  from the PR branch, so a PR cannot relax its own check. Local `scripts/check` runs the same set,
  can judge staged index or one commit, and installs pre-commit/pre-push hooks. [obs]
- **Supply-chain audit (PR):** scans added lines (excluding lockfiles) for base64-decode combined
  with exec/eval on one line, and subprocess calls with base64/hex/`chr(` obfuscation → critical,
  fail. Dependency-bounds check: an added `pyproject.toml` spec of the form `"pkg>=x"` without `,<`
  → fail. Results also posted as a review-status artifact. [obs]
- **Other gates:** blob size ≤ 500 KiB for any added/modified binary or newly added text file
  (allow-list file with 2 entries); uv.lock check re-resolves against PyPI with retries (a stale
  lock fails deterministically); contributor check fails a PR whose commit emails are unmapped
  (§3.21). [obs]
- **Scheduled / triggered workflows:** canary release daily 06:41 UTC; OSV scan Monday 09:00 UTC;
  skills index build 06:00 and 18:00 UTC (+ push); skills-index freshness every 4 h; docs search
  monitor every 6 h at :17; live provider canaries on push/manual (secrets-gated, cost table in
  the job summary); label-triggered rerun of CI/Docker/Nix with E2E; a JS autofix bot that
  generates and applies a patch; Windows live E2E lanes fire only on dedicated branch prefixes.
  [obs]
- **Hooks and local lint:** lefthook pre-commit runs ruff on staged `.py` with the pre-commit ruff
  config and eslint per JS workspace; a `justfile` exposes `lint-all`, `lint-python`,
  `lint-python-fix`, and per-workspace JS lint recipes. [obs]
- **Ruff config:** 88 cols, 4-space, double quotes, target py311, preview on; 342 explicitly
  selected rules (largest families: PYI 46, UP 38, RUF 35, F 34, PLE 30, B 21, PLW 18, FURB 17);
  mccabe max 20; banned PM-internal imports with per-path exemptions. The pre-commit variant
  differs only by a trailing comma. `pyproject.toml` also has a smaller `[tool.ruff]` table (6
  rules) that the standalone file shadows (R10.13). `ty` is configured for Python 3.13. [obs]

### 3.17 Release process

- **Versioning:** committed version stays `0.0.0`; SemVer derived at release time from the newer of
  (protected stable channel head, newest published non-prerelease `vX.Y.Z` GitHub release), seeded
  at 0.21.5 (end of a legacy CalVer line). [obs]
- **Stable flow:**
  1. `release --commit <sha on origin/main> [--bump patch|minor|major] [--autopublish]
     [--only|--with|--without lanes] [--remote]`; will not start while some earlier attempt (whatever its
     version) is still open (interactive: offers abandon-and-recut, default No; non-TTY: refuse). The chosen commit has to
     have the latest published `vX.Y.Z` as an ancestor. [obs]
  2. Push an annotated **attempt ref** `rc.<N>-vX.Y.Z` (a lock, not a prerelease; package version
     stays plain), create one GitHub **draft** with warning blocks ("do not publish by hand"), and
     dispatch the stable workflow **from the default branch** with the ref as input. The claim
     message binds commit, attempt number, autopublish, lane set and a monotonic epoch (release
     date and packaging clock for all legs). [obs]
  3. `admit` verifies the exact tag object, its peeled commit, ancestry on main, and that the run is
     the default-branch dispatch; every lane that builds/tests receives the admitted commit (the
     run's workflow SHA points at the default-branch head, which is not what gets tested). [obs]
  4. Lanes run (table below). Docker pushes immutable tagged artefacts after its tests; R2 holds
     immutable versioned artefacts; aliases do not move yet. [obs]
  5. At publish, create the annotated final `vX.Y.Z` **receipt** tag binding attempt ref, tag
     object, commit, archive prefix, candidate-manifest sha256 (or null), Docker manifest digest
     (or null), autopublish. [obs]
  6. A publication controller resolves releases oldest-first: creates the final tag, retargets the
     draft, strips warning blocks, makes it public **last**, then promotes the Docker digest and
     advances App Installer, macOS feed, APT, downloads page and protected R2 heads. It is
     idempotent across services that have no shared transaction; rerun after partial failure. [obs]
  - A sole green draft waits unless autopublish; a later green claim flushes earlier green drafts in
    order; a newer green claim cannot publish while an older claim is still running; `abandon --version` burns an attempt
    (marker ref `abandoned-rc.*`). Hand-publishing a draft breaks the pipeline irreparably. [obs]
- **Lanes:** `docker` on; `windows` on (signed x64+arm64 MSIX + universal bundle feeding updates);
  `windows-store` on (unsigned Store bundle); `macos-arm64` on; `macos-intel` off (adds >1 h);
  `termux` off; standalone suites on (`ci`, `nix`, `docker-tests`, `pm-bundle`,
  `install-e2e`, `windows-live`, `bootstrap-version`); `termux-checks` on (only with termux); `smoke` on (native install+chat
  smoke per package); `upgrade` on (signed OLD→NEW acceptance). A job runs only if all its lanes are
  on; the gate requires removed jobs to report `skipped` and all others `success`; the lane set is
  recorded in the claim, never a workflow input, and cannot change on rerun. Only a release that built
  both windows and macos-arm64 advances the desktop update channel. [obs]
- **Canary:** source identity `v<stable>+canary.<YYYYMMDDTHHMMSSZ>` (SemVer-equal to its stable;
  the protected channel record and timestamp decide order); scheduled daily, queued with
  cancel-in-progress false; no new commits → resume an unpublished canary or no-op; a separate app
  identity. [obs]
- **One-off / commit builds:** `--build-commit REV` → app identity and command include the 7-char
  SHA; deterministic downloads page `releases/commit/<sha>/index.html`; no update feed; listed
  bundle-env overrides on the page. Receipt tags `v0.0.0+commit.<ts>.<run-id>` and
  `v<x>+channel.<ts>.<run-id>` are written only after publication and trigger nothing. [obs]
- **Dynamic channels:** channel records live in object storage (R2) with ETag compare-and-swap;
  the privileged allocation runs in CI; the local command needs only a GitHub token. [obs]
- **Tag-namespace protection** (rulesets on `v*`, `rc.*`, `abandoned-rc.*`) is documented as not
  yet applied — locks are honour-system until then. [obs]

### 3.18 Localisation catalogues (packaged resource)

- **Files:** `locales/<lang>.yaml` for 17 languages — English (`en`) plus 16 others: East Asian
  `zh`, `zh-hant`, `ja`, `ko`; European and nearby `de`, `es`, `fr`, `it`, `pt`, `ga`, `hu`, `ru`,
  `uk`, `tr`; plus `af` and `ar` — nested YAML, 8 top-level sections, 3,454 leaf keys in English; every
  other language has exactly the same key set (0 missing, 0 extra). [obs]
- **TUI catalogues:** `<lang>.tui.yaml` for the 16 non-English languages, **flat dotted keys**,
  1,351 keys each, matching a key-list file `_keys.tui.json`; English TUI strings live in the TUI
  source, not here; function-valued English entries use positional `{0}`, `{1}` placeholders. [obs]
- **Desktop key list:** `_keys.desktop.json` = `{surface, keys: string[5114]}` (translations live
  with the desktop app). [obs]
- **Placeholders:** Python-style named `{name}` in the main catalogue; positional in TUI. [obs]
- **Runtime lookup** (summary; owned by the runtime area): layered resolution — plugin language
  packs, a user overlay under the profile home, the bundled file, the same chain for English, then
  the bare key; language from explicit arg > env var > config `display.language` > `en`; an alias
  table maps natural names and region tags (e.g. `zh-tw` → `zh-hant`, `pt-br` → `pt`). [obs]
- **Docs translation:** the docs site has one extra locale `zh-Hans` (293 files). [obs]

### 3.19 Docs website (structure)

- Docusaurus 3.10.2 site served under `/docs/` on the product domain; locales `en` + `zh-Hans`;
  broken links warn (not fail); Algolia DocSearch with public search-only credentials; Mermaid
  diagrams; client redirects; an edit-URL pointing at the repo. [obs]
- **Page tree (462 doc files):** `index.mdx`, `user-stories.mdx`, `getting-started/` (7:
  installation, learning path, Nix setup, platform support, quickstart, Termux, updating),
  `user-guide/` (333, incl. `features/` 57, `messaging/` 36, `skills/` 212 generated per-skill pages,
  `secrets/` 4, `egress/` 3, plus ~25 top-level pages: CLI, configuration, models, desktop, Docker,
  profiles, security, sessions, TUI, Windows native/WSL, …), `guides/` (35 how-tos),
  `integrations/` (4), `reference/` (14: CLI commands, symbols, env vars, FAQ, MCP config, model
  catalogue, skill catalogues, package management, profile commands, slash commands, tools,
  toolsets), `developer-guide/` (60 internals pages incl. stable releases and trajectory format). [obs]
- **Build:** a prebuild step extracts skill metadata to static JSON, generates `llms.txt` and
  `llms-full.txt`, and tries to download the latest unified skills index from the live site (falls
  back to empty data when Python deps are missing). Deploy: on release publish / main pushes
  touching site, skills or plugin catalogue → extract skills/plugins, regenerate skill pages,
  build, stage under `_site/docs` (llms files also at root), merge hashed assets retained from
  previous deploys (so old cached pages keep working), deploy to GitHub Pages; a separate hook
  triggers the marketing site deploy on Vercel. [obs]
- Custom React pieces: plugin catalogue, automation blueprints catalogue, user-stories collage,
  skills and plugins pages, a remark plugin for relative doc links; static `api/model-catalog.json`
  and an OAuth client-metadata document. [obs]

### 3.20 Test strategy

- **Size:** 7,038 files under `tests/`; 6,903 `.py`; 6,611 `test_*.py`; ~49,236 test functions;
  1,459,162 Python lines; 15 `conftest.py` files (root 1,533 lines). A separate `tests-js/` vitest
  workspace has 42 files. [obs]
- **Per-folder counts** (test files / all files / Python lines) [obs]:

| folder | test files | files | lines |
|---|---|---|---|
| `<ref>_cli` | 1663 | 1673 | 340,241 |
| gateway | 1561 | 1627 | 323,902 |
| agent | 1023 | 1030 | 245,146 |
| tools | 803 | 808 | 198,651 |
| tui_gateway | 322 | 327 | 78,569 |
| `<ref>_state` | 198 | 201 | 41,490 |
| cron | 163 | 165 | 34,815 |
| scripts | 162 | 165 | 28,392 |
| e2e | 150 | 256 | 53,052 |
| plugins | 131 | 144 | 31,067 |
| pm | 124 | 129 | 19,652 |
| acp_adapter | 82 | 84 | 10,530 |
| ci | 36 | 39 | 7,529 |
| skills | 33 | 33 | 4,978 |
| docker | 29 | 31 | 3,380 |
| providers | 15 | 17 | 2,111 |
| computer_use | 10 | 12 | 1,808 |
| `<ref>_platform` | 8 | 9 | 938 |
| website | 7 | 8 | 872 |
| conformance | 6 | 14 | 1,417 |
| monitoring | 5 | 6 | 530 |
| verify | 4 | 4 | 918 |
| evals / secret_sources | 3 / 3 | 4 / 5 | 117 / 837 |
| desktop | 2 | 2 | 234 |
| dashboard, installation, integration, perf_guards, security | 1 each | 1–2 | 75–359 |
| fakes (no tests) | 0 | 21 | 7,940 |
| _fixtures / fixtures / install / compat / manual | 0 | 5 / 57 / 74 / 4 / 2 | 1,065 / 42 / 1,444 / 322 / 279 |
| root-level `test_*.py` | 63 | — | 16,133 |

- **Placement rule:** tests mirror the source tree (`tests/<top-level dir>/…`); only root-level
  modules (batch runner, utils, constants, packaging) get root-level tests; tests for the install and
  desktop-update scripts sit in `tests/scripts/{install,desktop_update}/`; no issue numbers in file names (cite them
  in the docstring). [obs]
- **Runner:** one canonical script, never bare pytest. It forces `TZ=UTC`, `LANG=C.UTF-8`,
  `PYTHONHASHSEED=0`, blanks credential env vars, redirects the app home to a temp dir, re-executes
  itself under the checkout's activated test environment if stale, then runs a per-file parallel
  runner: one fresh `python -m pytest <file>` subprocess per file, bounded by CPU count (env
  override), discovery roots `tests` (env override; `:` or `;`), skipping `integration`, `e2e`,
  `docker` dirs unless asked; per-file timeout 300 s; one retry of a failed file (pass-on-retry is
  green but printed as FLAKY with both outputs; signal-killed or timed-out files are never
  retried); `--slice I/N` partitions files deterministically from the checkout only (sorted
  round-robin/LPT with a 2.0 s default per file — the duration cache is used only for reporting);
  stale per-file temp roots idle > 24 h are swept. Bare pytest flags pass through. [obs]
- **Default marker filter:** `-m 'not integration and not live'`. [obs]
- **Registered markers:** `integration`, `live` (real provider APIs, spends money),
  `real_concurrent_gate`, `real_agent_prewarm`, `real_retry_backoff`, `requires_wal`, `no_isolate`,
  `ssh`, `platforms(*specs, arch=, arch_negate=)` (pyproject) plus conftest-registered
  `allow_real_home_io`, `real_memory_guard`, `real_release_channels`, `real_safe_directory`,
  `require_symlinks`, and in-use `live_system_guard_bypass`, `spawns_gateway_lookalike`,
  `real_machine_home`, `real_audio_playback`. Usage counts: asyncio 4,492; parametrize 4,342;
  platforms 1,671; skipif 404; live_system_guard_bypass 147; require_symlinks 46; requires_wal 32;
  integration 27; no_isolate 21. [obs]
- **Host gating:** behaviour that differs per OS is tested on that OS with exactly **one**
  `platforms(...)` marker (any-of specs `linux|macos|windows|posix|any|not X`, optional arch);
  patching `sys.platform` is banned; two `platforms` markers on one test is a collection error; a
  bare `skipif(sys.platform…)` is banned because the OS lane selector would never import that file.
  [obs]
- **Root autouse fixtures (hermeticity):** blank credential and behavioural env vars; isolate the
  app home and the default-root resolution (without changing HOME); reset foreground fences;
  neutralise memory guards, git safe-directory reads, web browser opening, macOS keychain reads,
  audio playback; close leaked session DBs; guard writes to the real state DB and kanban root;
  reset TUI-server state; ensure a current event loop; isolate computer-use approvals and
  mixture-of-agents caches; forbid real user-path and real-home I/O; relocate pytest's basetemp
  outside the operator's home and reap leftover processes whose home is under it. Each guard has an
  opt-out marker. [obs]
- **Fakes:** in-process fake LLM provider and per-vendor wire fakes (Anthropic messages, Bedrock
  converse, OpenAI responses, Gemini native, Vertex, Codex app server, Copilot ACP, OAuth token
  server, catalogue), platform stand-ins (Discord, Slack, Telegram). [obs]
- **Policies:** no change-detector tests (snapshots of catalogues/counts); never read source text in
  tests (extract a pure function instead); no new non-zero sleeps in tests (ratchet rule) — wait on
  events or a poll helper; timing bounds ≥ 2 s. [obs]
- **E2E:** `tests/e2e/core/` sub-suites (chaos, CLI-in-tmux, compaction, dashboard, delivery,
  history, kanban, live, MCP plugins, parity, platforms, providers, security, sqlite, tenancy,
  terminal, TUI over PTY, upgrade, Windows, Windows update). Installer E2E shell/PowerShell drivers
  and known-failure lists under `tests/install/`. [obs]

### 3.21 Contributor attribution folder

- **What it is:** a directory mapping commit-author emails to GitHub logins, **one file per email**
  (file name = exact email, first non-comment line = login, `#` comments for PR context), replacing
  a frozen legacy dict in release tooling to avoid merge conflicts; directory entries win on
  duplicates. 2,064 mapping files (441 of them noreply addresses even though noreply addresses are
  documented to auto-resolve) and a `reattributed.tsv` (200 lines: commit SHA, date, subject) that
  re-credits commits stamped with an unverifiable local identity. A helper script adds a mapping;
  CI fails PRs whose new commit emails are unmapped and prints the fix command. Release notes use
  the mapping to credit authors. [obs]
- **Rebuild:** keep the mechanism, start empty; never import the reference's email files (personal
  data). [inf]

## 4. File map (what to read in the Read step)

| Sub-area | Reference path | Lines | Read what | Docs / tests to read with it |
|---|---|---|---|---|
| Batch runner | `batch_runner.py` | 1020 | all | `tests/test_batch_runner_*.py` (3 files) |
| Distributions | `toolset_distributions.py` | 106 | all | — |
| Trajectory save | `agent/trajectory.py` | 56 | all | `website/docs/developer-guide/trajectory-format.md` (232) |
| Trajectory convert | `agent/agent_runtime_helpers.py` | 3807 | only the trajectory helpers near the top (search "trajectory") | same doc |
| Compressor | `trajectory_compressor.py` | 868 | all | — |
| SWE runner | `mini_swe_runner.py` | 406 | all | — |
| Sample+compress | `scripts/sample_and_compress.py` | 407 | skim | — |
| Evals | `evals/` (236 files) | 22,217 py | READMEs; `compaction/runner.py` (473), `toolperf_abeval/ab_eval.py` (327), `browser_use/orchestrate.py` | `tests/evals/` (3) |
| Python packaging | `pyproject.toml` | 787 | all (comments explain pins) | `setup.py` (99) |
| Lock | `uv.lock` | 7261 | header + manifest only | `pm/uv.lock` (58), `pm/pyproject.toml` (18) |
| PM | `pm/` (55 py) | 12,759 | `__init__.py` (58), `cli.py` (900) parser, `lock.py` (312), `lock.json` (663) shape, `AGENTS.md` (29) | `tests/pm/` (124 test files) |
| Shell installer | `scripts/install.sh` | 1298 | all | `tests/scripts/install/`, `tests/install/` |
| PS installer | `scripts/install.ps1` | 1670 | stage list, git/uv bootstrap, receipts, desktop | `scripts/tests/*.ps1` (4) |
| cmd shim | `scripts/install.cmd` | 28 | all | — |
| Dev setup | `setup-<ref>.sh` / `.ps1` | 306 / 156 | skim | — |
| Termux | `scripts/termux/` (24 files) | 3135 | `deb_version.py` (81), `stage_apt_repo.py` (543), `build_config.sh` (28), `build_deb.sh` (263) | `website/docs/getting-started/termux.md`, `.github/workflows/termux-verify.yml` (162) |
| Docker | `Dockerfile` | 543 | stage headers, ENV/ENTRYPOINT | `docker/stage2-hook.sh` (792), `docker/main-wrapper.sh` (97), `docker/entrypoint-dispatch.sh` (33), `tests/docker/` |
| Compose | `docker-compose.yml` / `.windows.yml` | 76 / 38 | all | `.hadolint.yaml` (36), `.dockerignore` (128) |
| Nix | `flake.nix` + `nix/` (21 files) | 57 + 5646 | `packages.nix` (101), `nixosModules.nix` (715) options, `moduleCommon.nix` (1186) options | `nix/checks.nix` (1619), `getting-started/nix-setup.md` |
| MSIX | `scripts/msix-shared.mjs`, `stage-msixbundle.mjs`, `bundle-store-msixbundle.mjs` | 282 / 174 / 83 | headers | — |
| CI entry | `.github/workflows/ci.yaml` | 585 | all | `scripts/ci/classify_changes.py` (757), `scripts/ci/required_results.py` (123) |
| Tests CI | `.github/workflows/tests.yml`, `tests-os.yml` | 439 / 301 | steps | `scripts/ci/list_os_marked_tests.py` (135) |
| Lint CI | `.github/workflows/lint.yml` | 316 | steps | `scripts/check` (288), `scripts/code_health/config.py` (174), `scripts/lint_diff.py` (207) |
| Supply chain | `.github/workflows/supply-chain-audit.yml` | 285 | scan + bounds steps | `osv-scanner.yml` (51), `uv-lockfile-check.yml` (184) |
| Guards | `scripts/ci/check_blob_size.py` | 274 | limit + exits | `.github/blob-size-allowlist.txt` (12) |
| Docker CI | `.github/workflows/docker.yml` | 789 | matrix + tag policy | — |
| Nix CI | `.github/workflows/nix.yml` | 183 | skim | — |
| Release | `scripts/release.py` | 725 | argparse + dispatch | `website/docs/developer-guide/stable-releases.md` (530) |
| Release libs | `scripts/releases/` (30 files) | 9905 | `versioning.py` (213), `lanes.py` (236), `stable.py` (1040) gate, `sequencer.py` (522) | `tests/ci/`, `tests/scripts/` |
| Release workflows | `stable-release.yml`, `stable-release-publication.yml`, `canary-release.yml`, `desktop-bundled-release.yml` | 687 / 69 / 81 / 2634 | job graph only | — |
| Test runner | `scripts/run_tests.sh`, `scripts/run_tests_parallel.py` | 205 / 1664 | all of the shell; the parallel runner's options, retry, slicing | `tests/AGENTS.md` (154) |
| Root conftest | `tests/conftest.py` | 1533 | fixture list + collection hooks | — |
| Hooks/lint cfg | `lefthook.yml`, `justfile`, `ruff.toml`, `ruff.pre-commit.toml` | 35 / 37 / 473 / 473 | all | — |
| Locales | `locales/` (35 files) | 95,042 | key structure only | runtime loader `agent/i18n.py` (256) |
| Website | `website/` | — | `docusaurus.config.ts` (217), `sidebars.ts` (920) structure, `scripts/prebuild.mjs` (174) | `.github/workflows/deploy-site.yml` (269) |
| Contributors | `contributors/README.md`, `reattributed.tsv` | 38 / 200 | README only | `scripts/add_contributor.py` (138), `.github/workflows/contributor-check.yml` (114) |
| JS root | `package.json` | 91 | all | `.npmrc`, `.nvmrc` |

Giant files: `agent/agent_runtime_helpers.py [3807]` — only the trajectory block (search for the
trajectory-format converter and its three helpers just above it). `desktop-bundled-release.yml
[2634]` — job names only. `nix/checks.nix [1619]` — the `checks = {` attribute names. `uv.lock
[7261]` — first 30 lines. `scripts/run_tests_parallel.py [1664]` — option parser, retry and slice
functions. `scripts/releases/authors_legacy.py [2043]` — do not read (frozen email dict).

## 5. Minimum viable subset

v1 needs:
1. **Packaging:** `pyproject.toml` with exact/bounded pins, extras for optional backends, a
   `uv.lock`, a wheel/sdist guard *or* a decision to ship wheels (§7 Q1), console script `indus`.
2. **Installer:** the POSIX shell installer with the stage ladder, manifest/JSON frames, pinned
   uv bootstrap with sha256 + mirror fallback, checkout-in-staging-then-rename, config seeding,
   PATH wiring, first-writer-wins receipt. PowerShell installer second (Tier-1 Windows).
3. **Docker:** single-variant image (slim) with dispatcher/wrapper routing, UID/GID remap, data
   volume; Compose file with gateway + dashboard. s6 can be replaced by a smaller supervisor if
   the dispatcher semantics (PID-1 vs not) are kept.
4. **CI:** change classifier (fail-open), tests (sliced), ruff blocking + lint-diff, uv.lock check,
   dependency-bounds + supply-chain scan, blob-size guard, one aggregate gate with the strict-skip
   rule.
5. **Test infra:** canonical runner with per-file subprocess isolation, retry-once-flaky,
   deterministic slicing, hermetic root conftest (env blanking, home redirect), `platforms` marker,
   `integration`/`live` default exclusion.
6. **Research:** batch runner + distributions + trajectory converter + compressor — they are small,
   self-contained and are the reason the product doubles as a data engine.
7. **Locales:** the bundled catalogue format (nested YAML, `{name}` placeholders) + a key-parity
   check in CI.

Can wait (and why): Nix flake/modules (Tier 2), Termux APT (Tier 2, off by default), MSIX/Store
and macOS signing (needs desktop app first), canary/one-off/dynamic channels and the R2
controller (needs a hosting decision), code-health ratchet (useful once code exists), eval
harnesses (build per question), docs site (content after features), contributor mapping (needs
contributors), JS workspace tooling (only if a JS frontend is rebuilt).

## 6. Hard parts / traps

1. **R10.1 Crash-durable batch results.** A result counts as done only after its JSONL line is
   appended **and fsynced**; checkpoints are written atomically from the parent only. Writing the
   checkpoint before the row (or buffering rows) loses paid-for samples on crash. Keep the order:
   row → fsync → mark completed → checkpoint.
2. **R10.2 Discards need tombstones.** A sample dropped for having no reasoning must still leave a
   row, or content-based resume re-runs it at full cost forever; the merge step must then exclude
   tombstones. Any future discard reason needs the same treatment.
3. **R10.3 Index skip happens without `--resume`.** The checkpoint's completed indices are always
   honoured when the run name matches, while the content-based scan only runs with `--resume`. A
   user changing the dataset under the same run name silently skips rows by old index. Rebuild:
   make both behaviours explicit (bind the checkpoint to a dataset hash, or require `--resume`).
4. **R10.4 Advertised defaults that are not applied.** Help text says reasoning effort defaults to
   "medium" and names a different default model than the one used; the CLI default model differs
   from the class default. Write one source of truth for defaults and print the effective config.
5. **R10.5 Columnar-stable stats.** `tool_stats`/`tool_error_counts` must list *every* registered
   tool with zeros, or Arrow/Parquet export fails on schema drift between rows. Unknown tool names
   in stats mark a corrupt sample and are filtered at merge.
6. **R10.6 Tool name by position.** The converter labels each tool response with the name of the
   call at the same position, not by matching `tool_call_id`; reordered or missing responses get
   the wrong name or `unknown`. Rebuild: match by id, fall back to position.
7. **R10.7 Two subtly different trajectory writers.** The SWE runner emits think blocks without
   newlines and without empty padding, and truncates its output file; the batch path pads every gpt
   turn with a think block and appends. Downstream training sees mixed formats. Rebuild: one shared
   converter.
8. **R10.8 Compressor drops timed-out samples.** A trajectory that exceeds the 300 s per-item
   timeout disappears from the output (only counted), while other errors keep the original
   (possibly over-limit) entry. Decide explicitly; at least write a rejects file.
9. **R10.9 Missing default config and undeclared deps.** The compressor's default config path does
   not exist in the repo, and `transformers`/`datasets` are not declared anywhere; the timeout is
   not configurable from YAML; several YAML keys are inert. Ship a real default config and a
   `research` extra.
10. **R10.10 Remote code in the tokenizer.** The default tokenizer is loaded with
    `trust_remote_code = true`, which executes code from the model repo. Default it to false and
    require an explicit opt-in.
11. **R10.11 Contradictory pinning policy docs.** The packaging file says "exact pins, no ranges";
    the PM policy doc says bounded ranges and quotes a 14-day age rule while the config says 3 days;
    the CI check only rejects `>=` without `<`. Rebuild: one written policy that the CI check
    enforces literally.
12. **R10.12 Interpreter bridge markers.** All core deps carry `python_version >= 3.14` while the
    project allows 3.11+, so an install on 3.11–3.13 "succeeds" with zero dependencies. This exists
    only to let old installs reach their updater. A fresh rebuild should require one interpreter
    line and not copy the trick unless it has legacy installs to migrate.
13. **R10.13 Shadowed lint config.** A standalone `ruff.toml` makes the `[tool.ruff]` table in
    `pyproject.toml` dead; the pre-commit config is a near-identical copy; `ty` targets 3.13 while
    runtime is 3.14 and ruff targets 3.11. Keep one config, one target version.
14. **R10.14 Installer repository stage is the riskiest code.** It must never publish a half-cloned
    tree (clone in a sibling temp dir, then rename), must back up diverged commits to a ref before
    any hard reset, must auto-stash local edits with a findable label, must clear an unmerged index
    first, must respect a channel the user previously saved, and must treat an empty dir as
    replaceable but a non-checkout dir as an error. Stable-tag lookup needs layered fallbacks
    (API → redirect → ls-remote) and a strict tag regex.
15. **R10.15 Receipts and signals.** First-writer-wins receipts let both scripts record exactly one
    outcome even when a `finally` or EXIT trap fires again; signals must be noted, not acted on,
    while a foreground stage runs (bash 3.2 on macOS breaks Ctrl-C for background children), and
    the final exit status must be the signal's. A killed stage (SIGKILL/OOM) writes no receipt, so
    the ladder writes one on its behalf.
16. **R10.16 Mirror fallback only on network errors.** Falling back to the mirror on HTTP 4xx/5xx
    or digest mismatch would hide tampering; the reference falls back only on curl's network-class
    exit codes and always verifies the sha256 of whatever was fetched.
17. **R10.17 Fail-open change classification.** Lanes must err towards running; main pushes run
    every lane; tests asserting on JS/package files must live in the JS suite or they will be
    green on a JS-only PR and red on main. Data-only diffs may *replace* the Python suite with a
    targeted one only for a known file set.
18. **R10.18 Strict aggregate gate.** "Skipped" must count as failure for any job the classifier
    selected, and for all jobs in release mode except a short PR-only list; otherwise a broken
    `if:` silently skips a suite and the gate stays green.
19. **R10.19 Deterministic slicing.** Every CI slice computes its partition independently; using a
    per-job restored duration cache can drop or double-run files. Partition from the checkout alone;
    use timings only for reports.
20. **R10.20 Flake retry rules.** Retry a failed file once and surface pass-on-retry loudly; never
    retry a file killed by signal or timeout (a runaway would run twice). Per-file subprocesses are
    the isolation boundary; xdist-style shared workers leak module state.
21. **R10.21 Host gating via one marker.** A `skipif(sys.platform…)` test runs on no host because
    OS lanes import only marker-tagged files; two markers on one test must be a collection error.
22. **R10.22 Ratchet config read from main.** The code-health enforcement switch must be read from
    the base branch, never the PR, or a PR can disable its own check.
23. **R10.23 Release = tested bytes only.** Never rebuild for publication; push immutable artefacts
    by digest/version first, move aliases (`latest`, `stable`, channel heads) only from the receipt;
    make the controller idempotent because Docker Hub, object storage, APT, GitHub and the Store
    share no transaction; make the GitHub release public as the last call; order publication
    oldest-first.
24. **R10.24 Workflow SHA is not the commit under test.** Stable runs are dispatched from the
    default branch for cache scope; every build/test leg must receive the admitted commit
    explicitly (the reference lints for it).
25. **R10.25 Docker `--user` breaks bootstrap.** Under a supervisor that needs root for UID remap
    and volume chown, `docker run --user` must be rejected with guidance; use env UID/GID instead.
    A leading `-flag` CMD must be treated as a CLI option, never probed as an executable.
26. **R10.26 Canary versions must sort below stable.** Debian needs `~canary.<ts>`; SemVer build
    metadata makes the canary *equal* to its stable, so channel order must come from the channel
    record + timestamp, not SemVer. Keep one regex as the source of truth for both mapping and
    channel.
27. **R10.27 Termux/stable naming drift.** Comments still say "nightly" where code says "canary";
    one function's docstring disagrees with its body. Pick one word per channel and use it in suite
    names, versions and docs.
28. **R10.28 Translation parity.** All 16 non-English main catalogues have exactly the English key
    set; TUI catalogues are validated against a key-list file because English TUI strings live in
    source. A CI parity check is what keeps this true; a missing key must fall back to English, not
    crash.
29. **R10.29 Personal data in the repo.** Contributor email file names and a TSV of reattributed
    commits are personal data; do not copy, start empty.
30. **R10.30 Supply-chain scans are heuristics.** The PR scan looks for two obfuscation patterns on
    single added lines; it is cheap and catches the known worm style but is easy to evade. Pair it
    with exact pins, a lock-time age window, hash-verified downloads and an OSV scan.

## 7. Open questions

1. Should the rebuild publish wheels to PyPI, or keep "installer/Docker/Nix only" with a wheel
   guard? Needed: lead decision (affects assets layout and `pip install` support).
2. Exact content and order of the shipped Docker image's final layer (users, file modes, what is
   pre-seeded in `/opt/data` on first boot). Needed: needs REA (pull and inspect the published
   image and its first-boot volume).
3. What the hosted `install.ps1`/`install.sh` URLs actually serve today (same bytes as the repo at a
   stable tag, or main)? Needed: needs REA (fetch hosted scripts, diff against tags).
4. Real on-disk layout produced by a fresh install (launcher location, `.local/bin` symlink vs
   shim, PM store tree). Needed: needs REA (run installer in a clean VM, capture tree).
5. Termux `.deb` contents (file list, maintainer scripts, dependencies) and the APT `Release`
   signing key handling. Needed: needs REA (download the published `.deb` and `Release`).
6. MSIX identity/capabilities and App Installer feed fields of a shipped stable bundle. Needed:
   needs REA (unpack a published `.msixbundle`/`.appinstaller`).
7. Which uv `exclude-newer` window and pinning policy should the rebuild adopt (3 days vs 14 days;
   exact vs bounded)? Needed: lead decision (R10.11).
8. Does the rebuild need the interpreter-bridge marker trick (R10.12)? Needed: lead decision —
   only if migrating reference installs.
9. Hosting for release artefacts and channel records (R2-like object storage with conditional
   writes vs GitHub Releases only). Needed: lead decision; drives §3.17 scope.
10. Are batch-runner outputs consumed by an external training pipeline that depends on exact field
    order or the empty-think padding? Needed: lead/researcher input before changing R10.6/R10.7.
11. Default tokenizer and summariser models for the compressor in the rebuild (reference defaults
    are vendor-specific choices). Needed: lead decision.
12. Is the telemetry install receipt actually uploaded, and to where? Only the local write is in
    this area. Needed: needs REA (observe network after install) and the telemetry area spec.
13. Whether the docs site's `zh-Hans` translation and the 17-language catalogues are hand-written or
    machine-generated (affects licensing of the text and the rebuild's translation workflow).
    Needed: lead decision; history inspection.
14. Licences marked [unk] in §8's dependency table. Needed: read each package's metadata
    (`pip show`/PyPI JSON/npm view) during the THIRD_PARTY_NOTICES pass.

## 8. What to read later

- Reference docs: `website/docs/developer-guide/stable-releases.md`,
  `website/docs/developer-guide/trajectory-format.md`, `website/docs/getting-started/{installation,
  platform-support,termux,nix-setup,updating}.md`, `tests/AGENTS.md`, `pm/AGENTS.md`,
  `contributors/README.md`, every `evals/*/README.md`.
- Reference tests that list the edge cases: `tests/test_batch_runner_checkpoint.py`,
  `tests/test_batch_runner_discard_resume.py`, `tests/test_batch_runner_durability.py`,
  `tests/ci/test_required_results.py`, `tests/ci/test_stable_release_commit_threading.py`,
  `tests/scripts/test_termux_deb_version.py`, `tests/scripts/test_code_health*.py`,
  `tests/scripts/test_bootstrap_pins_fragment.py`, `tests/scripts/install/`, `tests/docker/`,
  `tests/pm/` (incl. the workspace build-input tests), `tests/install/KNOWN_FAILURES.md`.
- REA Evidence (outside the repo): none this session (REA not connected). Planned bundle:
  `~/rea-lab/evidence/10-packaging/` — Docker image inspect, hosted installer diff, fresh-install
  tree, Termux `.deb`, MSIX unpack.
- Related specs: runtime toolsets and tool registry (tools area) for §3.2 names; the agent loop
  area for the message list that §3.3 converts; the updater/self-update area for PM runtime, source
  completion and channel switching; the desktop area for bundle runtime and update feeds; the
  i18n runtime in the CLI/UI area for §3.18 lookup; the state-DB area for WAL checks used by CI.

### 8.1 Third-party dependencies and licences (feeds THIRD_PARTY_NOTICES)

Licences below are from general knowledge of each upstream project, **not** read from package
metadata in this session — all are [inf] unless marked [unk]; verify each before publishing
notices (§7 Q14). Versions are the reference pins (exact unless a range is shown).

**Python — core (`dependencies`)**

| Package | Pin | Licence |
|---|---|---|
| openai | 2.24.0 | Apache-2.0 |
| certifi | 2026.5.20 | MPL-2.0 |
| truststore | 0.10.4 | MIT |
| python-dotenv | 1.2.2 | BSD-3-Clause |
| fire | 0.7.1 | Apache-2.0 |
| httpx[socks] | 0.28.1 | BSD-3-Clause |
| rich | 14.3.3 | MIT |
| tenacity | 9.1.4 | Apache-2.0 |
| tomli-w | 1.2.0 | MIT |
| ruamel.yaml | 0.18.16 | MIT |
| requests | 2.33.0 | Apache-2.0 |
| jinja2 | 3.1.6 | BSD-3-Clause |
| firecrawl-anydoc | 0.2.4 | [unk] |
| pydantic | 2.13.4 | MIT |
| prompt_toolkit | 3.0.52 | BSD-3-Clause |
| croniter | 6.0.0 | MIT |
| snowballstemmer | 3.1.1 | BSD-3-Clause |
| packaging | 26.0 | Apache-2.0 OR BSD-2-Clause |
| Markdown | 3.10.2 | BSD-3-Clause |
| PyJWT[crypto] | 2.15.1 | MIT |
| urllib3 | >=2.8.0,<3 | MIT |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause |
| tzdata (win32) | 2025.3 | Apache-2.0 |
| psutil | 7.2.2 (+ git SHA on Android) | BSD-3-Clause |
| websockets | 15.0.1 | BSD-3-Clause |
| browser-harness | 0.1.13 | [unk] |
| pathspec | 1.1.1 | MPL-2.0 |
| fastapi | >=0.104.0,<1 | MIT |
| uvicorn | >=0.31.0,<1 | BSD-3-Clause |
| httptools | >=0.6.3,<0.9 | MIT |
| watchfiles | >=0.20,<2 | MIT |
| python-multipart | >=0.0.9,<1 | Apache-2.0 |
| ptyprocess (non-win) | >=0.7.0,<1 | ISC |
| pywinpty (win) | >=3.0.5,<4 | MIT |
| pywin32 (win) | >=306,<312 | PSF-2.0 |
| winrt-windows-* (4 pkgs, win) | >=3.2.1,<4 | MIT |
| Pillow | 12.3.0 | MIT-CMU (HPND) |
| pillow-heif | >=1.4.0,<2 | BSD-3-Clause (bundles libheif: LGPL-3.0 — check) |
| resvg-py | 0.4.0 | [unk] (resvg itself Apache-2.0/MIT) |
| concurrent-log-handler (win) | 0.9.29 | Apache-2.0 |
| nemo-relay (some platforms) | >=0.10,<0.11 | [unk] |

**Python — extras**

| Extra(s) | Packages (pin) | Licence |
|---|---|---|
| uvloop | uvloop (>=0.15.1,<0.24) | MIT OR Apache-2.0 |
| anthropic, test | anthropic 0.87.0 | MIT |
| exa | exa-py 2.10.2 | MIT |
| firecrawl | firecrawl-py 4.17.0 | MIT |
| parallel-web | parallel-web 0.4.2 | [unk] |
| ddgs | ddgs 9.16.0 | MIT |
| fal | fal-client 0.13.1 | Apache-2.0 |
| edge-tts | edge-tts 7.2.7 | [unk] (copyleft family — check) |
| neutts | neutts[all] 1.4.1 | [unk] |
| kittentts | kittentts 0.8.1 (wheel URL), misaki[en] (git fork), soundfile 0.14.0 | Apache-2.0, Apache-2.0, BSD-3-Clause |
| piper | piper-tts 1.8.0 | [unk] (recent releases GPL-3.0 — check) |
| modal | modal 1.5.5 | Apache-2.0 |
| daytona | daytona 0.155.0 | Apache-2.0 |
| vercel | vercel 0.7.2 | [unk] |
| google-meet | playwright 1.62.0 | Apache-2.0 |
| messaging / telegram / termux | python-telegram-bot[webhooks] 22.8 | LGPL-3.0 |
| messaging / discord | discord.py[voice] 2.7.1 | MIT |
| messaging / slack / matrix / sms / teams | aiohttp 3.14.3 | Apache-2.0 |
| messaging | brotlicffi 1.2.0.2 | MIT |
| messaging / slack | slack-bolt 1.30.0, slack-sdk 3.44.1 | MIT |
| messaging / dingtalk / feishu | qrcode 7.4.2 | BSD-3-Clause |
| matrix (Linux) | mautrix[encryption] 0.21.1, aiosqlite 0.22.1, asyncpg 0.31.0, aiohttp-socks 0.11.0 | MPL-2.0, MIT, Apache-2.0, Apache-2.0 |
| wecom | defusedxml 0.7.1 | PSF-2.0 |
| tts-premium | elevenlabs 1.59.0 | MIT |
| voice / stt-whisper | faster-whisper 1.2.1 | MIT |
| voice / wake / audio-io | sounddevice 0.5.5, numpy 2.4.3 | MIT, BSD-3-Clause |
| wake / wake-openwakeword | pyopen-wakeword 1.1.0 | [unk] |
| wake / wake-sherpa | sherpa-onnx 1.13.8, sherpa-onnx-core 1.13.8, sentencepiece 0.2.2, pypinyin 0.55.0 | Apache-2.0, Apache-2.0, Apache-2.0, MIT |
| wake / wake-porcupine | pvporcupine 4.0.3 | Apache-2.0 (SDK; engine needs a vendor access key — check terms) |
| silk | pilk 0.2.4 | [unk] |
| google-chat | google-cloud-pubsub 2.39.2 | Apache-2.0 |
| doc-extract | firecrawl-anydoc 0.2.4 | [unk] |
| trace-upload | huggingface-hub 1.24.0 | Apache-2.0 |
| mcp / computer-use / dev | mcp 2.3.0, httpx2 2.12.0, starlette 1.3.1 | MIT, [unk], BSD-3-Clause |
| teams | microsoft-teams-apps 2.0.13.4 | MIT |
| acp | agent-client-protocol 0.9.0 | Apache-2.0 |
| mistral | mistralai 2.4.8 | Apache-2.0 |
| otlp | opentelemetry-sdk 1.39.1, opentelemetry-exporter-otlp-proto-http 1.39.1 | Apache-2.0 |
| bedrock | boto3 1.42.89 | Apache-2.0 |
| vertex / google | google-auth 2.55.1 | Apache-2.0 |
| azure-identity | azure-identity 1.25.3 | MIT |
| dingtalk | dingtalk-stream 0.24.3, alibabacloud-dingtalk 2.2.42 | MIT, Apache-2.0 |
| feishu | lark-oapi 1.6.8 | MIT |
| google | google-api-python-client 2.194.0, google-auth-oauthlib 1.3.1, google-auth-httplib2 0.3.1, httplib2 0.32.0, pyasn1 0.6.4 | Apache-2.0, Apache-2.0, Apache-2.0, MIT, BSD-2-Clause |
| youtube | youtube-transcript-api 1.2.4 | MIT |
| web | fastapi 0.133.1, uvicorn 0.41.0, starlette 1.3.1, python-multipart 0.0.32 | MIT, BSD-3-Clause, BSD-3-Clause, Apache-2.0 |

**Python — dev/test groups and build**: debugpy 1.8.20 (MIT), pytest 9.1.1 (MIT), pytest-asyncio
1.3.0 (Apache-2.0), ty 0.0.82 (MIT), ruff 0.15.10 (MIT), setuptools 83.0.0 (MIT), wheel (MIT),
distlib 0.4.3 (PSF-2.0). Overrides: pynacl >=1.6,<1.7 (Apache-2.0). Research scripts also import
(undeclared) transformers (Apache-2.0) and datasets (Apache-2.0).

**PM-pinned tool binaries** (redistributed by the installer/bundles — notices matter):
uv 0.12.3 (MIT OR Apache-2.0); CPython via standalone builds 3.14.7 (PSF-2.0 + bundled libs);
Node 26.7.0 (MIT + bundled); npm 12.0.2 (Artistic-2.0); ripgrep 15.2.0 (MIT OR Unlicense); gh
2.97.0 (MIT); Git for Windows 2.53.0 (GPL-2.0 — source-offer obligations); ffmpeg 9.0.1 (LGPL-2.1+
or GPL depending on build — check which); Chromium build 1208 (BSD-3-Clause + many); llama.cpp
build 11671 (MIT); whisper.cpp b5130 (MIT); dmgbuild 1.2.5 (MIT); agent-browser 0.26.0 [unk];
bws 2.0.0 [unk] (vendor SDK licence — check); cua-driver 0.21.0 [unk]; iron-proxy 0.39.0 [unk];
termux-docker image [unk]. Docker base: Debian 13.4 (various, mostly GPL/LGPL/BSD), SQLite
amalgamation 3.53.4 (public domain), s6-overlay 3.2.3.0 (ISC).

**JavaScript (root dev tooling)**: @eslint/js 9.39.5 (MIT), eslint 10.12.0 (MIT),
eslint-plugin-perfectionist 5.10.0 (MIT), eslint-plugin-react-hooks 7.1.1 (MIT),
eslint-plugin-unused-imports 4.4.1 (MIT), globals 17.7.0 (MIT), lefthook 2.1.14 (MIT), typescript
6.0.3 (Apache-2.0), typescript-eslint 8.64.0 (MIT); runtime deps js-yaml 4.3.2 (MIT), semver 7.7.4
(ISC).

**JavaScript (docs site)**: @docusaurus/core, plugin-client-redirects, preset-classic,
theme-mermaid, module-type-aliases, tsconfig, types — all 3.10.2 (MIT); @mdx-js/react 3.1.1 (MIT);
@mermaid-js/layout-elk 0.1.9 (MIT); clsx 2.1.1 (MIT); prism-react-renderer 2.3.0 (MIT); react /
react-dom 19.2.7 (MIT); an `image-size` override aliased to a vendor-scoped fork 2.0.3 (MIT
upstream — [unk] for the fork).

**Nix flake inputs**: nixpkgs (MIT), flake-parts (MIT), pyproject.nix (MIT), uv2nix (MIT),
pyproject build-system-pkgs (MIT), home-manager (MIT), npm-lockfile-fix [unk].

**CI-only (not redistributed)**: GitHub Actions pinned by SHA (checkout, cache, upload-artifact,
setup-node, setup-uv, upload-pages-artifact, deploy-pages, a runner vendor's checkout fork),
hadolint (GPL-3.0, tool use only), OSV-Scanner (Apache-2.0), actionlint (MIT).
