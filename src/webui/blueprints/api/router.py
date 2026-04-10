from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import select

from src.database import Alarms, Video
from src.webui.extensions import csrf

api_router = Blueprint("api", __name__, url_prefix="/api")

_DATETIME_PATTERNS: tuple[str, ...] = (
    "%Y%m%d_%H%M%S",
    "%Y-%m-%d_%H-%M-%S",
    "%Y-%m-%d %H-%M-%S",
    "%Y-%m-%d_%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%d-%m-%Y_%H-%M-%S",
)


def _extract_video_datetime(file_name: str) -> datetime | None:
    stem = Path(file_name).stem
    chunks = re.findall(r"\(([^()]*)\)", file_name)
    candidates = chunks + [stem, file_name]
    for text in candidates:
        normalized = " ".join(str(text).replace("__", "_").split())
        for pattern in _DATETIME_PATTERNS:
            try:
                return datetime.strptime(normalized, pattern)
            except ValueError:
                continue
        match = re.search(r"(\d{4}-\d{2}-\d{2}[ _]\d{2}[-:]\d{2}[-:]\d{2})", normalized)
        if match:
            value = match.group(1).replace(" ", "_")
            for pattern in ("%Y-%m-%d_%H-%M-%S", "%Y-%m-%d_%H:%M:%S"):
                try:
                    return datetime.strptime(value, pattern)
                except ValueError:
                    continue
        compact_match = re.search(r"(\d{8}_\d{6})", normalized)
        if compact_match:
            try:
                return datetime.strptime(compact_match.group(1), "%Y%m%d_%H%M%S")
            except ValueError:
                pass
    return None


def _video_add_request_debug_preview(raw_body_full: str, max_len: int = 800) -> str:
    if len(raw_body_full) <= max_len:
        return raw_body_full
    return raw_body_full[:max_len] + f"... (+{len(raw_body_full) - max_len} симв.)"


@api_router.route("/video/add", methods=["POST"])
@csrf.exempt
def video_add():
    raw_body_full = request.get_data(cache=True, as_text=True) or ""
    payload = request.get_json(silent=True) or {}
    raw_path = (payload.get("path") or payload.get("video_path") or "").strip()
    if not raw_path:
        raw_path = (request.form.get("path") or request.form.get("video_path") or "").strip()
    if not raw_path:
        raw_path = raw_body_full.strip().strip('"').strip("'")
    if not raw_path:
        current_app.logger.warning(
            "video/add 400: нет path. content_type=%r json=%r form=%r raw_len=%s raw_preview=%r",
            request.content_type,
            payload,
            dict(request.form),
            len(raw_body_full),
            _video_add_request_debug_preview(raw_body_full),
        )
        return jsonify({"ok": False, "error": "Поле path обязательно."}), 400

    path = Path(raw_path).expanduser()
    file_name = path.name
    captured_at = _extract_video_datetime(file_name)
    if captured_at is None:
        current_app.logger.warning(
            "video/add 400: нет даты/времени в имени. content_type=%r json=%r form=%r "
            "raw_path=%r file_name=%r raw_len=%s raw_preview=%r",
            request.content_type,
            payload,
            dict(request.form),
            raw_path,
            file_name,
            len(raw_body_full),
            _video_add_request_debug_preview(raw_body_full),
        )
        return jsonify(
            {
                "ok": False,
                "error": "В имени файла не найдены дата и время.",
                "file_name": file_name,
                "expected_formats": [
                    "YYYYMMDD_HHMMSS",
                    "YYYY-MM-DD_HH-MM-SS",
                    "YYYY-MM-DD_HH:MM:SS",
                ],
            }
        ), 400

    session_factory = current_app.extensions["session_factory"]
    with session_factory() as session:
        existing = session.execute(select(Video).where(Video.file_path == str(path))).scalar_one_or_none()
        if existing is not None:
            return jsonify({"ok": True, "video_id": existing.id, "status": "already_exists"})

        nearest = session.execute(
            select(Alarms)
            .where(Alarms.created_at <= captured_at)
            .order_by(Alarms.created_at.desc(), Alarms.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if nearest is None:
            current_app.logger.warning(
                "video/add 404: нет alarm с created_at <= captured_at. captured_at=%s file_path=%r",
                captured_at.isoformat(),
                str(path),
            )
            return jsonify({"ok": False, "error": "Видео не попадает в интервал alarm."}), 404
        if (nearest.state or "").strip().lower() != "active":
            current_app.logger.warning(
                "video/add 404: ближайшая авария не active. captured_at=%s nearest_alarm_id=%s "
                "nearest_created_at=%s nearest_state=%r file_path=%r",
                captured_at.isoformat(),
                nearest.id,
                nearest.created_at.isoformat() if nearest.created_at else None,
                nearest.state,
                str(path),
            )
            return jsonify({"ok": False, "error": "Ближайшая авария имеет состояние inactive."}), 404

        row = Video(
            captured_at=captured_at,
            file_name=file_name,
            file_path=str(path),
            alarm_id=nearest.id,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return jsonify(
            {
                "ok": True,
                "video_id": row.id,
                "alarm_id": nearest.id,
                "alarm_name": nearest.name,
                "captured_at": captured_at.isoformat(),
                "file_name": file_name,
            }
        )
