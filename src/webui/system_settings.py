from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from modbus_acquire.instrument import build_instrument

from src.webui.modbus_service import RuntimeConfig, parse_fields


ENV_DEFAULTS: dict[str, str] = {
    "MODBUS_PORT": "/dev/ttyAMA0",
    "MODBUS_SLAVE": "1",
    "MODBUS_BAUDRATE": "9600",
    "MODBUS_TIMEOUT": "0.35",
    "MODBUS_INTERVAL": "0.12",
    "MODBUS_ADDRESS_OFFSET": "1",
    "RAM_BATCH_SIZE": "60",
}


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


def load_env_into_os(path: Path) -> None:
    for key, value in read_env_file(path).items():
        if key not in os.environ:
            os.environ[key] = value


def effective_runtime_from_env(static_csv_dir: Path, env_map: dict[str, str]) -> RuntimeConfig:
    merged = dict(ENV_DEFAULTS)
    merged.update(env_map)
    return RuntimeConfig(
        db_path=os.getenv("BLACKBOX_DB_PATH", "instance/blackbox.db"),
        modbus_port=merged["MODBUS_PORT"],
        modbus_slave=int(merged["MODBUS_SLAVE"]),
        modbus_baudrate=int(merged["MODBUS_BAUDRATE"]),
        modbus_timeout=float(merged["MODBUS_TIMEOUT"]),
        modbus_interval=float(merged["MODBUS_INTERVAL"]),
        address_offset=int(merged["MODBUS_ADDRESS_OFFSET"]),
        ram_batch_size=int(merged["RAM_BATCH_SIZE"]),
        static_csv_dir=static_csv_dir,
    )


def validate_parser_json(text: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        cfg = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"JSON parse error: {exc}"
    if not isinstance(cfg, dict):
        return None, "Parser settings must be a JSON object"
    if not isinstance(cfg.get("requests"), list) or not cfg.get("requests"):
        return None, "Field 'requests' must be a non-empty list"
    if not isinstance(cfg.get("fields"), list) or not cfg.get("fields"):
        return None, "Field 'fields' must be a non-empty list"
    return cfg, None


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
        source_values: dict[str, list[Any]] = {}
        for req in parser_cfg.get("requests", []):
            name = str(req["name"])
            fc = int(req["fc"])
            address = int(req["address"])
            count = int(req["count"])
            if fc == 3:
                source_values[name] = list(instrument.read_registers(address, count))
            elif fc == 1:
                source_values[name] = [bool(v) for v in instrument.read_bits(address, count, functioncode=1)]
            else:
                return False, f"Unsupported function code in settings: {fc}"
        _ = parse_fields(parser_cfg, source_values)
        return True, "Проверка пройдена: чтение и парсинг успешны."
    except Exception as exc:
        return False, f"Проверка не пройдена: {type(exc).__name__}: {exc}"
