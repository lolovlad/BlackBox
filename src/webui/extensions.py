from __future__ import annotations

from flask_login import LoginManager
from flask_socketio import SocketIO
from flask_session import Session
from flask_wtf import CSRFProtect

csrf = CSRFProtect()
server_session = Session()
login_manager = LoginManager()
socketio = SocketIO(async_mode="threading", cors_allowed_origins="*")
