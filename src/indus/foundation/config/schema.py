"""Config schema: Pydantic models, the one defaults table and the commented example.

`DEFAULTS` is generated from the models, so every default lives in exactly one place (D-16.32).
Sections without a model yet are kept as raw mappings; unknown keys inside a modelled section are
kept and reported, never dropped.
"""

from __future__ import annotations

import copy
import types
import typing
from collections.abc import Mapping
from typing import Any, Literal, Union, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from indus.foundation import yamlio

CURRENT_VERSION = 1
VERSION_KEY = "_config_version"


class _Section(BaseModel):
    model_config = ConfigDict(extra="allow", validate_default=True)


class ModelSection(_Section):
    """Which model to talk to; ``model: <id>`` in the file is shorthand for ``default``."""

    default: str = Field(default="", description="model id used when nothing else picks one")
    provider: str = Field(default="", description="provider name; empty means detect from keys")
    base_url: str = Field(default="", description="custom endpoint URL (OpenAI-compatible)")
    api_mode: str = Field(default="", description="wire format override for the endpoint")
    context_length: int | None = Field(
        default=None, description="context window override in tokens"
    )


class AgentSection(_Section):
    max_turns: int = Field(
        default=150, description="model calls per turn before a summary; 0 = no cap"
    )
    api_max_retries: int = Field(default=3, description="retries for a failed model call")
    gateway_timeout: int = Field(default=1800, description="seconds a messaging turn may run")
    clarify_timeout: int = Field(
        default=3600, description="seconds to wait for an answer to a question"
    )
    local_stream_stale_timeout: int = Field(
        default=900, description="seconds a local model server may stay silent mid-stream"
    )
    turn_liveness_s: float = Field(
        default=600,
        description="seconds a reply may show no activity before it is stopped (0 = off)",
    )
    run_budget_s: float = Field(
        default=0, description="wall-clock seconds one run may take, like --run-budget (0 = off)"
    )
    fast_mode: Literal["normal", "fast", "auto"] = Field(
        default="normal",
        description="the provider's priority tier: normal, fast (always) or auto (per turn)",
    )
    fast_window_s: float = Field(
        default=60, description="seconds each user turn asks for the priority tier in auto mode"
    )


