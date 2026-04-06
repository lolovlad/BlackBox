from __future__ import annotations

import json
from pathlib import Path

from flask import Blueprint, current_app, redirect, render_template, url_for

from src.database import Alarms, Analogs, Discretes
from src.webui.auth_utils import login_required
from src.webui.modbus_service import (
    ANALOG_CSV_COLUMNS,
    DISCRETE_CSV_COLUMNS,
    analog_discrete_for_csv,
    create_export_csv,
    decode_to_processed,
)

bp = Blueprint("data", __name__, url_prefix="/data")


def _collect_tables(limit: int = 100):
    session_factory = current_app.extensions["session_factory"]
    collector = current_app.extensions["modbus_collector"]
    dbs = session_factory()
    try:
        analog_rows = dbs.query(Analogs).order_by(Analogs.created_at.desc()).limit(limit).all()
        discrete_rows = dbs.query(Discretes).order_by(Discretes.created_at.desc()).limit(limit).all()
        alarm_rows = dbs.query(Alarms).order_by(Alarms.created_at.desc()).limit(limit).all() if collector._alarms_enabled else []
    finally:
        dbs.close()

    analog_table = []
    for item in analog_rows:
        processed = decode_to_processed(item.date)
        analog, _ = analog_discrete_for_csv(processed)
        analog_table.append({"created_at": item.created_at.strftime("%Y-%m-%d %H:%M:%S"), "values": [analog.get(c, "") for c in ANALOG_CSV_COLUMNS]})

    discrete_table = []
    for item in discrete_rows:
        processed = decode_to_processed(item.date)
        _, discrete = analog_discrete_for_csv(processed)
        discrete_table.append({"created_at": item.created_at.strftime("%Y-%m-%d %H:%M:%S"), "values": [1 if bool(discrete.get(c, False)) else 0 for c in DISCRETE_CSV_COLUMNS]})

    alarms_table = []
    for item in alarm_rows:
        try:
            payload = json.loads(item.date.decode("utf-8"))
        except Exception:
            payload = {}
        alarms_table.append(
            {
                "created_at": item.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "name": item.name,
                "description": item.description or "",
                "payload": json.dumps(payload, ensure_ascii=False),
            }
        )
    return analog_table, discrete_table, alarms_table


@bp.get("/")
@login_required
def page():
    return render_template("data/index.html")


@bp.get("/tables")
@login_required
def tables():
    analog_table, discrete_table, alarms_table = _collect_tables()
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
    export_path = create_export_csv(session_factory, static_csv_dir)
    rel = export_path.relative_to(current_app.static_folder)
    return render_template("data/_export_result.html", download_url=url_for("static", filename=str(rel).replace("\\", "/")))


@bp.get("/export.csv")
@login_required
def export_csv():
    session_factory = current_app.extensions["session_factory"]
    static_csv_dir: Path = current_app.extensions["static_csv_dir"]
    export_path = create_export_csv(session_factory, static_csv_dir)
    rel = export_path.relative_to(current_app.static_folder)
    return redirect(url_for("static", filename=str(rel).replace("\\", "/")))
