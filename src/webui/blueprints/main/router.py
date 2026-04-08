from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

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
from src.webui.emergency_rule_validation import validate_emergency_rule_expression
from src.webui.repositories.data_repository import DataRepository
from src.webui.repositories.emergency_repository import EmergencyRepository
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
from src.webui.timezone_utils import format_in_configured_timezone

main_router = Blueprint("main", __name__, template_folder=str(TEMPLATES_DIR))
DATETIME_UI_FORMAT = "%d.%m.%Y %H:%M:%S"


def _effective_parser_settings_path() -> Path:
    raw = current_app.config.get("PARSER_SETTINGS_PATH", "settings/settings.json")
    p = Path(raw)
    return p if p.is_absolute() else Path.cwd() / p


def _settings_page_context(env_values: dict, parser_text: str) -> dict:
    er_repo = EmergencyRepository(current_app.extensions["session_factory"])
    timezone_choices = [
        ("Europe/Kaliningrad", "UTC+2  Europe/Kaliningrad"),
        ("Europe/Moscow", "UTC+3  Europe/Moscow"),
        ("Europe/Samara", "UTC+4  Europe/Samara"),
        ("Asia/Yekaterinburg", "UTC+5  Asia/Yekaterinburg"),
        ("Asia/Omsk", "UTC+6  Asia/Omsk"),
        ("Asia/Krasnoyarsk", "UTC+7  Asia/Krasnoyarsk"),
        ("Asia/Irkutsk", "UTC+8  Asia/Irkutsk"),
        ("Asia/Yakutsk", "UTC+9  Asia/Yakutsk"),
        ("Asia/Vladivostok", "UTC+10 Asia/Vladivostok"),
        ("Asia/Magadan", "UTC+11 Asia/Magadan"),
        ("Asia/Kamchatka", "UTC+12 Asia/Kamchatka"),
    ]
    return {
        "env_values": env_values,
        "parser_text": parser_text,
        "emergency_conditions": er_repo.list_conditions(),
        "timezone_choices": timezone_choices,
    }


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
    parser_path = _effective_parser_settings_path()
    with parser_path.open("r", encoding="utf-8") as f:
        parser_text = f.read()
    return render_template("settings/index.html", **_settings_page_context(values, parser_text))