class TerminalSection(_Section):
    backend: str = Field(default="local", description="where commands run: local, docker, ssh, ...")
    cwd: str = Field(default=".", description="working directory for commands")
    timeout: int = Field(default=180, description="seconds before a command is stopped")
    env_passthrough: list[str] = Field(
        default_factory=list, description="extra variable names passed through to commands"
    )
    persistent_shell: bool = Field(
        default=True, description="exported variables carry over between commands (cwd always does)"
    )
    temp_dir: str = Field(
        default="", description="temp dir for shell snapshots; empty = TMPDIR or <home>/cache"
    )
    lifetime_s: int = Field(
        default=300, description="seconds an unused environment is kept before it is cleaned up"
    )
    degraded_mode: Literal["warn", "fail"] = Field(
        default="warn",
        description="backend unreachable: warn returns a degraded result, fail returns an error",
    )
    daemon_term_grace_s: float = Field(
        default=2.0, description="seconds a stopped command gets after SIGTERM before SIGKILL"
    )
    max_foreground_timeout: int = Field(
        default=600,
        description="longest foreground timeout; above it a command runs as a background job",
    )
    process_wait_auto_notify: bool = Field(
        default=True,
        description="two checks of a silent background job switch on a message when it ends",
    )
    docker_binary: str = Field(
        default="", description="docker or podman client path; empty = docker, then podman on PATH"
    )
    docker_image: str = Field(
        default="ghcr.io/indus-agent/indus-sandbox:slim",
        description="image the Docker sandbox runs (Indus's own sandbox image by default)",
    )
    docker_image_pinned: bool = Field(
        default=False, description="docker_image must name a digest (@sha256:...)"
    )
    docker_forward_env: list[str] = Field(
        default_factory=list,
        description="variable names passed into the container (values never on a command line)",
    )
    docker_env: dict[str, str] = Field(
        default_factory=dict, description="extra variables set inside the container"
    )
    docker_volumes: list[str] = Field(
        default_factory=list, description="host:container[:options] binds (prompts come back on)"
    )
    docker_extra_args: list[str] = Field(
        default_factory=list, description="extra docker run arguments, added last"
    )
    docker_shm_size: str = Field(
        default="1g", description="size of /dev/shm; empty or 0 keeps Docker's own default"
    )
    docker_network: bool = Field(default=True, description="false runs the container offline")
    docker_run_as_host_user: bool = Field(
        default=False, description="run as your uid:gid so files you share stay yours"
    )
    docker_snap_compat: bool = Field(
        default=False, description="snap-packaged Docker: run without no-new-privileges"
    )
    docker_mount_cwd_to_workspace: bool = Field(
        default=False,
        description="bind the host working directory at /workspace (approval prompts come back on)",
    )
    docker_persist_across_processes: bool = Field(
        default=True, description="reuse the labelled container a previous run left"
    )
    docker_shared_key: str = Field(
        default="", description="profiles with the same key share one container"
    )
    docker_orphan_reaper: bool = Field(
        default=True, description="remove old Indus containers of this profile at start"
    )
    container_cpu: float = Field(default=1.0, description="CPUs for a sandbox container")
    container_memory_mb: int = Field(default=5120, description="memory cap of a sandbox, in MB")
    container_disk_mb: int = Field(
        default=51200, description="disk cap of a sandbox in MB (where the host supports it)"
    )
    container_persistent: bool = Field(
        default=True,
        description="keep /root and /workspace between runs; false = a fresh sandbox per session",
    )
    ssh_host: str = Field(default="", description="ssh backend: host to run commands on")
    ssh_user: str = Field(default="", description="ssh backend: login user; empty = ssh's default")
    ssh_port: int = Field(default=22, description="ssh backend: port")
    ssh_key: str = Field(default="", description="ssh backend: identity file; empty = agent/keys")
    ssh_persistent_shell: bool = Field(
        default=True, description="ssh backend: exported variables carry over between commands"
    )
    credential_files: list[str] = Field(
        default_factory=list,
        description="host files copied into sandboxes (mounted read-only, pushed upload-only)",
    )


class ToolOutputSection(_Section):
    max_chars: int = Field(
        default=50000, description="characters of command output shown to the model"
    )


class ApprovalsSection(_Section):
    mode: Literal["manual", "smart", "off"] = Field(
        default="manual",
        description="manual asks before risky commands; smart pre-judges; off never asks",
    )
    timeout: int = Field(default=300, description="seconds to wait for an approval answer")
    deny: list[str] = Field(
        default_factory=list, description="command patterns that are always refused"
    )
    cron_mode: Literal["deny", "approve"] = Field(
        default="deny", description="risky commands in scheduled jobs: deny or approve"
    )
    single_query_mode: Literal["deny", "approve"] = Field(
        default="deny", description="risky commands in one-shot runs (indus -q): deny or approve"
    )
    unattended_mode: Literal["deny", "approve"] = Field(
        default="deny",
        description="risky commands where nobody can answer (API, webhooks): deny or approve",
    )
    denial_breaker_threshold: int = Field(
        default=3,
        description="smart mode: refusals in a row before the model is told to stop (0 = off)",
    )
    smart_policy: str = Field(
        default="", description="smart mode: extra rules for the judge, in plain words"
    )


class CheckpointsSection(_Section):
    """File snapshots taken before the first file change of a turn (P11-15..18)."""

    enabled: bool = Field(
        default=False, description="snapshot the working folder before the agent changes files"
    )
    max_snapshots: int = Field(default=20, description="checkpoints kept per folder")
    max_total_size_mb: int = Field(
        default=500, description="the automatic prune drops the oldest checkpoints above this"
    )
    max_file_size_mb: int = Field(
        default=10, description="files larger than this are left out of checkpoints"
    )
    retention_days: int = Field(
        default=7, description="folders not checkpointed for this many days are pruned"
    )
    min_interval_hours: int = Field(
        default=24, description="hours between two automatic prunes of the checkpoint store"
    )
    auto_prune: bool = Field(
        default=True, description="prune the checkpoint store automatically (at most once a day)"
    )


