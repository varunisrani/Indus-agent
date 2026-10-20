"""Vendor-name drift check (P15-01).

Every vendor library name our adapters (``Adapter.vendor_names``) and the wire fakes
(``SURFACE``) rely on must exist on the pinned library objects, so a version bump that renames
one fails here instead of in production. A library that is not installed (CI installs no
optional extras) skips its rows; ``uv sync --all-extras`` runs them.

The wire fakes are also checked against their own declaration, which needs no library: a fake
that lacks a declared name would let an adapter test pass against a surface that does not exist.

Telegram has no library (P15-33): its rows point at our own Bot API client, which is always
installed, and the client's methods are checked against the Bot API's documented method names.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator, Mapping

import pytest

from tests.contract.gateway.conftest import CLOCK_START
from tests.contract.gateway.test_adapter_contract import CASES
from tests.fakes.clock import FakeClock
from tests.fakes.platform.discord_wire import DiscordWire
from tests.fakes.platform.email_wire import EmailWire
from tests.fakes.platform.slack_wire import SlackWire
from tests.fakes.platform.telegram_wire import TelegramWire

WIRES: dict[str, type[TelegramWire] | type[DiscordWire] | type[SlackWire] | type[EmailWire]] = {
    "telegram": TelegramWire,
    "discord": DiscordWire,
    "slack": SlackWire,
    "email": EmailWire,
}


def _adapter_surfaces() -> Iterator[tuple[str, Mapping[str, tuple[str, ...]]]]:
    seen: set[type] = set()
    for case in CASES:
        cls = type(case.build(case.make_env(FakeClock(CLOCK_START))))
        if cls not in seen:
            seen.add(cls)
            yield f"adapter:{case.name}", cls.vendor_names


def _rows() -> list[tuple[str, str, tuple[str, ...]]]:
    rows: list[tuple[str, str, tuple[str, ...]]] = []
    for wire, cls in WIRES.items():
        rows.extend((f"wire:{wire}", target, names) for target, names in cls.SURFACE.items())
    for owner, surface in _adapter_surfaces():
        rows.extend((owner, target, names) for target, names in surface.items())
    return rows


def _resolve(target: str) -> object:
    module_name, _, qualname = target.partition(":")
    top = module_name.split(".", 1)[0]
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name is not None and exc.name.split(".", 1)[0] == top:
            pytest.skip(f"{top} is not installed (optional extra)")
        raise
    obj: object = module
    for part in qualname.split("."):
        assert hasattr(obj, part), f"{target}: {part!r} is missing from the installed library"
        obj = getattr(obj, part)
    return obj


@pytest.mark.parametrize(("owner", "target", "names"), _rows())
def test_vendor_names_exist_in_the_pinned_library(
    owner: str, target: str, names: tuple[str, ...]
) -> None:
    obj = _resolve(target)
    missing = [name for name in names if not hasattr(obj, name)]
    assert not missing, f"{owner}: {target} has no {missing} in the installed version"


@pytest.mark.parametrize("wire", sorted(WIRES))
def test_wire_fakes_implement_their_declared_surface(wire: str) -> None:
    cls = WIRES[wire]
    assert set(cls.FAKES) == set(cls.SURFACE), f"{wire}: FAKES and SURFACE name other objects"
    for target, names in cls.SURFACE.items():
        fake = cls.FAKES[target]
        missing = [name for name in names if not hasattr(fake, name)]
        assert not missing, f"{wire}: the fake for {target} lacks {missing}"


def test_every_adapter_declares_vendor_names_for_a_wire() -> None:
    """Adapters backed by a wire fake must list what they call (the echo adapter calls none)."""
    for owner, surface in _adapter_surfaces():
        if owner == "adapter:echo":
            assert surface == {}
            continue
        assert surface, f"{owner} declares no vendor_names"


# -- Telegram: our own Bot API client (P15-33) -----------------------------------------------------

# Bot API methods as documented at core.telegram.org/bots/api (Bot API 9.x): the ones our client
# may call. An interop fact list, written down by hand; add a name only after checking the docs.
TELEGRAM_DOCUMENTED_METHODS = frozenset(
    {
        "getMe",
        "getUpdates",
        "setWebhook",
        "deleteWebhook",
        "getWebhookInfo",
        "sendMessage",
        "editMessageText",
        "editMessageReplyMarkup",
        "deleteMessage",
        "sendChatAction",
        "setMessageReaction",
        "sendPhoto",
        "sendDocument",
        "sendVoice",
        "sendAudio",
        "sendVideo",
        "sendMediaGroup",
        "setMyCommands",
        "deleteMyCommands",
        "getFile",
        "answerCallbackQuery",
        "getChat",
        "sendMessageDraft",  # P16-33 native drafts (private chats)
        "createForumTopic",  # P16-34 topics in private chats (Threaded Mode)
    }
)


def _snake(name: str) -> str:
    return "".join(f"_{c.lower()}" if c.isupper() else c for c in name)


def test_telegram_client_methods_are_documented_bot_api_methods() -> None:
    from indus.gateway.adapters.telegram.bot_api import BOT_API_METHODS, BotApi

    for ours, documented in BOT_API_METHODS.items():
        assert documented in TELEGRAM_DOCUMENTED_METHODS, f"{documented} is not a Bot API method"
        assert ours == _snake(documented), f"{ours} does not name {documented}"
        assert callable(getattr(BotApi, ours, None)), f"BotApi has no {ours}"


def test_the_telegram_fake_covers_every_client_method_the_adapter_calls() -> None:
    from indus.gateway.adapters.telegram.adapter import BOT_TARGET, TelegramAdapter
    from indus.gateway.adapters.telegram.bot_api import BOT_API_METHODS

    called = set(TelegramAdapter.vendor_names[BOT_TARGET])
    assert called <= set(TelegramWire.SURFACE[BOT_TARGET])
    # every documented method the client speaks is one the adapter uses (no dead client code)
    assert set(BOT_API_METHODS) <= called
