"""Methods the terminal bridge adds to the core catalogue (P12-28..31).

The core methods (create, resume, submit, interrupt, steer, redirect, answer, capabilities) live in
``indus.protocol.methods``; this module declares the session, command, shell, config and composer
methods a terminal client also needs, plus the ``gateway.ready`` notification that opens a stdio
connection. ``methods`` appends ``BRIDGE_METHODS`` to its catalogue, so every name here is a
declared method with the same validation rules (unknown keys refused, blank text ``TEXT_EMPTY``).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, JsonValue, NonNegativeInt, PositiveInt

from indus.protocol.base import MessageText, ProtocolModel, SessionId
from indus.protocol.errors import (
    CONFIRM_REQUIRED,
    PROVIDER_UNAVAILABLE,
    SERVER_REQUEST_NOT_FOUND,
    SESSION_BUSY,
    SESSION_NOT_FOUND,
    SESSION_REFUSED,
    SHELL_REFUSED,
    SHELL_TIMEOUT,
    SLASH_NOT_OWNED,
    SLASH_WORKER_FAILED,
    STORE_FAILED,
    TEXT_EMPTY,
    UNSUPPORTED_COMMAND,
    ErrorCode,
)

READY_METHOD = "gateway.ready"
"""The notification a stdio bridge writes first."""


class BridgeReady(ProtocolModel):
    """The first frame of a connection (stdio or the daemon's WebSocket): the server is ready for
    requests."""

    theme: str
    """The configured theme name (``auto`` lets the client detect light or dark)."""
    heartbeat: bool = False
    """Whether the server answers pings; a stdio child does not need them."""
    protocol_version: NonNegativeInt
    """The protocol version the server speaks."""
    replay_epoch: str | None = None
    """The daemon's replay epoch (WebSocket attach): event cursors are only valid within it."""
    profile_id: str | None = None
    """The profile the daemon serves (WebSocket attach); a client refuses a mismatch."""
    instance_id: str | None = None
    """This daemon start (WebSocket attach); it changes on every restart."""
    capabilities: list[str] | None = None
    """What the daemon offers attached clients (WebSocket attach), as ``describe`` lists it."""


# --- history and sessions ------------------------------------------------------------------------


class HistoryRole(StrEnum):
    """Who wrote a history row."""

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class HistoryMessage(ProtocolModel):
    """One stored row, flattened for display."""

    role: HistoryRole
    """Who wrote it."""
    text: str
    """The text; image parts show as ``[image]``."""
    name: str | None = None
    """For tool rows: the tool's name."""
    arguments: dict[str, JsonValue] | None = None
    """For tool rows: the call's parsed arguments, masked as ``tool.started`` masks them (absent
    when the stored call has none that parse to an object)."""
    row_id: int | None = None
    """The stored row's id (undo and branch points)."""


class SessionRow(ProtocolModel):
    """One conversation in a session listing."""

    session_id: str
    """The conversation's current id."""
    title: str | None = None
    """Its title, when it has one."""
    preview: str = ""
    """The start of its first user message."""
    source: str = ""
    """Where it was started (``cli``, ``tui``, a platform name)."""
    model: str | None = None
    """The model it last ran on, when one is stored."""
    started_at: float
    """Start time, epoch seconds."""
    last_active: float | None = None
    """Newest activity, epoch seconds."""
    message_count: NonNegativeInt = 0
    """Stored messages."""


class SessionListParams(ProtocolModel):
    """List stored conversations, newest first."""

    limit: int = Field(default=50, ge=1, le=500)
    """How many rows at most."""
    query: str | None = None
    """Only conversations whose title or text matches this."""
    include_subagents: bool = False
    """Also list delegate runs (hidden by default)."""


class SessionListResult(ProtocolModel):
    """The listing."""

    sessions: list[SessionRow]
    """Rows, newest first."""


class SessionMostRecentParams(ProtocolModel):
    """Find the newest conversation a person started."""


class SessionMostRecentResult(ProtocolModel):
    """The newest conversation, if any."""

    session: SessionRow | None = None
    """Absent when there is none."""


class SessionCloseParams(ProtocolModel):
    """Detach a session: stop its runtime and keep its stored row."""

    session_id: SessionId
    """The session to close."""


class SessionCloseResult(ProtocolModel):
    """Whether a live session was closed."""

    closed: bool
    """False when the session was not attached."""


class SessionHistoryParams(ProtocolModel):
    """Read a session's stored conversation for display."""

    session_id: SessionId
    """The session to read."""


class SessionHistoryResult(ProtocolModel):
    """The display projection of the conversation."""

    messages: list[HistoryMessage]
    """Rows in order."""


class SessionTitleParams(ProtocolModel):
    """Read a session's title, or set it."""

    session_id: SessionId
    """The session."""
    title: str | None = None
    """The new title; absent only reads."""


class SessionTitleResult(ProtocolModel):
    """The session's title after the call."""

    title: str | None = None
    """Absent when the session has none."""
    pending: bool = False
    """True when the title waits for the session's first stored row."""


class SessionUndoParams(ProtocolModel):
    """Take back the newest user messages and the replies to them."""

    session_id: SessionId
    """The session."""
    count: PositiveInt = 1
    """How many user messages to take back."""


class SessionUndoResult(ProtocolModel):
    """What the undo removed."""

    removed: NonNegativeInt
    """Stored rows taken back."""
    prefill: str = ""
    """The oldest taken-back message, for the composer."""


class SessionCompressParams(ProtocolModel):
    """Compress the conversation now (between turns)."""

    session_id: SessionId
    """The session."""
    focus: str | None = None
    """What the summary should keep in view."""


class SessionCompressResult(ProtocolModel):
    """How the compression went."""

    status: Literal["compressed", "nothing", "blocked", "aborted", "preview"]
    """The engine's outcome."""
    message: str = ""
    """A line to show."""
    session_id: str
    """The session's id afterwards (a rotating compaction continues in a new id)."""


class SessionBranchParams(ProtocolModel):
    """Copy the conversation into a new, independent session."""

    session_id: SessionId
    """The session to branch from."""
    title: str | None = None
    """The branch's title; absent takes the next lineage title."""


class SessionBranchResult(ProtocolModel):
    """The new session."""

    session_id: str
    """The branch's id (not attached; resume it to use it)."""
    title: str | None = None
    """The branch's title."""


class SessionUsageParams(ProtocolModel):
    """Read the session's token and cost counters."""

    session_id: SessionId
    """The session."""


class UsageNumbers(ProtocolModel):
    """Token and cost counters of a session."""

    api_calls: NonNegativeInt = 0
    """Model calls made."""
    input_tokens: NonNegativeInt = 0
    """Uncached prompt tokens."""
    output_tokens: NonNegativeInt = 0
    """Answer tokens (reasoning included)."""
    cache_read_tokens: NonNegativeInt = 0
    """Prompt tokens served from the cache."""
    cache_write_tokens: NonNegativeInt = 0
    """Prompt tokens written to the cache."""
    reasoning_tokens: NonNegativeInt = 0
    """Reasoning tokens (part of the output)."""
    context_tokens: NonNegativeInt | None = None
    """The last prompt's size, when known."""
    context_window: NonNegativeInt | None = None
    """The model's window, when known."""
    cost_usd: float | None = None
    """Cost so far; absent when nothing could be priced."""
    cost_label: str = ""
    """The cost as shown to people."""


class SessionUsageResult(ProtocolModel):
    """The counters."""

    usage: UsageNumbers
    """Totals for this attachment of the session."""


class PromptBackgroundParams(ProtocolModel):
    """Run a prompt in a separate session; its reply arrives as a notice."""

    session_id: SessionId
    """The session the reply is reported to."""
    text: MessageText
    """The prompt."""


class PromptBackgroundResult(ProtocolModel):
    """The background task's handle."""

    task_id: str
    """The id of the session running it."""


# --- server requests -----------------------------------------------------------------------------


class ClarifyLockParams(ProtocolModel):
    """Lock one answer of a multi-question clarify request."""

    session_id: SessionId
    """The session that asked."""
    request_id: str = Field(min_length=1)
    """The clarify request."""
    question_id: str = Field(min_length=1)
    """The question being answered."""
    answer: list[str]
    """Picked choices or typed text; empty means skipped."""


class ClarifyLockResult(ProtocolModel):
    """What is still open."""

    remaining: list[str]
    """Question ids not locked yet; empty once the request was answered."""


class ServerRequestDeclineParams(ProtocolModel):
    """Decline an open server request the client cannot show; the server answers it with that
    kind's refusal (approval denied, clarify questions skipped, no sudo password, no secret)."""

    session_id: SessionId
    """The session that asked."""
    request_id: str = Field(min_length=1)
    """The request being declined."""
    reason: str | None = None
    """Why, in the client's words (passed to the model with an approval denial)."""


class ServerRequestDeclineResult(ProtocolModel):
    """Confirmation that the refusal was delivered."""

    accepted: bool
    """True when the waiting agent received the refusal."""


class GatewayPingParams(ProtocolModel):
    """Check that the server is alive (heartbeat)."""


class GatewayPingResult(ProtocolModel):
    """The heartbeat answer."""

    pong: bool = True
    """Always true."""
    protocol_version: NonNegativeInt
    """The protocol version the server speaks."""


# --- completion and commands ---------------------------------------------------------------------


class CompletionItem(ProtocolModel):
    """One completion candidate."""

    text: str
    """What replaces the typed word."""
    display: str
    """What the list shows."""
    meta: str = ""
    """A short note beside it."""


class CompleteSlashParams(ProtocolModel):
    """Complete a slash command being typed."""

    text: str
    """The composer text so far (starts with ``/``)."""
    session_id: str | None = None
    """The session whose skills count."""


class CompletePathParams(ProtocolModel):
    """Complete a path or an ``@`` reference being typed."""

    word: str
    """The word under the cursor."""
    session_id: str | None = None
    """The session whose folder and terminal backend count."""
    cwd: str | None = None
    """Folder to resolve relative paths in; absent uses the session's."""


class CompleteResult(ProtocolModel):
    """Completion candidates."""

    items: list[CompletionItem]
    """Best first."""


class CommandKind(StrEnum):
    """Where a slash command comes from."""

    BUILTIN = "builtin"
    QUICK = "quick"
    SKILL = "skill"


class CommandRunner(StrEnum):
    """Who runs a slash command."""

    SERVER = "server"
    CLIENT = "client"
    LATER = "later"
    UNAVAILABLE = "unavailable"
    """The server knows it but does not run it for this connection (a daemon refuses commands
    that change one viewer's own state or that only ``--local`` runs; W48-h)."""


class CatalogEntry(ProtocolModel):
    """One command the client can offer."""

    name: str
    """Canonical name, without the slash."""
    aliases: list[str] = Field(default_factory=list[str])
    """Other names."""
    description: str
    """One line."""
    category: str
    """Group heading key (``session``, ``config``, ``tools``, ``info``, ``exit``, ``custom``)."""
    args_hint: str = ""
    """Argument summary."""
    kind: CommandKind
    """Where it comes from."""
    runs: CommandRunner
    """Who runs it: the server (``slash.exec``), the client, nobody yet, or nobody on this
    connection (``unavailable``: do not offer it)."""


class CommandsCatalogParams(ProtocolModel):
    """List every slash command the client can offer."""

    session_id: str | None = None
    """The session whose skills count."""


class CommandsCatalogResult(ProtocolModel):
    """The catalogue."""

    commands: list[CatalogEntry]
    """Built-ins in registry order, then quick commands, then skills."""


class SlashExecParams(ProtocolModel):
    """Run a slash command on the server."""

    session_id: SessionId
    """The session it acts on."""
    command: MessageText
    """The whole command line, with or without the leading slash."""


class SlashExecResult(ProtocolModel):
    """What the command produced."""

    output: str = ""
    """Text to show."""
    prefill: str | None = None
    """Text to put back into the composer."""
    submit: str | None = None
    """Text the client should send as a prompt."""
    switch_to: str | None = None
    """A session the client should switch to."""


class DispatchKind(StrEnum):
    """What a dispatched command resolved to."""

    EXEC = "exec"
    ALIAS = "alias"
    SKILL = "skill"
    SEND = "send"
    PREFILL = "prefill"
    PLUGIN = "plugin"


class CommandDispatchParams(ProtocolModel):
    """Resolve a command word that is not a client command."""

    session_id: SessionId
    """The session it acts on."""
    name: str = Field(min_length=1)
    """The command word, with or without the slash."""
    arg: str = ""
    """Everything after the word."""


class CommandDispatchResult(ProtocolModel):
    """The resolution."""

    kind: DispatchKind
    """What to do next."""
    output: str | None = None
    """For ``exec``: the output to show."""
    message: str | None = None
    """For ``skill`` / ``send``: the prompt to submit; for ``prefill``: the composer text."""
    target: str | None = None
    """For ``alias``: the command it stands for."""


class ShellExecParams(ProtocolModel):
    """Run a shell command on this machine (the composer's ``!``)."""

    command: str
    """The command line."""
    session_id: str | None = None
    """The session whose folder it runs in (on the daemon: one this connection attached)."""


class ShellExecResult(ProtocolModel):
    """How the command ended."""

    stdout: str
    """The redacted tail of its standard output."""
    stderr: str
    """The redacted tail of its error output."""
    code: int
    """Exit status."""


# --- config and models ---------------------------------------------------------------------------


class ConfigGetParams(ProtocolModel):
    """Read settings: ``full`` (the client's settings), ``mtime`` (the file's change stamp) or one
    key."""

    key: str = Field(min_length=1)
    """``full``, ``mtime`` or a setting key."""
    session_id: str | None = None
    """The session whose live values count (model, reasoning, fast, yolo)."""


class ConfigGetResult(ProtocolModel):
    """The value read."""

    key: str
    """The key asked for."""
    value: JsonValue
    """The value; for ``full`` an object of every client setting; ``null`` when unset."""

    def to_wire(self) -> dict[str, Any]:
        """Keep a ``null`` value (and nulls inside it) on the wire."""
        return self.model_dump(mode="json")


class ConfigKey(StrEnum):
    """Settings a client may change."""

    MODEL = "model"
    VERBOSE = "verbose"
    YOLO = "yolo"
    REASONING = "reasoning"
    FAST = "fast"
    BUSY = "busy"
    THEME = "theme"
    INDICATOR = "indicator"
    DETAILS = "details"
    STATUSBAR = "statusbar"
    MOUSE = "mouse"


class ConfigSetParams(ProtocolModel):
    """Change one setting."""

    key: ConfigKey
    """Which setting."""
    value: JsonValue
    """The new value."""
    session_id: str | None = None
    """For session switches (model, reasoning, fast, yolo, verbose): the session."""
    confirm: bool = False
    """Confirms a change the server asked to confirm."""


class ConfigApplied(StrEnum):
    """When a change takes effect."""

    NOW = "now"
    DEFERRED = "deferred"


class ConfigSetResult(ProtocolModel):
    """The change as applied."""

    key: ConfigKey
    """The setting."""
    value: JsonValue
    """Its value now (or once deferred)."""
    applied: ConfigApplied
    """``deferred`` when it waits for the running turn to end."""
    saved: bool
    """True when it was written to the profile's settings file."""

    def to_wire(self) -> dict[str, Any]:
        """Keep a ``null`` value on the wire."""
        return self.model_dump(mode="json")


class ProviderOption(ProtocolModel):
    """One provider the model picker offers."""

    slug: str
    """The provider's name."""
    name: str
    """What people see."""
    auth_type: str
    """How it signs in (``api_key``, ``oauth``, …)."""
    authenticated: bool
    """Whether a key or sign-in is stored."""
    current: bool = False
    """The provider of the current model."""
    models: list[str] = Field(default_factory=list)
    """Model ids the provider's endpoint lists (filled for the current custom or local endpoint;
    empty when it lists none or could not be asked: the picker then takes a typed id)."""


class ModelOptionsParams(ProtocolModel):
    """List the providers and the current model."""

    session_id: str | None = None
    """The session whose model is current."""


class ModelOptionsResult(ProtocolModel):
    """The picker's data."""

    provider: str
    """The current provider."""
    model: str
    """The current model."""
    providers: list[ProviderOption]
    """Every provider, in catalogue order."""


class ModelSaveKeyParams(ProtocolModel):
    """Store an API key for a provider."""

    slug: str = Field(min_length=1)
    """The provider."""
    api_key: str = Field(min_length=1)
    """The key."""


class ModelSaveKeyResult(ProtocolModel):
    """The provider after the change."""

    provider: ProviderOption
    """Its refreshed row."""


class ModelDisconnectParams(ProtocolModel):
    """Remove a provider's stored keys."""

    slug: str = Field(min_length=1)
    """The provider."""


class ModelDisconnectResult(ProtocolModel):
    """Whether anything was removed."""

    removed: bool
    """False when nothing was stored."""


class ReloadMcpParams(ProtocolModel):
    """Reconnect the MCP servers and refresh their tools."""

    session_id: str | None = None
    """The session asking."""


class ReloadMcpResult(ProtocolModel):
    """How the reload went."""

    reloaded: bool
    """False when nothing could be reloaded."""
    message: str
    """A line to show."""


# --- composer helpers ----------------------------------------------------------------------------


class PasteCollapseParams(ProtocolModel):
    """Keep a long paste in a file and get a short placeholder for the composer."""

    text: MessageText
    """The pasted text."""


class PasteCollapseResult(ProtocolModel):
    """Where the paste went."""

    placeholder: str
    """What the composer shows instead of the paste."""
    path: str
    """The file holding the full text."""
    lines: PositiveInt
    """Lines in the paste."""


class ImageAttachParams(ProtocolModel):
    """Attach an image file to the next prompt."""

    session_id: SessionId
    """The session."""
    path: str = Field(min_length=1)
    """The image file."""


class ImageAttachResult(ProtocolModel):
    """The staged image."""

    name: str
    """The file name."""
    path: str
    """The resolved path."""
    attached: PositiveInt
    """Images staged for the next prompt so far."""


class InputDetectDropParams(ProtocolModel):
    """Tell whether pasted text is one or more dropped file paths."""

    text: str
    """The pasted text."""


class InputDetectDropResult(ProtocolModel):
    """The detected files."""

    is_drop: bool
    """True when every part names an existing file or folder."""
    paths: list[str] = Field(default_factory=list[str])
    """The resolved paths."""
    images: list[str] = Field(default_factory=list[str])
    """Those among them that are images."""


# --- the catalogue rows --------------------------------------------------------------------------

_SESSION = (SESSION_NOT_FOUND, SESSION_REFUSED, STORE_FAILED)

type MethodRow = tuple[str, type[ProtocolModel], type[ProtocolModel], tuple[ErrorCode, ...]]

BRIDGE_METHODS: tuple[MethodRow, ...] = (
    ("session.list", SessionListParams, SessionListResult, (STORE_FAILED,)),
    ("session.most_recent", SessionMostRecentParams, SessionMostRecentResult, (STORE_FAILED,)),
    ("session.close", SessionCloseParams, SessionCloseResult, ()),
    ("session.history", SessionHistoryParams, SessionHistoryResult, _SESSION),
    ("session.title", SessionTitleParams, SessionTitleResult, _SESSION),
    ("session.undo", SessionUndoParams, SessionUndoResult, (*_SESSION, SESSION_BUSY)),
    ("session.compress", SessionCompressParams, SessionCompressResult, (*_SESSION, SESSION_BUSY)),
    ("session.branch", SessionBranchParams, SessionBranchResult, (*_SESSION, SESSION_BUSY)),
    ("session.usage", SessionUsageParams, SessionUsageResult, (SESSION_NOT_FOUND,)),
    (
        "prompt.background",
        PromptBackgroundParams,
        PromptBackgroundResult,
        (*_SESSION, TEXT_EMPTY, PROVIDER_UNAVAILABLE),
    ),
    (
        "clarify.lock",
        ClarifyLockParams,
        ClarifyLockResult,
        (SESSION_NOT_FOUND, SERVER_REQUEST_NOT_FOUND),
    ),
    (
        "server_request.decline",
        ServerRequestDeclineParams,
        ServerRequestDeclineResult,
        (SESSION_NOT_FOUND, SERVER_REQUEST_NOT_FOUND),
    ),
    ("gateway.ping", GatewayPingParams, GatewayPingResult, ()),
    ("complete.slash", CompleteSlashParams, CompleteResult, ()),
    ("complete.path", CompletePathParams, CompleteResult, ()),
    ("commands.catalog", CommandsCatalogParams, CommandsCatalogResult, ()),
    (
        "slash.exec",
        SlashExecParams,
        SlashExecResult,
        (SESSION_NOT_FOUND, TEXT_EMPTY, SESSION_BUSY, SLASH_NOT_OWNED, SLASH_WORKER_FAILED),
    ),
    (
        "command.dispatch",
        CommandDispatchParams,
        CommandDispatchResult,
        (SESSION_NOT_FOUND, UNSUPPORTED_COMMAND, SHELL_REFUSED, SHELL_TIMEOUT),
    ),
    (
        "shell.exec",
        ShellExecParams,
        ShellExecResult,
        (TEXT_EMPTY, SHELL_REFUSED, SHELL_TIMEOUT, SESSION_NOT_FOUND),
    ),
    ("config.get", ConfigGetParams, ConfigGetResult, (SESSION_NOT_FOUND,)),
    (
        "config.set",
        ConfigSetParams,
        ConfigSetResult,
        (SESSION_NOT_FOUND, CONFIRM_REQUIRED, PROVIDER_UNAVAILABLE),
    ),
    ("model.options", ModelOptionsParams, ModelOptionsResult, ()),
    ("model.save_key", ModelSaveKeyParams, ModelSaveKeyResult, ()),
    ("model.disconnect", ModelDisconnectParams, ModelDisconnectResult, ()),
    ("reload.mcp", ReloadMcpParams, ReloadMcpResult, ()),
    ("paste.collapse", PasteCollapseParams, PasteCollapseResult, (TEXT_EMPTY,)),
    ("image.attach", ImageAttachParams, ImageAttachResult, (SESSION_NOT_FOUND,)),
    ("input.detect_drop", InputDetectDropParams, InputDetectDropResult, ()),
)
"""``(name, params, result, extra errors)`` for every bridge method, in declaration order."""

BRIDGE_NOTIFICATIONS: dict[str, type[ProtocolModel]] = {READY_METHOD: BridgeReady}
"""Notifications that are neither events nor server requests: method → params model."""
