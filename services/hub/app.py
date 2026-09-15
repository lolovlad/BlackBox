from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bb_platform.contracts import AlarmEvent, MapDocument, RawBatch, ResourceDescriptor, ResourceKind, TagSample, VmCommand, VmLifecycle, VmProtocol, VmStatus, WorkerCommandAck, WorkerError, WorkerHeartbeat, WorkerRegister
from bb_platform.parser import adapt_legacy_map, parse_batch

from .config import HubConfig
from .db import HubRepository
from .docker_manager import DockerManager, DockerUnavailable
from .registry import PROTOCOLS, protocol_spec
from .security import ACCESS_COOKIE, CSRF_COOKIE, REFRESH_COOKIE, _decode, csrf_protect, current_user, issue_tokens, set_auth_cookies
from .state import EventBus
from .storage import ParquetStore, StorageUnavailable


class LoginRequest(BaseModel):
    username: str
    password: str


class VmCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = ""
    protocol: VmProtocol
    preset_id: str | None = None
    map_version: str = "default-v1"
    resources: list[dict[str, Any]] = Field(default_factory=list)
    limits: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)


class VmPatchRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    map_version: str | None = None
    preset_id: str | None = None
    resources: list[dict[str, Any]] | None = None
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


def _default_map(repo: HubRepository, protocol: VmProtocol, version: str = "default-v1") -> None:
    existing = repo.map_by_version(version, protocol.value)
    if existing:
        return
    if protocol == VmProtocol.SIMULATOR:
        payload = {
            "requests": [{"name": "sim", "fc": 3, "address": 0, "count": 3}],
            "fields": [
                {"name": "counter", "type": "uint16", "source": "sim", "address": 0},
                {"name": "digital_1", "type": "bool", "source": "sim", "address": 1},
                {"name": "analog_1", "type": "expr", "expr": "counter * 0.5", "round": True},
            ],
        }
    else:
        payload = {"requests": [{"name": "holding", "fc": 3, "address": 0, "count": 1}], "fields": [{"name": "register_0", "type": "uint16", "source": "holding", "address": 0}]}
    doc = adapt_legacy_map(payload, protocol=protocol, version=version)
    repo.save_map(doc.model_dump(mode="json"))


def _discover_resources(data_root: Path) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    serial_paths = sorted(glob.glob("/dev/tty*") + glob.glob("/dev/serial/by-id/*") + glob.glob("COM*"))
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
        found.append(ResourceDescriptor(resource_id=f"tcp:{endpoint}", kind=ResourceKind.TCP, name=endpoint, address=endpoint).model_dump(mode="json"))
    data_root.mkdir(parents=True, exist_ok=True)
    found.append(ResourceDescriptor(resource_id="storage:data", kind=ResourceKind.STORAGE, name="Hub data", path=str(data_root)).model_dump(mode="json"))
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
        ids.append(resource_id)
        resolved.append({"resource_id": resource_id, "kind": descriptor["kind"], "name": descriptor["name"], "path": descriptor.get("path"), "address": descriptor.get("address"), "metadata": descriptor.get("metadata", {})})
    conflicts = repo.resource_conflicts(ids, exclude_vm_id=exclude_vm_id)
    if conflicts:
        raise HTTPException(409, detail={"code": "resource_conflict", "message": "Resource is unavailable or already leased", "details": conflicts})
    return resolved


