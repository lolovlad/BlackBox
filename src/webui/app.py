from __future__ import annotations

import atexit
import logging
import os
from datetime import timedelta
from pathlib import Path

from flask import Flask
from flask_login import current_user
from flask_wtf.csrf import generate_csrf
from flask_migrate import Migrate
from sqlalchemy import event, inspect
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import joinedload, sessionmaker

from src.database import User, db
from src.database import Emergency, EmergencyConditions, EventLog, Video  # noqa: F401 — метаданные для Alembic / Flask-Migrate
from src.webui.blueprints.auth import auth_router
from src.webui.blueprints.api import api_router
from src.webui.blueprints.data import data_router
from src.webui.blueprints.main import main_router
from src.webui.extensions import csrf, login_manager, server_session
from src.webui.app_runtime_config import (
    ROOT_ENV_DEFAULTS,
    apply_app_runtime_to_environ,
    build_runtime_config,
    ensure_app_runtime_file,
    load_app_runtime,
)
from src.webui.modbus_service import configure_settings_path, reload_settings_cache
from src.webui.paths import SRC_DIR, STATIC_DIR, TEMPLATES_DIR
from src.webui.reader_supervisor import ReaderSupervisor
from src.webui.background_tasks import MaintenanceScheduler
from src.webui.system_settings import (
    load_env_into_os,
    prune_parser_settings_json_files,
    read_env_file,
    repair_parser_settings_path,
)
from src.webui.timezone_utils import configured_timezone_name, format_in_configured_timezone

logger = logging.getLogger(__name__)


def create_app() -> Flask:
    template_dir = TEMPLATES_DIR
    alt_template_dir = SRC_DIR / "webui" / "templates"
    project_template_dir = SRC_DIR.parent / "templates"
    static_dir = STATIC_DIR
    static_csv_dir = static_dir / "csv"
    env_path = (Path.cwd() / ".env").resolve()
    if not env_path.exists():
        raise RuntimeError(
            "Файл .env не найден. Создайте его вручную в корне проекта перед запуском приложения."
        )
    load_env_into_os(env_path, override=True)

    project_root = env_path.parent.resolve()
    env_map = read_env_file(env_path)
    # .env перекрывает переменные процесса (например из systemd EnvironmentFile) при переносе в app_runtime.
    merged_runtime_fallback = {k: str(v) if v is not None else "" for k, v in os.environ.items()}
    merged_runtime_fallback.update(env_map)
    ensure_app_runtime_file(project_root, merged_runtime_fallback)
    app_rt = load_app_runtime(project_root, merged_runtime_fallback)
    settings_dir = project_root / "settings"
    settings_dir.mkdir(parents=True, exist_ok=True)
    prune_parser_settings_json_files(settings_dir)
    app_rt, _ = repair_parser_settings_path(project_root, app_rt, settings_dir)
    apply_app_runtime_to_environ(app_rt)
    instance_dir = Path(os.getenv("FLASK_INSTANCE_PATH", str(project_root / "instance"))).resolve()
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

    db_path_raw = env_map.get("BLACKBOX_DB_PATH", ROOT_ENV_DEFAULTS["BLACKBOX_DB_PATH"])
    config = build_runtime_config(app_rt, db_path=db_path_raw, static_csv_dir=static_csv_dir)
    _db_path = Path(config.db_path)
    db_file = _db_path.resolve() if _db_path.is_absolute() else (project_root / _db_path).resolve()
    db_file.parent.mkdir(parents=True, exist_ok=True)

    raw_parser_settings = app_rt.parser_settings_path
    parser_settings_abs = (
        Path(raw_parser_settings).resolve()
        if Path(raw_parser_settings).is_absolute()
        else (project_root / raw_parser_settings).resolve()
    )

    app.config.update(
        PROJECT_ROOT=str(project_root),
        SECRET_KEY=os.getenv("SECRET_KEY", "change-me"),
        PARSER_SETTINGS_PATH=raw_parser_settings,
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{db_file.as_posix()}",
        SQLALCHEMY_ENGINE_OPTIONS={
            # Несколько потоков (Modbus + emergency) + веб: SQLite ждёт блокировку до timeout сек.
            "connect_args": {"check_same_thread": False, "timeout": 60.0},
        },
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
    # Bind active parser settings file for this process and warm cache (абсолютный путь к каталогу проекта).
    configure_settings_path(parser_settings_abs)
    reload_settings_cache()

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
            return {**base, "nav_menu": [], "display_username": None, "is_admin": False, "is_authenticated": False}
        tu = getattr(current_user, "type_user", None)
        role = tu.system_name if tu is not None else "user"
        menu = [
            {"endpoint": "main_blueprint.dashboard", "title": "Панель"},
            {"endpoint": "main_blueprint.alarms_page", "title": "Аварии"},
            {"endpoint": "data_blueprint.page", "title": "Данные"},
            {"endpoint": "data_blueprint.charts_page", "title": "Графики"},
            {"endpoint": "main_blueprint.settings", "title": "Настройки"},
        ]
        fm_url = str(getattr(app.extensions.get("app_runtime_config"), "file_manager_url", "") or "").strip()
        if fm_url:
            menu.append({"url": fm_url, "title": "Файлы", "new_tab": True})
        if role == "admin":
            menu.append({"endpoint": "main_blueprint.event_logs_page", "title": "Логи"})
        menu.append({"endpoint": "auth_blueprint.logout", "title": "Выход", "is_logout": True})
        return {
            **base,
            "nav_menu": menu,
            "display_username": current_user.username,
            "is_admin": role == "admin",
            "is_authenticated": True,
            "app_timezone": configured_timezone_name(),
            "format_in_configured_timezone": format_in_configured_timezone,
        }

    with app.app_context():
        if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):

            @event.listens_for(db.engine, "connect")
            def _sqlite_pragmas(dbapi_connection, _connection_record):  # noqa: WPS430
                cur = dbapi_connection.cursor()
                try:
                    cur.execute("PRAGMA journal_mode=WAL")
                    cur.execute("PRAGMA synchronous=NORMAL")
                    cur.execute("PRAGMA busy_timeout=60000")
                finally:
                    cur.close()

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

    collector = ReaderSupervisor(
        runtime=config, alarms_enabled=alarms_enabled, instance_dir=instance_dir, project_root=project_root
    )
    maintenance = MaintenanceScheduler(
        session_factory=session_factory,
        env_path=env_path,
    )
    maintenance.start()
    atexit.register(maintenance.stop)
    if not app_rt.disable_modbus_collector and not missing_required:
        collector.start()
        atexit.register(collector.stop)

    app.extensions["session_factory"] = session_factory
    app.extensions["modbus_collector"] = collector
    app.extensions["maintenance_scheduler"] = maintenance
    app.extensions["static_csv_dir"] = static_csv_dir
    app.extensions["env_path"] = env_path
    app.extensions["project_root"] = project_root
    app.extensions["app_runtime_config"] = app_rt

    app.register_blueprint(auth_router, name="auth_blueprint")
    app.register_blueprint(api_router, name="api_blueprint")
    app.register_blueprint(main_router, name="main_blueprint")
    app.register_blueprint(data_router, name="data_blueprint")
    return app