@main_router.route("/settings", methods=["POST"])
@admin_required
def settings_save():
    env_path = current_app.extensions["env_path"]
    parser_path = _effective_parser_settings_path()
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
        "APP_TIMEZONE": (
            (request.form.get("APP_TIMEZONE_CUSTOM") or "").strip()
            or (request.form.get("APP_TIMEZONE") or "").strip()
        ),
    }
    parser_text = request.form.get("parser_json") or ""
    action = (request.form.get("action") or "test").strip().lower()

    if action == "import":
        uploaded = request.files.get("import_file")
        if uploaded is None or not uploaded.filename:
            flash("Выберите JSON-файл для импорта.", "error")
            return render_template("settings/index.html", **_settings_page_context(posted_env, parser_text)), 400
        try:
            raw = uploaded.read().decode("utf-8")
            payload = json.loads(raw)
        except Exception as exc:
            flash(f"Ошибка чтения файла импорта: {exc}", "error")
            return render_template("settings/index.html", **_settings_page_context(posted_env, parser_text)), 400

        if not isinstance(payload, dict):
            flash("Файл импорта должен быть JSON-объектом.", "error")
            return render_template("settings/index.html", **_settings_page_context(posted_env, parser_text)), 400

        imported_env = dict(posted_env)
        env_block = payload.get("runtime") or payload.get("env") or payload
        if isinstance(env_block, dict):
            for key in ENV_DEFAULTS:
                if key in env_block:
                    imported_env[key] = str(env_block[key]).strip()

        parser_block = payload.get("parser") or payload.get("settings") or payload
        if isinstance(parser_block, dict) and "requests" in parser_block and "fields" in parser_block:
            imported_parser_text = json.dumps(parser_block, ensure_ascii=False, indent=2)
        else:
            imported_parser_text = parser_text

        parser_cfg_import, err = validate_parser_json(imported_parser_text)
        if err:
            flash(f"Импортирован JSON, но parser-секция невалидна: {err}", "error")
            return render_template(
                "settings/index.html",
                **_settings_page_context(imported_env, imported_parser_text),
            ), 400
        try:
            _ = effective_runtime_from_env(static_csv_dir, imported_env)
        except Exception as exc:
            flash(f"Импортирован JSON, но runtime-секция невалидна: {exc}", "error")
            return render_template(
                "settings/index.html",
                **_settings_page_context(imported_env, imported_parser_text),
            ), 400

        _ = parser_cfg_import
        flash("Импорт выполнен. Проверьте значения и нажмите 'Проверить' или 'Сохранить и применить'.", "success")
        return render_template(
            "settings/index.html",
            **_settings_page_context(imported_env, imported_parser_text),
        )

    parser_cfg, err = validate_parser_json(parser_text)
    if err:
        flash(err, "error")
        return render_template("settings/index.html", **_settings_page_context(posted_env, parser_text)), 400

    try:
        runtime = effective_runtime_from_env(static_csv_dir, posted_env)
    except Exception as exc:
        flash(f"Некорректные значения runtime: {exc}", "error")
        return render_template("settings/index.html", **_settings_page_context(posted_env, parser_text)), 400

    # Avoid serial-port contention: stop background collector during active test.
    collector.stop()
    ok, msg = test_modbus_settings(runtime, parser_cfg)
    if not ok:
        collector.start()
        flash(msg, "error")
        return render_template("settings/index.html", **_settings_page_context(posted_env, parser_text)), 400

    if action == "test":
        collector.start()
        flash(msg, "success")
        return render_template("settings/index.html", **_settings_page_context(posted_env, parser_text))

    # action == save: test already passed.
    write_env_file(env_path, posted_env)
    with parser_path.open("w", encoding="utf-8") as f:
        f.write(parser_text.strip() + "\n")

    # Apply without full app restart.
    for key, value in posted_env.items():
        current_app.config[key] = value
        os.environ[key] = value
    reload_settings_cache()
    collector.restart(new_config=runtime)

    flash("Настройки сохранены и применены. Процесс чтения перезапущен.", "success")
    return redirect(url_for("main_blueprint.settings"))


@main_router.route("/alarms", methods=["GET"])
@login_required
def alarms_page():
    er_repo = EmergencyRepository(current_app.extensions["session_factory"])
    emergency_rows = er_repo.list_recent_emergencies(limit=200)
    return render_template("alarms/index.html", emergency_rows=emergency_rows)


@main_router.route("/settings/emergency-rules", methods=["POST"])
@admin_required
def emergency_rule_add():
    name = (request.form.get("rule_name") or "").strip()
    condition = (request.form.get("rule_condition") or "").strip()
    if not name:
        flash("Укажите название правила.", "error")
        return redirect(url_for("main_blueprint.settings"))
    parser_path = _effective_parser_settings_path()
    ok, err = validate_emergency_rule_expression(condition, settings_path=parser_path)
    if not ok:
        flash(err or "Некорректное правило.", "error")
        return redirect(url_for("main_blueprint.settings"))
    er_repo = EmergencyRepository(current_app.extensions["session_factory"])
    er_repo.create_condition(name=name, condition=condition)
    current_app.extensions["modbus_collector"].restart()
    flash("Правило аварии сохранено.", "success")
    return redirect(url_for("main_blueprint.settings"))


@main_router.route("/settings/emergency-rules/<int:rule_id>/edit", methods=["POST"])
@admin_required
def emergency_rule_edit(rule_id: int):
    name = (request.form.get("rule_name") or "").strip()
    condition = (request.form.get("rule_condition") or "").strip()
    if not name:
        flash("Укажите название правила.", "error")
        return redirect(url_for("main_blueprint.settings"))
    parser_path = _effective_parser_settings_path()
    ok, err = validate_emergency_rule_expression(condition, settings_path=parser_path)
    if not ok:
        flash(err or "Некорректное правило.", "error")
        return redirect(url_for("main_blueprint.settings"))
    er_repo = EmergencyRepository(current_app.extensions["session_factory"])
    if er_repo.update_condition(condition_id=rule_id, name=name, condition=condition):
        current_app.extensions["modbus_collector"].restart()
        flash("Правило обновлено.", "success")
    else:
        flash("Правило не найдено.", "error")
    return redirect(url_for("main_blueprint.settings"))


