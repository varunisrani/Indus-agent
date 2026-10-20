"""The dashboard's REST and WebSocket route modules.

Each module exposes ``router`` (a FastAPI ``APIRouter``); ``indus.web.app.create_app`` includes
the routers listed in ``ROUTERS`` in order. P18-11 owns this registration list; each later route
task adds one line.
"""

from __future__ import annotations

ROUTERS: tuple[str, ...] = (
    "indus.web.routes.auth:router",
    "indus.web.routes.ws_ticket:router",
)
"""``"module:attr"`` of every router ``create_app`` includes, in order."""