class WorktreesSection(_Section):
    """Session worktrees under <repo>/.worktrees (P11-19)."""

    prune_after_hours: int = Field(
        default=24, description="clean, merged worktrees older than this are pruned"
    )
    fetch_timeout_s: float = Field(
        default=5.0, description="seconds to refresh the base branch before creating a worktree"
    )
    sync_base: bool = Field(
        default=True, description="branch new worktrees from the fetched upstream, not local HEAD"
    )


class DatabaseSection(_Section):
    journal_mode: Literal["wal", "delete"] = Field(default="wal", description="SQLite journal mode")
    pragmas: dict[str, Any] = Field(default_factory=dict, description="extra SQLite pragmas")


class SessionsSection(_Section):
    retention_days: int = Field(default=90, description="days ended sessions are kept")
    auto_prune: bool = Field(default=False, description="delete old sessions automatically")
    resume_message_limit: int = Field(default=20000, description="messages loaded when resuming")
    search_slow_ms: int = Field(
        default=1000, description="log searches slower than this many ms (0 logs every search)"
    )


class LoggingSection(_Section):
    level: str = Field(default="INFO", description="level for agent.log")
    max_size_mb: int = Field(default=5, description="size of one log file before it rotates")
    backup_count: int = Field(default=3, description="rotated files kept")


class WebsiteBlocklist(_Section):
    enabled: bool = Field(default=False, description="refuse URLs whose host matches a rule")
    domains: list[str] = Field(
        default_factory=list, description="hosts, URLs or *.globs to refuse (subdomains included)"
    )
    shared_files: list[str] = Field(
        default_factory=list, description="files with one rule per line (relative to the home)"
    )


class SecuritySection(_Section):
    redact_secrets: bool = Field(default=True, description="mask secrets in logs and tool output")
    secret_store: Literal["auto", "keychain", "file"] = Field(
        default="auto",
        description="where API keys live: OS keychain first (auto), keychain or file",
    )
    allow_private_urls: bool = Field(
        default=False,
        description="let web tools reach private/LAN addresses (cloud metadata stays refused)",
    )
    fake_ip_ranges: list[str] = Field(
        default_factory=list,
        description="CIDR blocks a local TUN proxy answers DNS with (dialable, not private)",
    )
    website_blocklist: WebsiteBlocklist = Field(
        default_factory=WebsiteBlocklist, description="hosts web tools must never fetch"
    )
    protected_instruction_files: list[str] = Field(
        default_factory=list,
        description="more file globs whose every write asks first (AGENTS.md etc. always do)",
    )
    allow_data_training_tiers_noninteractive: bool = Field(
        default=False,
        description="let unattended runs use a -m/--provider route whose tier trains on your data",
    )
    acked_advisories: list[str] = Field(
        default_factory=list, description="advisory ids `indus doctor --ack` recorded as read"
    )


class DoctorSection(_Section):
    live_probe_timeout_s: float = Field(
        default=10.0, description="seconds `indus doctor` waits for each live provider check"
    )


class WebSection(_Section):
    backend: str = Field(default="", description="provider for search and extract; empty = auto")
    search_backend: str = Field(default="", description="provider for web_search only")
    extract_backend: str = Field(default="", description="provider for web_extract only")
    keyless_rescue: bool = Field(
        default=True, description="retry a failed keyed search once on a keyless provider"
    )
    searxng_url: str = Field(default="", description="your SearXNG instance, e.g. http://host:8080")


