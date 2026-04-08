from __future__ import annotations

import atexit
import logging
import os
import threading
from datetime import timedelta
from pathlib import Path

from flask import Flask, request
from flask_login import current_user
from flask_login import current_user as socket_current_user
from flask_wtf.csrf import generate_csrf
from flask_migrate import Migrate
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import joinedload, sessionmaker
from flask_socketio import disconnect

from src.database import User, db
from src.database import Emergency, EmergencyConditions  # noqa: F401 — метаданные для Alembic / Flask-Migrate
from src.webui.blueprints.auth import auth_router
from src.webui.blueprints.data import data_router
from src.webui.blueprints.main import main_router
from src.webui.extensions import csrf, login_manager, server_session, socketio
from src.webui.data_labels import all_analog_keys, all_discrete_keys
from src.webui.blueprints.main.router import render_live_dashboard_html
from src.webui.modbus_service import RuntimeConfig, reload_settings_cache
from src.webui.paths import SRC_DIR, STATIC_DIR, TEMPLATES_DIR
from src.webui.reader_supervisor import ReaderSupervisor
from src.webui.system_settings import ENV_DEFAULTS, ensure_env_file, load_env_into_os
from src.webui.timezone_utils import configured_timezone_name, format_in_configured_timezone

logger = logging.getLogger(__name__)


def _build_runtime_config(static_csv_dir: Path) -> RuntimeConfig:
    return RuntimeConfig(
        db_path=os.getenv("BLACKBOX_DB_PATH", "instance/blackbox.db"),
        modbus_port=os.getenv("MODBUS_PORT", "/dev/ttyAMA0"),
        modbus_slave=int(os.getenv("MODBUS_SLAVE", "1")),
        modbus_baudrate=int(os.getenv("MODBUS_BAUDRATE", "9600")),
        modbus_timeout=float(os.getenv("MODBUS_TIMEOUT", "0.35")),
        modbus_interval=float(os.getenv("MODBUS_INTERVAL", "0.12")),
        address_offset=int(os.getenv("MODBUS_ADDRESS_OFFSET", "1")),
        ram_batch_size=int(os.getenv("RAM_BATCH_SIZE", "60")),
        static_csv_dir=static_csv_dir,
    )


