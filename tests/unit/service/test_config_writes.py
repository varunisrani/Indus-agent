"""W50-a: ``config.set``'s checks and profile writes, shared by the bridge and the daemon."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from indus.foundation.config import loader
from indus.foundation.scope import Scope
from indus.protocol.bridge import ConfigKey
from indus.protocol.errors import CONFIRM_REQUIRED, INVALID_PARAMS
from indus.protocol.methods import ProtocolError
from indus.service import config_writes


@pytest.mark.parametrize(
    ("value", "mode"),
    [("OFF", "off"), (" new ", "new"), ("all", "all"), (True, "verbose"), (False, "all")],
)
def test_tool_modes(value: Any, mode: str) -> None:
    assert config_writes.tool_mode(value) == mode


@pytest.mark.parametrize(
    ("check", "value"),
    [
        (config_writes.tool_mode, "loud"),
        (config_writes.yolo_value, "on"),
        (config_writes.fast_value, "warp"),
        (config_writes.reasoning_value, " "),
        (config_writes.reasoning_value, 3),
        (config_writes.model_value, ""),
    ],
)
def test_bad_values_name_the_value(check: Any, value: Any) -> None:
    with pytest.raises(ProtocolError) as refused:
        check(value)
    assert refused.value.error == INVALID_PARAMS and refused.value.path == "value"


def test_a_session_switch_without_a_session_names_the_session() -> None:
    refused = config_writes.needs_session(ConfigKey.FAST)
    assert refused.path == "session_id" and refused.message == "fast needs a session"


def test_profile_keys_are_checked_and_saved(scope: Scope) -> None:
    async def main() -> None:
        busy = await config_writes.save_setting(scope, ConfigKey.BUSY, " Steer ")
        mouse = await config_writes.save_setting(scope, ConfigKey.MOUSE, True)
        mode = await config_writes.save_tool_mode(scope, True)
        assert (busy.value, busy.saved) == ("steer", True)
        assert mouse.value is True and mode.value == "verbose"
        with pytest.raises(ProtocolError):
            await config_writes.save_setting(scope, ConfigKey.DETAILS, "open")

    asyncio.run(main())
    view = loader.load(scope)
    assert view.get("display.busy_input_mode") == "steer"
    assert view.get("tui.mouse") is True and view.get("tui.tool_progress") == "verbose"
    assert view.get("tui.details") != "open"


def test_flagged_models_need_confirming(scope: Scope) -> None:
    (scope.home / "config.yaml").write_text(
        "tui:\n  confirm_models:\n    - big-model\n", encoding="utf-8"
    )
    for wanted in ("big-model", "lab:big-model"):
        with pytest.raises(ProtocolError) as asked:
            config_writes.check_confirm(scope, wanted, False)
        assert asked.value.error == CONFIRM_REQUIRED and asked.value.path == "confirm"
        config_writes.check_confirm(scope, wanted, True)
    config_writes.check_confirm(scope, "small-model", False)
