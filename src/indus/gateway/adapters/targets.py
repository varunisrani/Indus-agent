"""Send and cron target rules for every built-in chat platform (P16-32).

A target is ``platform:address`` in P14's grammar (``indus.cron.targets``). P14 cuts the address at
its first ``:`` into chat and thread unless the platform registers its own splitter; this module
holds one rule per platform: which chat ids its adapter can send to, how a thread is written, and
the message for anything else. ``install()`` registers every rule with P14's seam, so cron jobs,
``indus send``, webhook routes and Graph forwards all read addresses the same way.

A chat written ``#name`` is a friendly name on every platform: it is kept as written and the
delivery router resolves it through the channel directory when it sends (P15-20). Matrix keeps a
whole ``#alias:server`` as that name; Mattermost channel names written without ``#`` get one.

Rules only use ``re`` and the identity helpers, so importing this never imports a platform SDK.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from indus.gateway.adapters._identity import nostr_pubkey

__all__ = [
    "RULES",
    "TargetError",
    "TargetRule",
    "hash_is_native",
    "install",
    "parse_target",
    "render_target",
    "rule_for",
    "validate_target",
]

type Address = tuple[str, str | None]


class TargetError(ValueError):
    """An address that does not fit the platform's rule (the message names the accepted forms)."""


@dataclass(frozen=True)
class TargetRule:
    """How one platform's addresses are written."""

    platform: str
    split: Callable[[str], Address]
    forms: str
    examples: tuple[str, ...] = ()
    # ``#`` starts a real chat id here (IRC channels, Matrix aliases), not only a directory name.
    hash_native: bool = False


# -- shared pieces --------------------------------------------------------------------------------

_BAD_CHARS: Final = re.compile(r"[\s,\x00-\x1f\x7f]")
_DIRECTORY_NAME: Final = re.compile(r"#[^\s,\x00-\x1f\x7f:]{1,200}")
_UUID: Final = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_E164: Final = re.compile(r"\+[1-9][0-9]{6,14}")
_HEX64: Final = re.compile(r"[0-9a-fA-F]{64}")


def _refuse(platform: str, forms: str, address: str) -> TargetError:
    shown = address if len(address) <= 80 else address[:77] + "..."
    return TargetError(f"{shown!r} is not a {platform} target ({forms})")


def _clean(address: str) -> str:
    text = address.strip()
    if not text or _BAD_CHARS.search(text):
        raise TargetError("an address has no spaces, commas or control characters")
    return text


def _halves(address: str) -> Address:
    chat, sep, thread = address.partition(":")
    return chat, (thread if sep else None)


def _directory(address: str, thread_ok: Callable[[str], bool] | None) -> Address | None:
    """``#name[:thread]`` as a directory name, or None when ``address`` is not one."""
    if not address.startswith("#"):
        return None
    chat, thread = _halves(address)
    if not _DIRECTORY_NAME.fullmatch(chat):
        return None
    if thread is not None and (thread_ok is None or not thread_ok(thread)):
        return None
    return chat, thread


def _rule(
    platform: str,
    forms: str,
    examples: tuple[str, ...],
    *,
    chat: Callable[[str], str | None],
    thread: Callable[[str], bool] | None = None,
    hash_native: bool = False,
) -> TargetRule:
    """A rule for a platform whose chat ids hold no ``:``: ``chat[:thread]``.

    ``chat`` returns the canonical chat id (or None when the text is not one); ``thread`` says
    whether a thread id is acceptable (None: the platform has no threads).
    """

    def split(address: str) -> Address:
        text = _clean(address)
        if not hash_native:
            named = _directory(text, thread)
            if named is not None:
                return named
        head, tail = _halves(text)
        found = chat(head)
        if found is None or (tail is not None and (thread is None or not thread(tail))):
            raise _refuse(platform, forms, text)
        return found, tail

    return TargetRule(platform, split, forms, examples, hash_native)


def _matching(pattern: re.Pattern[str]) -> Callable[[str], str | None]:
    return lambda text: text if pattern.fullmatch(text) else None


def _thread(pattern: re.Pattern[str]) -> Callable[[str], bool]:
    return lambda text: bool(pattern.fullmatch(text))


# -- one rule per platform ------------------------------------------------------------------------

