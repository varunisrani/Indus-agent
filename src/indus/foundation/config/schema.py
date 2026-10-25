"""Config schema: Pydantic models, the one defaults table and the commented example.

`DEFAULTS` is generated from the models, so every default lives in exactly one place (D-16.32).
Sections without a model yet are kept as raw mappings; unknown keys inside a modelled section are
kept and reported, never dropped.

Start-up (W50-f-1): the models build their validators on first use (``defer_build``), and
``defaults.json`` is the generated twin of ``DEFAULTS`` (``python -m
indus.foundation.config.defaults_twin``), read instead of building every model at import while it
names the sha256 of this file and ``versions.py``; a missing or stale twin means the defaults are
built from the models as before. ``validate`` checks only the settings that differ from the
defaults, which are valid by construction.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import types
import typing
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, Union, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from indus.foundation import versions, yamlio
from indus.foundation.versions import SANDBOX_IMAGE

log = logging.getLogger(__name__)

CURRENT_VERSION = 1
VERSION_KEY = "_config_version"
DEFAULTS_TWIN = Path(__file__).with_name("defaults.json")


class _Section(BaseModel):
    model_config = ConfigDict(extra="allow", validate_default=True, defer_build=True)


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
    restart_drain_timeout: float = Field(
        default=0.0,
        ge=0,
        description="gateway stop: seconds running chat turns get to finish before they are "
        "interrupted (they resume after the restart)",
    )
    restart_after_turn_timeout: float = Field(
        default=1800.0,
        ge=0,
        description="gateway restart (SIGUSR1): longest wait for running turns to finish",
    )
    cron_drain_timeout: float = Field(
        default=30.0,
        ge=0,
        description="gateway stop: seconds running cron jobs get to finish (10 s of it is kept "
        "for cleanup)",
    )
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
    reconnect_attention_after: float = Field(
        default=1800,
        ge=0,
        description="seconds a messaging platform may keep failing to reconnect before the "
        "gateway flags it as needing attention (0 = never; retries go on either way)",
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
        default=f"{SANDBOX_IMAGE}:stable-slim",
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
    singularity_image: str = Field(
        default="",
        description="singularity backend: image (.sif path or docker://…); empty = docker_image",
    )
    scratch_dir: str = Field(
        default="",
        description="singularity backend: SIF cache and overlays (empty: under the profile home)",
    )
    disk_warning_gb: float = Field(
        default=500, description="singularity backend: warn once when scratch use passes this"
    )
    # P21 SDK sandboxes (Modal, Daytona, Vercel Sandbox) and sync-back.
    modal_mode: str = Field(
        default="auto",
        description="modal backend: auto or direct (both use your own Modal token pair)",
    )
    modal_image: str = Field(
        default="", description="modal backend: image (or im- id); empty = terminal.docker_image"
    )
    daytona_image: str = Field(
        default="", description="daytona backend: image; empty = terminal.docker_image"
    )
    vercel_runtime: Literal["", "node24", "node22", "python3.13"] = Field(
        default="", description="vercel_sandbox backend: runtime in place of the default image"
    )
    sync_back_max_bytes: int = Field(
        default=2 * 1024 * 1024 * 1024,
        ge=1,
        description="largest sandbox archive brought back at cleanup (bytes)",
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
    # P24-43: a transient systemd scope with MemoryMax around each local background process.
    background_memory_max: str | int = Field(
        default="1G",
        description="memory cap of a local background process on Linux with systemd "
        "(64M..4G, a bare number is MiB; off disables)",
    )

    @field_validator("modal_mode")
    @classmethod
    def _direct_modal_only(cls, value: str) -> str:
        return check_modal_mode(value)


MODAL_MANAGED_REFUSED = (
    "managed Modal is not offered: Indus runs Modal only in direct mode with your own Modal "
    "token pair (D-16.16); set terminal.modal_mode to auto or direct"
)


def check_modal_mode(value: str) -> str:
    """``terminal.modal_mode``: ``auto`` or ``direct`` (same meaning); ``managed`` is refused."""
    mode = str(value).strip().lower()
    if mode == "managed":
        raise ValueError(MODAL_MANAGED_REFUSED)
    if mode not in ("auto", "direct"):
        raise ValueError(f"modal_mode must be auto or direct, not {value!r}")
    return mode


class EgressSection(_Section):
    """P24-42: the credential-injection proxy for Docker sandboxes (``indus egress``)."""

    enabled: bool = Field(
        default=False, description="route Docker sandboxes through the host's egress proxy"
    )
    enforce: bool = Field(
        default=True,
        description="refuse a sandbox whose settings would bypass the proxy (off: they win)",
    )
    port: int = Field(default=47900, ge=0, le=65535, description="the proxy's port (0 = any)")
    bind: str = Field(
        default="auto",
        description="the proxy's listen address; auto = the Docker bridge on Linux, else loopback",
    )
    hosts: list[str] = Field(
        default_factory=list,
        description="extra hosts whose TLS the proxy opens (nothing injected unless a rule says)",
    )
    rules: list[dict[str, Any]] = Field(
        default_factory=list[dict[str, Any]],
        description="injection rules: {host, secret, header} (the secret's value never leaves "
        "the host)",
    )
    block_other_hosts: bool = Field(
        default=False, description="refuse hosts that are not on the egress list"
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
    mcp_reload_confirm: bool = Field(
        default=True,
        description="ask before /reload-mcp (it changes the tools, so the prompt cache misses)",
    )
    destructive_slash_confirm: bool = Field(
        default=False,
        description="ask before /rollback, /undo, /new and other commands that drop work "
        "(chat and messaging)",
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


class DelegationSection(_Section):
    """Subagents started by ``delegate_task`` (P10-01..07, D-16.24)."""

    max_concurrent_children: int = Field(
        default=10, description="subagents one delegate_task call may run at once"
    )
    max_depth: int = Field(
        default=1, description="how deep subagents may nest (1 = subagents cannot delegate)"
    )
    max_iterations: int = Field(default=250, description="model calls per subagent")
    max_async_children: int = Field(
        default=3, description="background subagents running at once per session"
    )
    summary_max_chars: int = Field(
        default=24000, description="longest subagent summary returned (at least 2000)"
    )
    oneshot_max_children: int = Field(
        default=2, description="subagents a one-shot run may start in total (0 = no limit)"
    )
    child_toolsets: list[str] = Field(
        default_factory=lambda: ["terminal", "file", "web"],
        description="toolsets a subagent may use (always within the parent's tools)",
    )
    model: str = Field(default="", description="model id for subagents; empty = the parent's")
    inherit_mcp_toolsets: bool = Field(
        default=True, description="subagents keep the parent's MCP toolsets"
    )
    worktree: bool = Field(
        default=False, description="give each subagent its own git worktree (local backend)"
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


class BrowserSection(_Section):
    inactivity_timeout: int = Field(
        default=120, description="seconds an unused browser session stays open (at least 30)"
    )
    command_timeout: int = Field(
        default=30, description="seconds one browser command may take (at least 5)"
    )
    snapshot_threshold: int = Field(
        default=15000, description="page outline characters shown before it is cut (at least 1000)"
    )
    snapshot_summary: bool = Field(
        default=False, description="let the side-task model condense long page outlines"
    )
    cdp_url: str = Field(
        default="", description="attach to this running browser (CDP) instead of launching one"
    )
    executable_path: str = Field(
        default="", description="Chromium to launch; empty = the one Playwright installed"
    )
    env_passthrough: list[str] = Field(
        default_factory=list,
        description="extra environment names the browser gets (values from your secrets)",
    )
    dialog_policy: Literal["must_respond", "auto_dismiss", "auto_accept"] = Field(
        default="must_respond", description="JavaScript dialogs: wait for the agent, or answer"
    )
    dialog_timeout_s: int = Field(
        default=300, description="seconds an unanswered dialog waits before it is dismissed"
    )
    # P24-38..41, 47 (browser backends, vault, private-URL routing, controller broker)
    provider: Literal["local", "browser-use", "browserbase", "firecrawl"] = Field(
        default="local", description="where browser sessions run: local, or a cloud provider"
    )
    engine: Literal["chromium", "lightpanda", "camofox"] = Field(
        default="chromium", description="local browser engine: chromium, lightpanda or camofox"
    )
    camofox_url: str = Field(
        default="", description="address of the Camofox server; empty = http://127.0.0.1:9377"
    )
    real_profile: bool = Field(
        default=False, description="let the agent drive your own browser profile (opt-in)"
    )
    real_profile_dir: str = Field(
        default="", description="your browser profile folder; empty = the default Chromium one"
    )
    route_private_local: bool = Field(
        default=False,
        description="with a cloud provider, open private addresses in a local browser instead",
    )
    vault_sources: list[str] = Field(
        default_factory=lambda: ["local", "onepassword", "bitwarden"],
        description="password sources browser_vault may use: local, onepassword, bitwarden",
    )
    control_enabled: bool = Field(
        default=False, description="let an external browser controller register (API server)"
    )
    control_developer_mode: bool = Field(
        default=False, description="allow raw CDP and evaluate for a negotiated controller"
    )


class DisplayPlatformSection(_Section):
    """Display settings for one messaging platform (``display.platforms.<name>``, P16-33)."""

    native_stream: bool = Field(
        default=False,
        description="show replies as they are written through the platform's own streaming "
        "(Telegram drafts, Slack streams) instead of a message edited in place",
    )


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
    busy_ack: bool = Field(
        default=True,
        description="messaging: a short note when text arrives while a reply runs (P15-12)",
    )
    busy_text_quiet_s: float = Field(
        default=0.35,
        description="messaging: seconds of quiet that close a burst of text sent while busy",
    )
    busy_text_cap_s: float = Field(
        default=1.0, description="messaging: longest a burst of busy text is held before merging"
    )
    ephemeral_system_ttl: float = Field(
        default=0.0,
        ge=0,
        description="messaging: seconds before a system notice deletes itself where the "
        "platform can delete; 0 keeps it",
    )
    tool_progress_grouping: Literal["accumulate", "separate"] = Field(
        default="accumulate",
        description="messaging: tool lines in one edited bubble (accumulate) or one per tool",
    )
    heartbeat_seconds: float = Field(
        default=180.0,
        ge=0,
        description="messaging: a 'still working' line every this many seconds on long turns; "
        "0 = never",
    )
    platforms: dict[str, DisplayPlatformSection] = Field(
        default_factory=dict[str, DisplayPlatformSection],
        description="messaging: per platform name, display settings (native_stream)",
    )


class TuiSection(_Section):
    """The terminal UI (``indus --tui``, P12): presentation settings the bridge reads and writes."""

    theme: str = Field(default="auto", description="colour theme; auto detects light or dark")
    indicator: Literal["spinner", "dots", "none"] = Field(
        default="spinner", description="what shows while a reply runs"
    )
    details: Literal["collapsed", "expanded"] = Field(
        default="collapsed", description="tool and reasoning sections start collapsed or expanded"
    )
    statusbar: bool = Field(default=True, description="show the status rule under the composer")
    mouse: bool = Field(default=False, description="let the terminal UI capture the mouse")
    tool_progress: Literal["off", "new", "all", "verbose"] = Field(
        default="all", description="tool activity shown: off, new (on a change), all or verbose"
    )
    confirm_models: list[str] = Field(
        default_factory=list[str],
        description="model ids that need a confirmation before the TUI switches to them",
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
    goal_judge: AuxTaskSection = Field(
        default_factory=AuxTaskSection,
        description="the /goal and /loop --until judge (30 s per call when timeout_s is empty)",
    )
    side_question: AuxTaskSection = Field(
        default_factory=AuxTaskSection,
        description="/btw answers; a provider or model here sends them there with a digest",
    )
    vision: AuxTaskSection = Field(
        default_factory=AuxTaskSection,
        description="images read by the aux model (120 s per call when timeout_s is empty)",
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
    provider: str = Field(
        default="",
        description="name of one external memory provider to run beside the built-in stores "
        "(empty = built-in only)",
    )
    provider_recall_spill: bool = Field(
        default=False,
        description="replace oversize external-provider recall with a head/tail preview and a "
        "saved file path",
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


class MoaSection(_Section):
    """Mixture-of-agents presets (P10-21, P10-22); `indus moa` edits them."""

    presets: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="name → {reference_models: [{provider, model, enabled, reasoning_effort}], "
        "aggregator, reference_timeout, fanout}",
    )
    default_preset: str = Field(default="", description="the preset /moa uses when none is named")
    reference_timeout: float | None = Field(
        default=None, description="seconds one reference model may take; empty = no limit"
    )
    privacy_filter: Literal["", "display", "full"] = Field(
        default="",
        description="mask emails and phone numbers in reference answers: display (shown text) "
        "or full (also what the model gets)",
    )


class GoalsSection(_Section):
    """Standing goals: /goal (P10-24)."""

    max_turns: int = Field(default=20, description="turns a goal may use before it pauses")


class CronSection(_Section):
    """Scheduled jobs (P14): catch-up, ticker, preflight, scripts, runs, output and delivery."""

    catch_up_missed: bool = Field(
        default=True,
        description="a job that missed its slot by more than its grace runs once to catch up; "
        "false skips to the next slot",
    )
    max_parallel_jobs: int = Field(
        default=0, ge=0, description="jobs run at once by one ticker; 0 = no limit, 1 = in turn"
    )
    preflight: bool = Field(
        default=True, description="check keys, skills, targets and scripts before a run spends"
    )
    script_timeout_seconds: int = Field(
        default=3600, description="seconds a job script may run before its process tree is killed"
    )
    toolsets: list[str] | None = Field(
        default=None, description="toolsets of agent jobs without their own; unset = indus-cron"
    )
    allow_agent_scheduling: bool = Field(
        default=False, description="let a scheduled run create or change scheduled jobs"
    )
    model: str = Field(default="", description="model for unpinned jobs; empty = the main model")
    provider: str = Field(
        default="", description="provider for unpinned jobs; empty = the main provider"
    )
    inactivity_timeout_seconds: int = Field(
        default=600, description="seconds an agent run may show no progress; 0 = no limit"
    )
    output_retention: int = Field(
        default=50, description="output documents kept per job; 0 or less keeps all"
    )
    cleanup_timeout_seconds: float = Field(
        default=10.0, description="seconds one cleanup step after a run may take before it is left"
    )
    wrap_response: bool = Field(
        default=True,
        description="frame delivered job output with a header naming the job and a footer",
    )
    local_notify: bool = Field(
        default=False,
        description="also show a local notification when a job's output is saved (local delivery)",
    )
    failure_nudge_threshold: int = Field(
        default=3,
        ge=0,
        description="failures in a row before a recurring job's alert asks for a review; 0 = off",
    )
    retry_unreachable: bool = Field(
        default=True,
        description="re-run a recurring job after 5, 15 and 30 min when the model was unreachable",
    )
    failure_repeat_alert_hours: float = Field(
        default=6.0,
        ge=0,
        description="hours before the same failure alerts again; 0 = alert on every failed run",
    )


class LoopsSection(_Section):
    """Repeating prompts: /loop (P10-25)."""

    min_interval_seconds: int = Field(
        default=30, description="shortest loop interval; shorter ones are raised (never below 5)"
    )
    max_ticks: int = Field(
        default=100, description="runs before a loop pauses itself; 0 = no limit"
    )


class KanbanSection(_Section):
    """The task board and its dispatcher (P24-11..13)."""

    dispatch_in_gateway: bool = Field(
        default=True,
        description="the gateway daemon starts workers for ready board tasks; false = only "
        "`indus kanban dispatch` does",
    )
    dispatch_interval_seconds: int = Field(
        default=60, ge=1, description="seconds between dispatcher passes in the daemon"
    )
    max_in_progress: int = Field(
        default=0,
        ge=0,
        description="workers running at once; 0 = from memory (one per 512 MB, 2 to 8)",
    )
    failure_limit: int = Field(
        default=2, ge=1, description="failed attempts in a row before a task is blocked"
    )


class ProjectsDiscoverySection(_Section):
    """Repository discovery for projects (P24-14)."""

    enabled: bool = Field(default=True, description="look for git repositories to offer")
    roots: list[str] = Field(
        default_factory=list, description="folders to scan; empty = your home folder"
    )
    excludes: list[str] = Field(
        default_factory=lambda: [
            "node_modules",
            "Library",
            "AppData",
            "Applications",
            "vendor",
            "target",
            "dist",
            "build",
            "venv",
            "__pycache__",
        ],
        description="folder names (or patterns, or absolute paths) never scanned",
    )
    max_depth: int = Field(default=3, ge=0, le=6, description="how many folders deep to look")


class ProjectsSection(_Section):
    """Projects: named workspaces of this profile (P24-14)."""

    default_directory: str = Field(
        default="", description="where a new chat starts when no project applies; empty = detached"
    )
    discovery: ProjectsDiscoverySection = Field(
        default_factory=ProjectsDiscoverySection, description="finding repositories"
    )


class ResearchSection(_Section):
    """Research tooling: batch runs, saved chat samples, dataset sampling (P22)."""

    runs_root: str = Field(
        default="./research-runs",
        description="folder that holds one folder per batch run (relative to where it is started)",
    )
    save_trajectories: bool = Field(
        default=False,
        description="CLI chats append every finished turn as a training sample to "
        "trajectory_samples.jsonl / failed_trajectories.jsonl in the working directory",
    )
    sample_datasets: list[str] = Field(
        default_factory=list,
        description="datasets `indus research sample` draws from (none by default: it refuses "
        "to run without a list)",
    )


class SkillsHubSection(_Section):
    """The skills hub (P13-25..27)."""

    search_timeout: float = Field(
        default=30.0, description="seconds a hub search may take across all sources"
    )
    well_known: list[str] = Field(
        default_factory=list,
        description="sites whose /.well-known/skills/index.json the hub searches",
    )
    advisory_scanner: str = Field(
        default="",
        description="extra scanner program run on hub skills before install (advice only)",
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
    hub: SkillsHubSection = Field(
        default_factory=SkillsHubSection, description="the skills hub (P13-25..27)"
    )
    trusted_project_dirs: list[str] = Field(
        default_factory=list,
        description="repos whose .indus/skills and .agents/skills load (P13-28)",
    )
    project_discovery: bool = Field(
        default=True, description="look for skills inside the current repo at all (P13-28)"
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


class CodeExecutionSection(_Section):
    timeout: int = Field(default=300, description="seconds one execute_code call may run")
    max_tool_calls: int = Field(
        default=50, description="tool calls one execute_code call may make through indus_tools"
    )
    mode: Literal["project", "strict"] = Field(
        default="project",
        description="project: the session's directory and active venv; strict: a temp dir and "
        "Indus's own Python",
    )
    kernel: Literal["auto", "off"] = Field(
        default="auto",
        description="auto: a persistent Python session per conversation where one can start; "
        "off: a fresh process per call",
    )
    kernel_idle_timeout: int = Field(
        default=1800, description="seconds an unused session kernel is kept"
    )
    max_session_kernels: int = Field(
        default=4, description="session kernels kept at once (delegated children not counted)"
    )


class VisionSection(_Section):
    """``vision_analyze`` (P10-15)."""

    download_timeout: float = Field(default=30, description="seconds an image download may take")
    embed_target_kb: int = Field(
        default=256,
        description="size an image is shrunk to before the main model sees it (64-4096 KB)",
    )


class ImageGenSection(_Section):
    """``image_generate`` (P10-16): the user's choice of service and model."""

    provider: str = Field(default="", description="image service; empty = the first ready one")
    model: str = Field(default="", description="model id; empty = the service's default")
    base_url: str = Field(
        default="", description="OpenAI-compatible images endpoint; empty = api.openai.com/v1"
    )
    key_name: str = Field(
        default="OPENAI_API_KEY", description="secret holding the images endpoint's key"
    )
    max_parallel_requests: int = Field(default=4, description="image requests that may run at once")


