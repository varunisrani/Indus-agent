"""Desktop control (toolset ``computer_use``, P10-20): look at and operate desktop apps.

``driver`` holds the ``DesktopDriver`` contract, the driver registry plugins add to
(``register_driver``, P13) and the built-in ``stdio`` driver, an external program started from
``computer_use.driver_cmd`` / ``INDUS_CUA_DRIVER_CMD``. ``tool`` holds the model tool. The tool is
offered only where the selected driver reports ready; ``indus doctor`` explains when it is not.
"""

from __future__ import annotations

from indus.tools.registry import ToolRegistry

__all__ = ["register_tools"]


def register_tools(registry: ToolRegistry) -> None:
    from indus.tools.builtin.computer_use import tool

    tool.register(registry)
