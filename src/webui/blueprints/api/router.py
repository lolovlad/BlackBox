from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
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
_DEFAULT_VIDEO_MATCH_WINDOW_MINUTES = 20


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


def _normalize_video_source_path(raw_path: str) -> str:
    value = str(raw_path).strip()
    if value.lower().startswith("file="):
        value = value.split("=", 1)[1].strip()
    if value.lower().startswith("file://"):
        value = value[7:].strip()
    return value


def _active_parser_settings_path() -> Path:
    raw = current_app.config.get("PARSER_SETTINGS_PATH", "settings/settings.json")
    p = Path(str(raw))
    project_root = Path(current_app.config["PROJECT_ROOT"])
    return p.resolve() if p.is_absolute() else (project_root / p).resolve()


def _video_match_window_delta() -> timedelta:
    path = _active_parser_settings_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return timedelta(minutes=_DEFAULT_VIDEO_MATCH_WINDOW_MINUTES)
    raw = payload.get("video_match_window_minutes", _DEFAULT_VIDEO_MATCH_WINDOW_MINUTES)
    try:
        minutes = int(raw)
    except Exception:
        minutes = _DEFAULT_VIDEO_MATCH_WINDOW_MINUTES
    if minutes < 1:
        minutes = 1
    if minutes > 24 * 60:
        minutes = 24 * 60
    return timedelta(minutes=minutes)


def _is_active_state(value: str | None) -> bool:
    return (value or "").strip().lower() == "active"


def _is_inactive_state(value: str | None) -> bool:
    return (value or "").strip().lower() == "inactive"


def _nearest_alarm_for_video(session, captured_at: datetime) -> Alarms | None:
    before = session.execute(
        select(Alarms).where(Alarms.created_at <= captured_at).order_by(Alarms.created_at.desc(), Alarms.id.desc()).limit(1)
    ).scalar_one_or_none()
    after = session.execute(
        select(Alarms).where(Alarms.created_at >= captured_at).order_by(Alarms.created_at.asc(), Alarms.id.asc()).limit(1)
    ).scalar_one_or_none()
    if before is None:
        return after
    if after is None:
        return before
    return before if (captured_at - before.created_at) <= (after.created_at - captured_at) else after


def _next_inactive_after(session, alarm: Alarms) -> Alarms | None:
    stmt = select(Alarms).where(Alarms.created_at > alarm.created_at, Alarms.state == "inactive")
    if getattr(alarm, "name", None):
        stmt = stmt.where(Alarms.name == alarm.name)
    return session.execute(stmt.order_by(Alarms.created_at.asc(), Alarms.id.asc()).limit(1)).scalar_one_or_none()


def _prev_active_before(session, alarm: Alarms) -> Alarms | None:
    stmt = select(Alarms).where(Alarms.created_at < alarm.created_at, Alarms.state == "active")
    if getattr(alarm, "name", None):
        stmt = stmt.where(Alarms.name == alarm.name)
    return session.execute(stmt.order_by(Alarms.created_at.desc(), Alarms.id.desc()).limit(1)).scalar_one_or_none()


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

    normalized_path = _normalize_video_source_path(raw_path)
    path = Path(normalized_path).expanduser()
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
    active_window = _video_match_window_delta()
    with session_factory() as session:
        existing = session.execute(select(Video).where(Video.file_path == str(path))).scalar_one_or_none()
        if existing is not None:
            return jsonify({"ok": True, "video_id": existing.id, "status": "already_exists"})

        nearest = _nearest_alarm_for_video(session, captured_at)
        if nearest is None:
            current_app.logger.warning(
                "video/add 404: в БД нет событий alarm для сопоставления. captured_at=%s file_path=%r",
                captured_at.isoformat(),
                str(path),
            )
            return jsonify({"ok": False, "error": "Видео не попадает в интервал alarm."}), 404

        matched_alarm: Alarms | None = None
        reason = ""
        if _is_active_state(nearest.state):
            if abs(captured_at - nearest.created_at) <= active_window:
                matched_alarm = nearest
                reason = "nearest_active_within_window"
            else:
                nearest_inactive = _next_inactive_after(session, nearest)
                if (
                    nearest_inactive is not None
                    and nearest.created_at <= captured_at <= nearest_inactive.created_at
                ):
                    matched_alarm = nearest
                    reason = "between_active_and_next_inactive"
        elif _is_inactive_state(nearest.state):
            prev_active = _prev_active_before(session, nearest)
            if prev_active is not None and prev_active.created_at <= captured_at <= nearest.created_at:
                matched_alarm = prev_active
                reason = "between_prev_active_and_nearest_inactive"
        if matched_alarm is None:
            current_app.logger.warning(
                "video/add 404: нет подходящего alarm по новой логике. captured_at=%s nearest_alarm_id=%s "
                "nearest_created_at=%s nearest_state=%r file_path=%r",
                captured_at.isoformat(),
                nearest.id,
                nearest.created_at.isoformat() if nearest.created_at else None,
                nearest.state,
                str(path),
            )
            return jsonify({"ok": False, "error": "Видео не попадает в допустимое окно active/inactive."}), 404

        row = Video(
            captured_at=captured_at,
            file_name=file_name,
            file_path=str(path),
            alarm_id=matched_alarm.id,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return (
            jsonify(
            {
                "ok": True,
                "video_id": row.id,
                "alarm_id": matched_alarm.id,
                "alarm_name": matched_alarm.name,
                "match_reason": reason,
                "captured_at": captured_at.isoformat(),
                "file_name": file_name,
            }
            ),
            201,
        )