class MediaSection(_Section):
    """Video generation (P10-16): the user's choice of service and model."""

    video_provider: str = Field(
        default="", description="video service; empty = the first ready one"
    )
    video_model: str = Field(default="", description="model id; empty = the service's default")


class TtsSection(_Section):
    """``text_to_speech`` (P10-17)."""

    provider: str = Field(default="piper", description="speech engine: piper (local) or openai")
    voice: str = Field(default="", description="voice name for engines that have several")
    speed: float = Field(default=1.0, description="speaking speed, 0.25-4.0")
    model: str = Field(default="", description="model id for the openai engine")
    base_url: str = Field(
        default="", description="OpenAI-compatible speech endpoint; empty = api.openai.com/v1"
    )
    key_name: str = Field(
        default="OPENAI_API_KEY", description="secret holding the speech endpoint's key"
    )
    piper_binary: str = Field(default="piper", description="the Piper program (name or path)")
    piper_model: str = Field(default="", description="path to a Piper voice model (.onnx)")


class ComputerUseSection(_Section):
    """Desktop control, ``computer_use`` (P10-20)."""

    driver: str = Field(default="stdio", description="desktop driver: stdio, or a plugin's name")
    driver_cmd: str = Field(
        default="",
        description="the stdio driver program and its arguments; INDUS_CUA_DRIVER_CMD wins",
    )
    action_timeout_s: float = Field(
        default=30.0, description="seconds one desktop action may take before the driver restarts"
    )


