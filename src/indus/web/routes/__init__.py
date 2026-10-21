"""The dashboard's REST and WebSocket route modules.

Each module exposes ``router`` (a FastAPI ``APIRouter``); ``indus.web.app.create_app`` includes
the routers listed in ``ROUTERS`` in order. P18-11 owns this registration list; each later route
task adds one line.
"""

from __future__ import annotations

ROUTERS: tuple[str, ...] = (
    "indus.web.routes.auth:router",
    "indus.web.routes.ws_ticket:router",
    "indus.web.routes.sessions:router",
    "indus.web.routes.config_env:router",
    "indus.web.routes.status:router",
    "indus.web.routes.logs:router",
    "indus.web.routes.analytics:router",
    "indus.web.routes.cron:router",
    "indus.web.routes.skills:router",
    "indus.web.routes.plugins:router",
    "indus.web.routes.mcp:router",
    "indus.web.routes.messaging:router",
    "indus.web.routes.webhooks:router",
    "indus.web.routes.daemon:router",
    "indus.web.routes.profiles:router",
    "indus.web.routes.models:router",
    "indus.web.routes.files:router",
    "indus.web.routes.system:router",
    "indus.web.routes.session_import:router",
    "indus.web.plugins:router",  # P24-37 dashboard plugins (list, rescan, assets)
)
"""``"module:attr"`` of every router ``create_app`` includes, in order."""