@main_router.route("/settings/emergency-rules/<int:rule_id>/delete", methods=["POST"])
@admin_required
def emergency_rule_delete(rule_id: int):
    er_repo = EmergencyRepository(current_app.extensions["session_factory"])
    if er_repo.soft_delete_condition(rule_id):
        current_app.extensions["modbus_collector"].restart()
        flash("Правило помечено удалённым.", "success")
    else:
        flash("Правило не найдено.", "error")
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
        analog_time = format_in_configured_timezone(row.created_at, DATETIME_UI_FORMAT)
        analog_items = [{"name": analog_label_map.get(k, k), "value": analog_map.get(k, "")} for k in analog_columns]

    discrete_items: list[dict] = []
    discrete_time = None
    discrete_label_map = dict(discrete_labels_for(discrete_columns))
    if discrete_row:
        row = discrete_row[0]
        processed = decode_to_processed(row.date)
        _, discrete_map = analog_discrete_for_csv(processed)
        discrete_time = format_in_configured_timezone(row.created_at, DATETIME_UI_FORMAT)
        discrete_items = [
            {"name": discrete_label_map.get(k, k), "is_on": bool(discrete_map.get(k, False))}
            for k in discrete_columns
        ]

    alarm_rows = [
        {"time": format_in_configured_timezone(item.created_at, DATETIME_UI_FORMAT), "name": item.name}
        for item in alarms
    ]

    system_monitor = _collect_system_monitor()

    return {
        "analog_time": analog_time,
        "analog_items": analog_items,
        "discrete_time": discrete_time,
        "discrete_items": discrete_items,
        "alarm_rows": alarm_rows,
        "alarms_enabled": collector._alarms_enabled,
        "system_monitor": system_monitor,
    }


def _collect_system_monitor() -> dict[str, Any]:
    stats: dict[str, Any] = {
        "disk": {"used_gb": None, "total_gb": None, "free_gb": None, "percent": None},
        "cpu": {"percent": None, "cores_logical": None, "cores_physical": None},
        "memory": {"used_gb": None, "total_gb": None, "percent": None},
        "process": {"pid": os.getpid(), "uptime_sec": None},
    }
    try:
        du = shutil.disk_usage(Path.cwd())
        stats["disk"] = {
            "used_gb": round((du.total - du.free) / (1024**3), 2),
            "total_gb": round(du.total / (1024**3), 2),
            "free_gb": round(du.free / (1024**3), 2),
            "percent": round(((du.total - du.free) / du.total) * 100.0, 1) if du.total else 0.0,
        }
    except Exception:
        pass

    try:
        import psutil  # type: ignore

        vm = psutil.virtual_memory()
        proc = psutil.Process(os.getpid())
        stats["cpu"] = {
            "percent": round(psutil.cpu_percent(interval=None), 1),
            "cores_logical": int(psutil.cpu_count(logical=True) or 0),
            "cores_physical": int(psutil.cpu_count(logical=False) or 0),
        }
        stats["memory"] = {
            "used_gb": round((vm.total - vm.available) / (1024**3), 2),
            "total_gb": round(vm.total / (1024**3), 2),
            "percent": round(float(vm.percent), 1),
        }
        stats["process"]["uptime_sec"] = int(max(0.0, (time.time() - proc.create_time())))
    except Exception:
        pass

    return stats


@main_router.route("/dashboard/live", methods=["GET"])
@login_required
def dashboard_live():
    analog_requested = request.args.getlist("analog_col")
    discrete_requested = request.args.getlist("discrete_col")
    analog_columns = filter_valid_analog(analog_requested if analog_requested else None)
    discrete_columns = filter_valid_discrete(discrete_requested if discrete_requested else None)
    ctx = _build_live_dashboard_context(analog_columns, discrete_columns)
    return render_template("dashboard/_live_panels.html", **ctx)