class SttSection(_Section):
    """Speech to text (P10-18)."""

    provider: str = Field(
        default="", description="local, openai or groq; empty = the first ready one"
    )
    model: str = Field(default="", description="model id for openai/groq; empty = their default")
    base_url: str = Field(
        default="", description="OpenAI-compatible transcription endpoint for the openai provider"
    )
    language: str = Field(default="", description="language code to expect; empty = detect")
    local_model: str = Field(default="base", description="faster-whisper model size")
    local_device: str = Field(default="auto", description="faster-whisper device: auto, cpu, cuda")
    max_file_mb: float = Field(
        default=25, description="largest file sent in one request when the provider sets no cap"
    )


class VoiceSection(_Section):
    """Voice mode of the classic CLI (P10-19)."""

    record_key: str = Field(
        default="ctrl+space",
        description="push-to-talk key: ctrl+space, ctrl+<letter>, alt+<key>, f1-f12",
    )
    silence_threshold: float = Field(
        default=300,
        description="loudness (RMS of 16-bit samples) under which audio counts as quiet",
    )
    silence_seconds: float = Field(
        default=2.0, description="quiet after speech that ends a recording"
    )
    wait_seconds: float = Field(
        default=10.0, description="a recording with no speech for this long is dropped"
    )
    max_seconds: float = Field(default=120.0, description="longest recording")
    read_aloud: bool = Field(default=False, description="speak each final answer while voice is on")