class DisplaySection(_Section):
    language: str = Field(default="en", description="interface language")
    interface: Literal["cli", "tui"] = Field(
        default="cli", description="interface on a terminal: cli (classic prompt) or tui"
    )
    busy_input_mode: str = Field(
        default="interrupt",
        description="Enter while a reply runs: interrupt, queue or steer (else: interrupt)",
    )
    final_response_markdown: Literal["render", "strip", "raw"] = Field(
        default="strip",
        description="final reply in the classic prompt: strip heavy markdown, render it, or raw",
    )
    resume_display: bool = Field(
        default=True, description="show the earlier conversation when a session is resumed"
    )
    resume_exchanges: int = Field(default=10, description="exchanges shown in the resume recap")
    resume_max_user_chars: int = Field(
        default=300, description="characters of each user message in the recap"
    )
    resume_max_assistant_chars: int = Field(
        default=200, description="characters of each reply in the recap"
    )
    resume_max_assistant_lines: int = Field(
        default=3, description="lines of each reply in the recap"
    )
    cli_refresh_interval: float = Field(
        default=1.0, description="seconds between status bar refreshes"
    )
    cli_multiline_shortcuts: bool = Field(
        default=True, description="Ctrl+J adds a new line in the classic prompt (false: it sends)"
    )
    show_reasoning: bool = Field(
        default=False, description="show the model's reasoning text in the classic prompt"
    )
    bell_on_prompt: bool = Field(
        default=True, description="ring the terminal bell when a question needs an answer"
    )
    learning_notices: Literal["off", "on", "verbose"] = Field(
        default="on",
        description="after a background review: off, on (one line per change) or verbose "
        "(with a short preview)",
    )


class OnboardingSeen(_Section):
    busy_input: bool = Field(
        default=False, description="the tip about typing while a reply runs was shown"
    )


class OnboardingSection(_Section):
    seen: OnboardingSeen = Field(
        default_factory=OnboardingSeen, description="one-time tips already shown"
    )
    setup_completed: bool = Field(
        default=False, description="`indus setup` has been run to the end once"
    )


class SessionSection(_Section):
    terminal_continue: bool = Field(
        default=True,
        description="a bare `indus -c` continues the session last used in the same terminal",
    )


class NetworkSection(_Section):
    force_ipv4: bool = Field(default=False, description="connect over IPv4 only")


class AuxTaskSection(_Section):
    """Which model one side task (titles, summaries, the approval judge, ...) uses."""

    provider: str = Field(default="auto", description="provider name; auto = the chat model's")
    model: str = Field(default="", description="model id; empty = the provider's chat model")
    base_url: str = Field(default="", description="endpoint URL for a custom provider")
    reasoning_effort: str = Field(default="", description="reasoning level; empty = none")
    timeout_s: float | None = Field(
        default=None, description="seconds one call may take; empty = the task's default"
    )
    fallback: list[dict[str, Any]] = Field(
        default_factory=list[dict[str, Any]],
        description="routes to try next: a list of {provider, model}",
    )


class AuxiliarySection(_Section):
    """Side-task models; tasks not listed here use ``auto`` (the chat model)."""

    compression: AuxTaskSection = Field(
        default_factory=AuxTaskSection, description="context compression summaries"
    )
    title: AuxTaskSection = Field(
        default_factory=AuxTaskSection, description="automatic session titles"
    )
    approval: AuxTaskSection = Field(
        default_factory=AuxTaskSection, description="the smart approval judge"
    )
    curator: AuxTaskSection = Field(
        default_factory=AuxTaskSection, description="the curator's consolidation pass"
    )
    review: AuxTaskSection = Field(
        default_factory=AuxTaskSection,
        description="the background review that saves memory and improves skills",
    )


class ModelCatalogSection(_Section):
    ttl_minutes: int = Field(
        default=20, description="minutes a provider's live model list is reused before refetching"
    )


