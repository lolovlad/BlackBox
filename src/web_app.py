from __future__ import annotations

import os

from src.webui.app import create_app
from src.webui.extensions import socketio

app = create_app()


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    socketio.run(app, host=host, port=port, debug=True)