def create_app() -> Flask:
    template_dir = TEMPLATES_DIR
    alt_template_dir = SRC_DIR / "webui" / "templates"
    project_template_dir = SRC_DIR.parent / "templates"
    static_dir = STATIC_DIR
    static_csv_dir = static_dir / "csv"
    instance_dir = Path(os.getenv("FLASK_INSTANCE_PATH", str(Path.cwd() / "instance"))).resolve()
    session_dir = instance_dir / "sessions"

    static_csv_dir.mkdir(parents=True, exist_ok=True)
    session_dir.mkdir(parents=True, exist_ok=True)

    app = Flask(
        __name__,
        template_folder=str(template_dir),
        static_folder=str(static_dir),
        static_url_path="/static",
        instance_path=str(instance_dir),
    )

    env_path = Path.cwd() / ".env"
    # First launch: create .env with all runtime settings.
    ensure_env_file(
        env_path,
        defaults={
            key: os.getenv(key, default)
            for key, default in ENV_DEFAULTS.items()
        },
    )
    # Next launches: read values from existing .env.
    load_env_into_os(env_path, override=True)
    config = _build_runtime_config(static_csv_dir)
    # Preload settings at startup; runtime updates are picked automatically by mtime.
    reload_settings_cache()
    db_file = Path(config.db_path).resolve()
    db_file.parent.mkdir(parents=True, exist_ok=True)

    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY", "change-me"),
        PARSER_SETTINGS_PATH=os.getenv("PARSER_SETTINGS_PATH", "settings/settings.json"),
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{db_file.as_posix()}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        PROPAGATE_EXCEPTIONS=True,
        TRAP_HTTP_EXCEPTIONS=False,
        TRAP_BAD_REQUEST_ERRORS=False,
        SESSION_TYPE="filesystem",
        SESSION_FILE_DIR=str(session_dir),
        SESSION_PERMANENT=True,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "0") == "1",
    )

    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    logger.info("Starting web app with DB path: %s", db_file)
    logger.info("Template dir: %s (exists=%s)", template_dir, template_dir.exists())
    logger.info("Alt template dir: %s (exists=%s)", alt_template_dir, alt_template_dir.exists())
    logger.info("Project template dir: %s (exists=%s)", project_template_dir, project_template_dir.exists())
    logger.info("Static dir: %s (exists=%s)", static_dir, static_dir.exists())

    db.init_app(app)
    Migrate(app, db, compare_type=True, render_as_batch=True)
    csrf.init_app(app)
    server_session.init_app(app)
    socketio.init_app(app, logger=False, engineio_logger=False, manage_session=False)

    login_manager.init_app(app)
    login_manager.login_view = "auth_blueprint.login"
    login_manager.login_message = "Требуется вход в систему."
    login_manager.login_message_category = "error"

    @login_manager.user_loader
    def load_user(user_id: str):  # noqa: WPS430
        if user_id is None:
            return None
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return None
        return (
            User.query.options(joinedload(User.type_user))
            .filter_by(id=uid, is_deleted=False)
            .first()
        )

    @app.context_processor
    def inject_nav() -> dict:
        base = {
            "csrf_token": generate_csrf,
            "app_timezone": configured_timezone_name(),
            "format_in_configured_timezone": format_in_configured_timezone,
        }
        if not current_user.is_authenticated:
            return {**base, "nav_menu": [], "display_username": None, "is_admin": False}
        tu = getattr(current_user, "type_user", None)
        role = tu.system_name if tu is not None else "user"
        menu = [
            {"endpoint": "main_blueprint.dashboard", "title": "Панель"},
            {"endpoint": "main_blueprint.alarms_page", "title": "Аварии"},
            {"endpoint": "data_blueprint.page", "title": "Данные"},
            {"endpoint": "data_blueprint.charts_page", "title": "Графики"},
        ]
        if role == "admin":
            menu.append({"endpoint": "main_blueprint.settings", "title": "Настройки"})
        return {
            **base,
            "nav_menu": menu,
            "display_username": current_user.username,
            "is_admin": role == "admin",
            "app_timezone": configured_timezone_name(),
            "format_in_configured_timezone": format_in_configured_timezone,
        }

    with app.app_context():
        required_tables = ("samples",)
        missing_required: list[str] = []
        alarms_enabled = False
        try:
            inspector = inspect(db.engine)
            missing_required = [t for t in required_tables if not inspector.has_table(t)]
            alarms_enabled = inspector.has_table("alarms")
        except OperationalError:
            missing_required = list(required_tables)
            logger.exception("Cannot inspect DB schema at %s", db_file)

        if missing_required:
            logger.error("Missing required tables %s. Run migrations.", ",".join(missing_required))
        if not alarms_enabled:
            logger.error("Table 'alarms' is missing. Run migrations.")

        session_factory = sessionmaker(bind=db.engine, autoflush=False, autocommit=False, expire_on_commit=False)

    collector = ReaderSupervisor(runtime=config, alarms_enabled=alarms_enabled, instance_dir=instance_dir)
    if os.getenv("DISABLE_MODBUS_COLLECTOR", "0") != "1" and not missing_required:
        collector.start()
        atexit.register(collector.stop)

    app.extensions["session_factory"] = session_factory
    app.extensions["modbus_collector"] = collector
    app.extensions["static_csv_dir"] = static_csv_dir
    app.extensions["env_path"] = env_path

    live_lock = threading.Lock()
    live_subscriptions: dict[str, dict[str, list[str]]] = {}
    live_thread_started = False

    def _normalize_live_columns(payload: dict | None) -> tuple[list[str], list[str]]:
        analog_allowed = set(all_analog_keys())
        discrete_allowed = set(all_discrete_keys())
        data = payload if isinstance(payload, dict) else {}
        analog_cols = [str(v) for v in (data.get("analog_col") or []) if str(v) in analog_allowed]
        discrete_cols = [str(v) for v in (data.get("discrete_col") or []) if str(v) in discrete_allowed]
        if not analog_cols:
            analog_cols = list(all_analog_keys())
        if not discrete_cols:
            discrete_cols = list(all_discrete_keys())
        return analog_cols, discrete_cols

    def _dashboard_live_emitter() -> None:
        logger.info("Dashboard Socket.IO emitter started")
        while True:
            socketio.sleep(2.0)
            with live_lock:
                targets = dict(live_subscriptions)
            if not targets:
                continue
            with app.app_context():
                for sid, cols in targets.items():
                    try:
                        html = render_live_dashboard_html(cols.get("analog_col", []), cols.get("discrete_col", []))
                        socketio.emit("dashboard_live_update", {"html": html}, to=sid, namespace="/dashboard")
                    except Exception:
                        logger.exception("Failed to emit dashboard live update to sid=%s", sid)

    @socketio.on("connect", namespace="/dashboard")
    def _dashboard_connect():
        nonlocal live_thread_started
        if not socket_current_user.is_authenticated:
            disconnect()
            return
        sid = request.sid  # type: ignore[name-defined]
        with live_lock:
            live_subscriptions[sid] = {
                "analog_col": list(all_analog_keys()),
                "discrete_col": list(all_discrete_keys()),
            }
        if not live_thread_started:
            socketio.start_background_task(_dashboard_live_emitter)
            live_thread_started = True
        logger.info("Dashboard socket connected sid=%s user=%s", sid, socket_current_user.get_id())

    @socketio.on("dashboard_subscribe", namespace="/dashboard")
    def _dashboard_subscribe(payload: dict | None):
        if not socket_current_user.is_authenticated:
            disconnect()
            return
        sid = request.sid  # type: ignore[name-defined]
        analog_cols, discrete_cols = _normalize_live_columns(payload)
        with live_lock:
            live_subscriptions[sid] = {"analog_col": analog_cols, "discrete_col": discrete_cols}

    @socketio.on("disconnect", namespace="/dashboard")
    def _dashboard_disconnect():
        sid = request.sid  # type: ignore[name-defined]
        with live_lock:
            live_subscriptions.pop(sid, None)
        logger.info("Dashboard socket disconnected sid=%s", sid)

    app.register_blueprint(auth_router, name="auth_blueprint")
    app.register_blueprint(main_router, name="main_blueprint")
    app.register_blueprint(data_router, name="data_blueprint")
    return app
