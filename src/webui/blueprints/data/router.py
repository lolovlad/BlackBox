from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, render_template, request, url_for
from flask_login import login_required

from src.webui.data_labels import (
    all_analog_keys,
    all_discrete_keys,
    analog_labels_for,
    discrete_labels_for,
    filter_valid_analog,
    filter_valid_discrete,
)
from src.webui.modbus_service import analog_discrete_for_csv, decode_to_processed
from src.webui.paths import TEMPLATES_DIR
from src.webui.repositories.data_repository import DataRepository
from src.webui.services.data_service import TABLE_PAGE_SIZE, DataService, parse_data_filter

data_router = Blueprint("data", __name__, url_prefix="/data", template_folder=str(TEMPLATES_DIR))
DATETIME_UI_FORMAT = "%d.%m.%Y %H:%M:%S"


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


@data_router.route("/charts/render", methods=["GET"])
@login_required
def charts_render():
    table = (request.args.get("table") or "analog").strip().lower()
    if table not in ("analog", "discrete"):
        table = "analog"

    date_from = _parse_dt_local(request.args.get("date_from"))
    date_to = _parse_dt_local(request.args.get("date_to"))
    if date_from is not None and date_to is not None and date_from > date_to:
        date_from, date_to = date_to, date_from

    repo = DataRepository(current_app.extensions["session_factory"])
    rows = (
        repo.list_analogs(created_from=date_from, created_to=date_to, sort_desc=False, limit=1000)
        if table == "analog"
        else repo.list_discretes(created_from=date_from, created_to=date_to, sort_desc=False, limit=1000)
    )

    requested_cols = request.args.getlist("analog_col") if table == "analog" else request.args.getlist("discrete_col")
    columns = (
        filter_valid_analog(requested_cols if requested_cols else None)
        if table == "analog"
        else filter_valid_discrete(requested_cols if requested_cols else None)
    )

    x_axis = [item.created_at.strftime(DATETIME_UI_FORMAT) for item in rows]
    series: list[dict[str, Any]] = [{"name": col, "type": "line", "showSymbol": False, "data": []} for col in columns]

    for item in rows:
        processed = decode_to_processed(item.date)
        analog, discrete = analog_discrete_for_csv(processed)
        for idx, col in enumerate(columns):
            if table == "analog":
                value = analog.get(col, None)
            else:
                value = 1 if bool(discrete.get(col, False)) else 0
            series[idx]["data"].append(value)

    return render_template(
        "data/_chart_result.html",
        table=table,
        x_axis=x_axis,
        series=series,
        row_count=len(rows),
        has_columns=bool(columns),
    )
