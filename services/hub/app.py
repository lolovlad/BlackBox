from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import os
import secrets
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from bb_platform.contracts import AlarmEvent, MapDocument, RawBatch, ResourceDescriptor, ResourceKind, TagSample, VmCommand, VmLifecycle, VmProtocol, VmStatus, WorkerCommandAck, WorkerError, WorkerHeartbeat, WorkerRegister
from bb_platform.parser import adapt_legacy_map, parse_batch

from .config import HubConfig
from .db import HubRepository
from .docker_manager import DockerManager, DockerUnavailable
from .registry import PROTOCOLS, protocol_spec
from .security import ACCESS_COOKIE, CSRF_COOKIE, REFRESH_COOKIE, _decode, csrf_protect, current_user, issue_tokens, set_auth_cookies
from .state import EventBus
from .storage import ParquetStore, StorageUnavailable, purge_vm_directories
from .vm_config import normalize_runtime_config

HUB_VERSION = "2.0.3"
HUB_VENDOR = "AGK"


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


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=8, max_length=1024)
    role: str = "user"


def _problem(code: str, message: str, status_code: int, details: Any = None) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": code, "message": message, "details": details, "request_id": secrets.token_hex(8)})


def _vm_json(vm: dict[str, Any]) -> dict[str, Any]:
    return vm