class PetSection(_Section):
    """The desktop pet and the TUI ``/pet`` pane (P24-19)."""

    enabled: bool = Field(default=False, description="show the active pet")
    active: str = Field(default="", description="slug of the pet to show (indus pet list)")
    scale: float = Field(default=1.0, description="pet size on every surface, 0.25-3")


class WakeWordSection(_Section):
    """Hands-free start of voice mode (P24-20); the microphone opens only while enabled."""

    enabled: bool = Field(default=False, description="listen for the wake phrase")
    consent: bool = Field(
        default=False,
        description="the person agreed to microphone listening (set only by an explicit on)",
    )
    engine: Literal["openwakeword", "sherpa"] = Field(
        default="sherpa", description="keyword engine: sherpa (default) or openwakeword"
    )
    phrase: str = Field(default="hey indus", description="the phrase to listen for")
    model: str = Field(
        default="", description="path to the wake model (none ships: train or supply one)"
    )
    threshold: float = Field(default=0.5, description="score a frame needs to count, 0-1")
    confirm_frames: int = Field(
        default=3, description="frames over the threshold in a row before it fires (1-10)"
    )
    cooldown_s: float = Field(default=2.0, description="quiet seconds after a detection")


class PluginsSection(_Section):
    """Python plugins (P13-13..17): which may load and how long they get."""

    enabled: list[str] = Field(
        default_factory=list[str],
        description="plugins allowed to load (`indus plugins enable <name>`); others are only "
        "listed",
    )
    disabled: list[str] = Field(
        default_factory=list[str], description="plugins never loaded, even when enabled"
    )
    project: bool = Field(
        default=False,
        description="also load plugins from the project's .indus/plugins/ (never in the gateway)",
    )
    load_timeout_seconds: float = Field(
        default=10.0,
        description="seconds one plugin's import and register() may take; 0 = no limit",
    )
    hook_callback_timeout: float = Field(
        default=30.0,
        description="seconds a hot-path plugin hook may take; 0 = no limit (a pre-tool hook that "
        "misses it blocks the tool)",
    )
    entries: dict[str, dict[str, Any]] = Field(
        default_factory=dict[str, dict[str, Any]],
        description="per plugin: settings, mcp_allowlist, allow_tool_override, "
        "granted_capabilities",
    )
    clone_timeout_seconds: float = Field(
        default=300.0, description="seconds one git step of a plugin install may take (3600 max)"
    )
    auto_update_check_hours: float = Field(
        default=24.0, description="hours between plugin update checks; 0 = never check"
    )
    auto_apply: bool = Field(
        default=False, description="apply plugin updates found by the check without asking"
    )


class McpSection(_Section):
    """The MCP client as a whole (P13-01); each server lives under ``mcp_servers.<name>``."""

    auto_reload_on_config_change: bool = Field(
        default=True, description="reconnect servers when mcp_servers changes on disk"
    )
    discovery_concurrency: int = Field(
        default=4, ge=0, description="servers connecting at once at startup and reload; 0 = all"
    )
    osv_preflight: bool = Field(
        default=True,
        description="check npx/uvx/pipx/bunx/pnpm dlx packages against OSV malware advisories "
        "before starting them",
    )


class HomeChannelSetting(_Section):
    """Where unsolicited output for a platform goes (cron results, notices)."""

    platform: str = Field(default="", description="platform name; empty = the section's own")
    chat_id: str = Field(default="", description="chat, channel or user id")
    name: str = Field(default="", description="display name")
    thread_id: str | None = Field(default=None, description="thread or topic inside the chat")


class ChannelOverride(_Section):
    model: str | None = Field(default=None, description="model id for this channel")
    provider: str | None = Field(default=None, description="provider for this channel")
    system_prompt: str | None = Field(
        default=None, description="extra system prompt text for this channel"
    )


