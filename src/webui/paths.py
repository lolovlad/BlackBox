from __future__ import annotations

from pathlib import Path

_WEBUI_DIR = Path(__file__).resolve().parent
SRC_DIR = _WEBUI_DIR.parent
TEMPLATES_DIR = SRC_DIR / "templates"
STATIC_DIR = SRC_DIR / "static"
