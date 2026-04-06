from __future__ import annotations

from flask_session import Session
from flask_wtf import CSRFProtect

csrf = CSRFProtect()
server_session = Session()
