from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, jsonify, render_template, request, url_for
from flask_login import login_required

from src.webui.data_labels import (
    all_analog_keys,
    all_discrete_keys,
    analog_labels_for,
    discrete_labels_for,
)
from src.webui.modbus_service import analog_discrete_for_csv, decode_to_processed
from src.webui.paths import TEMPLATES_DIR
from src.webui.repositories.data_repository import DataRepository
from src.webui.services.data_service import TABLE_PAGE_SIZE, DataService, parse_data_filter

data_router = Blueprint("data", __name__, url_prefix="/data", template_folder=str(TEMPLATES_DIR))
DATETIME_UI_FORMAT = "%d.%m.%Y %H:%M:%S"
DATETIME_API_FORMAT = "%Y-%m-%dT%H:%M:%S"


def _service() -> DataService:
    session_factory = current_app.extensions["session_factory"]
    collector = current_app.extensions["modbus_collector"]
    repo = DataRepository(session_factory)
    return DataService(repo, alarms_enabled=collector._alarms_enabled)


@data_router.route("/", methods=["GET"])
@login_required
def page():
    analog_opts = analog_labels_for(all_analog_keys())
    discrete_opts = discrete_labels_for(all_discrete_keys())
    return render_template(
        "data/index.html",
        analog_options=analog_opts,
        discrete_options=discrete_opts,
        table_page_size=TABLE_PAGE_SIZE,
    )


@data_router.route("/tables", methods=["GET"])
@login_required
def tables():
    flt = parse_data_filter(request.args)
    ctx = _service().collect_tab(flt)
    return render_template("data/_tables.html", **ctx)


@data_router.route("/export", methods=["POST"])
@login_required
def export_submit():
    flt = parse_data_filter(request.form)
    static_csv_dir: Path = current_app.extensions["static_csv_dir"]
    svc = _service()
    try:
        export_path = svc.build_export(flt, static_csv_dir)
    except ValueError as exc:
        if str(exc) == "alarms_disabled":
            return render_template(
                "data/_export_result.html",
                error="Таблица аварий недоступна (нет таблицы в БД). Выполните миграции.",
                download_url=None,
            )
        raise
    rel = export_path.relative_to(current_app.static_folder)
    download_url = url_for("static", filename=str(rel).replace("\\", "/"))
    return render_template("data/_export_result.html", error=None, download_url=download_url)


def _parse_dt_local(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    val = str(raw).strip()
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except ValueError:
        return None


@data_router.route("/charts", methods=["GET"])
@login_required
def charts_page():
    analog_opts = analog_labels_for(all_analog_keys())
    discrete_opts = discrete_labels_for(all_discrete_keys())
    return render_template(
        "data/charts.html",
        analog_options=analog_opts,
        discrete_options=discrete_opts,
    )


def _requested_chart_columns(table: str) -> list[str]:
    requested = request.args.getlist("analog_col") if table == "analog" else request.args.getlist("discrete_col")
    if table == "analog":
        allowed = set(all_analog_keys())
    else:
        allowed = set(all_discrete_keys())
    return [col for col in requested if col in allowed]


def _collect_second_points(rows: list[Any], table: str, columns: list[str]) -> list[dict[str, Any]]:
    by_second: dict[datetime, dict[str, Any]] = {}
    for item in rows:
        sec = item.created_at.replace(microsecond=0)
        processed = decode_to_processed(item.date)
        analog, discrete = analog_discrete_for_csv(processed)
        values: dict[str, Any] = {}
        for col in columns:
            if table == "analog":
                values[col] = analog.get(col, None)
            else:
                values[col] = 1 if bool(discrete.get(col, False)) else 0
        # Если в одну секунду несколько замеров, берем последний
        by_second[sec] = values

    points: list[dict[str, Any]] = []
    for sec in sorted(by_second.keys()):
        points.append(
            {
                "ts": sec.strftime(DATETIME_API_FORMAT),
                "label": sec.strftime(DATETIME_UI_FORMAT),
                "values": by_second[sec],
            }
        )
    return points


def _parse_since(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    val = str(raw).strip()
    if not val:
        return None
    for fmt in (DATETIME_API_FORMAT, DATETIME_UI_FORMAT):
        try:
            return datetime.strptime(val, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(val)
    except ValueError:
        return None


@data_router.route("/charts/api/init", methods=["GET"])
@login_required
def charts_api_init():
    table = (request.args.get("table") or "analog").strip().lower()
    if table not in ("analog", "discrete"):
        table = "analog"

    date_from = _parse_dt_local(request.args.get("date_from"))
    date_to = _parse_dt_local(request.args.get("date_to"))
    if date_from is not None and date_to is not None and date_from > date_to:
        date_from, date_to = date_to, date_from

    realtime = date_from is None and date_to is None
    if realtime:
        now = datetime.now()
        date_from = now.replace(hour=0, minute=0, second=0, microsecond=0)
        date_to = now

    repo = DataRepository(current_app.extensions["session_factory"])
    rows = (
        repo.list_analogs(created_from=date_from, created_to=date_to, sort_desc=False, limit=None)
        if table == "analog"
        else repo.list_discretes(created_from=date_from, created_to=date_to, sort_desc=False, limit=None)
    )

    columns = _requested_chart_columns(table)
    points = _collect_second_points(rows, table, columns)
    return jsonify(
        {
            "table": table,
            "columns": columns,
            "points": points,
            "last_ts": points[-1]["ts"] if points else None,
            "row_count": len(points),
            "realtime": realtime,
        }
    )


@data_router.route("/charts/api/update", methods=["GET"])
@login_required
def charts_api_update():
    table = (request.args.get("table") or "analog").strip().lower()
    if table not in ("analog", "discrete"):
        table = "analog"
    columns = _requested_chart_columns(table)
    since = _parse_since(request.args.get("since"))

    now = datetime.now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    created_from = since if since is not None else day_start
    if created_from < day_start:
        created_from = day_start

    repo = DataRepository(current_app.extensions["session_factory"])
    rows = (
        repo.list_analogs(created_from=created_from, created_to=now, sort_desc=False, limit=None)
        if table == "analog"
        else repo.list_discretes(created_from=created_from, created_to=now, sort_desc=False, limit=None)
    )
    points = _collect_second_points(rows, table, columns)

    if since is not None:
        filtered: list[dict[str, Any]] = []
        for point in points:
            pt = _parse_since(point["ts"])
            if pt is not None and pt > since:
                filtered.append(point)
        points = filtered

    return jsonify(
        {
            "table": table,
            "columns": columns,
            "points": points,
            "last_ts": points[-1]["ts"] if points else (since.strftime(DATETIME_API_FORMAT) if since else None),
            "row_count": len(points),
            "realtime": True,
        }
    )
