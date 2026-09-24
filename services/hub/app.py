from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from bb_platform.contracts import AlarmEvent, MapDocument, Quality, RawBatch, ResourceKind, TagSample, VmCommand, VmLifecycle, VmProtocol, VmStatus, WorkerCommandAck, WorkerError, WorkerHeartbeat, WorkerRegister
from bb_platform.parser import adapt_legacy_map, diagnose_read, field_channel, parse_batch

from .config import HubConfig
from .connection import PROFILES, connection_profile, inventory_kinds
from .db import HubRepository
from .discovery import KIND_LABELS, PROTOCOL_RESOURCE_KIND, discover_resources, discovery_summary, is_usable_can_interface, is_usable_serial_port
from .docker_manager import DockerManager, DockerUnavailable
from .link import vm_link_status
from .monitor import collect_system_monitor, gpio_panel
from .probe import run_probe
from .registry import PROTOCOLS, protocol_spec
from .security import ACCESS_COOKIE, CSRF_COOKIE, REFRESH_COOKIE, _decode, csrf_protect, current_user, issue_tokens, set_auth_cookies
from .state import EventBus
from .storage import ParquetStore, StorageUnavailable, purge_vm_directories
from .telemetry import CHART_POINT_CAP, PAGE_SIZE, READ_ROW_CAP, chart_payload, collect_roots, column_defs, describe_sources, format_timestamp, page_table, parse_bound, query_measurements, query_window, rows_as_csv
from .vm_config import normalize_runtime_config

logger = logging.getLogger("blackbox.hub")

HUB_VERSION = "2.0.21"
HUB_VENDOR = "AGK"

PROTOCOL_LABELS = {
    "simulator": "Симулятор",
    "modbus_rtu": "Modbus RTU",
    "modbus_tcp": "Modbus TCP",
    "can": "CAN",
}
PROTOCOL_ICONS = {
    "simulator": "bi-cpu",
    "modbus_rtu": "bi-usb-plug",
    "modbus_tcp": "bi-ethernet",
    "can": "bi-broadcast",
}
LIFECYCLE_LABELS = {
    "pending": "Ожидает",
    "created": "Создана",
    "starting": "Запускается",
    "running": "Работает",
    "stopping": "Останавливается",
    "stopped": "Остановлена",
    "failed": "Ошибка",
    "unknown": "Неизвестно",
}
LIFECYCLE_LOG_MESSAGES = {
    "pending": "ВМ ожидает запуска",
    "created": "Контейнер создан",
    "starting": "Старт: контейнер запускается",
    "running": "Старт: ВМ запущена",
    "stopping": "Остановка: контейнер останавливается",
    "stopped": "ВМ остановлена",
    "failed": "Ошибка: ВМ не смогла работать",
    "unknown": "Состояние ВМ неизвестно",
}
OPERATOR_LOG_LABELS = {
    "start": "старт",
    "stop": "остановка",
    "restart": "перезапуск",
}
# Poll/protocol faults must not flip the VM to failed: the container is still
# running, and the next heartbeat would immediately record failed → running.
PROTOCOL_ERROR_CODES = {"read_failed", "reader_unavailable"}


class LoginRequest(BaseModel):
    username: str
    password: str


class VmCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = ""
    protocol: VmProtocol
    preset_id: str | None = None
    map_version: str = Field(min_length=1, max_length=128)
    # ``resources`` is retained as a wire-compatibility alias.  New clients
    # should use the explicit read/storage split.
    resources: list[dict[str, Any]] | None = None
    read_resources: list[dict[str, Any]] | None = None
    storage_resource_id: str | None = None
    limits: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)


class VmPatchRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    map_version: str | None = None
    preset_id: str | None = None
    resources: list[dict[str, Any]] | None = None
    read_resources: list[dict[str, Any]] | None = None
    storage_resource_id: str | None = None
    limits: dict[str, Any] | None = None
    config: dict[str, Any] | None = None


class MapUploadRequest(BaseModel):
    protocol: VmProtocol
    version: str = Field(min_length=1, max_length=128)
    preset_id: str | None = None
    document: dict[str, Any]


class ProbeRequest(BaseModel):
    protocol: VmProtocol
    map_version: str = Field(min_length=1, max_length=128)
    read_resources: list[dict[str, Any]] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    vm_id: str | None = None


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=8, max_length=1024)
    role: str = "user"


def _exc_message(exc: BaseException) -> str:
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        if errors:
            err = errors[0]
            loc = ".".join(str(part) for part in err.get("loc", ()) if str(part) not in {"__root__", "checksum_matches_document"})
            msg = str(err.get("msg") or exc)
            return f"{loc}: {msg}" if loc else msg
    return str(exc) or type(exc).__name__


def _problem(code: str, message: str, status_code: int, details: Any = None) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": code, "message": message, "details": details, "request_id": secrets.token_hex(8)})


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            dt = datetime.now(timezone.utc)
        else:
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_clock(value: Any) -> str:
    return _as_utc(value).strftime("%Y-%m-%d %H:%M:%S")


def _split_docker_line(line: str) -> tuple[datetime | None, str]:
    text = str(line or "")
    if len(text) > 20 and text[10:11] == "T":
        stamp, _, rest = text.partition(" ")
        try:
            return _as_utc(stamp), rest
        except ValueError:
            pass
    return None, text