_TELEGRAM_CHAT = re.compile(r"-?[0-9]{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31}")
_SNOWFLAKE = re.compile(r"[0-9]{15,22}")
_SLACK_CHAT = re.compile(r"[CDGUW][A-Z0-9]{8,}")
_SLACK_MENTION = re.compile(r"<@([UW][A-Z0-9]{8,})(?:\|[^>]*)?>")
_SLACK_TS = re.compile(r"[0-9]{9,11}(?:\.[0-9]{1,6})?")
_SIGNAL_GROUP = re.compile(r"group\.[A-Za-z0-9+/=_-]{4,128}")
_MATTERMOST_ID = re.compile(r"[a-z0-9]{26}")
_MATTERMOST_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_EMAIL = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9-]{1,63})+"
)
_PHONE_SEPARATORS = re.compile(r"[ ().-]")
_WHATSAPP_NUMBER = re.compile(r"[1-9][0-9]{5,14}")
_LINE_CHAT = re.compile(r"[UCR][0-9a-fA-F]{32}")
_NTFY_TOPIC = re.compile(r"[-_A-Za-z0-9]{1,64}")
_IRC_CHANNEL = re.compile(r"[#&][^\s,:\x00-\x1f\x7f]{1,49}")
_IRC_NICK = re.compile(r"[A-Za-z\[\]\\`_^{|}][A-Za-z0-9\[\]\\`_^{|}-]{0,30}")
_QQ_ID = re.compile(r"[A-Za-z0-9_.=-]{1,128}")
_DINGTALK_CONVERSATION = re.compile(r"[A-Za-z0-9+/=_-]{8,128}")
_DINGTALK_STAFF = re.compile(r"[A-Za-z0-9_-]{1,64}")
_WECOM_ID = re.compile(r"[A-Za-z0-9_.@-]{1,128}")
_FEISHU_CHAT = re.compile(r"(?:oc|ou|on|chat|open)_[-A-Za-z0-9]{1,128}")
_FEISHU_THREAD = re.compile(r"[-A-Za-z0-9_]{1,128}")
_FEISHU_COMMENT = re.compile(r"cmt_[a-z]{2,16}_[A-Za-z0-9]{4,128}")
_FEISHU_COMMENT_THREAD = re.compile(r"whole|[A-Za-z0-9_-]{1,64}")
_BLUEBUBBLES_GUID = re.compile(r"[A-Za-z]{2,16};[-+];[^\s,:;]{1,200}")
_BLUEBUBBLES_PHONE = re.compile(r"\+?[0-9][0-9().-]{5,}")
_DIGITS = re.compile(r"[0-9]{1,20}")


def _slack_chat(text: str) -> str | None:
    mention = _SLACK_MENTION.fullmatch(text)
    if mention is not None:
        return mention.group(1)
    return text if _SLACK_CHAT.fullmatch(text) else None


def _signal_chat(text: str) -> str | None:
    if _E164.fullmatch(text) or _SIGNAL_GROUP.fullmatch(text):
        return text
    return text.lower() if _UUID.fullmatch(text) else None


def _email_chat(text: str) -> str | None:
    return text.lower() if _EMAIL.fullmatch(text) else None


def _whatsapp_chat(text: str) -> str | None:
    digits = _PHONE_SEPARATORS.sub("", text).removeprefix("+")
    return digits if _WHATSAPP_NUMBER.fullmatch(digits) else None


def _irc_chat(text: str) -> str | None:
    return text if _IRC_CHANNEL.fullmatch(text) or _IRC_NICK.fullmatch(text) else None


def _wecom_chat(text: str) -> str | None:
    return text if _WECOM_ID.fullmatch(text) else None


def _bluebubbles_chat(text: str) -> str | None:
    if _BLUEBUBBLES_GUID.fullmatch(text):
        return text
    if _BLUEBUBBLES_PHONE.fullmatch(text):
        return text
    return text.lower() if _EMAIL.fullmatch(text) else None


def _buzz_dm(text: str) -> str | None:
    if _HEX64.fullmatch(text):
        return text.lower()
    if text.lower().startswith("npub1") and nostr_pubkey(text) is not None:
        return text.lower()
    return None


# Platforms whose ids contain ``:`` split their own way.

_MATRIX_FORMS = "!room:server, #alias:server or @user:server, then :$event for a thread"
_MATRIX_ID = re.compile(r"[!#@][^\s,:\x00-\x1f\x7f]{1,255}:[A-Za-z0-9.-]{1,255}(?::[0-9]{1,5})?")
_MATRIX_LOCAL_NAME = re.compile(r"#[^\s,:\x00-\x1f\x7f]{1,200}")
_MATRIX_EVENT = re.compile(r"\$[^\s,:\x00-\x1f\x7f]{1,255}(?::[A-Za-z0-9.-]{1,255})?")


