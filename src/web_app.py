from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.middleware.wsgi import WSGIMiddleware

from src.webui.app import create_app
from src.webui.blueprints.main.router import render_live_dashboard_html
from src.webui.data_labels import all_analog_keys, all_discrete_keys

flask_app = create_app()
# Keep Flask CLI compatibility: FLASK_APP=src.web_app:app
app = flask_app

# ASGI app for uvicorn + native WebSocket endpoint.
asgi_app = FastAPI()
asgi_app.mount("/", WSGIMiddleware(flask_app))


def _normalized_columns(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    analog_allowed = set(all_analog_keys())
    discrete_allowed = set(all_discrete_keys())
    analog = [str(v) for v in (payload.get("analog_col") or []) if str(v) in analog_allowed]
    discrete = [str(v) for v in (payload.get("discrete_col") or []) if str(v) in discrete_allowed]
    if not analog:
        analog = list(all_analog_keys())
    if not discrete:
        discrete = list(all_discrete_keys())
    return analog, discrete


@asgi_app.websocket("/ws/dashboard")
async def ws_dashboard(websocket: WebSocket):
    await websocket.accept()
    analog_cols = list(all_analog_keys())
    discrete_cols = list(all_discrete_keys())
    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=2.0)
                payload = json.loads(raw)
                if isinstance(payload, dict) and payload.get("type") == "subscribe":
                    analog_cols, discrete_cols = _normalized_columns(payload)
            except TimeoutError:
                pass
            except json.JSONDecodeError:
                pass

            with flask_app.app_context():
                html = render_live_dashboard_html(analog_cols, discrete_cols)
            await websocket.send_json({"type": "dashboard_live_update", "html": html})
    except WebSocketDisconnect:
        return


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    import uvicorn

    uvicorn.run("src.web_app:asgi_app", host=host, port=port, log_level="debug")