class PlatformSection(_Section):
    """One messaging platform (P15-02). Credentials live in the keychain or .env, never here."""

    enabled: bool | None = Field(
        default=None,
        description="unset: on when its credentials are present; false: off even with them",
    )
    home_channel: HomeChannelSetting | None = Field(
        default=None, description="default chat for cron output and notices"
    )
    reply_to_mode: Literal["off", "first", "all"] = Field(
        default="first",
        description="which chunks of a long reply thread to your message: off, first or all",
    )
    restart_notice: bool = Field(
        default=True, description="post a note to the home channel after the gateway restarts"
    )
    typing_indicator: bool = Field(default=True, description="show typing while a reply runs")
    streaming: bool | None = Field(
        default=None,
        description="edit the reply in place as it is written; unset: the platform default "
        "(Telegram on, Discord and Slack off)",
    )
    tool_progress: Literal["off", "new", "all", "verbose", "log"] | None = Field(
        default=None,
        description="tool activity shown: off, new, all, verbose, or log (an audit file only); "
        "unset: the platform default (Telegram off, others all)",
    )
    channel_overrides: dict[str, ChannelOverride] = Field(
        default_factory=dict[str, ChannelOverride],
        description="per channel id: model, provider or system_prompt",
    )
    extra: dict[str, Any] = Field(
        default_factory=dict[str, Any], description="platform-specific settings"
    )


class PlatformsSection(_Section):
    """Messaging platforms by name; plugin platforms add their own names."""

    telegram: PlatformSection = Field(default_factory=PlatformSection, description="Telegram")
    discord: PlatformSection = Field(default_factory=PlatformSection, description="Discord")
    slack: PlatformSection = Field(default_factory=PlatformSection, description="Slack")
    signal: PlatformSection = Field(default_factory=PlatformSection, description="Signal")
    matrix: PlatformSection = Field(default_factory=PlatformSection, description="Matrix")
    mattermost: PlatformSection = Field(default_factory=PlatformSection, description="Mattermost")
    email: PlatformSection = Field(default_factory=PlatformSection, description="Email")
    whatsapp: PlatformSection = Field(
        default_factory=PlatformSection, description="WhatsApp (own bridge, WhatsApp Web)"
    )
    whatsapp_cloud: PlatformSection = Field(
        default_factory=PlatformSection, description="WhatsApp Business (Cloud API)"
    )
    sms: PlatformSection = Field(default_factory=PlatformSection, description="SMS (Twilio)")
    line: PlatformSection = Field(default_factory=PlatformSection, description="LINE")
    ntfy: PlatformSection = Field(
        default_factory=PlatformSection, description="ntfy push notifications"
    )
    irc: PlatformSection = Field(default_factory=PlatformSection, description="IRC")
    teams: PlatformSection = Field(default_factory=PlatformSection, description="Microsoft Teams")
    msgraph: PlatformSection = Field(
        default_factory=PlatformSection, description="Microsoft Graph change notifications"
    )
    google_chat: PlatformSection = Field(default_factory=PlatformSection, description="Google Chat")
    qqbot: PlatformSection = Field(default_factory=PlatformSection, description="QQ Bot")
    dingtalk: PlatformSection = Field(default_factory=PlatformSection, description="DingTalk")
    wecom: PlatformSection = Field(default_factory=PlatformSection, description="WeCom smart robot")
    wecom_callback: PlatformSection = Field(
        default_factory=PlatformSection, description="WeCom callback app"
    )
    feishu: PlatformSection = Field(default_factory=PlatformSection, description="Feishu / Lark")
    bluebubbles: PlatformSection = Field(
        default_factory=PlatformSection, description="BlueBubbles (iMessage)"
    )
    photon: PlatformSection = Field(
        default_factory=PlatformSection, description="iMessage through Photon (own sidecar)"
    )
    simplex: PlatformSection = Field(default_factory=PlatformSection, description="SimpleX Chat")
    buzz: PlatformSection = Field(default_factory=PlatformSection, description="Buzz (Nostr)")
    weixin: PlatformSection = Field(
        default_factory=PlatformSection, description="Weixin / WeChat (iLink bot)"
    )
    yuanbao: PlatformSection = Field(default_factory=PlatformSection, description="Tencent Yuanbao")
    raft: PlatformSection = Field(
        default_factory=PlatformSection, description="Raft (wake bridge for the Raft CLI)"
    )
    a2a: PlatformSection = Field(
        default_factory=PlatformSection,
        description="A2A: other AI agents send this agent tasks (settings under a2a)",
    )


class MessageTimestampsSection(_Section):
    enabled: bool = Field(
        default=False, description="tell the model when each message was sent (never shown)"
    )


class BotLoopGuardSection(_Section):
    """Stops two bots answering each other forever (P15-10)."""

    enabled: bool = Field(default=True, description="count bot-authored messages per chat")
    max_messages: int = Field(
        default=20, ge=1, description="bot-authored messages allowed in one window"
    )
    window_seconds: float = Field(default=300.0, gt=0, description="length of the window")
    cooldown_seconds: float = Field(
        default=600.0, gt=0, description="how long a tripped chat ignores bot messages"
    )


class ApiServerSection(_Section):
    """The OpenAI-compatible HTTP API inside the gateway daemon (P15-30, P15-31)."""

    enabled: bool = Field(default=False, description="serve the HTTP API from the gateway")
    host: str = Field(default="127.0.0.1", description="address to bind (loopback by default)")
    port: int = Field(default=8642, ge=0, le=65535, description="port to bind; 0 = any free one")
    cors_origins: list[str] = Field(
        default_factory=list[str],
        description="browser origins allowed to call the API (none by default)",
    )
    max_concurrent_runs: int = Field(
        default=10, ge=0, description="API turns running at once; more get 429; 0 = no cap"
    )
    model_name: str = Field(
        default="indus-agent", description="the model id /v1/models shows to clients"
    )
    idle_close_seconds: float = Field(
        default=900.0, gt=0, description="close an API session after this long with nothing to do"
    )


class WebhookSection(_Section):
    """The webhook receiver inside the gateway daemon (P15-32)."""

    enabled: bool = Field(default=False, description="receive webhooks in the gateway")
    host: str = Field(default="127.0.0.1", description="address to bind (loopback by default)")
    port: int = Field(default=8644, ge=0, le=65535, description="port to bind; 0 = any free one")
    rate_limit: int = Field(default=30, ge=1, description="requests per minute per route")
    max_body_bytes: int = Field(
        default=1_048_576, ge=1, description="largest request body accepted, in bytes"
    )
    routes: dict[str, dict[str, Any]] = Field(
        default_factory=dict[str, dict[str, Any]],
        description="webhook routes by name (secret names a stored secret, never its value)",
    )


