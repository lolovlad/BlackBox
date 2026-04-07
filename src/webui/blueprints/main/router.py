from __future__ import annotations

from flask import Blueprint, current_app, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from src.webui.auth_utils import admin_required
from src.webui.data_labels import (
    all_analog_keys,
    all_discrete_keys,
    analog_labels_for,
    discrete_labels_for,
    filter_valid_analog,
    filter_valid_discrete,
)
from src.webui.paths import TEMPLATES_DIR
from src.webui.repositories.data_repository import DataRepository
from src.webui.modbus_service import analog_discrete_for_csv, decode_to_processed

main_router = Blueprint("main", __name__, template_folder=str(TEMPLATES_DIR))
DATETIME_UI_FORMAT = "%d.%m.%Y %H:%M:%S"


@main_router.route("/", methods=["GET"])
def index():
    if current_user.is_authenticated:
        return redirect(url_for("main_blueprint.dashboard"))
    return redirect(url_for("auth_blueprint.login"))


@main_router.route("/dashboard", methods=["GET"])
@login_required
def dashboard():
    analog_opts = analog_labels_for(all_analog_keys())
    discrete_opts = discrete_labels_for(all_discrete_keys())
    return render_template(
        "dashboard/index.html",
        analog_options=analog_opts,
        discrete_options=discrete_opts,
    )


@main_router.route("/settings", methods=["GET"])
@admin_required
def settings():
    return render_template("settings/index.html")


def _build_live_dashboard_context(
    analog_columns: list[str],
    discrete_columns: list[str],
    *,
    alarm_limit: int = 12,
) -> dict:
    session_factory = current_app.extensions["session_factory"]
    collector = current_app.extensions["modbus_collector"]
    repo = DataRepository(session_factory)

    analog_row = repo.list_analogs(limit=1)
    discrete_row = repo.list_discretes(limit=1)
    alarms = repo.list_alarms(limit=alarm_limit) if collector._alarms_enabled else []

    analog_items: list[dict] = []
    analog_time = None
    analog_label_map = dict(analog_labels_for(analog_columns))
    if analog_row:
        row = analog_row[0]
        processed = decode_to_processed(row.date)
        analog_map, _ = analog_discrete_for_csv(processed)
        analog_time = row.created_at.strftime(DATETIME_UI_FORMAT)
        analog_items = [{"name": analog_label_map.get(k, k), "value": analog_map.get(k, "")} for k in analog_columns]

    discrete_items: list[dict] = []
    discrete_time = None
    discrete_label_map = dict(discrete_labels_for(discrete_columns))
    if discrete_row:
        row = discrete_row[0]
        processed = decode_to_processed(row.date)
        _, discrete_map = analog_discrete_for_csv(processed)
        discrete_time = row.created_at.strftime(DATETIME_UI_FORMAT)
        discrete_items = [
            {"name": discrete_label_map.get(k, k), "is_on": bool(discrete_map.get(k, False))}
            for k in discrete_columns
        ]

    alarm_rows = [
        {"time": item.created_at.strftime(DATETIME_UI_FORMAT), "name": item.name}
        for item in alarms
    ]

    return {
        "analog_time": analog_time,
        "analog_items": analog_items,
        "discrete_time": discrete_time,
        "discrete_items": discrete_items,
        "alarm_rows": alarm_rows,
        "alarms_enabled": collector._alarms_enabled,
    }


@main_router.route("/dashboard/live", methods=["GET"])
@login_required
def dashboard_live():
    analog_requested = request.args.getlist("analog_col")
    discrete_requested = request.args.getlist("discrete_col")
    analog_columns = filter_valid_analog(analog_requested if analog_requested else None)
    discrete_columns = filter_valid_discrete(discrete_requested if discrete_requested else None)
    ctx = _build_live_dashboard_context(analog_columns, discrete_columns)
    return render_template("dashboard/_live_panels.html", **ctx)