def _matrix_split(address: str) -> Address:
    text = _clean(address)
    cut = text.rfind(":$")
    room, thread = (text[:cut], text[cut + 1 :]) if cut > 0 else (text, None)
    if thread is not None and not _MATRIX_EVENT.fullmatch(thread):
        raise _refuse("matrix", _MATRIX_FORMS, text)
    if _MATRIX_ID.fullmatch(room) or _MATRIX_LOCAL_NAME.fullmatch(room):
        return room, thread
    raise _refuse("matrix", _MATRIX_FORMS, text)


_TEAMS_FORMS = (
    "a conversation id such as 19:…@thread.tacv2 or a:…, then ;messageid=<id> for a thread"
)
_TEAMS_BASE = re.compile(r"[0-9a-z]{1,3}:[A-Za-z0-9:@._-]{1,396}")
_TEAMS_THREAD = re.compile(r"[0-9]{1,32}")
_TEAMS_MARK = ";messageid="


def _teams_split(address: str) -> Address:
    text = _clean(address)
    named = _directory(text, _thread(_TEAMS_THREAD))
    if named is not None:
        return named
    base, mark, thread = text.partition(_TEAMS_MARK)
    if not mark:
        head, sep, tail = text.rpartition(":")
        # ``base:<digits>`` is a thread only when what is left is still a conversation id.
        if sep and _TEAMS_THREAD.fullmatch(tail) and ":" in head and _TEAMS_BASE.fullmatch(head):
            base, thread = head, tail
        else:
            base, thread = text, ""
    if not _TEAMS_BASE.fullmatch(base) or (thread and not _TEAMS_THREAD.fullmatch(thread)):
        raise _refuse("teams", _TEAMS_FORMS, text)
    if mark and not thread:
        raise _refuse("teams", _TEAMS_FORMS, text)
    return base, thread or None


_GOOGLE_FORMS = "spaces/<id>, then :<thread id> or :spaces/<id>/threads/<id>"
_GOOGLE_SPACE = re.compile(r"spaces/[A-Za-z0-9_-]{1,128}")
_GOOGLE_THREAD = re.compile(r"(spaces/[A-Za-z0-9_-]{1,128})/threads/([A-Za-z0-9_.-]{1,256})")
_GOOGLE_THREAD_ID = re.compile(r"[A-Za-z0-9_.-]{1,256}")


def _google_chat_split(address: str) -> Address:
    text = _clean(address)
    named = _directory(text, _thread(_GOOGLE_THREAD_ID))
    if named is not None:
        return named
    head, tail = _halves(text)
    whole = _GOOGLE_THREAD.fullmatch(head)
    if whole is not None and tail is None:
        return whole.group(1), head
    if not _GOOGLE_SPACE.fullmatch(head):
        raise _refuse("google_chat", _GOOGLE_FORMS, text)
    if tail is None:
        return head, None
    full = _GOOGLE_THREAD.fullmatch(tail)
    if full is not None:
        if full.group(1) != head:
            raise TargetError(f"{text!r}: the thread belongs to another space")
        return head, tail
    if _GOOGLE_THREAD_ID.fullmatch(tail):
        return head, f"{head}/threads/{tail}"
    raise _refuse("google_chat", _GOOGLE_FORMS, text)


_QQ_FORMS = "a user openid, group:<openid>, channel:<id> or dm:<guild id>"
_QQ_PREFIXES: Final = ("group", "channel", "dm", "c2c")


def _qqbot_split(address: str) -> Address:
    text = _clean(address)
    named = _directory(text, None)
    if named is not None:
        return named
    head, sep, rest = text.partition(":")
    if not sep:
        if _QQ_ID.fullmatch(text):
            return text, None
        raise _refuse("qqbot", _QQ_FORMS, text)
    if head not in _QQ_PREFIXES or not _QQ_ID.fullmatch(rest):
        raise _refuse("qqbot", _QQ_FORMS, text)
    return (rest if head == "c2c" else f"{head}:{rest}"), None


_DINGTALK_FORMS = "an open conversation id (cid…) or user:<staff id>"


def _dingtalk_split(address: str) -> Address:
    text = _clean(address)
    named = _directory(text, None)
    if named is not None:
        return named
    head, sep, rest = text.partition(":")
    if not sep:
        if _DINGTALK_CONVERSATION.fullmatch(text):
            return text, None
    elif head == "user" and _DINGTALK_STAFF.fullmatch(rest):
        return text, None
    raise _refuse("dingtalk", _DINGTALK_FORMS, text)


_SIMPLEX_FORMS = "a contact id (digits) or group:<digits>"


