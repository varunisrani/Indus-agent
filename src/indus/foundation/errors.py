"""Typed errors with stable codes.

Library code raises an ``IndusError`` subclass; boundaries (CLI, protocol servers) turn it into an
exit code or a protocol error. ``code`` is a stable machine string listed in ``ERROR_CODES``;
``message`` is what a person sees; ``hint`` is an optional next step; ``detail`` is for logs only
and never part of ``str(err)``.

This module imports only the standard library: ``foundation.preparse`` loads it before any app
module.
"""

from __future__ import annotations

from typing import ClassVar

# Every code the product raises, with a one-line meaning. Later generated into the protocol error
# table (D-16.27); a new code is added here first.
ERROR_CODES: dict[str, str] = {
    "indus.error": "unclassified product error",
    "home.unsafe_dir": "a resolved home or its parent is a filesystem root or top-level directory",
    "config.error": "configuration problem",
    "config.unreadable": "config file could not be read or parsed; nothing was written",
    "config.failed_read": "the loaded config is a fallback after a failed read and cannot be saved",
    "config.managed": "this install is managed by a package manager; config is read-only",
    "profile.error": "profile problem",
    "profile.not_found": "the named profile does not exist or was deleted",
    "profile.invalid_id": "the text is not a valid profile id",
    "profile.tombstoned": "the profile was deleted and cannot be recreated implicitly",
    "profile.sticky_missing": "the saved active profile no longer exists",
    "scope.unresolved": "no profile scope is set for this task or thread",
    "secrets.error": "secret store problem",
    "secrets.unavailable": "the OS keychain was required but cannot be used",
    "secrets.backend_failed": "the secret store backend failed to read, write or delete a value",
    "secrets.unreadable": "a secret store file cannot be read or parsed; it was left as it is",
    "secrets.bad_mode": "security.secret_store is not one of auto, keychain or file",
    "store.error": "session store problem",
    "store.isolation_refused": "a test run tried to open the real session store",
    "store.unopenable": "the session store file is not SQLite and could not be moved aside",
    "store.not_writable": "the session store file or its folder cannot be written by this user",
    "store.locked": "another process held the session store's write lock past the time budget",
    "store.replaced": "the session store file was replaced while this process had it open",
    "store.corrupt": "the session store reported structural damage; writes are stopped",
    "store.unavailable": "the session store is missing, uninitialised or from a newer version",
    "store.tombstoned": "the session id was deleted and cannot be used again",
    "store.resume_too_large": "the session has more messages than the configured limit",
    "store.compression_busy": "another compression of this session is already running",
    "store.compression_closed": "the session was compressed into a child; continue in the child",
    "store.turn_lease_lost": "another process took over this session's turn lease",
    "store.session_not_found": "no session has this id or id prefix",
    "store.ambiguous_id": "the id prefix matches more than one session",
    "store.ambiguous_chain": "a compressed session has more than one live continuation",
    "store.title_taken": "another session already uses this title",
    "store.transcript_moved": "the live transcript changed after the caller read it",
    "store.compression_parent_ended": "the session ended deliberately before its summary was saved",
    "store.rewind_unavailable": "there is no user turn at that position to take back",
    "store.rewind_mismatch": "the conversation changed before the undo could be saved",
    "store.branch_refused": "the session or message to branch from is missing or not live",
    "store.session_guarded": "the session is in use (live turn lease or compression lock)",
    "store.vacuum_refused": "another handle or process has the session store open; VACUUM refused",
    "store.meta_too_large": "a per-session metadata value is larger than the 64 KiB limit",
    "session.busy": "another surface or process is running this session (per-session lock)",
    # P17-01 runtime ledger refusals (one bounded reason each)
    "runtime.refused": "the runtime ledger refused the request",
    "runtime.invalid_params": "an admission field is missing, too long or not a JSON object",
    "runtime.not_found": "no admission has this id",
    "runtime.stale_epoch": "a newer daemon owns the session store; this owner is outdated",
    "runtime.admission_conflict": "this request id was already used with a different input",
    "runtime.queue_full": "the session already has 32 queued inputs",
    "runtime.stale_generation": "the turn generation named is not the one the ledger holds",
    "runtime.unknown_execution": "a turn lost in a restart must be resolved first",
    "runtime.admission_started": "the input has already started or been handed to a running turn",
    "runtime.session_idle": "no turn is running in this session",
    "runtime.retired": "the session was deleted; it takes no new input",
    "provider.error": "model provider problem",
    "provider.invalid_profile": "a provider profile has a missing or invalid field",
    "provider.invalid_base_url": "a provider base URL is not a usable http(s) URL",
    "provider.conflict": "a provider name or alias is already taken",
    "provider.no_usable": "no provider could be resolved with a usable endpoint and key",
    "provider.key_host_mismatch": "a provider key would be sent to a host it does not belong to",
    "provider.context_too_small": "the model's context window is below the supported minimum",
    "provider.route_refused": "a route switch was refused: the route has no model or base URL",
    "provider.http": "the model provider answered with an HTTP error status",
    "provider.stream": "the model provider reported an error inside a stream or ended it early",
    "provider.stale_breaker": "the provider stayed silent on too many attempts in a row",
    "tools.error": "tool registry or dispatch problem",
    "tools.bad_schema": "a tool schema or its parameters is not a JSON object",
    "tools.name_taken": "a tool name is already registered in another toolset",
    "blocking.error": "blocking work could not be run off the event loop",
    "blocking.pool_full": "every worker of a blocking pool is busy; the work was refused",
    "blocking.pool_closed": "the blocking pool was shut down; the work was refused",
    "blocking.deadline_missed": "blocking work missed its deadline and was abandoned",
    "plugins.error": "a plugin could not be loaded or used",
    "plugins.load_failed": "a plugin's import or register() failed; it was skipped",
    "plugins.permission": "a plugin asked for something its operator did not allow",
    "plugins.closed": "a plugin whose load was abandoned tried to register something",
    "backend.error": "terminal backend problem",
    "backend.connection": "the terminal backend could not be reached; the command did not run",
    "backend.unavailable": "the terminal backend cannot be created here (missing dependency)",
    "backend.deadline": "a sandbox vendor call missed our deadline and was abandoned",
    "backend.sandbox_records_busy": "the sandbox records file stayed locked past the time budget",
    "backend.singularity.build": "building the Singularity/Apptainer image (SIF) failed",
    "auth.error": "provider sign-in or credential pool problem",
    "auth.unreadable": "auth.json could not be read; it was left as it is",
    "auth.locked": "another process held the auth store lock past the time budget",
    "auth.isolation_refused": "a test run tried to open the real auth store",
    "auth.unsafe_path": "the auth store's folder is a filesystem root, top-level or install dir",
    "auth.unknown_target": "no pool entry matches the given index, id or label",
    "auth.flow_failed": "a sign-in flow failed (bad answer from the authorization server)",
    "auth.denied": "the person declined the sign-in request",
    "auth.expired": "the sign-in code expired before it was approved",
    "auth.service_down": "the sign-in service kept failing until the code expired",
    "auth.relogin_required": "the stored grant is no longer valid; sign in again",
    "auth.callback_timeout": "no browser callback arrived before the deadline",
    "auth.port_busy": "the provider's fixed callback port is in use",
    "mcp.error": "MCP client problem",
    "mcp.config_invalid": "an mcp_servers entry is malformed; that server was skipped",
    "mcp.unresolved_ref": "a ${NAME} in a remote MCP server's URL or headers is not set",
    "mcp.client_cert_missing": "an MCP server's client certificate or key file does not exist",
    "mcp.url_refused": "an MCP server URL was refused (scheme, blocklist or metadata address)",
    "mcp.connect_timeout": "an MCP server did not finish connecting within its connect timeout",
    "mcp.connect_failed": "an MCP server could not be started or reached",
    "mcp.unavailable": "the MCP server is not connected (reconnecting, parked or closed)",
    "mcp.auth_required": "an MCP server needs a new OAuth sign-in (indus mcp login)",
    "mcp.oauth_failed": "an MCP OAuth sign-in could not finish",
    "mcp.malware_refused": "an MCP server package has a malware advisory, so it was not started",
    # P14 scheduled jobs
    "cron.error": "scheduled job problem",
    "cron.bad_schedule": "the schedule text is not one of the accepted forms",
    "cron.oneshot_past": "a one-shot time is more than 120 s in the past",
    "cron.next_run_failed": "the next run of a recurring job cannot be computed",
    "cron.invalid_job": "a job definition breaks a rule (payload, mode, field value)",
    "cron.job_not_found": "no job has this id or name",
    "cron.ambiguous_job": "the name matches more than one job; use the id",
    "cron.store_busy": "another process held the cron job store lock past the time budget",
    "cron.slot_taken": "this scheduled slot already has an attempt; it is never run twice",
    "cron.attempt_final": "the attempt is already finished; finished attempts never change",
    "cron.notepad_limit": "a notepad write would break the size limits; nothing was written",
}


