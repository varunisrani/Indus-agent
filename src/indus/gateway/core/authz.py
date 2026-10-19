"""Who may talk to the agent, what a stranger sees, and when a group message is answered (P15-08).

``Authorizer.check`` runs an ordered chain; the first grant wins and anything that falls through
is denied (K14). Any error inside the chain is a denial: it fails closed.

1. ``trusted_platform``: webhook and ``trusted_inbound`` platforms (their own signature or key
   authenticated the event);
2. chat-scoped grants that need no user id: ``upstream`` (the adapter authorizes upstream),
   ``group_chat_allowlist`` (a group chat listed by id), ``bot_allowed`` (a bot sender admitted by
   the platform's allow-bots setting ``mentions`` or ``all``);
3. ``no_user_id``: deny;
4. ``platform_allow_all``: the platform's allow-all switch;
5. ``role``: an adapter-verified role grant carried on the source (Discord role allowlist);
6. ``paired``: a pairing approval (P15-09);
7. when no env allowlist exists at all: ``own_policy`` (the adapter's own access policy, only an
   ``allowlist`` policy is a verdict), ``config_allowlist`` (``allow_from`` /
   ``group_allow_from``), ``global_allow_all``;
8. otherwise ``allowlist``: the union of the platform, group-user and global env allowlists plus
   ids the adapter resolved from names; ``*`` means everyone;
9. ``default_deny``.
A bot-authored message that passes is still refused (``bot_cooldown``) while its conversation's
bot-loop guard cools down; the guard is only read here (admission counts once, P15-10).
Button presses are authorized by the same chain with the presser as the source.

Unknown DM behaviour (``pair`` / ``ignore`` / ``decline``, R06.8): the platform's own setting;
else the global one when it differs from the default; else the adapter's own DM policy (pairing →
pair, allowlist / disabled → ignore); else ``ignore`` when any allowlist is configured; else
``pair``. ``UnauthorizedHandler`` acts on it: groups and id-less messages are dropped silently,
bots never get a code, codes and login offers need a platform whose DMs are private (R06.24),
``ignore`` logs the fix and tells the home channel once per sender, ``decline`` answers once per
sender per 24 h (stamp first). Mention gating: in groups, answer only when mentioned unless the
channel is free-response.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from indus.gateway.core._shapes import AdapterLike, SourceLike, capability, flag, text_of
from indus.gateway.core.bot_loop import BotLoopGuard, conversation_of
from indus.gateway.registry import PlatformEntry, PlatformRegistry, policy_env_name
from indus.state.pairing import CODE_TTL_SECONDS, PairingStore

__all__ = [
    "DEFAULT_UNKNOWN_DM",
    "GLOBAL_ALLOWLIST_ENV",
    "GLOBAL_ALLOW_ALL_ENV",
    "UNKNOWN_DM_MODES",
    "AuthzDecision",
    "Authorizer",
    "MentionPolicy",
    "PlatformPolicy",
    "Response",
    "UnauthorizedHandler",
    "UnknownDmMode",
    "allow_set",
    "decline_text",
    "pairing_reply",
    "should_answer",
    "try_later_text",
]

log = logging.getLogger(__name__)

GLOBAL_ALLOWLIST_ENV = "INDUS_GATEWAY_ALLOWED_USERS"
GLOBAL_ALLOW_ALL_ENV = "INDUS_GATEWAY_ALLOW_ALL_USERS"
UNKNOWN_DM_MODES = ("pair", "ignore", "decline")
DEFAULT_UNKNOWN_DM = "pair"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_BOT_MODES = frozenset({"mentions", "all"})
_MAX_NAME = 64
_MAX_SEEN = 2048
_SIGILS = set("@<>[]()`*_~#|")

type UnknownMode = Literal["pair", "ignore", "decline"]


# -- helpers --------------------------------------------------------------------------------------


def allow_set(raw: object) -> set[str]:
    """An allowlist from a YAML list, a JSON-list string or a comma string; blanks dropped."""
    if raw is None:
        return set()
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            try:
                raw = json.loads(text)
            except ValueError:
                raw = text
        else:
            return {part.strip() for part in text.split(",") if part.strip()}
    if isinstance(raw, list | tuple | set | frozenset):
        items: Iterable[Any] = raw  # pyright: ignore[reportUnknownVariableType]
        return {str(item).strip() for item in items if str(item).strip()}
    text = str(raw).strip()
    return {text} if text else set()


def _allows(allowed: set[str], *candidates: str) -> bool:
    return "*" in allowed or any(c and c in allowed for c in candidates)


def _truthy(value: str) -> bool:
    return value.strip().lower() in _TRUTHY


def _is_group(source: SourceLike) -> bool:
    return (text_of(source.chat_type).lower() or "dm") != "dm"


def _str_or_empty(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


# -- inputs and results ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlatformPolicy:
    """What the chain needs to know about one platform (from its registry entry)."""

    platform: str
    allowlist_env: str = ""
    allow_all_env: str = ""
    allow_bots_env: str = ""
    group_users_env: str = ""
    group_chats_env: str = ""
    trusted_inbound: bool = False
    dm_is_private: bool = True

    @classmethod
    def from_entry(cls, entry: PlatformEntry) -> PlatformPolicy:
        return cls(
            platform=entry.name,
            allowlist_env=entry.allowlist_env,
            allow_all_env=entry.allow_all_env,
            allow_bots_env=entry.allow_bots_env or "",
            group_users_env=entry.group_users_env,
            group_chats_env=entry.group_chats_env,
            trusted_inbound=entry.trusted_inbound,
            dm_is_private=entry.dm_is_private,
        )

    @classmethod
    def standard(cls, platform: str) -> PlatformPolicy:
        """The ``INDUS_<PLATFORM>_*`` names for a platform with no special settings."""
        return cls(
            platform=platform,
            allowlist_env=policy_env_name(platform, "ALLOWED_USERS"),
            allow_all_env=policy_env_name(platform, "ALLOW_ALL_USERS"),
            allow_bots_env=policy_env_name(platform, "ALLOW_BOTS"),
        )


@dataclass(frozen=True)
class AuthzDecision:
    granted: bool
    step: str
    detail: str = ""


@dataclass(frozen=True)
class UnknownDmMode:
    mode: UnknownMode
    why: str


def _grant(step: str, detail: str = "") -> AuthzDecision:
    return AuthzDecision(True, step, detail)


def _deny(step: str, detail: str = "") -> AuthzDecision:
    return AuthzDecision(False, step, detail)


# -- the chain ------------------------------------------------------------------------------------


class Authorizer:
    """The authorization chain for one gateway profile.

    ``env(name)`` reads a policy value (the profile's secret store first, then its env);
    ``extra_for(platform)`` returns ``platforms.<platform>.extra``; ``adapter_for(platform)`` the
    connected adapter (or None); ``is_paired(platform, user_id)`` the pairing check.
    """

    def __init__(
        self,
        *,
        policy_for: Callable[[str], PlatformPolicy | None],
        env: Callable[[str], str | None],
        extra_for: Callable[[str], Mapping[str, Any] | None] = lambda _p: None,
        adapter_for: Callable[[str], AdapterLike | None] = lambda _p: None,
        is_paired: Callable[[str, str], bool] | None = None,
        global_unknown_dm: Callable[[], object] = lambda: None,
        bot_guard: BotLoopGuard | None = None,
        profile: str = "",
    ) -> None:
        self._policy_for = policy_for
        self._env = env
        self._extra_for = extra_for
        self._adapter_for = adapter_for
        self._is_paired = is_paired
        self._global_unknown_dm = global_unknown_dm
        self.bot_guard = bot_guard
        self.profile = profile

    @classmethod
    def from_registry(cls, registry: PlatformRegistry, **kwargs: Any) -> Authorizer:
        def policy_for(platform: str) -> PlatformPolicy | None:
            entry = registry.get(platform)
            return None if entry is None else PlatformPolicy.from_entry(entry)

        return cls(policy_for=policy_for, **kwargs)

    # -- readers ---------------------------------------------------------------------------------

    def policy(self, platform: str) -> PlatformPolicy | None:
        return self._policy_for(platform.strip().lower())

    def _read(self, name: str) -> str:
        if not name:
            return ""
        value = self._env(name)
        return value.strip() if isinstance(value, str) else ""

    def _extra(self, platform: str) -> Mapping[str, Any]:
        extra = self._extra_for(platform)
        return extra if isinstance(extra, Mapping) else {}

    def bot_mode(self, platform: str) -> str:
        """``none`` / ``mentions`` / ``all``: which bot senders the platform admits."""
        extra = self._extra(platform)
        raw = extra.get("allow_bots")
        if raw is None:
            policy = self._policy_for(platform)
            raw = self._read(policy.allow_bots_env) if policy is not None else ""
        mode = str(raw).strip().lower() if raw is not None else ""
        return mode if mode in _BOT_MODES else "none"

    def _env_lists(self, policy: PlatformPolicy, *, group: bool) -> dict[str, str]:
        """The env allowlists that apply here, by env name (empty ones left out)."""
        names = [policy.allowlist_env, GLOBAL_ALLOWLIST_ENV]
        if group:
            names += [policy.group_users_env, policy.group_chats_env]
        return {n: v for n in names if n and (v := self._read(n))}

    def configured_allowlists(self, platform: str) -> list[str]:
        """Every allowlist the chain would read for ``platform`` that has entries."""
        policy = self._policy_for(platform)
        found: list[str] = []
        if policy is not None:
            found += list(self._env_lists(policy, group=True))
        extra = self._extra(platform)
        for key in ("allow_from", "group_allow_from", "group_allowed_chats"):
            if allow_set(extra.get(key)):
                found.append(f"platforms.{platform}.extra.{key}")
        return found

    # -- check -----------------------------------------------------------------------------------

    def check(self, source: SourceLike, *, allow_delegation: bool = True) -> AuthzDecision:
        """May ``source`` reach the agent? Never raises: an error is a denial."""
        try:
            decision = self._principal(source, allow_delegation=allow_delegation)
            guard = self.bot_guard
            if (
                decision.granted
                and flag(source, "is_bot")
                and guard is not None
                and guard.blocked(conversation_of(source, self.profile))
            ):
                return _deny("bot_cooldown", "this chat's bot-loop guard is cooling down")
            return decision
        except Exception as exc:
            log.error("authorization failed closed for a %s message: %s", source.platform, exc)
            return _deny("error", str(exc))

    def _principal(self, source: SourceLike, *, allow_delegation: bool) -> AuthzDecision:
        platform = text_of(source.platform).lower()
        policy = self._policy_for(platform)
        if policy is None:
            return _deny("unknown_platform", platform)
        if platform == "webhook" or policy.trusted_inbound:
            return _grant("trusted_platform")

        group = _is_group(source)
        adapter = self._adapter_for(platform)
        extra = self._extra(platform)
        chat_id = text_of(source.chat_id)

        # Chat-scoped grants: no user id needed.
        if allow_delegation and capability(adapter, "auth_upstream"):
            return _grant("upstream")
        if group and chat_id:
            chats = allow_set(self._read(policy.group_chats_env)) | allow_set(
                extra.get("group_allowed_chats")
            )
            if _allows(chats, chat_id):
                return _grant("group_chat_allowlist")
        if flag(source, "is_bot") and self.bot_mode(platform) in _BOT_MODES:
            return _grant("bot_allowed")

        user = text_of(source.user_id)
        if not user:
            return _deny("no_user_id")
        alt = text_of(source.user_id_alt)
        if policy.allow_all_env and _truthy(self._read(policy.allow_all_env)):
            return _grant("platform_allow_all")
        if allow_delegation and source.role_authorized is True:
            return _grant("role")
        if self._is_paired is not None and self._is_paired(platform, user):
            return _grant("paired")

        env_lists = self._env_lists(policy, group=group)
        if not env_lists:
            owns = allow_delegation and capability(adapter, "owns_access_policy")
            if owns and adapter is not None:
                verdict = self._own_policy(adapter, user, group=group)
                if verdict is not None:
                    return (_grant if verdict else _deny)("own_policy")
            configured = allow_set(extra.get("group_allow_from" if group else "allow_from"))
            if configured and _allows(configured, user, alt):
                return _grant("config_allowlist")
            if _truthy(self._read(GLOBAL_ALLOW_ALL_ENV)):
                return _grant("global_allow_all")
            return _deny("default_deny", "no allowlist names this sender")

        ids: set[str] = set()
        for name in (policy.allowlist_env, policy.group_users_env, GLOBAL_ALLOWLIST_ENV):
            if name in env_lists:
                ids |= allow_set(env_lists[name])
        if policy.allowlist_env in env_lists:
            ids |= self._resolved_ids(adapter)
        if _allows(ids, user, alt):
            return _grant("allowlist")
        return _deny("default_deny", "not in the allowlist")

    @staticmethod
    def _own_policy(adapter: AdapterLike, user: str, *, group: bool) -> bool | None:
        """The adapter's own verdict: only an ``allowlist`` policy counts (open forwards anyone)."""
        policy = adapter.access_policy("group" if group else "dm")
        if not isinstance(policy, str) or policy.strip().lower() != "allowlist":
            return None
        if group:
            return True
        return adapter.allows_dm(user) is True

    @staticmethod
    def _resolved_ids(adapter: AdapterLike | None) -> set[str]:
        if adapter is None:
            return set()
        found: Iterable[object] = adapter.resolved_allowlist_ids()
        return {str(i).strip() for i in found if isinstance(i, str | int) and str(i).strip()}

    # -- unknown senders -------------------------------------------------------------------------

    def unknown_dm_mode(self, platform: str) -> UnknownDmMode:
        """What an unauthorized DM on ``platform`` gets, and why (setup and doctor print it)."""
        platform = platform.lower()
        extra = self._extra(platform)
        own = _str_or_empty(extra.get("unauthorized_dm_behavior")).lower()
        if own in UNKNOWN_DM_MODES:
            return UnknownDmMode(
                own,  # pyright: ignore[reportArgumentType]
                f"set by platforms.{platform}.extra.unauthorized_dm_behavior",
            )
        global_mode = _str_or_empty(self._global_unknown_dm()).lower()
        if global_mode in UNKNOWN_DM_MODES and global_mode != DEFAULT_UNKNOWN_DM:
            return UnknownDmMode(
                global_mode,  # pyright: ignore[reportArgumentType]
                "set by gateway.unauthorized_dm_behavior",
            )
        dm_policy = ""
        adapter = self._adapter_for(platform)
        if adapter is not None:
            got = adapter.access_policy("dm")
            dm_policy = got.strip().lower() if isinstance(got, str) else ""
        if not dm_policy:
            dm_policy = _str_or_empty(extra.get("dm_policy")).lower()
        if dm_policy == "pairing":
            return UnknownDmMode("pair", f"the {platform} DM policy is pairing")
        if dm_policy in {"allowlist", "disabled"}:
            return UnknownDmMode("ignore", f"the {platform} DM policy is {dm_policy}")
        lists = self.configured_allowlists(platform)
        if lists:
            return UnknownDmMode(
                "ignore",
                f"an allowlist is configured ({', '.join(lists)}), so unknown senders get no "
                "pairing code; set unauthorized_dm_behavior: pair to change that",
            )
        return UnknownDmMode("pair", "no allowlist is configured, so unknown senders get a code")

    def explain(self, platform: str) -> str:
        mode = self.unknown_dm_mode(platform)
        return f"{platform}: unknown direct messages → {mode.mode} ({mode.why})"

    def dm_is_private(self, platform: str) -> bool:
        """Codes and login offers go only where a DM is really private (R06.24)."""
        policy = self._policy_for(platform.lower())
        if policy is None or not policy.dm_is_private:
            return False
        adapter = self._adapter_for(platform.lower())
        return adapter is None or adapter.capabilities.dm_is_private is not False


# -- reply copy (ours, D-16.23) -------------------------------------------------------------------


def pairing_reply(platform: str, code: str, profile: str = "") -> str:
    profile_arg = f"-p {profile} " if profile and profile != "default" else ""
    hours = max(1, CODE_TTL_SECONDS // 3600)
    lifetime = "one hour" if hours == 1 else f"{hours} hours"
    return (
        "Hi! This bot doesn't know you yet. Its owner can let you in by running:\n\n"
        f"    indus {profile_arg}pairing approve {platform} {code}\n\n"
        f"The code works for {lifetime}. Once you're in, send your message again."
    )


def try_later_text() -> str:
    return "Too many pairing requests right now. Please try again later."


def decline_text() -> str:
    return "Sorry, this bot only talks to people its owner has let in."


def _safe_name(name: str) -> str:
    cut = "".join(ch for ch in name[:_MAX_NAME] if ch.isprintable() and ch not in _SIGILS)
    return " ".join(cut.split())


def owner_hint(platform: str, user_id: str, user_name: str, allowlist_env: str) -> str:
    name = _safe_name(user_name)
    who = f"{name} ({user_id})" if name else user_id
    where = (
        f"add {user_id} to {allowlist_env} in the profile's .env"
        if allowlist_env
        else "add their id to the platform's allowlist"
    )
    return (
        f"Ignored a direct message on {platform} from {who}, who is not allowed in. To let them "
        f"in, {where}, or set platforms.{platform}.extra.unauthorized_dm_behavior: pair."
    )


# -- acting on a denial ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Response:
    action: Literal["silent", "ignored", "declined", "paired", "try_later"]
    detail: str = ""


class UnauthorizedHandler:
    """What an unauthorized sender gets (and what the owner hears about it)."""

    def __init__(
        self,
        authorizer: Authorizer,
        pairing: PairingStore,
        *,
        max_seen: int = _MAX_SEEN,
    ) -> None:
        self.authorizer = authorizer
        self.pairing = pairing
        self._seen: dict[tuple[str, str], None] = {}
        self._max_seen = max(1, max_seen)
        self._lock = threading.Lock()

    def first_time(self, platform: str, user_id: str) -> bool:
        """True the first time this sender is seen (bounded memory: oldest senders forgotten)."""
        key = (platform, user_id)
        with self._lock:
            known = key in self._seen
            self._seen.pop(key, None)
            self._seen[key] = None  # newest last
            if len(self._seen) > self._max_seen:
                oldest = next(iter(self._seen))
                del self._seen[oldest]
            return not known

    async def respond(
        self,
        source: SourceLike,
        *,
        send_reply: Callable[[str], Awaitable[object]],
        notify_home: Callable[[str], Awaitable[object]] | None = None,
        home_chat_id: str | None = None,
    ) -> Response:
        platform = text_of(source.platform).lower()
        user = text_of(source.user_id)
        if _is_group(source) or not user:
            return Response("silent")
        mode = self.authorizer.unknown_dm_mode(platform).mode
        if mode == "pair" and (
            flag(source, "is_bot") or not self.authorizer.dm_is_private(platform)
        ):
            mode = "ignore"
        if mode == "decline":
            return await self._decline(platform, user, send_reply)
        if mode == "pair":
            return await self._pair(source, platform, user, send_reply)
        return await self._ignore(source, platform, user, notify_home, home_chat_id)

    async def _decline(
        self, platform: str, user: str, send_reply: Callable[[str], Awaitable[object]]
    ) -> Response:
        if self.pairing.has_recent_decline(platform, user):
            return Response("silent", "declined within the last 24 h")
        self.pairing.record_decline(platform, user)  # before the send: a failure cannot repeat
        try:
            await send_reply(decline_text())
        except Exception as exc:
            log.warning("could not send the decline reply on %s: %s", platform, exc)
        return Response("declined")

    async def _pair(
        self,
        source: SourceLike,
        platform: str,
        user: str,
        send_reply: Callable[[str], Awaitable[object]],
    ) -> Response:
        request = self.pairing.request_code(platform, user, text_of(source.user_name))
        if request.status == "issued" and request.code is not None:
            text = pairing_reply(platform, request.code, self.authorizer.profile)
            try:
                await send_reply(text)
            except Exception as exc:
                log.warning("could not send the pairing code on %s: %s", platform, exc)
            log.info(
                "pairing request %s opened on %s for user %s", request.request_id, platform, user
            )
            return Response("paired", request.request_id or "")
        if request.notify:
            try:
                await send_reply(try_later_text())
            except Exception as exc:
                log.warning("could not send the try-later reply on %s: %s", platform, exc)
            return Response("try_later", request.status)
        return Response("silent", request.status)

    async def _ignore(
        self,
        source: SourceLike,
        platform: str,
        user: str,
        notify_home: Callable[[str], Awaitable[object]] | None,
        home_chat_id: str | None,
    ) -> Response:
        if not self.first_time(platform, user):
            return Response("silent", "already reported")
        policy = self.authorizer.policy(platform)
        hint = owner_hint(
            platform,
            user,
            text_of(source.user_name),
            policy.allowlist_env if policy is not None else "",
        )
        log.warning("%s", hint)
        own_chat = text_of(source.chat_id)
        if notify_home is not None and home_chat_id and str(home_chat_id) != own_chat:
            try:
                await notify_home(hint)
            except Exception as exc:
                log.warning(
                    "could not tell the %s home channel about a stranger: %s", platform, exc
                )
        return Response("ignored")


# -- mention gating -------------------------------------------------------------------------------


@dataclass(frozen=True)
class MentionPolicy:
    require_mention: bool = True
    free_response: frozenset[str] = frozenset()

    @classmethod
    def from_extra(cls, extra: Mapping[str, Any] | None) -> MentionPolicy:
        data: Mapping[str, Any] = extra if isinstance(extra, Mapping) else {}
        raw = data.get("require_mention")
        require = True if raw is None else (raw if isinstance(raw, bool) else _truthy(str(raw)))
        return cls(require, frozenset(allow_set(data.get("free_response_channels"))))


def should_answer(
    source: SourceLike, *, mentioned: bool, policy: MentionPolicy, bot_mode: str = "none"
) -> bool:
    """In groups answer only when mentioned, unless the channel is free-response.

    A bot admitted with ``mentions`` must mention us like anyone else; ``all`` answers bots freely.
    """
    if not _is_group(source):
        return True
    if flag(source, "is_bot") and bot_mode == "all":
        return True
    if not policy.require_mention:
        return True
    where = {
        text_of(source.chat_id),
        text_of(source.parent_chat_id),
        text_of(source.thread_id),
    } - {""}
    if where & policy.free_response:
        return True
    return mentioned