class RelaySection(_Section):
    """The relay connector the gateway dials out to (P24-08); the shared secret is the stored
    secret INDUS_RELAY_SECRET, never a config value."""

    url: str = Field(
        default="", description="the connector's base URL (https://…); empty: no relay"
    )
    gateway_id: str = Field(default="", description="this gateway's id at the connector")
    connect_timeout_seconds: float = Field(
        default=30.0, gt=0, description="seconds to dial and finish the signed handshake"
    )
    request_timeout_seconds: float = Field(
        default=30.0, gt=0, description="seconds to wait for the answer to one operation"
    )


class GatewaySection(_Section):
    """The messaging gateway (``indus gateway``, P15)."""

    message_timestamps: MessageTimestampsSection = Field(
        default_factory=MessageTimestampsSection, description="send-time stamps in model context"
    )
    trust_env: bool = Field(
        default=True,
        description="platform connections use the HTTPS_PROXY / NO_PROXY environment",
    )
    group_sessions_per_user: bool = Field(
        default=True, description="each person in a group chat gets their own conversation"
    )
    thread_sessions_per_user: bool = Field(
        default=False, description="each person in a thread gets their own conversation"
    )
    unauthorized_dm_behavior: Literal["pair", "ignore", "decline"] = Field(
        default="pair",
        description="a direct message from someone not allowed: pair (send a pairing code), "
        "ignore, or decline (one short refusal a day)",
    )
    bot_loop_guard: BotLoopGuardSection = Field(
        default_factory=BotLoopGuardSection, description="bot-to-bot loop protection"
    )
    max_concurrent_sessions: int | None = Field(
        default=None, ge=1, description="most conversations running at once; unset = no cap"
    )
    media_strict: bool = Field(
        default=False,
        description="send files only from the gateway caches and media_allow_dirs",
    )
    media_allow_dirs: list[str] = Field(
        default_factory=list[str],
        description="extra folders (absolute or ~/) whose files replies may send",
    )
    media_trust_recent: bool = Field(
        default=False,
        description="also send files written in the last media_trust_recent_seconds outside "
        "the trusted folders (off: a file's age can be faked with touch)",
    )
    media_trust_recent_seconds: float = Field(
        default=600.0, ge=0, description="how recent a file must be for media_trust_recent"
    )
    connect_timeout_seconds: float = Field(
        default=30.0, gt=0, description="seconds a platform may take to connect at startup"
    )
    systemd_watchdog_seconds: float = Field(
        default=0.0, ge=0, description="systemd watchdog interval; 0 turns the pings off"
    )
    idle_exit_seconds: float = Field(
        default=600.0,
        ge=0,
        description="a gateway a client started on its own exits after this many seconds with no "
        "client and no work; 0 keeps it running",
    )
    delivery_ledger: bool = Field(
        default=True,
        description="record each chat reply before sending it, so a crash or restart cannot "
        "lose it (recovered replies are labelled)",
    )
    stt_enabled: bool = Field(
        default=True,
        description="transcribe voice messages people send (off: the agent gets the file path)",
    )
    api_server: ApiServerSection = Field(
        default_factory=ApiServerSection, description="the OpenAI-compatible HTTP API"
    )
    webhook: WebhookSection = Field(
        default_factory=WebhookSection, description="the webhook receiver"
    )
    multiplex_profiles: bool = Field(
        default=False,
        description="one gateway process serves every profile under this root (off: one "
        "gateway per profile)",
    )
    profile_routes: list[dict[str, Any]] = Field(
        default_factory=list[dict[str, Any]],
        description="with multiplex_profiles: hand messages a bot receives to another profile "
        "({name, platform, profile, bot, guild, chat, thread, user, enabled}; most specific wins)",
    )
    relay: RelaySection = Field(
        default_factory=RelaySection, description="the relay connector (dial-out front end)"
    )


class A2APeerSection(_Section):
    """One A2A peer the a2a_call tool may call; its key is the stored secret
    INDUS_A2A_PEER_<NAME>_KEY."""

    url: str = Field(default="", description="the peer's A2A JSON-RPC endpoint URL")
    trusted: bool = Field(
        default=False,
        description="allow a private, loopback or link-local address for this peer",
    )
    timeout_seconds: float = Field(default=120.0, gt=0, description="seconds to wait for an answer")


class A2ASection(_Section):
    """A2A (Agent2Agent) in both directions (P24-10). Inbound runs when platforms.a2a.enabled is
    true; each inbound peer's token is the stored secret INDUS_A2A_TOKEN_<PEER>."""

    host: str = Field(default="127.0.0.1", description="address to serve on (loopback by default)")
    port: int = Field(default=9900, ge=0, le=65535, description="port to serve on")
    public: bool = Field(default=False, description="required to serve on a non-loopback address")
    public_url: str = Field(
        default="", description="the URL peers reach this agent at (for the agent card)"
    )
    agent_name: str = Field(default="Indus Agent", description="name on the agent card")
    description: str = Field(
        default="An Indus agent reachable over A2A.", description="description on the agent card"
    )
    inbound_peers: list[str] = Field(
        default_factory=list[str],
        description="peers allowed to send tasks; none: every call is refused",
    )
    reply_timeout_seconds: float = Field(
        default=300.0, gt=0, description="seconds a peer's call waits for the agent's reply"
    )
    max_turns_per_context: int = Field(
        default=5, ge=1, description="messages one peer conversation may send before it is refused"
    )
    peers: dict[str, A2APeerSection] = Field(
        default_factory=dict[str, A2APeerSection],
        description="peer agents the a2a_call tool may call, by name",
    )


class DashboardBasicAuthSection(_Section):
    """The dashboard's username/password sign-in (P18-03, D-16.44)."""

    username: str = Field(default="", description="sign-in name; empty turns the provider off")
    password_hash: str = Field(
        default="",
        description="argon2id hash of the password (`indus dashboard` on a terminal writes it); "
        "never the password itself — INDUS_DASHBOARD_PASSWORD in the environment overrides it",
    )


class DashboardOidcSection(_Section):
    """The dashboard's OpenID Connect sign-in (P18-04, D-16.44)."""

    issuer: str = Field(default="", description="issuer URL (https; http only on loopback)")
    client_id: str = Field(default="", description="public client id (PKCE, no client secret)")
    scopes: str = Field(default="openid profile email", description="scopes asked for at sign-in")
    id_token_leeway: float = Field(
        default=60.0,
        description="seconds of clock skew allowed on ID token times; 0 = strict, an invalid "
        "value counts as 0",
    )


def _refuse_open_proxies(entries: list[str]) -> list[str]:
    for entry in entries:
        text = entry.strip()
        if text == "*" or text.endswith("/0"):
            raise ValueError(f"{text!r} would trust every client; list the proxy's address")
    return entries