class PromptSection(_Section):
    """How the system prompt is put together (P04-05/06)."""

    task_completion_guidance: bool = Field(
        default=True, description="ask the model to finish work with real tool output"
    )
    parallel_tool_guidance: bool = Field(
        default=True, description="suggest sending independent tool calls together"
    )
    tool_use_enforcement: str | bool | list[str] = Field(
        default="auto",
        description="act-with-tools guidance: auto (by model family), true, false or a list of "
        "model-name substrings",
    )
    execution_guidance: str | bool | list[str] = Field(
        default="auto",
        description="check-facts-with-tools guidance: auto, true, false or model-name substrings",
    )
    context_file_max_chars: int | None = Field(
        default=None,
        description="character cap for one project context file; empty = scale with the window",
    )
    context_file_read_timeout: float = Field(
        default=5.0, description="seconds to wait for a context file before skipping it"
    )
    platform_hints: dict[str, Any] = Field(
        default_factory=dict,
        description="per-surface hint changes: <surface>: text to append, or {append, replace}",
    )


class PromptCachingSection(_Section):
    """Prompt-cache markers on routes that use them (P08-07)."""

    cache_ttl: str = Field(
        default="5m",
        description="how long marked prefixes stay cached: 5m, 1h, or auto (1h when a person "
        "drives the session, 5m for automated runs)",
    )


class FallbackSection(_Section):
    """How the main fallback chain (``fallback_providers``) switches (P08-13)."""

    min_switch_reset_s: float = Field(
        default=0.0,
        description="stay on a rate-limited main model when its limit resets sooner than this "
        "many seconds (wait instead of switching); 0 = always switch",
    )


class CompressionSection(_Section):
    """When and how a long conversation is summarised (P08-02..05)."""

    enabled: bool = Field(default=True, description="compress long conversations automatically")
    threshold: float = Field(
        default=0.50,
        description="share of the usable window that triggers compression (windows under 512K "
        "never trigger below 0.75)",
    )
    threshold_tokens: int | None = Field(
        default=None, description="absolute trigger cap in tokens; empty = no cap"
    )
    model_thresholds: dict[str, float] = Field(
        default_factory=dict,
        description="per-model share overrides: <model substring> or <provider>:<substring>",
    )
    max_attempts: int = Field(
        default=3, description="ineffective compression attempts allowed per turn (1-10)"
    )
    timeout_s: float = Field(
        default=120.0, description="seconds the summary call may take; also the shortest cooldown"
    )
    tail_mode: Literal["lean", "legacy"] = Field(
        default="lean",
        description="lean: small recent tail + verbatim user messages in the summary",
    )
    target_ratio: float = Field(
        default=0.20, description="legacy mode: tail size as a share of the trigger"
    )
    min_tail_user_messages: int = Field(
        default=1, description="real user messages always kept verbatim after a compression"
    )
    in_place: bool = Field(
        default=True, description="keep one session id (true) or continue in a child session"
    )
    abort_on_summary_failure: bool = Field(
        default=False, description="never drop the middle when the summary call fails"
    )
    idle_after_s: int = Field(
        default=0, description="compress after this many idle seconds; 0 = off"
    )


class MemorySection(_Section):
    """The two built-in memory stores (P09-01..03)."""

    notes_enabled: bool = Field(
        default=True, description="keep the agent's own notes (memories/MEMORY.md)"
    )
    profile_enabled: bool = Field(
        default=True, description="keep what the agent learns about you (memories/USER.md)"
    )
    notes_char_limit: int = Field(
        default=2200, description="characters the notes store may hold, separators included"
    )
    profile_char_limit: int = Field(
        default=1375, description="characters the user profile store may hold"
    )
    write_approval: bool = Field(
        default=False,
        description="ask before memory writes (interactive CLI) or stage them for "
        "`indus memory approve`",
    )
    review_interval: int = Field(
        default=10, description="user turns between background memory reviews; 0 = never"
    )


class SessionSearchSection(_Section):
    summarize: bool = Field(
        default=False,
        description="let the side-task model condense long session_search reads",
    )


class ContextSection(_Section):
    engine: str = Field(
        default="builtin", description="context engine; plugins never activate by themselves"
    )


