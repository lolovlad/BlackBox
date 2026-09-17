"""Validated per-VM reader, storage and buffering configuration.

The legacy application kept most of these values in environment variables and
``settings/app_runtime.json``.  vNext stores the same knobs with the VM, so two
workers can use different devices, poll rates and storage targets.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ReaderSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    poll_interval_sec: float = Field(default=0.12, gt=0.01, le=3600)
    timeout_sec: float = Field(default=0.35, gt=0.01, le=60)
    retries: int = Field(default=3, ge=1, le=20)
    retry_delay_sec: float = Field(default=0.2, ge=0, le=60)
    address_offset: int = Field(default=1, ge=0, le=10000)
    enabled: bool = True
    # Legacy reader opened/closed the serial handle for every transaction to
    # recover cleanly from RS-485 contention. Keep that safe default per VM.
    close_port_after_each_call: bool = True
    clear_buffers_before_each_transaction: bool = True

    # Serial/RTU settings (kept harmless for other protocols).
    port: str = Field(default="/dev/ttyAMA0", min_length=1, max_length=255)
    slave_id: int = Field(default=1, ge=1, le=247)
    baudrate: int = Field(default=9600, ge=300, le=4_000_000)
    bytesize: int = Field(default=8, ge=5, le=8)
    parity: str = Field(default="N", min_length=1, max_length=1)
    stopbits: float = Field(default=1, ge=1, le=2)
    mode: str = Field(default="rtu", max_length=16)

    # TCP settings.
    host: str = Field(default="127.0.0.1", min_length=1, max_length=255)
    tcp_port: int = Field(default=502, ge=1, le=65535)
    unit_id: int = Field(default=1, ge=0, le=247)

    @field_validator("parity")
    @classmethod
    def normalize_parity(cls, value: str) -> str:
        value = value.strip().upper()
        if value not in {"N", "E", "O"}:
            raise ValueError("parity must be N, E or O")
        return value

    @field_validator("mode")
    @classmethod
    def normalize_mode(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"rtu", "ascii"}:
            raise ValueError("mode must be rtu or ascii")
        return value


class StorageSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    # Resource ID is resolved by Hub; paths supplied by a browser are ignored.
    target_resource_id: str = Field(default="storage:data", min_length=1, max_length=255)
    flush_seconds: float = Field(default=5.0, gt=0.1, le=3600)
    min_free_bytes: int = Field(default=64 * 1024 * 1024, ge=0)
    quota_bytes: int | None = Field(default=None, ge=1)
    telemetry_subdir: str = Field(default="telemetry", min_length=1, max_length=64)
    # The vNext writer is intentionally Parquet/ZSTD only.  The legacy
    # directory names are retained as metadata so an imported configuration is
    # not silently discarded; actual paths still come from the approved
    # storage resource, never from browser input.
    data_format: Literal["parquet"] = "parquet"
    compression: Literal["zstd"] = "zstd"
    legacy_data_format: str | None = None
    alarm_subdir: str = Field(default="alarms", min_length=1, max_length=64)
    backup_subdir: str = Field(default="backup", min_length=1, max_length=64)
    log_subdir: str = Field(default="logs", min_length=1, max_length=64)
    overwrite_data: bool = False
    overwrite_alarms: bool = False
    fsync_on_write: bool = True
    enable_backup_storage: bool = False
    legacy_data_directory: str | None = None
    legacy_alarm_directory: str | None = None
    legacy_backup_directory: str | None = None
    legacy_log_directory: str | None = None

    @field_validator("telemetry_subdir", "alarm_subdir", "backup_subdir", "log_subdir")
    @classmethod
    def safe_relative_subdir(cls, value: str) -> str:
        raw = str(value).replace("\\", "/").strip()
        path = PurePosixPath(raw)
        normalized = raw.strip("/")
        first_part = path.parts[0] if path.parts else ""
        if not normalized or raw.startswith("/") or path.is_absolute() or ".." in path.parts or ":" in first_part:
            raise ValueError("storage subdirectories must stay below the approved storage root")
        return normalized


class BufferSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    # Legacy RAM_BATCH_SIZE equivalent.  It is a row threshold, not a disk
    # write for every sample, which protects flash/SSD media.
    ram_rows: int = Field(default=60, ge=1, le=1_000_000)
    max_queue: int = Field(default=2048, ge=1, le=1_000_000)


class VmRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    reader: ReaderSettings = Field(default_factory=ReaderSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    buffer: BufferSettings = Field(default_factory=BufferSettings)
    # Settings that belonged to the legacy DataLogger but do not change the
    # protocol worker (alarm rules, CSV presentation, timezone, etc.) remain
    # attached to the VM for an auditable migration instead of being dropped.
    legacy: dict[str, Any] = Field(default_factory=dict)


def _first(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None and mapping[key] != "":
            return mapping[key]
    return default


def _boolish(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def normalize_runtime_config(config: dict[str, Any] | None, *, protocol: str | None = None) -> dict[str, Any]:
    """Normalize both legacy flat settings and the vNext nested shape.

    Accepted legacy aliases include ``MODBUS_PORT``, ``MODBUS_INTERVAL``,
    ``modbus_poll_interval`` and ``RAM_BATCH_SIZE``.  Unknown keys are retained
    under their respective section for protocol-specific extensions.
    """

    raw = dict(config or {})
    reader_raw = raw.get("reader")
    storage_raw = raw.get("storage")
    buffer_raw = raw.get("buffer")
    reader = dict(reader_raw) if isinstance(reader_raw, dict) else {}
    storage = dict(storage_raw) if isinstance(storage_raw, dict) else {}
    buffer = dict(buffer_raw) if isinstance(buffer_raw, dict) else {}
    # ``DataLoggerConfig.modbus_reader_config`` was the legacy extension point
    # for serial parameters. Keep it as a low-priority source: explicit vNext
    # reader values always win.
    legacy_reader = raw.get("modbus_reader_config")
    if isinstance(legacy_reader, dict):
        reader = {**legacy_reader, **reader}

    aliases = {
        "poll_interval_sec": ("poll_interval_sec", "poll_interval", "analog_poll_interval", "modbus_poll_interval", "MODBUS_INTERVAL"),
        "timeout_sec": ("timeout_sec", "timeout", "modbus_timeout", "MODBUS_TIMEOUT"),
        "retries": ("retries", "retry_count", "retry-count"),
        "retry_delay_sec": ("retry_delay_sec", "retry_delay", "retry_delay_seconds"),
        "address_offset": ("address_offset", "modbus_address_offset", "MODBUS_ADDRESS_OFFSET"),
        "port": ("port", "modbus_port", "MODBUS_PORT"),
        "slave_id": ("slave_id", "slave", "modbus_slave", "MODBUS_SLAVE"),
        "baudrate": ("baudrate", "baud_rate", "modbus_baudrate", "MODBUS_BAUDRATE"),
        "bytesize": ("bytesize", "data_bits"),
        "parity": ("parity",),
        "stopbits": ("stopbits", "stop_bits"),
        "mode": ("mode",),
        "enabled": ("enabled",),
        "close_port_after_each_call": ("close_port_after_each_call",),
        "clear_buffers_before_each_transaction": ("clear_buffers_before_each_transaction",),
        "host": ("host", "ip", "ip_address", "address"),
        "tcp_port": ("tcp_port", "port_tcp", "modbus_tcp_port"),
        "unit_id": ("unit_id", "tcp_unit_id"),
        "legacy_parser_settings_path": ("parser_settings_path", "PARSER_SETTINGS_PATH"),
        "legacy_app_timezone": ("app_timezone", "APP_TIMEZONE"),
    }
    for canonical, keys in aliases.items():
        value = _first(raw, *keys, default=None)
        if value is not None and canonical not in reader:
            reader[canonical] = value

    storage_target = _first(
        raw,
        "storage_target_id",
        "storage_resource_id",
        "target_resource_id",
        default=None,
    )
    if storage_target is not None and "target_resource_id" not in storage:
        storage["target_resource_id"] = storage_target
    for canonical, keys in {
        "flush_seconds": ("flush_seconds", "storage_flush_seconds"),
        "min_free_bytes": ("min_free_bytes", "telemetry_min_free_bytes"),
        "quota_bytes": ("quota_bytes", "telemetry_quota_bytes"),
        "telemetry_subdir": ("telemetry_subdir",),
        "data_format": ("data_format",),
        "compression": ("compression",),
        "alarm_subdir": ("alarm_subdir",),
        "backup_subdir": ("backup_subdir",),
        "log_subdir": ("log_subdir",),
        "overwrite_data": ("overwrite_data",),
        "overwrite_alarms": ("overwrite_alarms",),
        "fsync_on_write": ("fsync_on_write",),
        "enable_backup_storage": ("enable_backup_storage",),
        "legacy_data_directory": ("data_directory",),
        "legacy_alarm_directory": ("alarm_directory",),
        "legacy_backup_directory": ("backup_directory",),
        "legacy_log_directory": ("log_directory",),
    }.items():
        value = _first(raw, *keys, default=None)
        if value is not None and canonical not in storage:
            storage[canonical] = value
    # ``DISABLE_MODBUS_COLLECTOR`` was the legacy process-wide switch.  A
    # disabled reader VM remains visible in the Hub but does not poll when a
    # worker honours this flag.
    if "disable_modbus_collector" in raw and "enabled" not in reader:
        reader["enabled"] = not _boolish(raw.get("disable_modbus_collector"))
    if "modbus_enabled" in raw and "enabled" not in reader:
        reader["enabled"] = _boolish(raw.get("modbus_enabled"))
    # Legacy DataLoggerConfig supported CSV/JSON/BINARY.  vNext deliberately
    # writes one durable format, so retain the requested legacy value for
    # visibility while normalizing the actual writer to Parquet/ZSTD.
    legacy_format = storage.get("data_format")
    if legacy_format is not None and str(legacy_format).lower() != "parquet":
        storage["legacy_data_format"] = str(legacy_format)
        storage["data_format"] = "parquet"
    for canonical, keys in {
        "ram_rows": ("ram_rows", "ram_batch_size", "RAM_BATCH_SIZE", "buffer_rows"),
        "max_queue": ("max_queue", "queue_size"),
    }.items():
        value = _first(raw, *keys, default=None)
        if value is not None and canonical not in buffer:
            buffer[canonical] = value

    # A TCP VM should not silently use a serial port in its visible config.
    if protocol == "modbus_tcp" and "host" not in reader:
        reader["host"] = "127.0.0.1"

    legacy = dict(raw.get("legacy")) if isinstance(raw.get("legacy"), dict) else {}
    legacy_keys = {
        "max_discrete_inputs",
        "analog_current_inputs",
        "analog_voltage_inputs",
        "discrete_poll_on_change",
        "alarm_conditions",
        "alarm_pre_time",
        "alarm_post_time",
        "csv_delimiter",
        "csv_include_timestamp",
        "csv_include_discrete",
        "csv_include_analog",
        "csv_column_order",
        "csv_column_names",
        "log_level",
        "log_to_console",
        "modbus_to_analog_map",
        "modbus_alarm_bits_to_discrete_map",
        "app_timezone",
        "parser_settings_path",
        "video_match_window_minutes",
        "file_manager_url",
    }
    for key in legacy_keys:
        if key in raw and key not in legacy:
            legacy[key] = raw[key]
    normalized = VmRuntimeConfig.model_validate({"reader": reader, "storage": storage, "buffer": buffer, "legacy": legacy})
    return normalized.model_dump(mode="json")


__all__ = [
    "BufferSettings",
    "ReaderSettings",
    "StorageSettings",
    "VmRuntimeConfig",
    "normalize_runtime_config",
]
