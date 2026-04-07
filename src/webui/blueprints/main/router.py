from __future__ import annotations

import os

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
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
from src.webui.modbus_service import reload_settings_cache
from src.webui.system_settings import (
    ENV_DEFAULTS,
    effective_runtime_from_env,
    read_env_file,
    test_modbus_settings,
    validate_parser_json,
    write_env_file,
)

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
    env_path = current_app.extensions["env_path"]
    env_current = read_env_file(env_path)
    values = {k: env_current.get(k, ENV_DEFAULTS[k]) for k in ENV_DEFAULTS}
    parser_path = current_app.config.get("PARSER_SETTINGS_PATH", "settings/settings.json")
    with open(parser_path, "r", encoding="utf-8") as f:
        parser_text = f.read()
    return render_template("settings/index.html", env_values=values, parser_text=parser_text)


@main_router.route("/settings", methods=["POST"])
@admin_required
def settings_save():
    env_path = current_app.extensions["env_path"]
    parser_path = current_app.config.get("PARSER_SETTINGS_PATH", "settings/settings.json")
    static_csv_dir = current_app.extensions["static_csv_dir"]
    collector = current_app.extensions["modbus_collector"]

    posted_env = {
        "MODBUS_PORT": (request.form.get("MODBUS_PORT") or "").strip(),
        "MODBUS_SLAVE": (request.form.get("MODBUS_SLAVE") or "").strip(),
        "MODBUS_BAUDRATE": (request.form.get("MODBUS_BAUDRATE") or "").strip(),
        "MODBUS_TIMEOUT": (request.form.get("MODBUS_TIMEOUT") or "").strip(),
        "MODBUS_INTERVAL": (request.form.get("MODBUS_INTERVAL") or "").strip(),
        "MODBUS_ADDRESS_OFFSET": (request.form.get("MODBUS_ADDRESS_OFFSET") or "").strip(),
        "RAM_BATCH_SIZE": (request.form.get("RAM_BATCH_SIZE") or "").strip(),
    }
    parser_text = request.form.get("parser_json") or ""
    action = (request.form.get("action") or "test").strip().lower()

    parser_cfg, err = validate_parser_json(parser_text)
    if err:
        flash(err, "error")
        return render_template("settings/index.html", env_values=posted_env, parser_text=parser_text), 400

    try:
        runtime = effective_runtime_from_env(static_csv_dir, posted_env)
    except Exception as exc:
        flash(f"Некорректные значения runtime: {exc}", "error")
        return render_template("settings/index.html", env_values=posted_env, parser_text=parser_text), 400

    ok, msg = test_modbus_settings(runtime, parser_cfg)
    if not ok:
        flash(msg, "error")
        return render_template("settings/index.html", env_values=posted_env, parser_text=parser_text), 400

    if action == "test":
        flash(msg, "success")
        return render_template("settings/index.html", env_values=posted_env, parser_text=parser_text)

    # action == save: test already passed.
    write_env_file(env_path, posted_env)
    with open(parser_path, "w", encoding="utf-8") as f:
        f.write(parser_text.strip() + "\n")

    # Apply without full app restart.
    for key, value in posted_env.items():
        current_app.config[key] = value
        os.environ[key] = value
    reload_settings_cache()
    collector.restart(new_config=runtime)

    flash("Настройки сохранены и применены. Процесс чтения перезапущен.", "success")
    return redirect(url_for("main_blueprint.settings"))


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