class SkillsSection(_Section):
    """Where skills come from and how they load (P09-06..13)."""

    create_dir: str = Field(
        default="", description="folder for skills the agent creates; empty = <home>/skills"
    )
    external_dirs: list[str] = Field(
        default_factory=list, description="more skill folders, searched after your own (~, ${VAR})"
    )
    disabled: list[str] = Field(
        default_factory=list, description="skills hidden everywhere and refused when viewed"
    )
    platform_disabled: dict[str, list[str]] = Field(
        default_factory=dict, description="per surface (cli, telegram, ...): more hidden skills"
    )
    auto_load: list[str] = Field(
        default_factory=list, description="skills loaded in full into every new session"
    )
    template_vars: bool = Field(
        default=True, description="replace ${INDUS_SKILL_DIR} / ${INDUS_SESSION_ID} in skills"
    )
    inline_shell: bool = Field(
        default=False, description="run !`command` snippets in skills when they load (10 s each)"
    )
    config: dict[str, Any] = Field(
        default_factory=dict, description="values for settings that skills declare"
    )
    scan_agent_created: bool = Field(
        default=False, description="scan skills the agent writes with the content scanner"
    )
    ledger_max_bytes: int = Field(
        default=5 * 1024 * 1024, description="size of the skill change ledger before it is trimmed"
    )
    write_approval: bool = Field(
        default=False,
        description="hold every skill write the agent makes as a pending change you approve",
    )
    review_interval: int = Field(
        default=10,
        description="tool-calling steps between background skill reviews; 0 = never",
    )


class LearningReviewSection(_Section):
    """The background review that runs after busy turns (P09-14..16)."""

    enabled: bool = Field(
        default=True, description="review automatically after busy turns (/refine always works)"
    )
    extra_tools: list[str] = Field(
        default_factory=list, description="more of the session's tools the review may call"
    )
    max_input_tokens: int | None = Field(
        default=None,
        description="input tokens one review may spend in all; empty = 75% of the model's "
        "window (at most 600000); 0 = no limit",
    )
    defer: Literal["auto", "never"] = Field(
        default="auto",
        description="auto: reviews on a local model server wait until the machine is quiet",
    )
    defer_max_age_s: float = Field(
        default=1800.0, description="a waiting review runs anyway after this many seconds"
    )


class LearningSection(_Section):
    review: LearningReviewSection = Field(
        default_factory=LearningReviewSection, description="the background review"
    )


class CuratorBackupSection(_Section):
    enabled: bool = Field(
        default=True, description="snapshot the skills before a consolidation pass"
    )
    keep: int = Field(default=2, description="snapshots kept; older ones are removed")


class CuratorSection(_Section):
    """Idle-time upkeep of the skills the background review wrote (P09-18..20)."""

    enabled: bool = Field(default=True, description="age unused agent-made skills")
    interval_hours: int = Field(default=168, description="hours between curator runs")
    min_idle_hours: float = Field(
        default=2.0, description="hours the agent must be idle before a run"
    )
    stale_after_days: int = Field(default=14, description="unused days before a skill is stale")
    archive_after_days: int = Field(
        default=30, description="unused days before a skill is moved to the archive"
    )
    consolidate: bool = Field(
        default=False, description="let a model merge overlapping skills on each run"
    )
    prune_builtins: bool = Field(
        default=False, description="also archive unused bundled skills (never merged)"
    )
    archive_ttl_days: int = Field(
        default=0, description="`indus curator purge` deletes archives older than this; 0 = never"
    )
    backup: CuratorBackupSection = Field(
        default_factory=CuratorBackupSection, description="skill snapshots"
    )