def create_app(config: HubConfig | None = None, *, docker_client: Any = None) -> FastAPI:
    cfg = config or HubConfig.from_env()
    repo = HubRepository(cfg.db_path)
    repo.bootstrap_admin(cfg.bootstrap_username, cfg.bootstrap_password)
    bus = EventBus()
    store = ParquetStore(cfg.data_root / "telemetry", min_free_bytes=cfg.telemetry_min_free_bytes, quota_bytes=cfg.telemetry_quota_bytes)
    docker_manager = DockerManager(docker_client, enabled=cfg.docker_enabled)
    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "ui" / "templates"))

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
        app.state.ingest_queue = asyncio.Queue(maxsize=max(1, cfg.queue_size))
        stop_ingest = asyncio.Event()

        async def process_batch(batch: RawBatch) -> dict[str, Any]:
            with repo.connect() as c:
                exists = c.execute("SELECT 1 FROM ingest_batches WHERE batch_id=?", (str(batch.batch_id),)).fetchone()
            if exists:
                return {"ok": True, "duplicate": True, "count": 0}
            document_payload = repo.map_by_version(batch.map_version, getattr(batch.protocol, "value", batch.protocol))
            if document_payload is None:
                return {"error": ("map_not_found", "Map version not found", 422)}
            try:
                parsed = parse_batch(batch, MapDocument(**document_payload))
                store.append(parsed)
            except StorageUnavailable as exc:
                await bus.publish_log(str(batch.vm_id), str(exc), level="error")
                return {"error": ("storage_unavailable", str(exc), 503)}
            except ValueError as exc:
                return {"error": ("invalid_batch", str(exc), 422)}
            with repo.connect() as c:
                c.execute("INSERT OR IGNORE INTO ingest_batches VALUES(?,?,?,?)", (str(batch.batch_id), str(batch.vm_id), batch.seq_start, datetime.now(timezone.utc).isoformat()))
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
                                    updated = repo.update_vm(vm["id"], {"container_id": None, "lifecycle": VmLifecycle.STOPPED.value, "last_error": None})
                                    if updated:
                                        await bus.publish_status(_status_from_vm(updated))
                                continue
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
        stop_ingest.set()
        await ingest_task
        stop_reconciler.set()
        await reconciler_task
        try:
            store.flush()
        except StorageUnavailable:
            pass

    app = FastAPI(title="BlackBox Hub", version="1.0.0", lifespan=lifespan)
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
        if repo.map_by_version(payload.map_version, payload.protocol.value) is None:
            if payload.map_version != "default-v1":
                raise HTTPException(422, detail={"code": "map_not_found", "message": "Map version not found"})
            _default_map(repo, payload.protocol, payload.map_version)
        resources = _approved_resources(repo, payload.resources)
        image = cfg.worker_image_simulator if payload.protocol == VmProtocol.SIMULATOR else cfg.worker_image_rtu
        try:
            vm = repo.create_vm({**payload.model_dump(mode="json"), "resources": resources, "worker_image": image})
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
        if repo.get_vm(vm_id) is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "VM not found"})
        values = payload.model_dump(exclude_none=True)
        if "resources" in values:
            values["resources"] = _approved_resources(repo, values["resources"], exclude_vm_id=vm_id)
        current_vm = repo.get_vm(vm_id)
        if "map_version" in values and repo.map_by_version(values["map_version"], current_vm["protocol"] if current_vm else None) is None:
            raise HTTPException(422, detail={"code": "map_not_found", "message": "Map version not found"})
        updated = repo.update_vm(vm_id, values)
        repo.record_audit(int(account["id"]), "vm.update", vm_id, {"fields": sorted(values)})
        return updated

    @app.delete("/api/v1/vms/{vm_id}", dependencies=[Depends(csrf_protect)])
    async def delete_vm(vm_id: str, account=Depends(admin)):
        vm = repo.get_vm(vm_id)
        if vm is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "VM not found"})
        if vm.get("container_id"):
            try:
                docker_manager.remove(vm)
            except DockerUnavailable:
                return _problem("docker_unavailable", "Docker runtime unavailable", 503)
        repo.delete_vm(vm_id)
        repo.record_audit(int(account["id"]), "vm.delete", vm_id)
        return {"ok": True}

    async def vm_action(vm_id: str, action: str, account):
        vm = repo.get_vm(vm_id)
        if vm is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "VM not found"})
        try:
            token = app.state.worker_tokens.setdefault(vm_id, secrets.token_urlsafe(32))
            if vm.get("container_id"):
                try:
                    docker_manager.inspect(vm)
                except Exception as exc:
                    if DockerManager.is_not_found(exc):
                        vm = repo.update_vm(vm_id, {"container_id": None, "lifecycle": VmLifecycle.PENDING.value, "last_error": None}) or vm
                    else:
                        raise
            if not vm.get("container_id"):
                created = docker_manager.create(vm, token)
                vm = repo.update_vm(vm_id, {"container_id": created["container_id"], "lifecycle": created["lifecycle"]}) or vm
            if action == "start":
                repo.update_vm(vm_id, {"lifecycle": VmLifecycle.STARTING.value, "desired_state": "running"})
                docker_manager.start(vm)
                vm = repo.update_vm(vm_id, {"lifecycle": VmLifecycle.RUNNING.value}) or vm
            elif action == "stop":
                repo.update_vm(vm_id, {"lifecycle": VmLifecycle.STOPPING.value, "desired_state": "stopped"})
                docker_manager.stop(vm)
                vm = repo.update_vm(vm_id, {"lifecycle": VmLifecycle.STOPPED.value}) or vm
            elif action == "restart":
                repo.update_vm(vm_id, {"desired_state": "running", "lifecycle": VmLifecycle.STARTING.value})
                docker_manager.restart(vm)
                vm = repo.update_vm(vm_id, {"lifecycle": VmLifecycle.RUNNING.value}) or vm
            repo.record_audit(int(account["id"]), f"vm.{action}", vm_id, {"config_revision": vm.get("config_revision")})
            return vm
        except DockerUnavailable as exc:
            repo.update_vm(vm_id, {"lifecycle": VmLifecycle.FAILED.value, "last_error": str(exc)})
            return _problem("docker_unavailable", str(exc), 503)
        except Exception as exc:
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
        with repo.connect() as c:
            rows = c.execute("SELECT id,version,protocol,preset_id,checksum,created_at FROM map_versions ORDER BY created_at DESC").fetchall()
        return {"items": [dict(row) for row in rows]}

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
    async def vm_logs(vm_id: str, account=Depends(user)):
        vm = repo.get_vm(vm_id)
        if vm is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "VM not found"})
        try:
            return {"vm_id": vm_id, "lines": docker_manager.logs(vm)}
        except Exception as exc:
            return {"vm_id": vm_id, "lines": [], "error": str(exc)}

    @app.get("/api/v1/resources")
    async def resources(account=Depends(admin)):
        return {"items": repo.list_resources()}

    @app.post("/api/v1/resources/scan", dependencies=[Depends(csrf_protect)])
    async def scan_resources(account=Depends(admin)):
        items = repo.upsert_resources(_discover_resources(cfg.data_root))
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
        worker_auth(request, str(batch.vm_id))
        try:
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            app.state.ingest_queue.put_nowait((batch, future))
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
        return templates.TemplateResponse("login.html", {"request": request, "error": None})

    @app.post("/login", response_class=HTMLResponse)
    async def login_form(request: Request, username: str = Form(...), password: str = Form(...)):
        account = repo.authenticate(username.strip(), password)
        if account is None:
            return templates.TemplateResponse("login.html", {"request": request, "error": "Неверный логин или пароль"}, status_code=401)
        access, refresh, _ = issue_tokens(repo, cfg, account)
        out = RedirectResponse("/dashboard", status_code=303)
        set_auth_cookies(out, access, refresh, secrets.token_urlsafe(24), secure=cfg.cookie_secure)
        return out

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request):
        try:
            account = current_user(request, repo, cfg)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse("dashboard.html", {"request": request, "user": account, "vms": repo.list_vms()})

    @app.get("/vms", response_class=HTMLResponse)
    async def vms_page(request: Request):
        try:
            account = current_user(request, repo, cfg)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse("vms.html", {"request": request, "user": account, "vms": repo.list_vms()})

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
        return templates.TemplateResponse("vm_detail.html", {"request": request, "user": account, "vm": vm, "lines": lines})

    @app.get("/admin/vms", response_class=HTMLResponse)
    async def admin_vms_page(request: Request):
        try:
            account = current_user(request, repo, cfg)
            if account["role"] != "admin":
                raise HTTPException(403)
        except HTTPException as exc:
            return RedirectResponse("/login" if exc.status_code == 401 else "/dashboard", status_code=303)
        return templates.TemplateResponse("admin_vms.html", {"request": request, "user": account, "vms": repo.list_vms()})

    @app.get("/admin/vms/{vm_id}/edit", response_class=HTMLResponse)
    async def edit_vm_page(vm_id: str, request: Request):
        try:
            account = current_user(request, repo, cfg)
            if account["role"] != "admin":
                raise HTTPException(403)
        except HTTPException as exc:
            return RedirectResponse("/login" if exc.status_code == 401 else "/dashboard", status_code=303)
        vm = repo.get_vm(vm_id)
        if vm is None:
            return RedirectResponse("/admin/vms", status_code=303)
        return templates.TemplateResponse("vm_edit.html", {"request": request, "user": account, "vm": vm})

    @app.get("/admin/resources", response_class=HTMLResponse)
    async def resources_page(request: Request):
        try:
            account = current_user(request, repo, cfg)
            if account["role"] != "admin":
                raise HTTPException(403)
        except HTTPException as exc:
            return RedirectResponse("/login" if exc.status_code == 401 else "/dashboard", status_code=303)
        return templates.TemplateResponse("resources.html", {"request": request, "user": account, "resources": repo.list_resources()})

    @app.get("/admin/logs", response_class=HTMLResponse)
    async def admin_logs_page(request: Request):
        try:
            account = current_user(request, repo, cfg)
            if account["role"] != "admin":
                raise HTTPException(403)
        except HTTPException as exc:
            return RedirectResponse("/login" if exc.status_code == 401 else "/dashboard", status_code=303)
        log_items = []
        for vm in repo.list_vms():
            try:
                lines = docker_manager.logs(vm, tail=200) if vm.get("container_id") else []
                log_items.append({"vm": vm, "lines": lines, "error": None})
            except Exception as exc:
                log_items.append({"vm": vm, "lines": [], "error": str(exc)})
        return templates.TemplateResponse("logs.html", {"request": request, "user": account, "logs": log_items})

    return app


app = create_app()
