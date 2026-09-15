"""Конфигурация чтения Modbus / парсера в settings/app_runtime.json (не .env)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.webui.modbus_service import RuntimeConfig

APP_RUNTIME_FILENAME = "app_runtime.json"

# --- Только .env: БД, обслуживание, видео, веб, сиды пользователей ---
ROOT_ENV_DEFAULTS: dict[str, str] = {
    "BLACKBOX_DB_PATH": "instance/blackbox.db",
    "DB_CLEANUP_INTERVAL_MINUTES": "60",
    "DB_RETENTION_DAYS": "30",
    "VIDEO_STORAGE_DIR": "",
    "VIDEO_GC_INTERVAL_DAYS": "10",
    "SECRET_KEY": "change-me",
    "HOST": "0.0.0.0",
    "PORT": "5000",
    "FLASK_APP": "src.web_app:app",
    "SESSION_COOKIE_SECURE": "0",
    "SEED_ADMIN_USERNAME": "admin",
    "SEED_ADMIN_PASSWORD": "admin",
    "SEED_USER_USERNAME": "user",
    "SEED_USER_PASSWORD": "user",
}


class AppRuntimeConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    modbus_port: str = Field(min_length=1, max_length=255)
    modbus_slave: int = Field(ge=1, le=247)
    modbus_baudrate: int = Field(ge=1200, le=115200)
    modbus_timeout: float = Field(gt=0.01, le=10.0)
    modbus_interval: float = Field(ge=0.05, le=60.0)
    modbus_address_offset: int = Field(ge=0, le=10000)
    ram_batch_size: int = Field(ge=1, le=10000)
    app_timezone: str = Field(min_length=1, max_length=128)
    parser_settings_path: str = Field(min_length=1, max_length=2048)
    disable_modbus_collector: bool = False
    video_match_window_minutes: int = Field(default=20, ge=1, le=1440)
    file_manager_url: str = Field(default="", max_length=2048)


def _runtime_defaults_dict() -> dict[str, Any]:
    return AppRuntimeConfigModel(
        modbus_port="/dev/ttyAMA0",
        modbus_slave=1,
        modbus_baudrate=9600,
        modbus_timeout=0.35,
        modbus_interval=0.12,
        modbus_address_offset=1,
        ram_batch_size=60,
        app_timezone="Europe/Moscow",
        parser_settings_path="settings/settings.json",
        disable_modbus_collector=False,
        video_match_window_minutes=20,
        file_manager_url="",
    ).model_dump()


def _legacy_env_to_runtime(env: dict[str, str]) -> dict[str, Any]:
    """Подтягивает старые ключи из .env при отсутствии app_runtime.json."""
    out: dict[str, Any] = {}
    if v := env.get("MODBUS_PORT", "").strip():
        out["modbus_port"] = v
    if v := env.get("MODBUS_SLAVE", "").strip():
        try:
            out["modbus_slave"] = int(v)
        except ValueError:
            pass
    if v := env.get("MODBUS_BAUDRATE", "").strip():
        try:
            out["modbus_baudrate"] = int(v)
        except ValueError:
            pass
    if v := env.get("MODBUS_TIMEOUT", "").strip():
        try:
            out["modbus_timeout"] = float(v)
        except ValueError:
            pass
    if v := env.get("MODBUS_INTERVAL", "").strip():
        try:
            out["modbus_interval"] = float(v)
        except ValueError:
            pass
    if v := env.get("MODBUS_ADDRESS_OFFSET", "").strip():
        try:
            out["modbus_address_offset"] = int(v)
        except ValueError:
            pass
    if v := env.get("RAM_BATCH_SIZE", "").strip():
        try:
            out["ram_batch_size"] = int(v)
        except ValueError:
            pass
    if v := env.get("APP_TIMEZONE", "").strip():
        out["app_timezone"] = v
    if v := env.get("PARSER_SETTINGS_PATH", "").strip():
        out["parser_settings_path"] = v.replace("\\", "/")
    if v := env.get("DISABLE_MODBUS_COLLECTOR", "").strip():
        out["disable_modbus_collector"] = v == "1"
    return out


def app_runtime_file_path(project_root: Path) -> Path:
    return (project_root / "settings" / APP_RUNTIME_FILENAME).resolve()


def load_app_runtime(project_root: Path, env_fallback: dict[str, str]) -> AppRuntimeConfigModel:
    path = app_runtime_file_path(project_root)
    data = _runtime_defaults_dict()
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update(raw)
        except (OSError, json.JSONDecodeError):
            pass
    else:
        data.update(_legacy_env_to_runtime(env_fallback))
    return AppRuntimeConfigModel.model_validate(data)


def ensure_app_runtime_file(project_root: Path, env_fallback: dict[str, str]) -> None:
    path = app_runtime_file_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        return
    data = _runtime_defaults_dict()
    data.update(_legacy_env_to_runtime(env_fallback))
    cfg = AppRuntimeConfigModel.model_validate(data)
    save_app_runtime(project_root, cfg)


def save_app_runtime(project_root: Path, cfg: AppRuntimeConfigModel) -> None:
    path = app_runtime_file_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg.model_dump(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def apply_app_runtime_to_environ(cfg: AppRuntimeConfigModel) -> None:
    os.environ["MODBUS_PORT"] = cfg.modbus_port
    os.environ["MODBUS_SLAVE"] = str(cfg.modbus_slave)
    os.environ["MODBUS_BAUDRATE"] = str(cfg.modbus_baudrate)
    os.environ["MODBUS_TIMEOUT"] = str(cfg.modbus_timeout)
    os.environ["MODBUS_INTERVAL"] = str(cfg.modbus_interval)
    os.environ["MODBUS_ADDRESS_OFFSET"] = str(cfg.modbus_address_offset)
    os.environ["RAM_BATCH_SIZE"] = str(cfg.ram_batch_size)
    os.environ["APP_TIMEZONE"] = cfg.app_timezone
    os.environ["PARSER_SETTINGS_PATH"] = cfg.parser_settings_path.replace("\\", "/")
    os.environ["DISABLE_MODBUS_COLLECTOR"] = "1" if cfg.disable_modbus_collector else "0"


def build_runtime_config(cfg: AppRuntimeConfigModel, *, db_path: str, static_csv_dir: Path) -> RuntimeConfig:
    return RuntimeConfig(
        db_path=db_path,
        modbus_port=cfg.modbus_port,
        modbus_slave=cfg.modbus_slave,
        modbus_baudrate=cfg.modbus_baudrate,
        modbus_timeout=cfg.modbus_timeout,
        modbus_interval=cfg.modbus_interval,
        address_offset=cfg.modbus_address_offset,
        ram_batch_size=cfg.ram_batch_size,
        static_csv_dir=static_csv_dir,
    )


def io_form_to_runtime(
    *,
    modbus_port: str,
    modbus_slave: str,
    modbus_baudrate: str,
    modbus_timeout: str,
    modbus_interval: str,
    modbus_address_offset: str,
    ram_batch_size: str,
    app_timezone: str,
    parser_settings_path: str,
    disable_modbus_collector: bool,
    video_match_window_minutes: str,
    file_manager_url: str,
) -> AppRuntimeConfigModel:
    return AppRuntimeConfigModel(
        modbus_port=modbus_port.strip(),
        modbus_slave=int(modbus_slave),
        modbus_baudrate=int(modbus_baudrate),
        modbus_timeout=float(modbus_timeout),
        modbus_interval=float(modbus_interval),
        modbus_address_offset=int(modbus_address_offset),
        ram_batch_size=int(ram_batch_size),
        app_timezone=app_timezone.strip(),
        parser_settings_path=parser_settings_path.strip().replace("\\", "/"),
        disable_modbus_collector=disable_modbus_collector,
        video_match_window_minutes=int(video_match_window_minutes),
        file_manager_url=file_manager_url.strip(),
    )