class IndusError(Exception):
    """Base of every product error.

    ``IndusError(code, message)`` names the code explicitly; subclasses carry a default code, so
    ``ProfileNotFound("message")`` is enough.
    """

    default_code: ClassVar[str] = "indus.error"

    def __init__(
        self,
        code_or_message: str,
        message: str | None = None,
        *,
        detail: object = None,
        hint: str | None = None,
    ) -> None:
        if message is None:
            code, text = self.default_code, code_or_message
        else:
            code, text = code_or_message, message
        super().__init__(text)
        self.code: str = code
        self.message: str = text
        self.detail: object = detail
        self.hint: str | None = hint

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class UnsafeHomeDir(IndusError):
    default_code = "home.unsafe_dir"


class ConfigError(IndusError):
    default_code = "config.error"


class ConfigUnreadable(ConfigError):
    default_code = "config.unreadable"


class ConfigFailedRead(ConfigError):
    default_code = "config.failed_read"


class ConfigManaged(ConfigError):
    default_code = "config.managed"


class ProfileError(IndusError):
    default_code = "profile.error"


class ProfileNotFound(ProfileError):
    default_code = "profile.not_found"


class InvalidProfileId(ProfileError):
    default_code = "profile.invalid_id"


class ProfileTombstoned(ProfileError):
    default_code = "profile.tombstoned"