class DashboardSection(_Section):
    """The web dashboard and headless backend (`indus dashboard`, `indus serve`, P18)."""

    public_url: str = Field(
        default="",
        description="the full address people reach the dashboard at behind a proxy; a "
        "non-loopback host turns sign-in on",
    )
    trusted_proxies: list[str] = Field(
        default_factory=list[str],
        description="proxy addresses or bounded networks whose X-Forwarded-* headers are "
        "believed (loopback always is; '*' and /0 are refused)",
    )
    basic_auth: DashboardBasicAuthSection = Field(
        default_factory=DashboardBasicAuthSection, description="username/password sign-in"
    )
    oidc: DashboardOidcSection = Field(
        default_factory=DashboardOidcSection, description="OpenID Connect sign-in"
    )
    theme: Literal["system", "light", "dark"] = Field(
        default="system", description="dashboard colours: system, light or dark"
    )
    show_token_analytics: bool = Field(
        default=False, description="show the token and cost analytics page"
    )

    @field_validator("trusted_proxies")
    @classmethod
    def _no_open_proxy(cls, value: list[str]) -> list[str]:
        return _refuse_open_proxies(value)


class UpdatesSection(_Section):
    """``indus update`` (P23-17..19)."""

    check: bool = Field(
        default=True,
        description="show passive update notices (indus --version); explicit `indus update "
        "--check` always works",
    )
    pre_update_backup: Literal["quick", "full", "off"] = Field(
        default="quick", description="what `indus update` saves first: quick, full or off"
    )
    backup_keep: int = Field(
        default=5, ge=0, description="full pre-update backups kept in the backup folder"
    )
    installs: dict[str, dict[str, Any]] = Field(
        default_factory=dict[str, dict[str, Any]],
        description="per install id (`indus update --install-id`): channel (stable, canary or "
        "main) and extras",
    )


class AcpSection(_Section):
    """The editor agent (``indus acp``, P19)."""

    allow_local_fallback: bool = Field(
        default=False,
        description="with no gateway daemon running, run editor sessions in the `indus acp` "
        "process instead of refusing them (D-16.83)",
    )


class LspSection(_Section):
    """Language servers for edits (P24-15, P24-16)."""

    enabled: bool = Field(
        default=True,
        description="check edits with a language server when one is installed (nothing is "
        "ever installed for you; see `indus lsp status`)",
    )
    wait_timeout: float = Field(
        default=5.0, gt=0, description="seconds to wait for a server's verdict on an edit"
    )
    idle_timeout: float = Field(
        default=600.0,
        ge=0,
        description="seconds an unused server keeps running (values under 30 count as 30; 0 = "
        "never stop idle servers)",
    )
    broken_retry_s: float = Field(
        default=0.0,
        ge=0,
        description="seconds before a server that failed to start is tried again (0 = only "
        "after `indus lsp restart`)",
    )
    trusted_workspaces: list[str] = Field(
        default_factory=list[str],
        description="folders where servers that run project code (rust-analyzer, gopls, ...) "
        "may start, besides the session's own work tree",
    )
    servers: dict[str, dict[str, Any]] = Field(
        default_factory=dict[str, dict[str, Any]],
        description="per-server overrides or new servers: command, extensions, root_markers, "
        "language_id, env, initialization_options, disabled",
    )


class VerifySection(_Section):
    """`indus verify` and verify-on-stop (P24-17)."""

    on_stop: bool = Field(
        default=False,
        description="after code edits, run the workspace's build and test before the agent's "
        "answer is final, and send it back to work when they fail",
    )
    phase_timeout: float = Field(
        default=600.0, gt=0, description="seconds each bootstrap/build/test command may run"
    )
    ready_timeout: float = Field(
        default=60.0, gt=0, description="seconds a started service gets to answer on 127.0.0.1"
    )
    max_stop_attempts: int = Field(
        default=2, ge=0, description="times per turn verify-on-stop may send the agent back"
    )
    max_hook_nudges: int = Field(
        default=3, ge=0, description="times per turn a pre_verify plugin hook may send it back"
    )


class PmSection(_Section):
    """The helper-binary manager (``indus pm``, P24-23)."""

    mirror_url: str = Field(
        default="",
        description="content-addressed mirror tried after a download's own URL "
        "(<mirror_url>/sha256/<hash>); empty: none",
    )
    timeout_s: float = Field(
        default=60.0, gt=0, description="seconds one pm network request may take"
    )


class ConfigModel(BaseModel):
    """The whole file. Unknown top-level sections are kept as they are."""

    model_config = ConfigDict(
        extra="allow", validate_default=True, populate_by_name=True, defer_build=True
    )

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
    egress: EgressSection = Field(
        default_factory=EgressSection, description="the egress proxy for Docker sandboxes"
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
    browser: BrowserSection = Field(
        default_factory=BrowserSection, description="the agent's headless browser"
    )
    display: DisplaySection = Field(default_factory=DisplaySection, description="the interface")
    tui: TuiSection = Field(default_factory=TuiSection, description="the terminal UI")
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
    code_execution: CodeExecutionSection = Field(
        default_factory=CodeExecutionSection, description="the execute_code tool"
    )
    delegation: DelegationSection = Field(
        default_factory=DelegationSection, description="subagents (delegate_task)"
    )
    vision: VisionSection = Field(
        default_factory=VisionSection, description="looking at images (vision_analyze)"
    )
    image_gen: ImageGenSection = Field(
        default_factory=ImageGenSection, description="image generation"
    )
    media: MediaSection = Field(default_factory=MediaSection, description="video generation")
    tts: TtsSection = Field(default_factory=TtsSection, description="text to speech")
    stt: SttSection = Field(default_factory=SttSection, description="speech to text")
    voice: VoiceSection = Field(
        default_factory=VoiceSection, description="CLI voice mode (/voice, chat --voice)"
    )
    computer_use: ComputerUseSection = Field(
        default_factory=ComputerUseSection, description="desktop control (computer_use)"
    )
    pet: PetSection = Field(default_factory=PetSection, description="the desktop pet (indus pet)")
    wake_word: WakeWordSection = Field(
        default_factory=WakeWordSection, description="wake phrase that starts voice mode"
    )
    moa: MoaSection = Field(default_factory=MoaSection, description="mixture of agents (/moa)")
    goals: GoalsSection = Field(default_factory=GoalsSection, description="standing goals (/goal)")
    loops: LoopsSection = Field(
        default_factory=LoopsSection, description="repeating prompts (/loop)"
    )
    cron: CronSection = Field(
        default_factory=CronSection, description="scheduled jobs (indus cron)"
    )
    research: ResearchSection = Field(
        default_factory=ResearchSection, description="research tooling (indus research)"
    )
    kanban: KanbanSection = Field(
        default_factory=KanbanSection, description="the task board (indus kanban)"
    )
    projects: ProjectsSection = Field(
        default_factory=ProjectsSection, description="projects (indus project)"
    )
    plugins: PluginsSection = Field(
        default_factory=PluginsSection, description="Python plugins and their hooks"
    )
    mcp_servers: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="MCP servers by name: command/args/env (stdio) or url/headers (http, sse); "
        "secrets only as ${NAME}",
    )
    mcp: McpSection = Field(default_factory=McpSection, description="the MCP client")
    hooks: dict[str, Any] = Field(
        default_factory=dict[str, Any],
        description="shell hooks by event: a list of {command, matcher?, timeout?} each; every "
        "command is asked about once before it runs (`indus hooks list`)",
    )
    hooks_auto_accept: bool = Field(
        default=False, description="run configured shell hooks without asking first"
    )
    platforms: PlatformsSection = Field(
        default_factory=PlatformsSection, description="messaging platforms (indus gateway)"
    )
    gateway: GatewaySection = Field(
        default_factory=GatewaySection, description="the messaging gateway"
    )
    dashboard: DashboardSection = Field(
        default_factory=DashboardSection, description="the web dashboard (indus dashboard)"
    )
    acp: AcpSection = Field(default_factory=AcpSection, description="the editor agent (indus acp)")
    updates: UpdatesSection = Field(
        default_factory=UpdatesSection, description="updating this install (indus update)"
    )
    a2a: A2ASection = Field(
        default_factory=A2ASection, description="A2A peer agents, inbound and outbound"
    )
    lsp: LspSection = Field(
        default_factory=LspSection, description="language-server checks of edits (indus lsp)"
    )
    verify: VerifySection = Field(
        default_factory=VerifySection, description="workspace checks (indus verify)"
    )
    pm: PmSection = Field(default_factory=PmSection, description="helper programs (indus pm)")
    config_version: int = Field(default=CURRENT_VERSION, alias=VERSION_KEY)