class ConfigModel(BaseModel):
    """The whole file. Unknown top-level sections are kept as they are."""

    model_config = ConfigDict(extra="allow", validate_default=True, populate_by_name=True)

    model: str | ModelSection = Field(
        default_factory=ModelSection, description="the chat model and its endpoint"
    )
    providers: dict[str, dict[str, Any]] = Field(
        default_factory=dict, description="named custom providers"
    )
    fallback_providers: list[dict[str, Any]] = Field(
        default_factory=list[dict[str, Any]],
        description="routes tried in order when the main one fails: {provider, model} entries",
    )
    fallback: FallbackSection = Field(
        default_factory=FallbackSection, description="switching to the fallback chain"
    )
    agent: AgentSection = Field(default_factory=AgentSection, description="the turn loop")
    terminal: TerminalSection = Field(
        default_factory=TerminalSection, description="where and how commands run"
    )
    tool_output: ToolOutputSection = Field(
        default_factory=ToolOutputSection, description="how much tool output the model sees"
    )
    file_read_max_chars: int = Field(
        default=100000, description="characters one read_file call returns at most"
    )
    approvals: ApprovalsSection = Field(
        default_factory=ApprovalsSection, description="asking before risky commands"
    )
    command_allowlist: list[str] = Field(
        default_factory=list,
        description="commands that never need approval: exact text, or a glob for one command",
    )
    database: DatabaseSection = Field(
        default_factory=DatabaseSection, description="the session store"
    )
    sessions: SessionsSection = Field(
        default_factory=SessionsSection, description="keeping and resuming sessions"
    )
    logging: LoggingSection = Field(
        default_factory=LoggingSection, description="log files under <home>/logs"
    )
    security: SecuritySection = Field(
        default_factory=SecuritySection, description="secrets and redaction"
    )
    web: WebSection = Field(default_factory=WebSection, description="web search and page extract")
    display: DisplaySection = Field(default_factory=DisplaySection, description="the interface")
    session: SessionSection = Field(
        default_factory=SessionSection, description="how the CLI picks a session to continue"
    )
    onboarding: OnboardingSection = Field(
        default_factory=OnboardingSection, description="one-time hints"
    )
    quick_commands: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="your own slash commands: <name>: {type: exec, command: ...} or "
        "{type: alias, target: ...}",
    )
    network: NetworkSection = Field(default_factory=NetworkSection, description="connections")
    model_catalog: ModelCatalogSection = Field(
        default_factory=ModelCatalogSection, description="provider model lists"
    )
    prompt: PromptSection = Field(
        default_factory=PromptSection, description="how the system prompt is put together"
    )
    auxiliary: AuxiliarySection = Field(
        default_factory=AuxiliarySection, description="models for side tasks"
    )
    prompt_caching: PromptCachingSection = Field(
        default_factory=PromptCachingSection, description="provider prompt caching"
    )
    compression: CompressionSection = Field(
        default_factory=CompressionSection, description="summarising long conversations"
    )
    context: ContextSection = Field(
        default_factory=ContextSection, description="the context engine"
    )
    timezone: str = Field(
        default="", description="IANA zone name, e.g. Asia/Kolkata; empty = system zone"
    )
    credential_pool_strategies: dict[str, str] = Field(
        default_factory=dict,
        description="per provider: how its credential pool picks a key — fill_first (default), "
        "round_robin, least_used or random",
    )
    platform_toolsets: dict[str, list[str]] = Field(
        default_factory=dict,
        description="per platform (cli, ...): the toolsets it gets; unset = the platform's bundle "
        "(`indus tools`)",
    )
    doctor: DoctorSection = Field(default_factory=DoctorSection, description="`indus doctor`")
    checkpoints: CheckpointsSection = Field(
        default_factory=CheckpointsSection, description="file snapshots and /rollback"
    )
    worktrees: WorktreesSection = Field(
        default_factory=WorktreesSection, description="session worktrees (indus -w)"
    )
    memory: MemorySection = Field(
        default_factory=MemorySection, description="what the agent remembers between sessions"
    )
    session_search: SessionSearchSection = Field(
        default_factory=SessionSearchSection, description="recalling earlier conversations"
    )
    skills: SkillsSection = Field(
        default_factory=SkillsSection, description="skill folders and loading"
    )
    curator: CuratorSection = Field(
        default_factory=CuratorSection, description="upkeep of skills the agent wrote"
    )
    learning: LearningSection = Field(
        default_factory=LearningSection, description="learning from finished conversations"
    )
    config_version: int = Field(default=CURRENT_VERSION, alias=VERSION_KEY)


def _build_defaults() -> dict[str, Any]:
    return ConfigModel().model_dump(by_alias=True, mode="python")