class StickyProfileMissing(ProfileError):
    default_code = "profile.sticky_missing"


class ScopeUnresolved(IndusError):
    default_code = "scope.unresolved"


class SecretStoreError(IndusError):
    default_code = "secrets.error"


class SecretStoreUnavailable(SecretStoreError):
    """``security.secret_store: keychain`` was asked for but the keychain is not usable."""

    default_code = "secrets.unavailable"


class StoreError(IndusError):
    """Session store errors; P02 adds the subclasses."""

    default_code = "store.error"


class StoreIsolationRefused(StoreError):
    """A process that looks like a test run tried to open the real store (P02-01)."""

    default_code = "store.isolation_refused"


class StoreUnopenable(StoreError):
    """The store file is not SQLite and moving it aside failed (P02-01)."""

    default_code = "store.unopenable"


class StoreNotWritable(StoreError):
    """The store file, a sidecar or its folder cannot be written (P02-01)."""

    default_code = "store.not_writable"


class StoreLocked(StoreError):
    """Write-lock contention outlasted the write budget; the data is not damaged (P02-03)."""

    default_code = "store.locked"


class StoreReplaced(StoreError):
    """The file at the store path is not the one this handle opened; writes are fenced (P02-07)."""

    default_code = "store.replaced"


class StoreCorrupt(StoreError):
    """Structural corruption was seen; the handle is quarantined for the process (P02-07)."""

    default_code = "store.corrupt"


