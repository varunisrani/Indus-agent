"""The sample plugin's backend: mounted under /api/ext/hello/."""

from fastapi import APIRouter
from starlette.websockets import WebSocket

from indus.service.ws_auth import WEB_PLUGIN
from indus.web.ws import WsGrant, guarded_ws

router = APIRouter()


@router.get("/ping")
async def ping() -> dict[str, str]:
    return {"pong": "hello"}


@guarded_ws(WEB_PLUGIN)
async def live(websocket: WebSocket, grant: WsGrant) -> None:
    await websocket.send_text(f"hello {grant.credential}")
    await websocket.close(code=1000)


router.add_api_websocket_route("/live", live)
