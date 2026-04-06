from __future__ import annotations

from pathlib import Path

from flask import Blueprint, current_app, redirect, render_template, url_for

from src.webui.auth_utils import login_required
from src.webui.modbus_service import ANALOG_CSV_COLUMNS, DISCRETE_CSV_COLUMNS
from src.webui.repositories.data_repository import DataRepository
from src.webui.services.data_service import DataService

bp = Blueprint("data", __name__, url_prefix="/data")


def _service() -> DataService:
    session_factory = current_app.extensions["session_factory"]
    collector = current_app.extensions["modbus_collector"]
    repo = DataRepository(session_factory)
    return DataService(repo, alarms_enabled=collector._alarms_enabled)


@bp.get("/")
@login_required
def page():
    return render_template("data/index.html")


@bp.get("/tables")
@login_required
def tables():
    analog_table, discrete_table, alarms_table = _service().collect_tables()
    return render_template(
        "data/_tables.html",
        analog_cols=ANALOG_CSV_COLUMNS,
        discrete_cols=DISCRETE_CSV_COLUMNS,
        analog_table=analog_table,
        discrete_table=discrete_table,
        alarms_table=alarms_table,
    )


@bp.get("/export-link")
@login_required
def export_link():
    session_factory = current_app.extensions["session_factory"]
    static_csv_dir: Path = current_app.extensions["static_csv_dir"]
    export_path = _service().build_export(session_factory, static_csv_dir)
    rel = export_path.relative_to(current_app.static_folder)
    return render_template("data/_export_result.html", download_url=url_for("static", filename=str(rel).replace("\\", "/")))


@bp.get("/export.csv")
@login_required
def export_csv():
    session_factory = current_app.extensions["session_factory"]
    static_csv_dir: Path = current_app.extensions["static_csv_dir"]
    export_path = _service().build_export(session_factory, static_csv_dir)
    rel = export_path.relative_to(current_app.static_folder)
    return redirect(url_for("static", filename=str(rel).replace("\\", "/")))