def build_defaults() -> dict[str, Any]:
    return ConfigModel().model_dump(by_alias=True, mode="python")


def defaults_sources() -> tuple[Path, ...]:
    """The files whose text decides ``DEFAULTS``: this module and the pinned versions it reads."""
    return (Path(__file__), Path(versions.__file__))


def source_digest() -> str:
    """The sha256 the defaults twin names (each source's file name and text, newlines as read)."""
    digest = hashlib.sha256()
    for source in defaults_sources():
        digest.update(source.name.encode("utf-8") + b"\0")
        digest.update(source.read_text(encoding="utf-8").encode("utf-8") + b"\0")
    return digest.hexdigest()


def defaults_from_twin(twin_text: str, digest: str) -> dict[str, Any] | None:
    """The defaults a twin carries; ``None`` when it is not JSON, not a twin, or for other text."""
    try:
        twin = json.loads(twin_text)
    except ValueError:
        return None
    if not isinstance(twin, dict):
        return None
    twin_map = cast(dict[str, Any], twin)
    defaults = twin_map.get("defaults")
    if twin_map.get("source_sha256") != digest or not isinstance(defaults, dict):
        return None
    return cast(dict[str, Any], defaults)


def _load_defaults() -> dict[str, Any]:
    try:
        found = defaults_from_twin(DEFAULTS_TWIN.read_text(encoding="utf-8"), source_digest())
    except (OSError, UnicodeDecodeError):
        found = None
    if found is None:
        log.debug("%s is missing or stale; building the defaults from the models", DEFAULTS_TWIN)
        return build_defaults()
    return found


_DEFAULTS: dict[str, Any] = _load_defaults()
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


def same_value(left: object, right: object) -> bool:
    """Equal with equal types all the way down (``1``, ``1.0`` and ``True`` are three values)."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        left_map = cast(dict[Any, Any], left)
        right_map = cast(dict[Any, Any], right)
        return list(left_map) == list(right_map) and all(
            same_value(value, right_map[key]) for key, value in left_map.items()
        )
    if isinstance(left, list | tuple):
        left_seq = cast(list[Any] | tuple[Any, ...], left)
        right_seq = cast(list[Any] | tuple[Any, ...], right)
        return len(left_seq) == len(right_seq) and all(
            same_value(a, b) for a, b in zip(left_seq, right_seq, strict=True)
        )
    return left == right


_ADAPTERS: dict[str, TypeAdapter[Any]] = {}


def _adapter(name: str) -> TypeAdapter[Any]:
    """A validator for one top-level setting alone (built on first use, then kept)."""
    adapter = _ADAPTERS.get(name)
    if adapter is None:
        info = ConfigModel.model_fields[name]
        annotation: Any = info.annotation
        if info.metadata:
            annotation = Annotated[annotation, *info.metadata]
        adapter = _ADAPTERS[name] = TypeAdapter(annotation)
    return adapter


def _warnings(
    errors: list[tuple[str, ValidationError]], parsed: list[tuple[str, Any]]
) -> list[str]:
    if errors:
        return [
            ".".join(str(p) for p in (*((key,) if key else ()), *err["loc"])) + f": {err['msg']}"
            for key, exc in errors
            for err in exc.errors()
        ]
    warnings: list[str] = []
    for name, section in parsed:
        if isinstance(section, _Section) and section.model_extra:
            for extra in section.model_extra:
                warnings.append(f"{name}.{extra}: unknown setting (kept)")
    return warnings


def _validate_whole(data: Mapping[str, Any]) -> list[str]:
    """The whole-model check: every setting through ``ConfigModel`` (builds every section)."""
    try:
        parsed = ConfigModel.model_validate(dict(data))
    except ValidationError as exc:
        return _warnings([("", exc)], [])
    return _warnings([], [(name, getattr(parsed, name)) for name in ConfigModel.model_fields])


def validate(data: Mapping[str, Any]) -> list[str]:
    """Check ``data`` against the models; return warnings (never raises, never drops keys).

    Only the settings whose value differs from the default are validated, each with its own
    field's validator, in the model's field order; the warnings are those of the whole-model check
    (``_validate_whole``), which a key spelled by field name instead of its alias still takes.
    """
    fields = ConfigModel.model_fields
    if any(info.alias and info.alias != name and name in data for name, info in fields.items()):
        return _validate_whole(data)
    errors: list[tuple[str, ValidationError]] = []
    parsed: list[tuple[str, Any]] = []
    for name, info in fields.items():
        key = info.alias or name
        if key not in data:
            continue  # absent: the model would use the default, which is valid
        value = data[key]
        if key in _DEFAULTS and same_value(value, _DEFAULTS[key]):
            continue
        try:
            parsed.append((name, _adapter(name).validate_python(value)))
        except ValidationError as exc:
            errors.append((key, exc))
    return _warnings(errors, parsed)


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
