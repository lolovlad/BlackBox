from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from modbus_acquire.instrument import build_instrument
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.webui.modbus_service import RuntimeConfig, parse_fields


ENV_DEFAULTS: dict[str, str] = {
    "BLACKBOX_DB_PATH": "instance/blackbox.db",
    "MODBUS_PORT": "/dev/ttyAMA0",
    "MODBUS_SLAVE": "1",
    "MODBUS_BAUDRATE": "9600",
    "MODBUS_TIMEOUT": "0.35",
    "MODBUS_INTERVAL": "0.12",
    "MODBUS_ADDRESS_OFFSET": "1",
    "RAM_BATCH_SIZE": "60",
    "APP_TIMEZONE": "UTC",
    "DB_CLEANUP_INTERVAL_MINUTES": "60",
    "DB_RETENTION_DAYS": "30",
    "VIDEO_STORAGE_DIR": "",
    "VIDEO_GC_INTERVAL_DAYS": "10",
}


class RuntimeEnvModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    BLACKBOX_DB_PATH: str = Field(min_length=1, max_length=2048)
    MODBUS_PORT: str = Field(min_length=1, max_length=255)
    MODBUS_SLAVE: int = Field(ge=1, le=247)
    MODBUS_BAUDRATE: int = Field(ge=1200, le=115200)
    MODBUS_TIMEOUT: float = Field(gt=0.01, le=10.0)
    MODBUS_INTERVAL: float = Field(ge=0.05, le=60.0)
    MODBUS_ADDRESS_OFFSET: int = Field(ge=0, le=10000)
    RAM_BATCH_SIZE: int = Field(ge=1, le=10000)
    APP_TIMEZONE: str = Field(min_length=1, max_length=128)
    DB_CLEANUP_INTERVAL_MINUTES: int = Field(ge=1, le=100000)
    DB_RETENTION_DAYS: int = Field(ge=1, le=36500)
    VIDEO_STORAGE_DIR: str = Field(default="", max_length=2048)
    VIDEO_GC_INTERVAL_DAYS: int = Field(ge=1, le=36500)


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    fc: int
    address: int = Field(ge=0, le=65535)
    count: int = Field(ge=1, le=2000)

    @field_validator("fc")
    @classmethod
    def _fc_supported(cls, v: int) -> int:
        if v not in (1, 3):
            raise ValueError("fc must be 1 or 3")
        return v


class FieldModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str = Field(min_length=1, max_length=255)
    type: str = Field(min_length=1, max_length=64)
    display_name: str | None = None
    source: str | None = None
    address: int | None = Field(default=None, ge=0, le=65535)
    expr: str | None = None
    system: bool | None = None
    is_system: bool | None = None
    internal: bool | None = None


class ParserSettingsModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    requests: list[RequestModel] = Field(min_length=1)
    fields: list[FieldModel] = Field(min_length=1)


def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def write_env_file(path: Path, updates: dict[str, str]) -> None:
    current = read_env_file(path)
    current.update(updates)
    lines = [f"{k}={v}" for k, v in sorted(current.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ensure_env_file(path: Path, defaults: dict[str, str] | None = None) -> None:
    if path.exists():
        return
    seed = dict(ENV_DEFAULTS)
    if defaults:
        seed.update(defaults)
    lines = [f"{k}={v}" for k, v in sorted(seed.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_env_into_os(path: Path, *, override: bool = True) -> None:
    for key, value in read_env_file(path).items():
        if override or key not in os.environ:
            os.environ[key] = value


def effective_runtime_from_env(static_csv_dir: Path, env_map: dict[str, str]) -> RuntimeConfig:
    merged = dict(ENV_DEFAULTS)
    merged.update(env_map)
    validated = RuntimeEnvModel.model_validate(merged)
    return RuntimeConfig(
        db_path=validated.BLACKBOX_DB_PATH,
        modbus_port=validated.MODBUS_PORT,
        modbus_slave=validated.MODBUS_SLAVE,
        modbus_baudrate=validated.MODBUS_BAUDRATE,
        modbus_timeout=validated.MODBUS_TIMEOUT,
        modbus_interval=validated.MODBUS_INTERVAL,
        address_offset=validated.MODBUS_ADDRESS_OFFSET,
        ram_batch_size=validated.RAM_BATCH_SIZE,
        static_csv_dir=static_csv_dir,
    )


def validate_parser_json(text: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        cfg = json.loads(text)
        validated = ParserSettingsModel.model_validate(cfg)
        cfg_valid = validated.model_dump(mode="python")
    except json.JSONDecodeError as exc:
        return None, f"JSON parse error: {exc}"
    except ValidationError as exc:
        return None, f"Schema validation error: {exc.errors()[0].get('msg', 'invalid settings')}"
    return cfg_valid, None


def test_modbus_settings(runtime: RuntimeConfig, parser_cfg: dict[str, Any]) -> tuple[bool, str]:
    try:
        instrument = build_instrument(
            {
                "port": runtime.modbus_port,
                "slave_id": runtime.modbus_slave,
                "baudrate": runtime.modbus_baudrate,
                "timeout": runtime.modbus_timeout,
                "clear_buffers_before_each_transaction": True,
                "close_port_after_each_call": True,
            }
        )
        source_values: dict[str, list[Any]] = {str(r["name"]): [] for r in parser_cfg.get("requests", [])}
        # One-shot link check: read only the first request to avoid long/unstable test cycles.
        first_req = parser_cfg.get("requests", [])[0]
        name = str(first_req["name"])
        fc = int(first_req["fc"])
        address = int(first_req["address"])
        count = int(first_req["count"])
        if fc == 3:
            source_values[name] = list(instrument.read_registers(address, count))
        elif fc == 1:
            source_values[name] = [bool(v) for v in instrument.read_bits(address, count, functioncode=1)]
        else:
            return False, f"Unsupported function code in settings: {fc}"
        _ = parse_fields(parser_cfg, source_values)
        return True, "Проверка пройдена: единоразовое чтение и парсинг успешны."
    except Exception as exc:
        text = f"{type(exc).__name__}: {exc}"
        if "Checksum error in rtu mode" in text:
            text += (
                " | Проверьте, что порт не занят другим процессом, и совпадают "
                "MODBUS_BAUDRATE / MODBUS_SLAVE / физическая линия RS485."
            )
        return False, f"Проверка не пройдена: {text}"