def _simplex_split(address: str) -> Address:
    text = _clean(address)
    named = _directory(text, None)
    if named is not None:
        return named
    head, sep, rest = text.partition(":")
    if not sep and _DIGITS.fullmatch(text):
        return text, None
    if sep and head == "group" and _DIGITS.fullmatch(rest):
        return text, None
    raise _refuse("simplex", _SIMPLEX_FORMS, text)


_FEISHU_FORMS = "oc_/ou_/on_ ids with an optional :<thread id>, or cmt_<type>_<token>:<comment id>"


def _feishu_split(address: str) -> Address:
    text = _clean(address)
    named = _directory(text, _thread(_FEISHU_THREAD))
    if named is not None:
        return named
    head, tail = _halves(text)
    if _FEISHU_COMMENT.fullmatch(head):
        if tail is None or _FEISHU_COMMENT_THREAD.fullmatch(tail):
            return head, tail
    elif _FEISHU_CHAT.fullmatch(head) and (tail is None or _FEISHU_THREAD.fullmatch(tail)):
        return head, tail
    raise _refuse("feishu", _FEISHU_FORMS, text)


_MATTERMOST_FORMS = "a channel id (26 characters) or a channel name, then :<root post id>"


def _mattermost_split(address: str) -> Address:
    text = _clean(address)
    named = _directory(text, _thread(_MATTERMOST_ID))
    if named is not None:
        return named
    head, tail = _halves(text)
    if tail is not None and not _MATTERMOST_ID.fullmatch(tail):
        raise _refuse("mattermost", _MATTERMOST_FORMS, text)
    if _MATTERMOST_ID.fullmatch(head):
        return head, tail
    if _MATTERMOST_NAME.fullmatch(head):
        return f"#{head}", tail  # a name: the router looks it up in the channel directory
    raise _refuse("mattermost", _MATTERMOST_FORMS, text)


_BUZZ_FORMS = "a channel UUID (then :<event id> for a thread), or a member's hex pubkey or npub"


def _buzz_split(address: str) -> Address:
    text = _clean(address)
    named = _directory(text, _thread(_HEX64))
    if named is not None:
        return named
    head, tail = _halves(text)
    if _UUID.fullmatch(head):
        if tail is None or _HEX64.fullmatch(tail):
            return head.lower(), tail.lower() if tail else None
    elif tail is None:
        dm = _buzz_dm(head)
        if dm is not None:
            return dm, None
    raise _refuse("buzz", _BUZZ_FORMS, text)


def _build() -> dict[str, TargetRule]:
    rules = [
        _rule(
            "telegram",
            "a chat id (negative for groups) or @channelname, then :<topic id>",
            ("telegram:-1001234567890", "telegram:-1001234567890:42", "telegram:@newsroom"),
            chat=_matching(_TELEGRAM_CHAT),
            thread=_thread(re.compile(r"[0-9]{1,12}")),
        ),
        _rule(
            "discord",
            "a channel id, then :<thread id>",
            ("discord:123456789012345678", "discord:123456789012345678:223456789012345678"),
            chat=_matching(_SNOWFLAKE),
            thread=_thread(_SNOWFLAKE),
        ),
        _rule(
            "slack",
            "a C…/G…/D… conversation or U… user id, then :<message ts>",
            ("slack:C0123456789", "slack:C0123456789:1700000000.000100", "slack:U0123456789"),
            chat=_slack_chat,
            thread=_thread(_SLACK_TS),
        ),
        _rule(
            "signal",
            "a +number, an account UUID or group.<id>",
            ("signal:+15551234567", "signal:group.Q2hhdEdyb3VwSWQ="),
            chat=_signal_chat,
        ),
        TargetRule(
            "matrix",
            _matrix_split,
            _MATRIX_FORMS,
            ("matrix:!ops:example.org", "matrix:#ops:example.org", "matrix:!ops:example.org:$root"),
            hash_native=True,
        ),
        TargetRule(
            "mattermost",
            _mattermost_split,
            _MATTERMOST_FORMS,
            ("mattermost:town-square", "mattermost:" + "a" * 26),
        ),
        _rule(
            "email",
            "one email address",
            ("email:ops@example.com",),
            chat=_email_chat,
        ),
        _rule(
            "whatsapp_cloud",
            "the customer's phone number with its country code",
            ("whatsapp_cloud:15551234567",),
            chat=_whatsapp_chat,
        ),
        _rule(
            "sms",
            "a phone number in E.164 form (+ and country code)",
            ("sms:+15551234567",),
            chat=_matching(_E164),
        ),
        _rule(
            "line",
            "a U…, C… or R… id (32 hex characters after the letter)",
            ("line:U" + "0" * 32,),
            chat=_matching(_LINE_CHAT),
        ),
        _rule(
            "ntfy",
            "a topic name (letters, digits, - and _)",
            ("ntfy:alerts",),
            chat=_matching(_NTFY_TOPIC),
        ),
        _rule(
            "irc",
            "#channel or a nick",
            ("irc:#ops", "irc:alice"),
            chat=_irc_chat,
            hash_native=True,
        ),
        TargetRule(
            "teams",
            _teams_split,
            _TEAMS_FORMS,
            ("teams:19:abc@thread.tacv2", "teams:19:abc@thread.tacv2;messageid=1700000000000"),
        ),
        TargetRule(
            "google_chat",
            _google_chat_split,
            _GOOGLE_FORMS,
            ("google_chat:spaces/AAAA", "google_chat:spaces/AAAA/threads/BBBB"),
        ),
        TargetRule("qqbot", _qqbot_split, _QQ_FORMS, ("qqbot:group:ABCDEF", "qqbot:ABCDEF")),
        TargetRule(
            "dingtalk",
            _dingtalk_split,
            _DINGTALK_FORMS,
            ("dingtalk:cidAbCdEf12==", "dingtalk:user:manager1"),
        ),
        _rule("wecom", "a user id or a group chat id", ("wecom:zhangsan",), chat=_wecom_chat),
        _rule(
            "wecom_callback", "a member's user id", ("wecom_callback:zhangsan",), chat=_wecom_chat
        ),
        TargetRule(
            "feishu", _feishu_split, _FEISHU_FORMS, ("feishu:oc_abc123", "feishu:oc_abc123:omt_1")
        ),
        _rule(
            "bluebubbles",
            "a chat GUID such as iMessage;-;+15551234567, a phone number or an email address",
            ("bluebubbles:iMessage;-;+15551234567", "bluebubbles:+15551234567"),
            chat=_bluebubbles_chat,
        ),
        TargetRule("simplex", _simplex_split, _SIMPLEX_FORMS, ("simplex:12", "simplex:group:3")),
        TargetRule(
            "buzz",
            _buzz_split,
            _BUZZ_FORMS,
            ("buzz:123e4567-e89b-12d3-a456-426614174000",),
        ),
    ]
    return {rule.platform: rule for rule in rules}