_DEFAULTS: dict[str, Any] = _build_defaults()
DEFAULTS: Mapping[str, Any] = types.MappingProxyType(_DEFAULTS)


def defaults_tree() -> dict[str, Any]:
    """A fresh deep copy of the defaults, safe to mutate."""
    return copy.deepcopy(_DEFAULTS)


_MISSING = object()


def default_for(key: str, fallback: Any = None) -> Any:
    """The default at a dotted path (plain segments only), or ``fallback``."""
    node: Any = _DEFAULTS
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return fallback
        node = typing.cast(dict[str, Any], node)[part]
    return copy.deepcopy(node)


def _strip_optional(annotation: Any) -> Any:
    origin = typing.get_origin(annotation)
    if origin in (Union, types.UnionType):
        members = [a for a in typing.get_args(annotation) if a is not type(None)]
        return members[0] if members else annotation
    return annotation


def _type_of(raw_annotation: Any) -> type | None:
    annotation: object = _strip_optional(raw_annotation)
    origin = typing.get_origin(annotation)
    if origin is Literal:
        values: tuple[object, ...] = typing.get_args(annotation)
        return type(values[0]) if values else str
    if origin in (list, tuple, set):
        return list
    if origin in (dict, Mapping):
        return dict
    if isinstance(annotation, type):
        plain = cast(type[object], annotation)
        return dict if issubclass(plain, BaseModel) else plain
    return None


def schema_for(key: str) -> type | None:
    """The type a value at a dotted path should have (``str``, ``int``, ``list``, ``dict``...).

    Used by `config set` coercion. Unknown paths give ``None``.
    """
    model: type[BaseModel] | None = ConfigModel
    annotation: Any = None
    for part in key.split("."):
        if model is None:
            # Inside a free-form mapping (``providers.x``) the shape is open.
            return None
        field = None
        for name, info in model.model_fields.items():
            if part in (name, info.alias):
                field = info
                break
        if field is None:
            return None
        annotation = field.annotation
        is_union = typing.get_origin(annotation) in (Union, types.UnionType)
        members = [
            m
            for m in (typing.get_args(annotation) if is_union else (annotation,))
            if m is not type(None)
        ]
        # ``model`` is ``str | ModelSection``: the shorthand string is the slot's own type, and
        # its sub-keys come from the section model.
        model = next((m for m in members if isinstance(m, type) and issubclass(m, BaseModel)), None)
        annotation = members[0] if members else annotation
    return _type_of(annotation)


def validate(data: Mapping[str, Any]) -> list[str]:
    """Check ``data`` against the models; return warnings (never raises, never drops keys)."""
    warnings: list[str] = []
    try:
        parsed = ConfigModel.model_validate(dict(data))
    except ValidationError as exc:
        for err in exc.errors():
            where = ".".join(str(p) for p in err["loc"])
            warnings.append(f"{where}: {err['msg']}")
        return warnings
    for name in ConfigModel.model_fields:
        section = getattr(parsed, name)
        if isinstance(section, _Section) and section.model_extra:
            for extra in section.model_extra:
                warnings.append(f"{name}.{extra}: unknown setting (kept)")
    return warnings


_EXAMPLE_HEADER = """\
# Indus Agent settings.
#
# Every setting is shown below with its built-in value, commented out. Uncomment a line and change
# it to override the built-in value; anything left commented keeps following the built-in value,
# also when a later release changes it. Secrets (API keys) belong in the keychain or .env, never
# here; refer to one with ${NAME} if a setting needs it.
"""


def render_example() -> str:
    """The commented example text that starts every newly created settings file."""
    parts = [_EXAMPLE_HEADER]
    for name, info in ConfigModel.model_fields.items():
        key = info.alias or name
        if key == VERSION_KEY:
            continue
        body = yamlio.dump_plain({key: _DEFAULTS[key]})
        lines = [f"# {line}" if line else "#" for line in body.rstrip("\n").split("\n")]
        parts.append("")
        if info.description:
            parts.append(f"# -- {info.description}")
        parts.extend(lines)
    return "\n".join(parts) + "\n"