def _discover_resources(data_root: Path) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    configured_serial = [x.strip() for x in os.getenv("BB_DISCOVERY_SERIAL_PATHS", "").split(",") if x.strip()]
    serial_paths = sorted(set(glob.glob("/dev/tty*") + glob.glob("/dev/serial/by-id/*") + glob.glob("COM*") + configured_serial))
    for path in serial_paths:
        found.append(ResourceDescriptor(resource_id=f"serial:{path}", kind=ResourceKind.SERIAL, name=Path(path).name, path=path).model_dump(mode="json"))
    for path in sorted(glob.glob("/dev/gpiochip*")):
        found.append(ResourceDescriptor(resource_id=f"gpio:{path}", kind=ResourceKind.GPIO, name=Path(path).name, path=path).model_dump(mode="json"))
    for iface in sorted(glob.glob("/sys/class/net/*")):
        try:
            if Path(iface, "type").read_text().strip() == "280":
                name = Path(iface).name
                found.append(ResourceDescriptor(resource_id=f"can:{name}", kind=ResourceKind.CAN, name=name, path=f"/sys/class/net/{name}").model_dump(mode="json"))
        except OSError:
            continue
    for endpoint in filter(None, (x.strip() for x in os.getenv("BB_DISCOVERY_TCP_ENDPOINTS", "").split(","))):
        endpoint = endpoint.removeprefix("tcp://").removeprefix("tcp:")
        found.append(ResourceDescriptor(resource_id=f"tcp:{endpoint}", kind=ResourceKind.TCP, name=endpoint, address=endpoint).model_dump(mode="json"))
    data_root.mkdir(parents=True, exist_ok=True)
    try:
        usage = shutil.disk_usage(data_root)
        storage_meta = {"class": "internal", "free_bytes": int(usage.free), "total_bytes": int(usage.total)}
    except OSError:
        storage_meta = {"class": "internal"}
    found.append(ResourceDescriptor(resource_id="storage:data", kind=ResourceKind.STORAGE, name="Внутренний диск Hub", path=str(data_root), metadata=storage_meta).model_dump(mode="json"))
    # Additional mounted volumes may be supplied explicitly as
    # ``id=/container/path`` entries (comma separated).  On a Raspberry Pi we
    # also notice conventional mount roots; approval is still required before
    # any VM can write to one of them.
    storage_candidates: list[tuple[str, str]] = []
    for item in filter(None, (x.strip() for x in os.getenv("BB_STORAGE_PATHS", "").split(","))):
        if "=" not in item:
            continue
        resource_id, path = item.split("=", 1)
        resource_id, path = resource_id.strip(), path.strip()
        if resource_id and path and Path(path).exists():
            storage_candidates.append((resource_id, path))
    for mount_root in ("/mnt", "/media", "/run/media"):
        for candidate in sorted(glob.glob(f"{mount_root}/*")):
            path = Path(candidate)
            try:
                if not path.is_dir() or not os.path.ismount(path):
                    continue
            except OSError:
                continue
            storage_candidates.append((path.name, str(path)))
            for nested in sorted(glob.glob(f"{candidate}/*")):
                nested_path = Path(nested)
                try:
                    if nested_path.is_dir() and os.path.ismount(nested_path):
                        storage_candidates.append((nested_path.name, str(nested_path)))
                except OSError:
                    continue
    seen_storage_paths: set[str] = set()
    seen_storage_ids: set[str] = set()
    for resource_id, path in storage_candidates:
        resolved_path = str(Path(path).resolve())
        if resource_id == "data" or resolved_path == str(data_root.resolve()) or resolved_path in seen_storage_paths or resource_id in seen_storage_ids:
            continue
        seen_storage_paths.add(resolved_path)
        seen_storage_ids.add(resource_id)
        try:
            usage = shutil.disk_usage(path)
            metadata = {"class": "external", "free_bytes": int(usage.free), "total_bytes": int(usage.total)}
        except OSError:
            metadata = {"class": "external"}
        found.append(ResourceDescriptor(resource_id=f"storage:{resource_id}", kind=ResourceKind.STORAGE, name=resource_id, path=path, metadata=metadata).model_dump(mode="json"))
    return found


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
    """Require an approved physical source for non-simulator workers."""
    if protocol == VmProtocol.SIMULATOR.value:
        return
    read_ids = {str(item.get("resource_id")) for item in resources if isinstance(item, dict)}
    reader = runtime_config.get("reader", {}) if isinstance(runtime_config, dict) else {}
    if protocol == VmProtocol.MODBUS_RTU.value:
        port = str(reader.get("port", ""))
        if not any(item_id in {f"serial:{port}", f"serial:COM{port}"} or item_id.endswith(port) for item_id in read_ids):
            raise HTTPException(409, detail={"code": "read_resource_required", "message": "Approve and select the serial resource used by Modbus RTU"})
    elif protocol == VmProtocol.MODBUS_TCP.value:
        endpoint = f"{reader.get('host', '127.0.0.1')}:{reader.get('tcp_port', 502)}"
        # Loopback is useful for a simulator/fake instrument in the lab; remote
        # endpoints must still be explicitly discovered and approved.
        if str(reader.get("host", "127.0.0.1")) not in {"127.0.0.1", "localhost", "::1"} and not any(item_id in {f"tcp:{endpoint}", f"tcp://{endpoint}"} for item_id in read_ids):
            raise HTTPException(409, detail={"code": "read_resource_required", "message": "Discover and approve the Modbus TCP endpoint before use"})


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
    repo.upsert_resources(_discover_resources(cfg.data_root))
    if repo.resource_by_id("storage:data"):
        repo.approve_resource("storage:data", None)
    bus = EventBus()
    store = ParquetStore(cfg.data_root / "telemetry", min_free_bytes=cfg.telemetry_min_free_bytes, quota_bytes=cfg.telemetry_quota_bytes)
    docker_manager = DockerManager(docker_client, enabled=cfg.docker_enabled)
    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "ui" / "templates"))
    templates.env.globals["app_version"] = HUB_VERSION
    templates.env.globals["app_vendor"] = HUB_VENDOR

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

        ingest_task = asyncio.create_task(ingest_loop())
        stop_reconciler = asyncio.Event()

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
                                    lines = await asyncio.to_thread(docker_manager.logs, vm, tail=200)
                                    seen = app.state.log_seen.setdefault(vm["id"], set())
                                    for line in lines:
                                        if line not in seen:
                                            await bus.publish_log(vm["id"], line)
                                    app.state.log_seen[vm["id"]] = set(lines[-200:])
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
                            changed = actual != vm.get("lifecycle") or inspected.get("error") != vm.get("last_error")
                            if changed:
                                updated = repo.update_vm(vm["id"], {"lifecycle": inspected["lifecycle"], "last_error": inspected.get("error")})
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
        # Keep API errors stable and do not expose stack traces or secrets.
        return _problem("internal_error", "Internal server error", 500)

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
        return {"items": [{"protocol": spec.protocol.value, "enabled": spec.enabled, "worker_kind": spec.worker_kind, "description": spec.description} for spec in PROTOCOLS]}

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
        resources = _approved_resources(repo, read_input)
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
        return vm

    @app.get("/api/v1/vms/{vm_id}")
    async def get_vm(vm_id: str, account=Depends(user)):
        vm = repo.get_vm(vm_id)
        if vm is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "VM not found"})
        if docker_manager.client is not None and vm.get("container_id"):
            try:
                state = await asyncio.to_thread(docker_manager.inspect, vm)
                vm = repo.update_vm(vm_id, {"lifecycle": state["lifecycle"], "container_id": state["container_id"], "last_error": state.get("error")}) or vm
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
            values["read_resources"] = _approved_resources(repo, values["read_resources"], exclude_vm_id=vm_id)
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
        if updated and updated.get("desired_state") == "running" and any(key in values for key in {"config", "map_version", "read_resources", "storage_resource_id"}):
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
                return vm
            if action == "start" and inspected and inspected.get("lifecycle") == VmLifecycle.RUNNING.value:
                vm = repo.update_vm(vm_id, {"desired_state": "running", "lifecycle": VmLifecycle.RUNNING.value, "last_error": None}) or vm
                repo.record_audit(int(account["id"]), f"vm.{action}", vm_id, {"config_revision": vm.get("config_revision")})
                return vm
            if not vm.get("container_id"):
                created = docker_manager.create(vm, token)
                vm = repo.update_vm(vm_id, {"container_id": created["container_id"], "lifecycle": created["lifecycle"]}) or vm
            if action == "start":
                repo.update_vm(vm_id, {"lifecycle": VmLifecycle.STARTING.value, "desired_state": "running"})
                docker_manager.start(vm)
                vm = repo.update_vm(vm_id, {"lifecycle": VmLifecycle.RUNNING.value}) or vm
            elif action == "stop":
                if inspected and inspected.get("lifecycle") in {VmLifecycle.CREATED.value, VmLifecycle.STOPPED.value}:
                    repo.update_vm(vm_id, {"lifecycle": VmLifecycle.STOPPED.value, "desired_state": "stopped"})
                    repo.release_resource_leases(vm_id)
                    vm = repo.get_vm(vm_id) or vm
                    repo.record_audit(int(account["id"]), f"vm.{action}", vm_id, {"config_revision": vm.get("config_revision")})
                    return vm
                repo.update_vm(vm_id, {"lifecycle": VmLifecycle.STOPPING.value, "desired_state": "stopped"})
                docker_manager.stop(vm)
                vm = repo.update_vm(vm_id, {"lifecycle": VmLifecycle.STOPPED.value}) or vm
                repo.release_resource_leases(vm_id)
            elif action == "restart":
                repo.update_vm(vm_id, {"desired_state": "running", "lifecycle": VmLifecycle.STARTING.value})
                if inspected and inspected.get("lifecycle") == VmLifecycle.RUNNING.value:
                    docker_manager.restart(vm)
                else:
                    docker_manager.start(vm)
                vm = repo.update_vm(vm_id, {"lifecycle": VmLifecycle.RUNNING.value}) or vm
            repo.record_audit(int(account["id"]), f"vm.{action}", vm_id, {"config_revision": vm.get("config_revision")})
            return vm
        except DockerUnavailable as exc:
            repo.release_resource_leases(vm_id)
            repo.update_vm(vm_id, {"lifecycle": VmLifecycle.FAILED.value, "last_error": str(exc)})
            return _problem("docker_unavailable", str(exc), 503)
        except Exception as exc:
            repo.release_resource_leases(vm_id)
            repo.update_vm(vm_id, {"lifecycle": VmLifecycle.FAILED.value, "last_error": str(exc)})
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
        return {"ok": True, "vm_id": vm_id, "map_version": vm["map_version"], "config_revision": vm["config_revision"]}

    @app.post("/api/v1/maps", dependencies=[Depends(csrf_protect)])
    async def save_map(payload: MapUploadRequest, account=Depends(admin)):
        document = adapt_legacy_map(payload.document, protocol=payload.protocol, preset_id=payload.preset_id, version=payload.version)
        try:
            repo.save_map(document.model_dump(mode="json"))
        except ValueError as exc:
            raise HTTPException(409, detail={"code": "map_immutable", "message": str(exc)}) from exc
        repo.record_audit(int(account["id"]), "map.publish", document.version, {"checksum": document.checksum})
        return document

    @app.post("/api/v1/maps/upload", dependencies=[Depends(csrf_protect)])
    async def upload_map(protocol: VmProtocol = Form(...), version: str = Form(...), preset_id: str | None = Form(None), file: UploadFile = File(...), account=Depends(admin)):
        filename = file.filename or ""
        if file.content_type not in {"application/json", "text/json", None} and not filename.lower().endswith(".json"):
            raise HTTPException(415, detail={"code": "invalid_map_type", "message": "Only JSON map files are supported"})
        try:
            payload = json.loads((await file.read()).decode("utf-8"))
            document = adapt_legacy_map(payload, protocol=protocol, preset_id=preset_id, version=version)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(422, detail={"code": "invalid_map", "message": str(exc)}) from exc
        try:
            repo.save_map(document.model_dump(mode="json"))
        except ValueError as exc:
            raise HTTPException(409, detail={"code": "map_immutable", "message": str(exc)}) from exc
        repo.record_audit(int(account["id"]), "map.publish", document.version, {"checksum": document.checksum})
        return document

    @app.get("/api/v1/maps")
    async def list_maps(account=Depends(user)):
        return {"items": repo.list_maps()}

    @app.get("/api/v1/maps/{version}")
    async def get_map(version: str, protocol: str | None = None, account=Depends(user)):
        record = repo.map_record(version, protocol)
        if record is None:
            raise HTTPException(404, detail={"code": "map_not_found", "message": "Map version not found"})
        return record

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
    async def vm_logs(vm_id: str, tail: int = Query(default=200, ge=1, le=1000), account=Depends(user)):
        vm = repo.get_vm(vm_id)
        if vm is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "VM not found"})
        try:
            lines = docker_manager.logs(vm, tail=tail)
            return {"vm_id": vm_id, "lines": lines[-tail:], "tail": tail, "truncated": len(lines) > tail}
        except Exception as exc:
            return {"vm_id": vm_id, "lines": [], "tail": tail, "truncated": False, "error": str(exc)}

    @app.get("/api/v1/resources")
    async def resources(account=Depends(admin)):
        return {"items": repo.list_resources()}

    @app.post("/api/v1/resources/scan", dependencies=[Depends(csrf_protect)])
    async def scan_resources(account=Depends(admin)):
        items = repo.upsert_resources(_discover_resources(cfg.data_root))
        repo.approve_resource("storage:data", None)
        repo.record_audit(int(account["id"]), "resources.scan", None, {"count": len(items)})
        return {"items": items}

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
        vm = repo.update_vm(str(payload.vm_id), {"lifecycle": VmLifecycle.RUNNING.value, "heartbeat_at": payload.timestamp.isoformat()})
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
        await bus.publish_log(str(payload.vm_id), payload.message, level="error")
        repo.update_vm(str(payload.vm_id), {"last_error": payload.message, "lifecycle": VmLifecycle.FAILED.value})
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
            if "logs" not in requested_topics:
                snapshot["logs"] = {}
            if "alarms" not in requested_topics:
                snapshot["alarms"] = []
        if vm_filter:
            snapshot["vm_status"] = [item for item in snapshot.get("vm_status", []) if item.get("vm_id") == vm_filter]
            snapshot["tags"] = [item for item in snapshot.get("tags", []) if item.get("vm_id") == vm_filter]
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
                if vm_filter and str(event.payload.get("vm_id", "")) != vm_filter:
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

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request):
        try:
            account = current_user(request, repo, cfg)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(request=request, name="dashboard.html", context={"user": account, "vms": repo.list_vms()})

    @app.get("/vms", response_class=HTMLResponse)
    async def vms_page(request: Request):
        try:
            account = current_user(request, repo, cfg)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(request=request, name="vms.html", context={"user": account, "vms": repo.list_vms()})

    @app.get("/vms/{vm_id}", response_class=HTMLResponse)
    async def vm_page(vm_id: str, request: Request):
        try:
            account = current_user(request, repo, cfg)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        vm = repo.get_vm(vm_id)
        if vm is None:
            return RedirectResponse("/vms", status_code=303)
        try:
            lines = docker_manager.logs(vm, tail=200) if vm.get("container_id") else []
        except Exception:
            lines = []
        return templates.TemplateResponse(request=request, name="vm_detail.html", context={"user": account, "vm": vm, "lines": lines, "map_record": repo.map_record(vm["map_version"], vm["protocol"])})

    @app.get("/admin/vms", response_class=HTMLResponse)
    async def admin_vms_page(request: Request):
        account = _require_admin_html(request)
        if isinstance(account, RedirectResponse):
            return account
        approved = [r for r in repo.list_resources() if r.get("approved")]
        read_resources = [r for r in approved if r.get("kind") != ResourceKind.STORAGE.value]
        storage_resources = [r for r in approved if r.get("kind") == ResourceKind.STORAGE.value]
        return templates.TemplateResponse(
            request=request,
            name="admin_vms.html",
            context={"user": account, "vms": repo.list_vms(), "maps": repo.list_maps(), "approved_resources": read_resources, "storage_resources": storage_resources},
        )

    @app.get("/admin/vms/{vm_id}/edit", response_class=HTMLResponse)
    async def edit_vm_page(vm_id: str, request: Request):
        account = _require_admin_html(request)
        if isinstance(account, RedirectResponse):
            return account
        vm = repo.get_vm(vm_id)
        if vm is None:
            return RedirectResponse("/admin/vms", status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="vm_edit.html",
            context={
                "user": account,
                "vm": vm,
                "maps": repo.list_maps(),
                "read_resources": [r for r in repo.list_resources() if r.get("approved") and r.get("kind") != ResourceKind.STORAGE.value],
                "storage_resources": [r for r in repo.list_resources() if r.get("approved") and r.get("kind") == ResourceKind.STORAGE.value],
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
                "read_resources": [r for r in resources if r.get("kind") != ResourceKind.STORAGE.value],
                "storage_resources": [r for r in resources if r.get("kind") == ResourceKind.STORAGE.value],
            },
        )

    @app.get("/admin/logs", response_class=HTMLResponse)
    async def admin_logs_page(request: Request):
        account = _require_admin_html(request)
        if isinstance(account, RedirectResponse):
            return account
        log_items = []
        for vm in repo.list_vms():
            try:
                lines = docker_manager.logs(vm, tail=200) if vm.get("container_id") else []
                log_items.append({"vm": vm, "lines": lines, "error": None})
            except Exception as exc:
                log_items.append({"vm": vm, "lines": [], "error": str(exc)})
        return templates.TemplateResponse(request=request, name="logs.html", context={"user": account, "logs": log_items})

    return app


app = create_app()