RULES: Final[Mapping[str, TargetRule]] = MappingProxyType(_build())
"""Platform name → its target rule (every built-in platform people chat on)."""


# -- the public calls -----------------------------------------------------------------------------


def rule_for(platform: str) -> TargetRule | None:
    return RULES.get(platform.strip().lower())


def hash_is_native(platform: str) -> bool:
    """Whether a ``#…`` chat is a real id on ``platform`` (IRC channels, Matrix aliases)."""
    rule = rule_for(platform)
    return rule is not None and rule.hash_native


def parse_target(platform: str, address: str) -> Address:
    """``address`` (the part after ``platform:``) → ``(chat, thread)``; ``TargetError`` when it does
    not fit. A platform without a rule gets P14's plain split."""
    rule = rule_for(platform)
    if rule is None:
        text = _clean(address)
        return _halves(text)
    return rule.split(address)


def render_target(platform: str, chat_id: str, thread_id: str | None = None) -> str:
    """The canonical token (the same text as P14's ``DeliveryTarget.render``)."""
    name = platform.strip().lower()
    return f"{name}:{chat_id}" if not thread_id else f"{name}:{chat_id}:{thread_id}"


def validate_target(platform: str, chat_id: str, thread_id: str | None = None) -> str | None:
    """A problem with already split values (a home channel, a stored origin), or None."""
    if not str(chat_id or "").strip():
        return "no chat id"
    address = render_target(platform, chat_id, thread_id).partition(":")[2]
    try:
        chat, thread = parse_target(platform, address)
    except TargetError as exc:
        return str(exc)
    if chat != chat_id.strip() or (thread or None) != (thread_id or None):
        return (
            f"{render_target(platform, chat_id, thread_id)!r} reads as "
            f"{render_target(platform, chat, thread)!r}"
        )
    return None


_install_lock = threading.Lock()
_undo: list[Callable[[], None]] = []


def install() -> Callable[[], None]:
    """Register every rule with P14's target grammar (once; returns the undo)."""
    from indus.cron import targets as cron_targets

    with _install_lock:
        if not _undo:
            for rule in RULES.values():
                _undo.append(cron_targets.register_platform(rule.platform, splitter=rule.split))

    return uninstall


def uninstall() -> None:
    """Remove every rule again (the undo of ``install``; tests reset the process with it)."""
    with _install_lock:
        while _undo:
            _undo.pop()()