class StoreUnavailable(StoreError):
    """The store cannot be used as asked: ``kind`` is ``missing``, ``uninitialised`` or
    ``incompatible`` (P02-06, P02-20)."""

    default_code = "store.unavailable"

    def __init__(
        self,
        code_or_message: str,
        message: str | None = None,
        *,
        kind: str = "unavailable",
        detail: object = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(code_or_message, message, detail=detail, hint=hint)
        self.kind: str = kind


class SessionTombstoned(StoreError):
    """The session id was deleted here and can never be created or imported again (P02-18)."""

    default_code = "store.tombstoned"


class ResumeTooLarge(StoreError):
    """More live messages than the limit: ``numeric`` is 4130 (resume) or 4131 (export) (P02-10)."""

    default_code = "store.resume_too_large"

    def __init__(
        self,
        code_or_message: str,
        message: str | None = None,
        *,
        count: int = 0,
        numeric: int = 4130,
        detail: object = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(code_or_message, message, detail=detail, hint=hint)
        self.count: int = count
        self.numeric: int = numeric


class ProviderError(IndusError):
    """Model provider errors; P03 adds the subclasses."""

    default_code = "provider.error"


class InvalidProviderProfile(ProviderError):
    default_code = "provider.invalid_profile"


class InvalidBaseURL(ProviderError):
    default_code = "provider.invalid_base_url"


class ProviderConflict(ProviderError):
    default_code = "provider.conflict"


class NoUsableProvider(ProviderError):
    """No route could be built; ``env_names`` lists the variables that would fix it."""

    default_code = "provider.no_usable"

    def __init__(
        self,
        code_or_message: str,
        message: str | None = None,
        *,
        env_names: tuple[str, ...] = (),
        detail: object = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(code_or_message, message, detail=detail, hint=hint)
        self.env_names: tuple[str, ...] = env_names


class KeyHostMismatch(ProviderError):
    default_code = "provider.key_host_mismatch"


class ContextTooSmall(ProviderError):
    """The resolved context window is below the minimum a session needs."""

    default_code = "provider.context_too_small"

    def __init__(
        self,
        code_or_message: str,
        message: str | None = None,
        *,
        tokens: int = 0,
        source: str = "",
        detail: object = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(code_or_message, message, detail=detail, hint=hint)
        self.tokens: int = tokens
        self.source: str = source


class RouteRefused(ProviderError):
    """A route cannot be bound: it names no model or no base URL (P04-23)."""

    default_code = "provider.route_refused"


class ProviderHTTPError(ProviderError):
    """A non-2xx answer from a model API. ``retry_after`` is in seconds when the server said."""

    default_code = "provider.http"

    def __init__(
        self,
        message: str,
        *,
        status: int,
        retry_after: float | None = None,
        headers: dict[str, str] | None = None,
        body: object = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.status: int = status
        self.retry_after: float | None = retry_after
        self.headers: dict[str, str] = dict(headers or {})
        self.body: object = body


class ProviderStreamError(ProviderError):
    """An error the provider put inside a 2xx stream (or a stream that ended early).

    ``body`` is always OpenAI-shaped, ``{"error": {"message": ..., "type"?, "code"?}}``, so the
    classifier reads one shape for every wire format.
    """

    default_code = "provider.stream"

    def __init__(
        self,
        message: str,
        *,
        body: dict[str, object],
        status: int | None = None,
        raw_text: str = "",
    ) -> None:
        super().__init__(message)
        self.status: int | None = status
        self.body: dict[str, object] = body
        self.raw_text: str = raw_text


class StaleBreakerTripped(ProviderError):
    """Too many consecutive silent attempts; calls fail at once until the route changes."""

    default_code = "provider.stale_breaker"


class AuthError(IndusError):
    """Auth store, credential pool and sign-in flow errors (P07)."""

    default_code = "auth.error"


class AuthStoreUnreadable(AuthError):
    """``auth.json`` exists but the OS refused the read; the file was not touched (R04.16)."""

    default_code = "auth.unreadable"


class AuthStoreLocked(AuthError):
    """Another process held the auth store lock past the timeout."""

    default_code = "auth.locked"

    def __init__(
        self,
        code_or_message: str,
        message: str | None = None,
        *,
        holder_pid: int | None = None,
        detail: object = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(code_or_message, message, detail=detail, hint=hint)
        self.holder_pid: int | None = holder_pid


class AuthIsolationRefused(AuthError):
    """A process that looks like a test run tried to open the real auth store (R03.24)."""

    default_code = "auth.isolation_refused"


class AuthUnsafePath(AuthError):
    """The auth store would be written into ``/``, a top-level dir or the install tree."""

    default_code = "auth.unsafe_path"