def _looks_error(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(token in lowered for token in ("error", "exception", "traceback", "failed", "errno", "critical", "ошибка"))


def _journal_entry(timestamp: Any, source: str, line: str, *, level: str = "info", kind: str = "log") -> dict[str, Any]:
    dt = _as_utc(timestamp)
    return {
        "timestamp": dt.isoformat(),
        "clock": _format_clock(dt),
        "source": source,
        "level": "error" if level == "error" or _looks_error(line) else level,
        "kind": kind,
        "line": str(line or "").strip(),
    }


def _normalize_log_line(text: str) -> str:
    return " ".join(str(text or "").split())


def vm_connection(vm: dict[str, Any]) -> str:
    protocol = str(vm.get("protocol") or "")
    config = vm.get("config") if isinstance(vm.get("config"), dict) else {}
    reader = config.get("reader") if isinstance(config.get("reader"), dict) else {}
    resources = vm.get("read_resources") or vm.get("resources") or []
    paths = [str(item.get("path")) for item in resources if isinstance(item, dict) and item.get("path")]
    if protocol == "modbus_rtu":
        return str(reader.get("port") or (paths[0] if paths else "serial не выбран"))
    if protocol == "modbus_tcp":
        host = str(reader.get("host") or "").strip()
        port = reader.get("tcp_port") or 502
        return f"{host}:{port}" if host else "TCP не настроен"
    if protocol == "can":
        return str(reader.get("can_interface") or "CAN не выбран")
    return "без физического прибора"


def _compose_vm_journal(repo: HubRepository, bus: EventBus, docker_manager: DockerManager, vm: dict[str, Any], *, tail: int) -> dict[str, Any]:
    vm_id = str(vm["id"])
    cap = max(1, min(int(tail), 5000))
    entries: list[dict[str, Any]] = []
    error: str | None = None

    for event in reversed(repo.list_lifecycle_events(vm_id, limit=cap)):
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        to_state = str(event.get("event") or payload.get("to") or "")
        from_state = str(payload.get("from") or "")
        message = LIFECYCLE_LOG_MESSAGES.get(to_state, f"Состояние: {to_state or 'неизвестно'}")
        if from_state and to_state:
            message = f"{message} ({from_state} → {to_state})"
        level = "error" if to_state == VmLifecycle.FAILED.value else "info"
        entries.append(_journal_entry(event.get("created_at"), "lifecycle", message, level=level, kind="lifecycle"))

    try:
        docker_lines = docker_manager.logs(vm, tail=cap) if vm.get("container_id") else []
    except Exception as exc:
        docker_lines = []
        error = str(exc)

    docker_bodies: set[str] = set()
    for line in docker_lines:
        stamp, text = _split_docker_line(line)
        body = text or str(line)
        docker_bodies.add(_normalize_log_line(body))
        entries.append(_journal_entry(stamp or datetime.now(timezone.utc), "worker", body, kind="worker"))

    for item in bus.logs_for(vm_id, limit=cap):
        raw = str(item.get("line") or "")
        stamp, text = _split_docker_line(raw)
        body = text if stamp else raw
        if _normalize_log_line(body) in docker_bodies:
            continue
        source = "worker" if stamp else "hub"
        entries.append(
            _journal_entry(
                stamp or item.get("timestamp"),
                source,
                body,
                level=str(item.get("level") or "info"),
                kind="lifecycle" if body.startswith("Оператор:") else "log",
            )
        )

    last_error = str(vm.get("last_error") or "").strip()
    if last_error and not any(_normalize_log_line(last_error) in _normalize_log_line(item["line"]) for item in entries):
        entries.append(_journal_entry(vm.get("updated_at"), "hub", last_error, level="error", kind="error"))

    entries.sort(key=lambda item: item["timestamp"])
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in entries:
        key = (item["clock"], item["source"], _normalize_log_line(item["line"]))
        if not item["line"] or key in seen:
            continue
        seen.add(key)
        unique.append(item)
    unique = unique[-cap:]
    lines = [f"{item['clock']}  [{item['source']}]  {item['line']}" for item in unique]
    return {"entries": unique, "lines": lines, "error": error, "truncated": len(seen) > cap}


def _vm_json(vm: dict[str, Any]) -> dict[str, Any]:
    return vm


_is_usable_serial_port = is_usable_serial_port


def _discover_resources(data_root: Path, repo: HubRepository | None = None, *, probe_network: bool = False) -> list[dict[str, Any]]:
    """Physical inventory only. TCP is a typed endpoint, not a discovered node."""
    return discover_resources(data_root, include_tcp=False)


def _status_from_vm(vm: dict[str, Any], *, health: str = "unknown", heartbeat_at: datetime | None = None) -> VmStatus:
    now = datetime.now(timezone.utc)
    if heartbeat_at is None and vm.get("heartbeat_at"):
        try:
            heartbeat_at = datetime.fromisoformat(vm["heartbeat_at"])
        except (TypeError, ValueError):
            heartbeat_at = None
    normalized_health = health if health in {"healthy", "unhealthy", "unknown"} else "unknown"
    return VmStatus(vm_id=UUID(vm["id"]), lifecycle=VmLifecycle(vm.get("lifecycle", "unknown")), health=normalized_health, container_id=vm.get("container_id"), heartbeat_at=heartbeat_at, map_version=vm.get("map_version"), config_revision=int(vm.get("config_revision", 0)), last_error=vm.get("last_error"), updated_at=now)


def _approved_resources(repo: HubRepository, resources: list[dict[str, Any]], *, exclude_vm_id: str | None = None) -> list[dict[str, Any]]:
    """Resolve user input to discovered resource metadata.

    Paths supplied by a browser are never trusted; the Docker manager receives
    only the path returned by a resource that an administrator approved.
    """
    resolved: list[dict[str, Any]] = []
    ids: list[str] = []
    for item in resources:
        if not isinstance(item, dict) or not item.get("resource_id"):
            raise HTTPException(422, detail={"code": "resource_id_required", "message": "Each resource needs a resource_id"})
        resource_id = str(item["resource_id"])
        descriptor = repo.resource_by_id(resource_id)
        if descriptor is None or not descriptor["approved"] or not descriptor["available"]:
            raise HTTPException(409, detail={"code": "resource_not_approved", "message": "Resource must be discovered and approved before use", "details": [resource_id]})
        if descriptor["kind"] == ResourceKind.STORAGE.value:
            raise HTTPException(422, detail={"code": "storage_resource_in_read_list", "message": "Storage targets belong in storage_resource_id, not read_resources"})
        ids.append(resource_id)
        resolved.append({"resource_id": resource_id, "kind": descriptor["kind"], "name": descriptor["name"], "path": descriptor.get("path"), "address": descriptor.get("address"), "metadata": descriptor.get("metadata", {})})
    conflicts = repo.resource_conflicts(ids, exclude_vm_id=exclude_vm_id)
    if conflicts:
        raise HTTPException(409, detail={"code": "resource_conflict", "message": "Resource is unavailable or already leased", "details": conflicts})
    return resolved


def _bind_read_resources(repo: HubRepository, resources: list[dict[str, Any]], account_id: int | None, *, exclude_vm_id: str | None = None) -> list[dict[str, Any]]:
    """Approve a discovered device when an admin selects it for a VM."""
    for item in resources or []:
        if not isinstance(item, dict) or not item.get("resource_id"):
            continue
        resource_id = str(item["resource_id"])
        descriptor = repo.resource_by_id(resource_id)
        if descriptor and descriptor.get("available") and not descriptor.get("approved"):
            repo.approve_resource(resource_id, account_id)
    return _approved_resources(repo, resources or [], exclude_vm_id=exclude_vm_id)


def _bind_protocol_read_resources(
    repo: HubRepository,
    protocol: str,
    resources: list[dict[str, Any]],
    account_id: int | None,
    *,
    exclude_vm_id: str | None = None,
) -> list[dict[str, Any]]:
    if not connection_profile(protocol).requires_resource:
        return []
    return _bind_read_resources(repo, resources, account_id, exclude_vm_id=exclude_vm_id)


def _candidates_payload(repo: HubRepository) -> dict[str, list[dict[str, Any]]]:
    leases = repo.list_resource_leases()
    names = {str(vm["id"]): vm.get("name") or vm["id"] for vm in repo.list_vms()}
    grouped: dict[str, list[dict[str, Any]]] = {
        protocol: [] for protocol, profile in PROFILES.items() if profile.discovers
    }
    kind_to_protocol = {kind: protocol for protocol, kind in PROTOCOL_RESOURCE_KIND.items()}
    for item in repo.list_resources():
        if not item.get("available"):
            continue
        protocol = kind_to_protocol.get(str(item.get("kind") or ""))
        if protocol not in grouped:
            continue
        leased = leases.get(str(item["resource_id"]))
        grouped[protocol].append(
            {
                "resource_id": item["resource_id"],
                "kind": item["kind"],
                "name": item["name"],
                "path": item.get("path"),
                "address": item.get("address"),
                "approved": bool(item.get("approved")),
                "available": True,
                "metadata": item.get("metadata") or {},
                "leased_by": leased,
                "leased_name": names.get(leased) if leased else None,
            }
        )
    return grouped


def _approved_storage_resource(repo: HubRepository, resource_id: str | None) -> str:
    resource_id = resource_id or "storage:data"
    descriptor = repo.resource_by_id(resource_id)
    if descriptor is None or descriptor["kind"] != ResourceKind.STORAGE.value or not descriptor["approved"] or not descriptor["available"]:
        raise HTTPException(
            409,
            detail={
                "code": "storage_not_approved",
                "message": "Storage target must be discovered and approved before use",
                "details": [resource_id],
            },
        )
    return resource_id


def _vm_storage_roots(vm: dict[str, Any], repo: HubRepository, cfg: HubConfig) -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()

    def add(path: Path | str | None) -> None:
        if not path:
            return
        try:
            resolved = Path(path).resolve()
        except OSError:
            return
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            roots.append(resolved)

    add(cfg.data_root)
    config = vm.get("config") if isinstance(vm.get("config"), dict) else {}
    storage_cfg = config.get("storage") if isinstance(config.get("storage"), dict) else {}
    target_id = storage_cfg.get("target_resource_id") or vm.get("storage_resource_id")
    if target_id:
        descriptor = repo.resource_by_id(str(target_id))
        if descriptor:
            add(descriptor.get("path"))
    for resource in repo.list_resources():
        if resource.get("kind") == ResourceKind.STORAGE.value:
            add(resource.get("path"))
    return roots


def _vm_storage_subdirs(vm: dict[str, Any]) -> list[str]:
    config = vm.get("config") if isinstance(vm.get("config"), dict) else {}
    storage_cfg = config.get("storage") if isinstance(config.get("storage"), dict) else {}
    return [
        str(storage_cfg.get("telemetry_subdir") or "telemetry"),
        str(storage_cfg.get("alarm_subdir") or "alarms"),
        str(storage_cfg.get("backup_subdir") or "backup"),
        str(storage_cfg.get("log_subdir") or "logs"),
    ]


def _validate_reader_allowlist(repo: HubRepository, protocol: str, runtime_config: dict[str, Any], resources: list[dict[str, Any]]) -> None:
    """Bind the physical source the protocol actually uses.

    Serial and CAN lease a discovered node. TCP is a host:port the operator
    types in; there is no inventory of “TCP devices”.
    """
    profile = connection_profile(protocol)
    reader = runtime_config.setdefault("reader", {}) if isinstance(runtime_config, dict) else {}
    if protocol == VmProtocol.SIMULATOR.value:
        return
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for item in resources:
        if isinstance(item, dict) and item.get("kind"):
            by_kind.setdefault(str(item["kind"]), []).append(item)

    if profile.link == "serial":
        serials = by_kind.get(ResourceKind.SERIAL.value, [])
        path = str(serials[0].get("path") or "") if serials else ""
        if not path or not _is_usable_serial_port(path):
            raise HTTPException(
                409,
                detail={
                    "code": "read_resource_required",
                    "message": "Нужен UART вроде /dev/ttyAMA0, /dev/ttyUSB0 или /dev/serial0. /dev/tty — это не порт прибора.",
                },
            )
        reader["port"] = path
        return
    if profile.link == "network":
        host = str(reader.get("host") or "").strip()
        if not host:
            raise HTTPException(422, detail={"code": "tcp_endpoint_required", "message": "Укажите IP или hostname прибора"})
        try:
            port = int(reader.get("tcp_port") or 502)
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, detail={"code": "tcp_endpoint_required", "message": "Укажите TCP-порт прибора"}) from exc
        if port < 1 or port > 65535:
            raise HTTPException(422, detail={"code": "tcp_endpoint_required", "message": "TCP-порт должен быть от 1 до 65535"})
        reader["host"] = host
        reader["tcp_port"] = port
        return
    if profile.link == "can":
        cans = by_kind.get(ResourceKind.CAN.value, [])
        iface = str((cans[0].get("name") if cans else "") or reader.get("can_interface") or "")
        if not cans or not iface or not is_usable_can_interface(iface):
            raise HTTPException(409, detail={"code": "read_resource_required", "message": "Нужен интерфейс вроде can0. Сначала найдите его в форме ВМ."})
        reader["can_interface"] = iface


def _approved_resource_groups(repo: HubRepository) -> dict[str, list[dict[str, Any]]]:
    candidates = _candidates_payload(repo)
    storage = [
        item
        for item in repo.list_resources()
        if item.get("kind") == ResourceKind.STORAGE.value and item.get("approved") and item.get("available")
    ]
    return {
        "serial_resources": candidates.get("modbus_rtu", []),
        "tcp_resources": [],
        "can_resources": candidates.get("can", []),
        "gpio_resources": candidates.get("gpio", []),
        "storage_resources": storage,
    }


