from __future__ import annotations

from pathlib import Path

from flask import Blueprint, current_app, render_template, request, url_for

from src.webui.auth_utils import admin_required
from src.webui.data_labels import all_analog_keys, all_discrete_keys, analog_labels_for, discrete_labels_for
from src.webui.paths import TEMPLATES_DIR
from src.webui.repositories.data_repository import DataRepository
from src.webui.services.data_service import DEFAULT_ROW_LIMIT, DataService, parse_data_filter

data_router = Blueprint("data", __name__, url_prefix="/data", template_folder=str(TEMPLATES_DIR))


def _service() -> DataService:
    session_factory = current_app.extensions["session_factory"]
    collector = current_app.extensions["modbus_collector"]
    repo = DataRepository(session_factory)
    return DataService(repo, alarms_enabled=collector._alarms_enabled)


@data_router.route("/", methods=["GET"])
@admin_required
def page():
    analog_opts = analog_labels_for(all_analog_keys())
    discrete_opts = discrete_labels_for(all_discrete_keys())
    return render_template(
        "data/index.html",
        analog_options=analog_opts,
        discrete_options=discrete_opts,
        default_row_limit=DEFAULT_ROW_LIMIT,
    )


@data_router.route("/tables", methods=["GET"])
@admin_required
def tables():
    flt = parse_data_filter(request.args)
    ctx = _service().collect_tab(flt)
    return render_template("data/_tables.html", **ctx)


@data_router.route("/export", methods=["POST"])
@admin_required
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