def _resource_ids(resources: list[dict[str, Any]] | None) -> list[str]:
    return [str(item["resource_id"]) for item in (resources or []) if isinstance(item, dict) and item.get("resource_id")]


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge a partial API config without resetting unrelated VM settings."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def create_app(config: HubConfig | None = None, *, docker_client: Any = None) -> FastAPI:
    cfg = config or HubConfig.from_env()
    repo = HubRepository(cfg.db_path)
    repo.bootstrap_admin(cfg.bootstrap_username, cfg.bootstrap_password)
    # The Hub's own metadata/telemetry disk is the safe default destination;
    # unlike removable devices it is available as soon as the service starts.
    repo.upsert_resources(_discover_resources(cfg.data_root, repo, probe_network=False))
    if repo.resource_by_id("storage:data"):
        repo.approve_resource("storage:data", None)
    bus = EventBus()
    store = ParquetStore(cfg.data_root / "telemetry", min_free_bytes=cfg.telemetry_min_free_bytes, quota_bytes=cfg.telemetry_quota_bytes)
    docker_manager = DockerManager(docker_client, enabled=cfg.docker_enabled)

    def _replace_worker_container(vm: dict[str, Any], token: str) -> dict[str, Any]:
        """Recreate the worker so Docker --device matches the current UART/GPIO."""
        vm_id = str(vm["id"])
        if vm.get("container_id"):
            try:
                docker_manager.remove(vm)
            except Exception as exc:
                if not DockerManager.is_not_found(exc):
                    raise
            vm = repo.update_vm(vm_id, {"container_id": None}) or vm
        created = docker_manager.create(vm, token)
        return repo.update_vm(vm_id, {"container_id": created["container_id"], "lifecycle": created["lifecycle"]}) or vm
    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "ui" / "templates"))
    templates.env.globals["app_version"] = HUB_VERSION
    templates.env.globals["app_vendor"] = HUB_VENDOR
    templates.env.globals["kind_labels"] = KIND_LABELS
    templates.env.globals["protocol_labels"] = PROTOCOL_LABELS
    templates.env.globals["protocol_icons"] = PROTOCOL_ICONS
    templates.env.globals["lifecycle_labels"] = LIFECYCLE_LABELS
    templates.env.globals["vm_connection"] = vm_connection
    templates.env.globals["connection_profiles"] = {key: profile.as_dict() for key, profile in PROFILES.items()}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.repo = repo
        app.state.cfg = cfg
        app.state.bus = bus
        app.state.store = store
        app.state.docker = docker_manager
        app.state.worker_tokens = {}
        app.state.worker_commands = {}
        app.state.log_seen = {}
        # Keep buffers independent per VM, even when several VMs share one
        # approved SSD. ``app.state.store`` remains as a compatibility handle
        # for integrations that used the original default store.
        app.state.storage_stores = {}
        app.state.ingest_pending = {}
        app.state.ingest_queue = asyncio.Queue(maxsize=max(1, cfg.queue_size))
        app.state.vm_alerts = {}
        stop_ingest = asyncio.Event()

        def store_for_vm(vm: dict[str, Any], runtime_config: dict[str, Any]) -> ParquetStore:
            storage_cfg = runtime_config.get("storage", {}) if isinstance(runtime_config, dict) else {}
            target_id = str(storage_cfg.get("target_resource_id") or vm.get("storage_resource_id") or "storage:data")
            descriptor = repo.resource_by_id(target_id)
            if descriptor is None or descriptor.get("kind") != ResourceKind.STORAGE.value or not descriptor.get("approved") or not descriptor.get("available"):
                raise StorageUnavailable(f"Storage target is not approved: {target_id}")
            base = Path(descriptor.get("path") or cfg.data_root).resolve()
            subdir = str(storage_cfg.get("telemetry_subdir") or "telemetry")
            root = (base / subdir).resolve()
            try:
                root.relative_to(base)
            except ValueError as exc:
                raise StorageUnavailable("Telemetry directory escapes the approved storage root") from exc
            key = f"{root}|{vm['id']}"
            existing = app.state.storage_stores.get(key)
            if existing is None:
                existing = ParquetStore(
                    root,
                    min_free_bytes=int(storage_cfg.get("min_free_bytes", cfg.telemetry_min_free_bytes)),
                    quota_bytes=storage_cfg.get("quota_bytes") or cfg.telemetry_quota_bytes,
                )
                app.state.storage_stores[key] = existing
            return existing

        async def process_batch(batch: RawBatch) -> dict[str, Any]:
            idempotency_key = repo.ingest_idempotency_key(str(batch.batch_id), str(batch.vm_id), batch.seq_start)
            with repo.connect() as c:
                exists = c.execute("SELECT 1 FROM ingest_batches WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if exists:
                return {"ok": True, "duplicate": True, "count": 0}
            document_payload = repo.map_by_version(batch.map_version, getattr(batch.protocol, "value", batch.protocol))
            if document_payload is None:
                return {"error": ("map_not_found", "Map version not found", 422)}
            if not repo.claim_ingest_batch(str(batch.batch_id), str(batch.vm_id), batch.seq_start):
                return {"ok": True, "duplicate": True, "count": 0}
            try:
                vm = repo.get_vm(str(batch.vm_id))
                if vm is None:
                    repo.release_ingest_batch(str(batch.batch_id), str(batch.vm_id), batch.seq_start)
                    return {"error": ("vm_not_found", "VM not found", 404)}
                runtime_config = normalize_runtime_config(vm.get("config", {}), protocol=vm.get("protocol"))
                parsed = parse_batch(batch, MapDocument(**document_payload))
                runtime_buffer = runtime_config.get("buffer", {})
                runtime_storage = runtime_config.get("storage", {})
                store_for_vm(vm, runtime_config).append(
                    parsed,
                    flush_rows=int(runtime_buffer.get("ram_rows", 60)),
                    flush_seconds=float(runtime_storage.get("flush_seconds", 5.0)),
                )
            except StorageUnavailable as exc:
                repo.release_ingest_batch(str(batch.batch_id), str(batch.vm_id), batch.seq_start)
                alarm = AlarmEvent(
                    vm_id=batch.vm_id,
                    severity="critical",
                    code="storage_unavailable",
                    message=str(exc),
                    active=True,
                    payload={"storage_resource_id": vm.get("storage_resource_id") if "vm" in locals() and vm else None},
                )
                await bus.publish("alarms", alarm.model_dump(mode="json"))
                await bus.publish_log(str(batch.vm_id), str(exc), level="error")
                return {"error": ("storage_unavailable", str(exc), 503)}
            except ValueError as exc:
                repo.release_ingest_batch(str(batch.batch_id), str(batch.vm_id), batch.seq_start)
                return {"error": ("invalid_batch", str(exc), 422)}
            except Exception:
                repo.release_ingest_batch(str(batch.batch_id), str(batch.vm_id), batch.seq_start)
                raise
            for sample in parsed:
                await bus.publish_tags(sample)
                quality_value = getattr(sample.quality, "value", sample.quality)
                if quality_value == Quality.BAD.value:
                    continue
                vm_key = str(sample.vm_id)
                alert_names = {str(item).strip() for item in sample.alerts if str(item).strip()}
                for event in repo.sync_alarm_edges(vm_key, sample.captured_at, alert_names, kind="alert"):
                    await _publish_alarm_edge(sample, event)
                protocol_value = getattr(sample.protocol, "value", sample.protocol)
                if protocol_value == VmProtocol.GPIO.value:
                    pins = {str(name) for name, value in sample.discrete.items() if value}
                    for event in repo.sync_alarm_edges(vm_key, sample.captured_at, pins, kind="gpio"):
                        await _publish_alarm_edge(sample, event)
                app.state.vm_alerts[vm_key] = sorted(alert_names)
            last_quality = parsed[-1].quality if parsed else None
            if parsed and last_quality == Quality.GOOD:
                current = repo.get_vm(str(batch.vm_id))
                if current and current.get("last_error"):
                    cleared = repo.update_vm(str(batch.vm_id), {"last_error": None})
                    if cleared:
                        await bus.publish_status(_status_from_vm(cleared, health="healthy"))
            return {"ok": True, "duplicate": False, "count": len(parsed), "last_seq": parsed[-1].seq}

        async def ingest_loop() -> None:
            while not stop_ingest.is_set() or not app.state.ingest_queue.empty():
                try:
                    batch, future = await asyncio.wait_for(app.state.ingest_queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                try:
                    result = await process_batch(batch)
                    if not future.done():
                        future.set_result(result)
                except Exception as exc:
                    if not future.done():
                        future.set_result({"error": ("ingest_failed", str(exc), 500)})
                finally:
                    vm_key = str(batch.vm_id)
                    app.state.ingest_pending[vm_key] = max(0, app.state.ingest_pending.get(vm_key, 1) - 1)
                    app.state.ingest_queue.task_done()

        async def _publish_alarm_edge(sample: TagSample, event: dict[str, Any]) -> None:
            started = event["state"] == "active"
            kind = str(event.get("kind") or "alert")
            await bus.publish(
                "alarms",
                AlarmEvent(
                    vm_id=sample.vm_id,
                    timestamp=sample.captured_at,
                    severity="warning" if started else "info",
                    code="gpio_alert" if kind == "gpio" else "device_alert",
                    message=str(event["name"]),
                    active=started,
                    payload={"kind": kind, "state": event["state"]},
                ).model_dump(mode="json"),
            )
            vm_key = str(sample.vm_id)
            if kind == "gpio":
                text = f"GPIO активно: {event['name']}" if started else f"GPIO снято: {event['name']}"
            else:
                text = f"Алерт прибора: {event['name']}" if started else f"Алерт снят: {event['name']}"
            await bus.publish_log(vm_key, text, level="error" if started else "info")

        ingest_task = asyncio.create_task(ingest_loop())
        stop_reconciler = asyncio.Event()
        stop_system = asyncio.Event()

        def _system_payload() -> dict[str, Any]:
            payload = collect_system_monitor(cfg.data_root)
            vms = repo.list_vms()
            latest = {str(vm["id"]): sample for vm in vms if (sample := bus.latest_tags(str(vm["id"]))) is not None}
            gpio = gpio_panel(vms, latest)
            payload["gpio_items"] = gpio["items"]
            payload["gpio_time"] = gpio["updated_at"]
            payload["server_time"] = datetime.now().astimezone().strftime("%d.%m.%Y %H:%M:%S")
            return payload

        async def system_loop() -> None:
            while not stop_system.is_set():
                try:
                    await bus.publish("system", _system_payload())
                except Exception:
                    logger.exception("system monitor publish failed")
                try:
                    await asyncio.wait_for(stop_system.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass

        system_task = asyncio.create_task(system_loop())

        async def reconcile() -> None:
            while not stop_reconciler.is_set():
                if docker_manager.client is not None:
                    for vm in repo.list_vms():
                        try:
                            token = app.state.worker_tokens.get(vm["id"])
                            if not token and vm.get("container_id"):
                                try:
                                    token = await asyncio.to_thread(docker_manager.bootstrap_token, vm)
                                except Exception:
                                    token = None
                            if token:
                                app.state.worker_tokens[vm["id"]] = token
                            # A container may disappear after a manual Docker
                            # cleanup.  Desired state remains authoritative, so
                            # recreate it before attempting to start it.
                            if not vm.get("container_id"):
                                if vm.get("desired_state") != "running":
                                    repo.release_resource_leases(vm["id"])
                                    if vm.get("lifecycle") != VmLifecycle.STOPPED.value or vm.get("last_error"):
                                        updated = repo.update_vm(vm["id"], {"lifecycle": VmLifecycle.STOPPED.value, "last_error": None})
                                        if updated:
                                            await bus.publish_status(_status_from_vm(updated))
                                    continue
                                initial_resource_ids = _resource_ids(vm.get("read_resources", vm.get("resources", [])))
                                conflicts = repo.resource_conflicts(initial_resource_ids, exclude_vm_id=vm["id"])
                                if not conflicts:
                                    conflicts = repo.acquire_resource_leases(vm["id"], initial_resource_ids)
                                if conflicts:
                                    updated = repo.update_vm(
                                        vm["id"],
                                        {
                                            "lifecycle": VmLifecycle.FAILED.value,
                                            "last_error": f"Resource conflict: {', '.join(conflicts)}",
                                        },
                                    )
                                    if updated:
                                        await bus.publish_status(_status_from_vm(updated, health="unhealthy"))
                                    continue
                                token = app.state.worker_tokens.setdefault(vm["id"], secrets.token_urlsafe(32))
                                created = await asyncio.to_thread(docker_manager.create, vm, token)
                                vm = repo.update_vm(vm["id"], {"container_id": created["container_id"], "lifecycle": created["lifecycle"], "last_error": None}) or vm
                            inspected = await asyncio.to_thread(docker_manager.inspect, vm)
                            if vm.get("container_id"):
                                try:
                                    lines = await asyncio.to_thread(docker_manager.logs, vm, tail=500)
                                    seen = app.state.log_seen.setdefault(vm["id"], set())
                                    for line in lines:
                                        if line not in seen:
                                            await bus.publish_log(vm["id"], line)
                                    app.state.log_seen[vm["id"]] = set(lines[-500:])
                                except Exception:
                                    pass
                            desired = vm.get("desired_state", "stopped")
                            actual = inspected.get("lifecycle")
                            resource_ids = _resource_ids(vm.get("read_resources", vm.get("resources", [])))
                            if desired == "running":
                                conflicts = repo.resource_conflicts(resource_ids, exclude_vm_id=vm["id"])
                                if not conflicts:
                                    conflicts = repo.acquire_resource_leases(vm["id"], resource_ids)
                                if conflicts:
                                    updated = repo.update_vm(
                                        vm["id"],
                                        {
                                            "lifecycle": VmLifecycle.FAILED.value,
                                            "last_error": f"Resource conflict: {', '.join(conflicts)}",
                                        },
                                    )
                                    if updated:
                                        await bus.publish_status(_status_from_vm(updated, health="unhealthy"))
                                    continue
                            else:
                                repo.release_resource_leases(vm["id"])
                            if desired == "running" and actual in {VmLifecycle.CREATED.value, VmLifecycle.STOPPED.value, VmLifecycle.FAILED.value, VmLifecycle.UNKNOWN.value}:
                                await asyncio.to_thread(docker_manager.start, vm)
                                inspected = await asyncio.to_thread(docker_manager.inspect, vm)
                                actual = inspected.get("lifecycle")
                            elif desired == "stopped" and actual == VmLifecycle.RUNNING.value:
                                await asyncio.to_thread(docker_manager.stop, vm)
                                inspected = await asyncio.to_thread(docker_manager.inspect, vm)
                                actual = inspected.get("lifecycle")
                            values: dict[str, Any] = {}
                            if actual != vm.get("lifecycle"):
                                values["lifecycle"] = actual
                            docker_error = inspected.get("error") or None
                            if docker_error and docker_error != vm.get("last_error"):
                                values["last_error"] = docker_error
                            if values:
                                updated = repo.update_vm(vm["id"], values)
                                if updated:
                                    await bus.publish_status(_status_from_vm(updated, health=inspected.get("health", "unknown")))
                        except Exception as exc:
                            if DockerManager.is_not_found(exc):
                                # Clear a stale id so the next reconciliation
                                # can recreate a desired-running worker.
                                if vm.get("desired_state") == "running":
                                    repo.update_vm(vm["id"], {"container_id": None, "lifecycle": VmLifecycle.PENDING.value, "last_error": str(exc)})
                                else:
                                    repo.release_resource_leases(vm["id"])
                                    updated = repo.update_vm(vm["id"], {"container_id": None, "lifecycle": VmLifecycle.STOPPED.value, "last_error": None})
                                    if updated:
                                        await bus.publish_status(_status_from_vm(updated))
                                continue
                            repo.release_resource_leases(vm["id"])
                            updated = repo.update_vm(vm["id"], {"lifecycle": VmLifecycle.UNKNOWN.value, "last_error": str(exc)})
                            if updated:
                                await bus.publish_status(_status_from_vm(updated))
                try:
                    await asyncio.wait_for(stop_reconciler.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass

        reconciler_task = asyncio.create_task(reconcile())
        yield
        stop_system.set()
        await system_task
        await app.state.ingest_queue.join()
        for target_store in app.state.storage_stores.values():
            try:
                target_store.flush()
            except StorageUnavailable:
                pass
        stop_ingest.set()
        await ingest_task
        stop_reconciler.set()
        await reconciler_task
        try:
            store.flush()
        except StorageUnavailable:
            pass

    app = FastAPI(title="BlackBox Hub", version=HUB_VERSION, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parents[2] / "ui" / "static")), name="static")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return _problem("validation_error", "Request validation failed", 422, exc.errors())

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        return _problem(str(detail.get("code", "http_error")), str(detail.get("message", "Request failed")), exc.status_code, detail.get("details"))

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return _problem("internal_error", "Internal server error", 500)

    def _publish_map_document(document_payload: dict[str, Any], *, protocol: VmProtocol, preset_id: str | None, version: str, account: dict[str, Any]) -> MapDocument:
        try:
            document = adapt_legacy_map(document_payload, protocol=protocol, preset_id=preset_id, version=version)
        except (ValueError, ValidationError, TypeError) as exc:
            raise HTTPException(422, detail={"code": "invalid_map", "message": _exc_message(exc)}) from exc
        try:
            repo.save_map(document.model_dump(mode="json"))
        except ValueError as exc:
            raise HTTPException(409, detail={"code": "map_immutable", "message": str(exc)}) from exc
        repo.record_audit(int(account["id"]), "map.publish", document.version, {"checksum": document.checksum})
        return document

    def admin(request: Request):
        account = current_user(request, repo, cfg)
        if account["role"] != "admin":
            raise HTTPException(403, detail={"code": "forbidden", "message": "Admin role required"})
        return account

    def user(request: Request):
        return current_user(request, repo, cfg)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "service": "hub"}

    @app.get("/api/v1/protocols")
    async def protocols(account=Depends(user)):
        return {
            "items": [
                {
                    "protocol": spec.protocol.value,
                    "enabled": spec.enabled,
                    "worker_kind": spec.worker_kind,
                    "description": spec.description,
                    "connection": connection_profile(spec.protocol).as_dict(),
                }
                for spec in PROTOCOLS
            ]
        }

    @app.get("/api/v1/connections")
    async def connections(account=Depends(user)):
        vms = repo.list_vms()
        items = await asyncio.gather(
            *[asyncio.to_thread(vm_link_status, vm, bus.latest_tags(str(vm["id"]))) for vm in vms]
        )
        return {"items": list(items)}

    @app.post("/api/v1/auth/login")
    async def login(payload: LoginRequest):
        account = repo.authenticate(payload.username.strip(), payload.password)
        if account is None:
            return _problem("invalid_credentials", "Invalid username or password", 401)
        access, refresh, _sid = issue_tokens(repo, cfg, account)
        out = JSONResponse({"ok": True, "user": {"id": account["id"], "username": account["username"], "role": account["role"]}})
        set_auth_cookies(out, access, refresh, secrets.token_urlsafe(24), secure=cfg.cookie_secure)
        return out

    @app.post("/api/v1/auth/refresh")
    async def refresh(request: Request):
        token = request.cookies.get(REFRESH_COOKIE)
        if not token:
            return _problem("auth_required", "Refresh token required", 401)
        payload = _decode(token, cfg, "refresh")
        sid = str(payload.get("sid", ""))
        if not sid or not repo.refresh_token_matches(sid, hashlib.sha256(token.encode()).hexdigest()):
            return _problem("invalid_token", "Refresh token revoked or expired", 401)
        session = repo.refresh_session(sid)
        if session is None:
            return _problem("invalid_token", "Refresh session not found", 401)
        repo.revoke_session(sid)
        access, new_refresh, _ = issue_tokens(repo, cfg, session)
        out = JSONResponse({"ok": True})
        set_auth_cookies(out, access, new_refresh, secrets.token_urlsafe(24), secure=cfg.cookie_secure)
        return out

    @app.post("/api/v1/auth/logout", dependencies=[Depends(csrf_protect)])
    async def logout(request: Request):
        token = request.cookies.get(REFRESH_COOKIE)
        if token:
            try:
                payload = _decode(token, cfg, "refresh")
                if payload.get("sid"):
                    repo.revoke_session(str(payload["sid"]))
            except HTTPException:
                pass
        out = JSONResponse({"ok": True})
        for name in (ACCESS_COOKIE, REFRESH_COOKIE, CSRF_COOKIE):
            out.delete_cookie(name)
        return out

    @app.get("/api/v1/auth/me")
    async def me(account=Depends(user)):
        return {"id": account["id"], "username": account["username"], "role": account["role"]}

    @app.get("/api/v1/vms")
    async def list_vms(account=Depends(user)):
        return {"items": [_vm_json(x) for x in repo.list_vms()]}

    @app.post("/api/v1/vms", dependencies=[Depends(csrf_protect)])
    async def create_vm(payload: VmCreateRequest, account=Depends(admin)):
        spec = protocol_spec(payload.protocol)
        if not spec.enabled:
            raise HTTPException(422, detail={"code": "protocol_unsupported", "message": spec.description})
        try:
            runtime_config = normalize_runtime_config(payload.config, protocol=payload.protocol.value)
        except ValidationError as exc:
            raise HTTPException(422, detail={"code": "invalid_vm_config", "message": "Invalid VM reader/storage configuration", "details": exc.errors()}) from exc
        if repo.map_by_version(payload.map_version, payload.protocol.value) is None:
            raise HTTPException(422, detail={"code": "map_not_found", "message": "Map version not found"})
        read_input = payload.read_resources if payload.read_resources is not None else (payload.resources or [])
        resources = _bind_protocol_read_resources(repo, payload.protocol.value, read_input, int(account["id"]))
        _validate_reader_allowlist(repo, payload.protocol.value, runtime_config, resources)
        storage_id = _approved_storage_resource(repo, payload.storage_resource_id or runtime_config["storage"]["target_resource_id"])
        runtime_config["storage"]["target_resource_id"] = storage_id
        image = {
            VmProtocol.SIMULATOR: cfg.worker_image_simulator,
            VmProtocol.MODBUS_RTU: cfg.worker_image_rtu,
            VmProtocol.MODBUS_TCP: cfg.worker_image_tcp,
        }.get(payload.protocol)
        if image is None:
            raise HTTPException(422, detail={"code": "protocol_unsupported", "message": "No worker image is configured for this protocol"})
        try:
            vm = repo.create_vm({
                "name": payload.name,
                "description": payload.description,
                "protocol": payload.protocol.value,
                "preset_id": payload.preset_id,
                "map_version": payload.map_version,
                "read_resources": resources,
                "storage_resource_id": storage_id,
                "limits": payload.limits,
                "config": runtime_config,
                "worker_image": image,
            })
        except Exception as exc:
            return _problem("vm_create_failed", str(exc), 409)
        repo.record_audit(int(account["id"]), "vm.create", vm["id"], {"protocol": vm["protocol"], "map_version": vm["map_version"]})
        await bus.publish_log(
            vm["id"],
            f"ВМ создана · {PROTOCOL_LABELS.get(vm['protocol'], vm['protocol'])} · карта {vm['map_version']}",
        )
        return vm

    @app.post("/api/v1/vms/probe", dependencies=[Depends(csrf_protect)])
    async def probe_vm(payload: ProbeRequest, account=Depends(admin)):
        try:
            runtime_config = normalize_runtime_config(payload.config, protocol=payload.protocol.value)
        except ValidationError as exc:
            raise HTTPException(422, detail={"code": "invalid_vm_config", "message": "Invalid VM reader/storage configuration", "details": exc.errors()}) from exc
        document_payload = repo.map_by_version(payload.map_version, payload.protocol.value)
        if document_payload is None:
            raise HTTPException(422, detail={"code": "map_not_found", "message": "Сначала выберите карту этого протокола"})
        profile = connection_profile(payload.protocol.value)
        if not profile.probe_read:
            raise HTTPException(422, detail={"code": "probe_unsupported", "message": "Для этого протокола нет тестового чтения"})
        reader = runtime_config.setdefault("reader", {})
        descriptor = None
        if profile.requires_resource:
            selected = payload.read_resources[0] if payload.read_resources else None
            if isinstance(selected, dict) and selected.get("resource_id"):
                descriptor = repo.resource_by_id(str(selected["resource_id"]))
                if descriptor is None or not descriptor.get("available"):
                    raise HTTPException(
                        409,
                        detail={"code": "resource_not_found", "message": f"Сначала нажмите «{profile.scan_label or 'Найти порты'}» и выберите устройство из списка"},
                    )
                if profile.resource_kind and descriptor.get("kind") != profile.resource_kind:
                    raise HTTPException(409, detail={"code": "resource_kind_mismatch", "message": "Выбранный ресурс не подходит для этого протокола"})
                if profile.link == "serial":
                    reader["port"] = descriptor.get("path")
                elif profile.link == "can":
                    reader["can_interface"] = descriptor.get("name")
                elif profile.link == "gpio":
                    reader["gpio_chip"] = descriptor.get("path") or descriptor.get("name")
                leases = repo.list_resource_leases()
                owner = leases.get(str(descriptor["resource_id"]))
                if owner and owner != str(payload.vm_id or ""):
                    owner_vm = repo.get_vm(owner) or {}
                    return {
                        "ok": False,
                        "quality": "bad",
                        "last_error": f"resource leased by {owner}",
                        "diagnosis": {
                            "code": "busy",
                            "title": "Устройство занято",
                            "detail": f"Сейчас его держит ВМ «{owner_vm.get('name') or owner}». Остановите её или выберите другое устройство.",
                            "link": "down",
                            "cause": "port",
                            "can_read_alerts": False,
                        },
                        "analog": [],
                        "discrete": [],
                        "alerts": [],
                        "active_alerts": [],
                        "device": {"resource_id": descriptor["resource_id"], "path": descriptor.get("path"), "leased_by": owner},
                    }
            if profile.link == "serial" and not str(reader.get("port") or "").strip():
                raise HTTPException(422, detail={"code": "read_resource_required", "message": f"Выберите serial-порт или нажмите «{profile.scan_label}»"})
            if profile.link == "can" and not str(reader.get("can_interface") or "").strip():
                raise HTTPException(422, detail={"code": "read_resource_required", "message": f"Выберите CAN-интерфейс или нажмите «{profile.scan_label}»"})
        elif profile.link == "network" and not str(reader.get("host") or "").strip():
            raise HTTPException(422, detail={"code": "tcp_endpoint_required", "message": "Укажите IP или hostname прибора"})
        result = await asyncio.to_thread(run_probe, protocol=payload.protocol.value, reader=reader, map_document=MapDocument(**document_payload))
        if descriptor:
            result["device"] = {**(result.get("device") or {}), "resource_id": descriptor["resource_id"], "name": descriptor.get("name")}
        repo.record_audit(int(account["id"]), "vm.probe", payload.vm_id, {"protocol": payload.protocol.value, "ok": result.get("ok"), "code": (result.get("diagnosis") or {}).get("code")})
        return result

    @app.get("/api/v1/vms/{vm_id}")
    async def get_vm(vm_id: str, account=Depends(user)):
        vm = repo.get_vm(vm_id)
        if vm is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "VM not found"})
        if docker_manager.client is not None and vm.get("container_id"):
            try:
                state = await asyncio.to_thread(docker_manager.inspect, vm)
                values: dict[str, Any] = {"container_id": state["container_id"]}
                if state.get("lifecycle"):
                    values["lifecycle"] = state["lifecycle"]
                if state.get("error"):
                    values["last_error"] = state["error"]
                vm = repo.update_vm(vm_id, values) or vm
            except Exception:
                pass
        return vm

    @app.patch("/api/v1/vms/{vm_id}", dependencies=[Depends(csrf_protect)])
    async def patch_vm(vm_id: str, payload: VmPatchRequest, account=Depends(admin)):
        current_vm = repo.get_vm(vm_id)
        if current_vm is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "VM not found"})
        values = payload.model_dump(exclude_none=True)
        if "resources" in values and "read_resources" not in values:
            values["read_resources"] = values.pop("resources")
        values.pop("resources", None)
        if "read_resources" in values:
            values["read_resources"] = _bind_protocol_read_resources(
                repo, current_vm["protocol"], values["read_resources"], int(account["id"]), exclude_vm_id=vm_id
            )
        elif not connection_profile(current_vm["protocol"]).requires_resource:
            values["read_resources"] = []
        if "map_version" in values and repo.map_by_version(values["map_version"], current_vm["protocol"] if current_vm else None) is None:
            raise HTTPException(422, detail={"code": "map_not_found", "message": "Map version not found"})
        if "config" in values or "storage_resource_id" in payload.model_dump(exclude_none=True):
            raw_config = current_vm.get("config", {})
            if "config" in values:
                incoming_config = values.get("config") or {}
                raw_config = _deep_merge(raw_config, incoming_config)
            try:
                runtime_config = normalize_runtime_config(raw_config, protocol=current_vm["protocol"])
            except ValidationError as exc:
                raise HTTPException(422, detail={"code": "invalid_vm_config", "message": "Invalid VM reader/storage configuration", "details": exc.errors()}) from exc
            requested_storage = payload.storage_resource_id or runtime_config["storage"]["target_resource_id"]
            storage_id = _approved_storage_resource(repo, requested_storage)
            runtime_config["storage"]["target_resource_id"] = storage_id
            values["config"] = runtime_config
            values["storage_resource_id"] = storage_id
        candidate_resources = values.get("read_resources", current_vm.get("read_resources", current_vm.get("resources", [])))
        try:
            candidate_config = normalize_runtime_config(values.get("config", current_vm.get("config", {})), protocol=current_vm["protocol"])
        except ValidationError as exc:
            raise HTTPException(422, detail={"code": "invalid_vm_config", "message": "Invalid VM reader/storage configuration", "details": exc.errors()}) from exc
        _validate_reader_allowlist(repo, current_vm["protocol"], candidate_config, candidate_resources)
        if "read_resources" in values or "config" in payload.model_dump(exclude_none=True):
            values["config"] = candidate_config
        # If a running VM is edited, update the physical-resource lease before
        # the worker receives its apply-map command.  ``acquire_resource_leases``
        # replaces the complete set atomically, releasing resources removed
        # from the configuration and reserving newly selected ones.
        running_before_update = current_vm.get("desired_state") == "running" or current_vm.get("lifecycle") in {
            VmLifecycle.STARTING.value,
            VmLifecycle.RUNNING.value,
            VmLifecycle.STOPPING.value,
        }
        if running_before_update and "read_resources" in values:
            conflicts = repo.acquire_resource_leases(vm_id, _resource_ids(values["read_resources"]))
            if conflicts:
                return _problem("resource_conflict", "Resource is already leased by another running VM", 409, conflicts)
        updated = repo.update_vm(vm_id, values)
        if updated and (updated.get("desired_state") == "running" or updated.get("lifecycle") in {
            VmLifecycle.STARTING.value,
            VmLifecycle.RUNNING.value,
        }):
            old_devices = DockerManager.device_mappings(current_vm)
            new_devices = DockerManager.device_mappings(updated)
            if old_devices != new_devices:
                token = app.state.worker_tokens.setdefault(vm_id, secrets.token_urlsafe(32))
                updated = _replace_worker_container(updated, token)
                docker_manager.start(updated)
                updated = repo.update_vm(
                    vm_id,
                    {"lifecycle": VmLifecycle.RUNNING.value, "desired_state": "running", "last_error": None},
                ) or updated
            elif any(key in values for key in {"config", "map_version", "read_resources", "storage_resource_id"}):
                app.state.worker_commands.setdefault(vm_id, []).append(
                    VmCommand(
                        vm_id=UUID(vm_id),
                        action="apply_map",
                        config_revision=updated["config_revision"],
                        map_version=updated["map_version"],
                    ).model_dump(mode="json")
                )
        repo.record_audit(int(account["id"]), "vm.update", vm_id, {"fields": sorted(values)})
        return updated

    @app.delete("/api/v1/vms/{vm_id}", dependencies=[Depends(csrf_protect)])
    async def delete_vm(vm_id: str, account=Depends(admin)):
        vm = repo.get_vm(vm_id)
        if vm is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "VM not found"})
        try:
            if docker_manager.client is not None:
                docker_manager.remove(vm)
            elif vm.get("container_id"):
                raise DockerUnavailable("Docker runtime unavailable")
        except DockerUnavailable:
            repo.release_resource_leases(vm_id)
            return _problem("docker_unavailable", "Docker runtime unavailable", 503)
        except Exception as exc:
            if not DockerManager.is_not_found(exc):
                repo.release_resource_leases(vm_id)
                raise
        app.state.worker_tokens.pop(vm_id, None)
        app.state.worker_commands.pop(vm_id, None)
        app.state.log_seen.pop(vm_id, None)
        app.state.vm_alerts.pop(vm_id, None)
        app.state.ingest_pending.pop(vm_id, None)
        for key in [item for item in list(app.state.storage_stores) if str(item).endswith(f"|{vm_id}")]:
            store_item = app.state.storage_stores.pop(key, None)
            if store_item is not None:
                store_item.discard_vm(vm_id)
        app.state.store.discard_vm(vm_id)
        try:
            deleted_paths = purge_vm_directories(vm_id, _vm_storage_roots(vm, repo, cfg), extra_subdirs=_vm_storage_subdirs(vm))
        except (OSError, ValueError) as exc:
            repo.release_resource_leases(vm_id)
            raise HTTPException(500, detail={"code": "vm_files_delete_failed", "message": str(exc)}) from exc
        repo.release_resource_leases(vm_id)
        repo.delete_vm(vm_id)
        bus.forget_vm(vm_id)
        repo.record_audit(int(account["id"]), "vm.delete", vm_id, {"deleted_paths": deleted_paths})
        return {"ok": True, "deleted_paths": deleted_paths}

    async def _record_operator_event(vm: dict[str, Any], action: str, *, error: str | None = None) -> None:
        label = OPERATOR_LOG_LABELS.get(action, action)
        vm_id = str(vm["id"])
        if error:
            await bus.publish_log(vm_id, f"Оператор: {label} — ошибка: {error}", level="error")
        else:
            await bus.publish_log(vm_id, f"Оператор: {label}")
        health = "unhealthy" if error or vm.get("lifecycle") == VmLifecycle.FAILED.value else "unknown"
        await bus.publish_status(_status_from_vm(vm, health=health))

    async def vm_action(vm_id: str, action: str, account):
        vm = repo.get_vm(vm_id)
        if vm is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "VM not found"})
        try:
            if action in {"start", "restart"}:
                read_resources = vm.get("read_resources", vm.get("resources", [])) or []
                ids = _resource_ids(read_resources)
                conflicts = repo.resource_conflicts(ids, exclude_vm_id=vm_id)
                if conflicts:
                    return _problem(
                        "resource_conflict",
                        "Resource is already leased by another running VM",
                        409,
                        conflicts,
                    )
                conflicts = repo.acquire_resource_leases(vm_id, ids)
                if conflicts:
                    return _problem("resource_conflict", "Resource is already leased by another running VM", 409, conflicts)
            token = app.state.worker_tokens.setdefault(vm_id, secrets.token_urlsafe(32))
            inspected: dict[str, Any] | None = None
            if vm.get("container_id"):
                try:
                    inspected = docker_manager.inspect(vm)
                except Exception as exc:
                    if DockerManager.is_not_found(exc):
                        repo.release_resource_leases(vm_id)
                        vm = repo.update_vm(vm_id, {"container_id": None, "lifecycle": VmLifecycle.PENDING.value, "last_error": None}) or vm
                    else:
                        raise
            if action == "stop" and not vm.get("container_id"):
                repo.update_vm(vm_id, {"desired_state": "stopped", "lifecycle": VmLifecycle.STOPPED.value})
                repo.release_resource_leases(vm_id)
                vm = repo.get_vm(vm_id) or vm
                repo.record_audit(int(account["id"]), f"vm.{action}", vm_id, {"config_revision": vm.get("config_revision")})
                await _record_operator_event(vm, action)
                return vm
            if action == "start" and inspected and inspected.get("lifecycle") == VmLifecycle.RUNNING.value:
                vm = repo.update_vm(vm_id, {"desired_state": "running", "lifecycle": VmLifecycle.RUNNING.value, "last_error": None}) or vm
                repo.record_audit(int(account["id"]), f"vm.{action}", vm_id, {"config_revision": vm.get("config_revision")})
                await _record_operator_event(vm, action)
                return vm
            if action in {"start", "restart"}:
                vm = _replace_worker_container(vm, token)
                repo.update_vm(vm_id, {"lifecycle": VmLifecycle.STARTING.value, "desired_state": "running"})
                docker_manager.start(vm)
                vm = repo.update_vm(vm_id, {"lifecycle": VmLifecycle.RUNNING.value, "last_error": None}) or vm
            elif action == "stop":
                if inspected and inspected.get("lifecycle") in {VmLifecycle.CREATED.value, VmLifecycle.STOPPED.value}:
                    repo.update_vm(vm_id, {"lifecycle": VmLifecycle.STOPPED.value, "desired_state": "stopped"})
                    repo.release_resource_leases(vm_id)
                    vm = repo.get_vm(vm_id) or vm
                    repo.record_audit(int(account["id"]), f"vm.{action}", vm_id, {"config_revision": vm.get("config_revision")})
                    await _record_operator_event(vm, action)
                    return vm
                repo.update_vm(vm_id, {"lifecycle": VmLifecycle.STOPPING.value, "desired_state": "stopped"})
                docker_manager.stop(vm)
                vm = repo.update_vm(vm_id, {"lifecycle": VmLifecycle.STOPPED.value}) or vm
                repo.release_resource_leases(vm_id)
            repo.record_audit(int(account["id"]), f"vm.{action}", vm_id, {"config_revision": vm.get("config_revision")})
            await _record_operator_event(vm, action)
            return vm
        except DockerUnavailable as exc:
            repo.release_resource_leases(vm_id)
            failed = repo.update_vm(vm_id, {"lifecycle": VmLifecycle.FAILED.value, "last_error": str(exc)}) or vm
            await _record_operator_event(failed, action, error=str(exc))
            return _problem("docker_unavailable", str(exc), 503)
        except Exception as exc:
            repo.release_resource_leases(vm_id)
            failed = repo.update_vm(vm_id, {"lifecycle": VmLifecycle.FAILED.value, "last_error": str(exc)}) or vm
            await _record_operator_event(failed, action, error=str(exc))
            return _problem("vm_action_failed", str(exc), 502)

    @app.post("/api/v1/vms/{vm_id}/start", dependencies=[Depends(csrf_protect)])
    async def start_vm(vm_id: str, account=Depends(admin)):
        return await vm_action(vm_id, "start", account)

    @app.post("/api/v1/vms/{vm_id}/stop", dependencies=[Depends(csrf_protect)])
    async def stop_vm(vm_id: str, account=Depends(admin)):
        return await vm_action(vm_id, "stop", account)

    @app.post("/api/v1/vms/{vm_id}/restart", dependencies=[Depends(csrf_protect)])
    async def restart_vm(vm_id: str, account=Depends(admin)):
        return await vm_action(vm_id, "restart", account)

    @app.post("/api/v1/vms/{vm_id}/apply-map", dependencies=[Depends(csrf_protect)])
    async def apply_map(vm_id: str, account=Depends(admin)):
        vm = repo.get_vm(vm_id)
        if vm is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "VM not found"})
        if repo.map_by_version(vm["map_version"], vm["protocol"]) is None:
            raise HTTPException(422, detail={"code": "map_not_found", "message": "Map version not found"})
        command = VmCommand(vm_id=UUID(vm_id), action="apply_map", config_revision=vm["config_revision"], map_version=vm["map_version"])
        app.state.worker_commands.setdefault(vm_id, []).append(command.model_dump(mode="json"))
        repo.record_audit(int(account["id"]), "vm.apply_map", vm_id, {"map_version": vm["map_version"]})
        await bus.publish_log(vm_id, f"Оператор применил карту {vm['map_version']}")
        return {"ok": True, "vm_id": vm_id, "map_version": vm["map_version"], "config_revision": vm["config_revision"]}

    @app.post("/api/v1/maps", dependencies=[Depends(csrf_protect)])
    async def save_map(payload: MapUploadRequest, account=Depends(admin)):
        return _publish_map_document(payload.document, protocol=payload.protocol, preset_id=payload.preset_id, version=payload.version, account=account)

    @app.post("/api/v1/maps/upload", dependencies=[Depends(csrf_protect)])
    async def upload_map(protocol: VmProtocol = Form(...), version: str = Form(...), preset_id: str | None = Form(None), file: UploadFile = File(...), account=Depends(admin)):
        filename = file.filename or ""
        if file.content_type not in {"application/json", "text/json", None} and not filename.lower().endswith(".json"):
            raise HTTPException(415, detail={"code": "invalid_map_type", "message": "Only JSON map files are supported"})
        try:
            payload = json.loads((await file.read()).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(422, detail={"code": "invalid_map", "message": str(exc)}) from exc
        return _publish_map_document(payload, protocol=protocol, preset_id=preset_id, version=version, account=account)

    @app.get("/api/v1/maps")
    async def list_maps(account=Depends(user)):
        return {"items": repo.list_maps()}

    @app.get("/api/v1/maps/{version}")
    async def get_map(version: str, protocol: str | None = None, account=Depends(user)):
        record = repo.map_record(version, protocol)
        if record is None:
            raise HTTPException(404, detail={"code": "map_not_found", "message": "Map version not found"})
        return record

    @app.delete("/api/v1/maps/{version}", dependencies=[Depends(csrf_protect)])
    async def delete_map(version: str, protocol: str = Query(..., min_length=1), account=Depends(admin)):
        record = repo.map_record(version, protocol)
        if record is None:
            raise HTTPException(404, detail={"code": "map_not_found", "message": "Map version not found"})
        used = repo.vms_using_map(version, protocol)
        if used:
            names = ", ".join(item["name"] for item in used)
            return _problem(
                "map_in_use",
                f"Карту нельзя удалить: она назначена ВМ {names}",
                409,
                [item["id"] for item in used],
            )
        repo.delete_map(version, protocol)
        repo.record_audit(int(account["id"]), "map.delete", version, {"protocol": protocol, "checksum": record.get("checksum")})
        return {"ok": True, "version": version, "protocol": protocol}

    @app.get("/api/v1/users")
    async def list_users(account=Depends(admin)):
        return {"items": repo.list_users()}

    @app.post("/api/v1/users", dependencies=[Depends(csrf_protect)])
    async def create_user_account(payload: UserCreateRequest, account=Depends(admin)):
        try:
            created = repo.create_user(payload.username.strip(), payload.password, payload.role)
            repo.record_audit(int(account["id"]), "user.create", str(created["id"]), {"role": created["role"]})
            return created
        except Exception as exc:
            return _problem("user_create_failed", str(exc), 409)

    @app.get("/api/v1/vms/{vm_id}/logs")
    async def vm_logs(vm_id: str, tail: int = Query(default=2000, ge=1, le=5000), account=Depends(user)):
        vm = repo.get_vm(vm_id)
        if vm is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "VM not found"})
        journal = _compose_vm_journal(repo, bus, docker_manager, vm, tail=tail)
        return {"vm_id": vm_id, "tail": tail, **journal}

    @app.get("/api/v1/vms/{vm_id}/reading")
    async def vm_reading(vm_id: str, account=Depends(user)):
        vm = repo.get_vm(vm_id)
        if vm is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "VM not found"})
        sample = bus.latest_tags(vm_id)
        if sample is not None and getattr(sample.quality, "value", sample.quality) == Quality.BAD.value:
            live = bus.latest_good_tags(vm_id)
        else:
            live = sample
        tags = dict(live.tags) if live is not None else {}
        analog_values = dict(live.analog) if live is not None else {}
        discrete_values = dict(live.discrete) if live is not None else {}
        alert_names = list(live.alerts) if live is not None else []
        map_record = repo.map_record(vm["map_version"], vm["protocol"])
        document = (map_record or {}).get("document") if isinstance(map_record, dict) else {}
        analog_rows: list[dict[str, Any]] = []
        discrete_rows: list[dict[str, Any]] = []
        alert_catalog: list[dict[str, Any]] = []
        fields = []
        quality = str(getattr(sample, "quality", "") or "") if sample is not None else None
        use_tag_fallback = quality != Quality.BAD.value
        for field in (document or {}).get("fields") or []:
            if not isinstance(field, dict) or not field.get("name"):
                continue
            if field.get("system") or field.get("is_system") or field.get("internal"):
                continue
            name = str(field["name"])
            label = field.get("display_name") or name
            channel = field_channel(field)
            if channel == "analog":
                value = analog_values[name] if name in analog_values else (tags.get(name) if use_tag_fallback else None)
            elif channel == "discrete":
                if name in discrete_values:
                    value = discrete_values[name]
                elif use_tag_fallback:
                    value = bool(tags.get(name))
                else:
                    value = None
            else:
                value = tags.get(name) if use_tag_fallback else None
            row = {
                "name": name,
                "label": label,
                "kind": channel,
                "type": field.get("type"),
                "source": field.get("source"),
                "address": field.get("address"),
                "value": value,
            }
            fields.append(row)
            if channel == "analog":
                analog_rows.append(row)
            elif channel == "discrete":
                discrete_rows.append(row)
            else:
                labels = field.get("bits") if isinstance(field.get("bits"), dict) else {}
                if labels:
                    active = {str(item) for item in alert_names}
                    for bit, alarm_name in labels.items():
                        text = str(alarm_name)
                        alert_catalog.append({"name": text, "bit": str(bit), "active": text in active, "source": name})
                elif isinstance(value, list):
                    for item in value or []:
                        alert_catalog.append({"name": str(item), "bit": None, "active": True, "source": name})
        seen_alerts = {item["name"] for item in alert_catalog}
        for name in alert_names:
            if name not in seen_alerts:
                alert_catalog.append({"name": name, "bit": None, "active": True, "source": "active_alarms"})
        captured_at = None
        if sample is not None:
            captured_at = sample.captured_at.isoformat() if hasattr(sample.captured_at, "isoformat") else str(sample.captured_at)
        diagnosis = diagnose_read(
            quality=quality,
            last_error=vm.get("last_error"),
            alerts=alert_names,
            has_sample=sample is not None,
        )
        return {
            "vm_id": vm_id,
            "lifecycle": vm.get("lifecycle"),
            "last_error": vm.get("last_error"),
            "map_version": vm.get("map_version"),
            "captured_at": captured_at,
            "quality": quality,
            "tags": tags,
            "fields": fields,
            "analog": analog_rows,
            "discrete": discrete_rows,
            "alerts": alert_catalog,
            "active_alerts": alert_names,
            "alerts_stale": bool(
                sample is not None
                and quality == Quality.BAD.value
                and live is not None
            ),
            "diagnosis": diagnosis,
            "connection": vm_link_status(vm, sample),
        }

    @app.get("/api/v1/resources")
    async def resources(account=Depends(admin)):
        return {"items": repo.list_resources()}

    @app.get("/api/v1/resources/candidates")
    async def resource_candidates(protocol: str | None = None, account=Depends(admin)):
        grouped = _candidates_payload(repo)
        items = grouped.get(str(protocol or ""), None)
        return {"items": items if items is not None else [row for rows in grouped.values() for row in rows], "candidates": grouped}

    @app.post("/api/v1/resources/scan", dependencies=[Depends(csrf_protect)])
    async def scan_resources(network: bool = Query(default=False), account=Depends(admin)):
        items = repo.upsert_resources(_discover_resources(cfg.data_root, repo))
        repo.approve_resource("storage:data", None)
        summary = discovery_summary(items)
        repo.record_audit(int(account["id"]), "resources.scan", None, {"count": len(items), "summary": summary, "network": False})
        return {"items": items, "summary": summary, "candidates": _candidates_payload(repo)}

    @app.post("/api/v1/resources/{resource_id:path}/approve", dependencies=[Depends(csrf_protect)])
    async def approve_resource(resource_id: str, account=Depends(admin)):
        if not repo.approve_resource(resource_id, int(account["id"])):
            raise HTTPException(404, detail={"code": "not_found", "message": "Resource not found"})
        repo.record_audit(int(account["id"]), "resource.approve", resource_id)
        return {"ok": True, "resource_id": resource_id}

    def worker_auth(request: Request, vm_id: str) -> dict[str, Any]:
        token = request.headers.get("x-worker-token", "")
        vm = repo.get_vm(vm_id)
        if vm is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "VM not found"})
        expected = app.state.worker_tokens.get(vm_id)
        if not expected and vm.get("container_id"):
            try:
                expected = docker_manager.bootstrap_token(vm)
            except Exception:
                expected = None
            if expected:
                app.state.worker_tokens[vm_id] = expected
        if not token or not expected or not secrets.compare_digest(token, expected):
            raise HTTPException(401, detail={"code": "worker_unauthorized", "message": "Invalid worker token"})
        return vm

    @app.post("/api/v1/internal/workers/register")
    async def worker_register(payload: WorkerRegister, request: Request):
        worker_auth(request, str(payload.vm_id))
        vm = repo.update_vm(str(payload.vm_id), {"lifecycle": VmLifecycle.RUNNING.value}) or repo.get_vm(str(payload.vm_id))
        await bus.publish_status(_status_from_vm(vm))
        return {"ok": True, "config_revision": vm["config_revision"] if vm else payload.config_revision}

    @app.get("/api/v1/internal/workers/{vm_id}/commands")
    async def worker_commands(vm_id: str, request: Request):
        worker_auth(request, vm_id)
        commands = app.state.worker_commands.get(vm_id, [])
        app.state.worker_commands[vm_id] = []
        return {"items": commands}

    @app.get("/api/v1/internal/workers/{vm_id}/config")
    async def worker_config(vm_id: str, request: Request):
        """Return the current reader settings and immutable map to a worker."""
        vm = worker_auth(request, vm_id)
        document = repo.map_by_version(vm["map_version"], vm["protocol"])
        if document is None:
            raise HTTPException(409, detail={"code": "map_not_found", "message": "VM map is not available"})
        return {
            "vm_id": vm_id,
            "protocol": vm["protocol"],
            "config_revision": vm["config_revision"],
            "map_version": vm["map_version"],
            "config": vm.get("config", {}),
            "map": {"requests": document.get("requests", []), "fields": document.get("fields", [])},
        }

    @app.post("/api/v1/internal/workers/heartbeat")
    async def worker_heartbeat(payload: WorkerHeartbeat, request: Request):
        worker_auth(request, str(payload.vm_id))
        current = repo.get_vm(str(payload.vm_id))
        values: dict[str, Any] = {"heartbeat_at": payload.timestamp.isoformat()}
        if current and current.get("lifecycle") in {
            VmLifecycle.PENDING.value,
            VmLifecycle.CREATED.value,
            VmLifecycle.STARTING.value,
            VmLifecycle.UNKNOWN.value,
        }:
            values["lifecycle"] = VmLifecycle.RUNNING.value
        vm = repo.update_vm(str(payload.vm_id), values)
        if vm:
            status = _status_from_vm(vm, health=payload.health, heartbeat_at=payload.timestamp)
            await bus.publish_status(status)
        return {"ok": True}

    @app.post("/api/v1/internal/workers/batches")
    async def worker_batch(batch: RawBatch, request: Request):
        vm = worker_auth(request, str(batch.vm_id))
        try:
            runtime_config = normalize_runtime_config(vm.get("config", {}), protocol=vm.get("protocol"))
            max_pending = int(runtime_config.get("buffer", {}).get("max_queue", cfg.queue_size))
        except (TypeError, ValueError, ValidationError):
            max_pending = cfg.queue_size
        pending_key = str(batch.vm_id)
        if app.state.ingest_pending.get(pending_key, 0) >= max(1, max_pending):
            alarm = AlarmEvent(vm_id=batch.vm_id, severity="critical", code="ingest_backpressure", message="VM ingest queue is full", active=True)
            await bus.publish("alarms", alarm.model_dump(mode="json"))
            await bus.publish_log(pending_key, alarm.message, level="error")
            raise HTTPException(503, detail={"code": "ingest_backpressure", "message": alarm.message})
        try:
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            app.state.ingest_queue.put_nowait((batch, future))
            app.state.ingest_pending[pending_key] = app.state.ingest_pending.get(pending_key, 0) + 1
        except asyncio.QueueFull:
            alarm = AlarmEvent(vm_id=batch.vm_id, severity="critical", code="ingest_backpressure", message="Ingest queue is full", active=True)
            await bus.publish("alarms", alarm.model_dump(mode="json"))
            await bus.publish_log(str(batch.vm_id), alarm.message, level="error")
            raise HTTPException(503, detail={"code": "ingest_backpressure", "message": alarm.message})
        result = await future
        if "error" in result:
            code, message, status_code = result["error"]
            raise HTTPException(status_code, detail={"code": code, "message": message})
        return result

    @app.post("/api/v1/internal/workers/command-ack")
    async def worker_command_ack(payload: WorkerCommandAck, request: Request):
        worker_auth(request, str(payload.vm_id))
        await bus.publish_log(str(payload.vm_id), f"command {payload.command_id}: {'accepted' if payload.accepted else 'rejected'}")
        return {"ok": True}

    @app.post("/api/v1/internal/workers/error")
    async def worker_error(payload: WorkerError, request: Request):
        worker_auth(request, str(payload.vm_id))
        protocol_fault = payload.code in PROTOCOL_ERROR_CODES
        line = payload.message if not protocol_fault else f"Чтение не удалось: {payload.message}"
        await bus.publish_log(str(payload.vm_id), line, level="error")
        values: dict[str, Any] = {"last_error": payload.message}
        if not protocol_fault:
            values["lifecycle"] = VmLifecycle.FAILED.value
        updated = repo.update_vm(str(payload.vm_id), values) or repo.get_vm(str(payload.vm_id))
        if updated:
            await bus.publish_status(_status_from_vm(updated, health="unhealthy"))
        return {"ok": True}

    @app.websocket("/ws/v1/events")
    async def events(websocket: WebSocket):
        token = websocket.cookies.get(ACCESS_COOKIE)
        try:
            if not token:
                await websocket.close(code=4401)
                return
            payload = _decode(token, cfg, "access")
            if repo.user_by_id(int(payload["sub"])) is None:
                await websocket.close(code=4401)
                return
        except HTTPException:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        snapshot = bus.snapshot()
        requested_topics = {topic.strip() for topic in websocket.query_params.get("topics", "").split(",") if topic.strip()}
        vm_filter = websocket.query_params.get("vm_id")
        if requested_topics:
            if "vm_status" not in requested_topics:
                snapshot["vm_status"] = []
            if "tags" not in requested_topics:
                snapshot["tags"] = []
                snapshot["tags_good"] = []
            if "logs" not in requested_topics:
                snapshot["logs"] = {}
            if "alarms" not in requested_topics:
                snapshot["alarms"] = []
            if "system" not in requested_topics:
                snapshot["system"] = {}
        if vm_filter:
            snapshot["vm_status"] = [item for item in snapshot.get("vm_status", []) if item.get("vm_id") == vm_filter]
            snapshot["tags"] = [item for item in snapshot.get("tags", []) if item.get("vm_id") == vm_filter]
            snapshot["tags_good"] = [item for item in snapshot.get("tags_good", []) if item.get("vm_id") == vm_filter]
            snapshot["logs"] = {key: value for key, value in snapshot.get("logs", {}).items() if key == vm_filter}
            snapshot["alarms"] = [item for item in snapshot.get("alarms", []) if item.get("vm_id") == vm_filter]
        raw_cursor = websocket.query_params.get("cursor")
        try:
            cursor = max(0, int(raw_cursor)) if raw_cursor is not None else snapshot["seq"]
        except ValueError:
            cursor = snapshot["seq"]
        queue = await bus.subscribe(after_seq=cursor)
        try:
            await websocket.send_json({"type": "snapshot", "payload": snapshot})
            while True:
                event = await queue.get()
                if requested_topics and event.topic not in requested_topics:
                    continue
                if vm_filter and event.topic != "system" and str(event.payload.get("vm_id", "")) != vm_filter:
                    continue
                await websocket.send_json({"type": "delta", "seq": event.seq, "topic": event.topic, "payload": event.payload})
        except WebSocketDisconnect:
            pass
        finally:
            await bus.unsubscribe(queue)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return templates.TemplateResponse(request=request, name="login.html", context={"error": None, "user": None})

    @app.post("/login", response_class=HTMLResponse)
    async def login_form(request: Request, username: str = Form(...), password: str = Form(...)):
        account = repo.authenticate(username.strip(), password)
        if account is None:
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={"error": "Неверный логин или пароль", "user": None},
                status_code=401,
            )
        access, refresh, _ = issue_tokens(repo, cfg, account)
        out = RedirectResponse("/dashboard", status_code=303)
        set_auth_cookies(out, access, refresh, secrets.token_urlsafe(24), secure=cfg.cookie_secure)
        return out

    def _require_admin_html(request: Request):
        try:
            account = current_user(request, repo, cfg)
            if account["role"] != "admin":
                raise HTTPException(403)
            return account
        except HTTPException as exc:
            return RedirectResponse("/login" if exc.status_code == 401 else "/dashboard", status_code=303)

    def _vms_page_context(account: dict[str, Any], *, selected_vm_id: str = "") -> dict[str, Any]:
        groups = _approved_resource_groups(repo) if account.get("role") == "admin" else {
            "serial_resources": [],
            "tcp_resources": [],
            "can_resources": [],
            "gpio_resources": [],
            "storage_resources": [],
        }
        return {
            "user": account,
            "vms": repo.list_vms(),
            "maps": repo.list_maps() if account.get("role") == "admin" else [],
            "selected_vm_id": selected_vm_id,
            "active_protocol": "simulator",
            "reader": {},
            "storage": {},
            "buffer": {},
            "selected_read_ids": [],
            "current_storage_id": "",
            **groups,
        }

    def _telemetry_stores() -> list[ParquetStore]:
        stores = list(getattr(app.state, "storage_stores", {}).values())
        if all(item is not store for item in stores):
            stores.append(store)
        return stores

    def _telemetry_roots() -> list[Path]:
        return collect_roots(cfg.data_root, repo.list_resources(), repo.list_vms(), [item.root for item in _telemetry_stores()])

    def _source_documents() -> dict[tuple[str, str], dict[str, Any]]:
        documents: dict[tuple[str, str], dict[str, Any]] = {}
        for vm in repo.list_vms():
            key = (str(vm.get("map_version") or ""), str(vm.get("protocol") or ""))
            if not key[0] or key in documents:
                continue
            document = repo.map_by_version(key[0], key[1])
            if document:
                documents[key] = document
        return documents

    def _sources(vm_ids: set[str] | None = None) -> list[dict[str, Any]]:
        vms = [vm for vm in repo.list_vms() if vm_ids is None or str(vm["id"]) in vm_ids]
        live = {str(vm["id"]): sample for vm in vms if (sample := bus.latest_tags(str(vm["id"]))) is not None}
        return describe_sources(vms, _source_documents(), live, _telemetry_roots())

    def _selected_vm_ids(requested: list[str] | None) -> list[str]:
        known = {str(vm["id"]) for vm in repo.list_vms()}
        if not requested:
            return sorted(known)
        return [vm_id for vm_id in requested if vm_id in known]

    def _measurement_rows(vm_ids: list[str], date_from: datetime | None, date_to: datetime | None, *, today_if_open: bool = False, point_cap: int | None = None) -> tuple[list[Any], bool, bool]:
        start, end = date_from, date_to
        realtime = start is None and end is None
        if today_if_open and realtime:
            now = datetime.now().astimezone()
            start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        rows, truncated = query_measurements(
            _telemetry_roots(),
            _telemetry_stores(),
            vm_ids=set(vm_ids),
            date_from=start,
            date_to=end,
            point_cap=point_cap,
        )
        return rows, truncated, realtime

    @app.get("/api/v1/telemetry/catalog")
    async def telemetry_catalog(request: Request, vm_id: list[str] | None = Query(default=None)):
        current_user(request, repo, cfg)
        selected = set(_selected_vm_ids(vm_id))
        return {"sources": _sources(selected)}

    @app.get("/api/v1/telemetry/rows")
    async def telemetry_rows(
        request: Request,
        vm_id: list[str] | None = Query(default=None),
        tab: str = "analog",
        date_from: str | None = None,
        date_to: str | None = None,
        sort: str = "desc",
        page: int = Query(default=1, ge=1),
        column: list[str] | None = Query(default=None),
    ):
        current_user(request, repo, cfg)
        active = tab if tab in {"analog", "discrete", "alarms", "gpio"} else "analog"
        selected = _selected_vm_ids(vm_id)
        start = parse_bound(date_from)
        end = parse_bound(date_to, end_of_day=True)
        names = {str(vm["id"]): str(vm["name"]) for vm in repo.list_vms()}
        if active in {"alarms", "gpio"}:
            events, total = repo.list_alarm_events(
                selected,
                kind="gpio" if active == "gpio" else "alert",
                date_from=start.isoformat() if start else None,
                date_to=end.isoformat() if end else None,
                sort_desc=sort != "asc",
                offset=(page - 1) * PAGE_SIZE,
                limit=PAGE_SIZE,
            )
            total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE) if total else 1
            page_eff = min(page, total_pages)
            rows = []
            for event in events:
                state = str(event["state"])
                if active == "gpio":
                    state_label = "Активно" if state == "active" else "Снято"
                else:
                    state_label = "Активна" if state == "active" else "Снята"
                created = datetime.fromisoformat(str(event["created_at"]))
                rows.append(
                    {
                        "time": format_timestamp(created),
                        "ts": created.isoformat(),
                        "vm_id": event["vm_id"],
                        "vm_name": names.get(str(event["vm_id"]), str(event["vm_id"])),
                        "name": event["name"],
                        "state": state,
                        "state_label": state_label,
                    }
                )
            return {
                "tab": active,
                "columns": [],
                "rows": rows,
                "page": page_eff,
                "total_pages": total_pages,
                "total_rows": total,
                "page_size": PAGE_SIZE,
                "truncated": False,
            }
        sources = _sources(set(selected))
        columns = column_defs(sources, active, column or None)
        measurements, total, page_used = query_window(
            _telemetry_roots(),
            _telemetry_stores(),
            vm_ids=set(selected),
            date_from=start,
            date_to=end,
            page=page,
            page_size=PAGE_SIZE,
            sort_desc=sort != "asc",
        )
        payload = page_table(
            measurements,
            tab=active,
            columns=columns,
            vm_names=names,
            sort_desc=sort != "asc",
            page=page_used,
            total_override=total,
            already_paged=True,
        )
        payload["truncated"] = False
        return payload

    @app.get("/api/v1/telemetry/series")
    async def telemetry_series(
        request: Request,
        vm_id: list[str] | None = Query(default=None),
        table: str = "analog",
        date_from: str | None = None,
        date_to: str | None = None,
        column: list[str] | None = Query(default=None),
    ):
        current_user(request, repo, cfg)
        active = "discrete" if table == "discrete" else "analog"
        selected = _selected_vm_ids(vm_id)
        start = parse_bound(date_from)
        end = parse_bound(date_to, end_of_day=True)
        measurements, _truncated, realtime = _measurement_rows(selected, start, end, today_if_open=True, point_cap=CHART_POINT_CAP)
        sources = _sources(set(selected))
        labels = {item["key"]: item["label"] for item in column_defs(sources, active, None)}
        fields = column or list(labels)
        return chart_payload(
            measurements,
            table=active,
            vm_ids=selected,
            vm_names={str(vm["id"]): str(vm["name"]) for vm in repo.list_vms()},
            labels=labels,
            fields=fields,
            realtime=realtime,
        )

    @app.get("/api/v1/telemetry/export")
    async def telemetry_export(
        request: Request,
        vm_id: list[str] | None = Query(default=None),
        tab: str = "analog",
        date_from: str | None = None,
        date_to: str | None = None,
        sort: str = "desc",
        column: list[str] | None = Query(default=None),
    ):
        current_user(request, repo, cfg)
        active = tab if tab in {"analog", "discrete", "alarms", "gpio"} else "analog"
        selected = _selected_vm_ids(vm_id)
        names = {str(vm["id"]): str(vm["name"]) for vm in repo.list_vms()}
        if active in {"alarms", "gpio"}:
            events, _total = repo.list_alarm_events(
                selected,
                kind="gpio" if active == "gpio" else "alert",
                date_from=parse_bound(date_from).isoformat() if parse_bound(date_from) else None,
                date_to=parse_bound(date_to, end_of_day=True).isoformat() if parse_bound(date_to, end_of_day=True) else None,
                sort_desc=sort != "asc",
                offset=0,
                limit=10_000,
            )
            rows = []
            for event in events:
                state = str(event["state"])
                created = datetime.fromisoformat(str(event["created_at"]))
                rows.append(
                    {
                        "time": format_timestamp(created),
                        "vm_name": names.get(str(event["vm_id"]), str(event["vm_id"])),
                        "name": event["name"],
                        "state_label": ("Активно" if state == "active" else "Снято") if active == "gpio" else ("Активна" if state == "active" else "Снята"),
                    }
                )
            payload = {"tab": active, "rows": rows}
        else:
            measurements, total, _page_used = query_window(
                _telemetry_roots(),
                _telemetry_stores(),
                vm_ids=set(selected),
                date_from=parse_bound(date_from),
                date_to=parse_bound(date_to, end_of_day=True),
                page=1,
                page_size=READ_ROW_CAP,
                sort_desc=sort != "asc",
            )
            columns = column_defs(_sources(set(selected)), active, column or None)
            payload = page_table(
                measurements,
                tab=active,
                columns=columns,
                vm_names=names,
                sort_desc=sort != "asc",
                page=1,
                page_size=max(len(measurements), 1),
                total_override=total,
                already_paged=True,
            )
        text = "\ufeff" + rows_as_csv(payload)
        filename = {"alarms": "alarms.csv", "gpio": "gpio.csv"}.get(active, f"{active}.csv")
        return Response(content=text, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request):
        try:
            account = current_user(request, repo, cfg)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={"user": account, "vms": repo.list_vms(), "sources": _sources()},
        )

    @app.get("/data", response_class=HTMLResponse)
    async def data_page(request: Request):
        try:
            account = current_user(request, repo, cfg)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(request=request, name="data.html", context={"user": account, "vms": repo.list_vms()})

    @app.get("/charts", response_class=HTMLResponse)
    async def charts_page(request: Request):
        try:
            account = current_user(request, repo, cfg)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(request=request, name="charts.html", context={"user": account, "vms": repo.list_vms()})

    @app.get("/vms", response_class=HTMLResponse)
    async def vms_page(request: Request):
        try:
            account = current_user(request, repo, cfg)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(request=request, name="vms.html", context=_vms_page_context(account))

    @app.get("/vms/{vm_id}", response_class=HTMLResponse)
    async def vm_page(vm_id: str, request: Request):
        try:
            account = current_user(request, repo, cfg)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        vm = repo.get_vm(vm_id)
        if vm is None:
            return RedirectResponse("/vms", status_code=303)
        return templates.TemplateResponse(request=request, name="vms.html", context=_vms_page_context(account, selected_vm_id=vm_id))

    @app.get("/admin/vms", response_class=HTMLResponse)
    async def admin_vms_page(request: Request):
        account = _require_admin_html(request)
        if isinstance(account, RedirectResponse):
            return account
        return RedirectResponse("/vms", status_code=303)

    @app.get("/admin/vms/{vm_id}/edit", response_class=HTMLResponse)
    async def edit_vm_page(vm_id: str, request: Request):
        account = _require_admin_html(request)
        if isinstance(account, RedirectResponse):
            return account
        vm = repo.get_vm(vm_id)
        if vm is None:
            return RedirectResponse("/vms", status_code=303)
        groups = _approved_resource_groups(repo)
        read_ids = [
            str(item.get("resource_id"))
            for item in (vm.get("read_resources") or vm.get("resources") or [])
            if isinstance(item, dict) and item.get("resource_id")
        ]
        return templates.TemplateResponse(
            request=request,
            name="vm_edit.html",
            context={
                "user": account,
                "vm": vm,
                "maps": repo.list_maps(),
                "active_protocol": vm["protocol"],
                "reader": (vm.get("config") or {}).get("reader") or {},
                "storage": (vm.get("config") or {}).get("storage") or {},
                "buffer": (vm.get("config") or {}).get("buffer") or {},
                "selected_read_ids": read_ids,
                "current_storage_id": vm.get("storage_resource_id") or "",
                **groups,
            },
        )

    @app.get("/admin/maps", response_class=HTMLResponse)
    async def admin_maps_page(request: Request):
        account = _require_admin_html(request)
        if isinstance(account, RedirectResponse):
            return account
        return templates.TemplateResponse(
            request=request,
            name="maps.html",
            context={"user": account, "maps": repo.list_maps()},
        )

    @app.get("/admin/resources", response_class=HTMLResponse)
    async def resources_page(request: Request):
        account = _require_admin_html(request)
        if isinstance(account, RedirectResponse):
            return account
        resources = repo.list_resources()
        return templates.TemplateResponse(
            request=request,
            name="resources.html",
            context={
                "user": account,
                "resources": resources,
                "read_resource_groups": [
                    {
                        "kind": kind,
                        "label": KIND_LABELS.get(kind, kind),
                        "devices": [item for item in resources if item.get("kind") == kind],
                    }
                    for kind in inventory_kinds()
                    if any(item.get("kind") == kind for item in resources)
                ],
                "read_resources": [r for r in resources if r.get("kind") != ResourceKind.STORAGE.value],
                "storage_resources": [r for r in resources if r.get("kind") == ResourceKind.STORAGE.value],
            },
        )

    @app.get("/admin/logs", response_class=HTMLResponse)
    async def admin_logs_page(request: Request):
        account = _require_admin_html(request)
        if isinstance(account, RedirectResponse):
            return account
        return RedirectResponse("/vms", status_code=303)

    return app


app = create_app()
