from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient

from bb_platform.contracts import RawBatch, RawSample, VmProtocol
from bb_platform.parser import parse_source_values
from workers.modbus_rtu.main import ModbusReader
from services.hub.app import create_app
from services.hub.config import HubConfig


class FakeContainer:
    def __init__(self, name: str, *, environment: dict | None = None):
        self.id = f"container-{name}"
        self.name = name
        self.running = False
        env_list = [f"{key}={value}" for key, value in (environment or {}).items()]
        self.attrs = {"State": {"Status": "created", "Health": {"Status": "healthy"}, "Error": ""}, "Config": {"Env": env_list}}

    def reload(self):
        return None

    def start(self):
        self.running = True
        self.attrs["State"]["Status"] = "running"

    def stop(self, timeout=10):
        self.running = False
        self.attrs["State"]["Status"] = "exited"

    def restart(self, timeout=10):
        self.start()

    def remove(self, force=True):
        self.attrs["State"]["Status"] = "dead"

    def logs(self, stream=False, timestamps=True, tail=200):
        return b"2026-09-15T00:00:00Z worker ready\n"


class FakeContainers:
    def __init__(self):
        self.items = {}

    def create(self, image, name=None, **kwargs):
        item = FakeContainer(name or f"anon-{len(self.items)}", environment=kwargs.get("environment"))
        self.items[item.name] = item
        return item

    def run(self, image, name, **kwargs):
        item = self.create(image, name=name, **kwargs)
        item.start()
        return item

    def get(self, key):
        for item in self.items.values():
            if key in {item.id, item.name}:
                return item
        raise KeyError(key)


class FakeDocker:
    def __init__(self):
        self.containers = FakeContainers()


def _client(tmp_path: Path) -> TestClient:
    cfg = HubConfig(
        db_path=tmp_path / "hub.db",
        data_root=tmp_path / "data",
        jwt_secret="test-secret",
        bootstrap_password="admin-password",
        docker_enabled=True,
    )
    return TestClient(create_app(cfg, docker_client=FakeDocker()))


def test_hub_login_crud_and_role_guard(tmp_path: Path):
    with _client(tmp_path) as client:
        assert client.get("/healthz").json()["status"] == "ok"
        login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"})
        assert login.status_code == 200
        assert client.get("/api/v1/auth/me").json()["role"] == "admin"
        csrf = client.cookies.get("bb_csrf")
        created = client.post(
            "/api/v1/vms",
            json={"name": "sim-1", "protocol": "simulator", "map_version": "default-v1"},
            headers={"X-CSRF-Token": csrf},
        )
        assert created.status_code == 200
        vm_id = created.json()["id"]
        started = client.post(f"/api/v1/vms/{vm_id}/start", headers={"X-CSRF-Token": csrf})
        assert started.status_code == 200
        assert started.json()["lifecycle"] == "running"
        assert client.get("/api/v1/vms").json()["items"][0]["name"] == "sim-1"
        assert client.get(f"/api/v1/vms/{vm_id}/logs").json()["lines"]


def test_modbus_vm_persists_reader_and_storage_settings(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BB_DISCOVERY_SERIAL_PATHS", "/dev/ttyUSB0")
    with _client(tmp_path) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
        csrf = client.cookies.get("bb_csrf")
        scanned = client.post("/api/v1/resources/scan", headers={"X-CSRF-Token": csrf})
        assert any(item["resource_id"] == "serial:/dev/ttyUSB0" for item in scanned.json()["items"])
        assert client.post("/api/v1/resources/serial:/dev/ttyUSB0/approve", headers={"X-CSRF-Token": csrf}).status_code == 200
        response = client.post(
            "/api/v1/vms",
            headers={"X-CSRF-Token": csrf},
            json={
                "name": "rtu-settings",
                "protocol": "modbus_rtu",
                "map_version": "deif-gempac-v1",
                "read_resources": [{"resource_id": "serial:/dev/ttyUSB0"}],
                "config": {"reader": {"port": "/dev/ttyUSB0", "poll_interval_sec": 0.25, "baudrate": 19200}, "buffer": {"ram_rows": 120}},
            },
        )
        assert response.status_code == 200, response.text
        vm = response.json()
        assert vm["read_resources"][0]["path"] == "/dev/ttyUSB0"
        assert vm["config"]["reader"]["poll_interval_sec"] == 0.25
        assert vm["config"]["buffer"]["ram_rows"] == 120
        assert vm["storage_resource_id"] == "storage:data"


def test_exclusive_read_resource_conflict_is_rejected_on_second_start(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BB_DISCOVERY_SERIAL_PATHS", "/dev/ttyUSB0")
    with _client(tmp_path) as client:
        client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"})
        csrf = client.cookies.get("bb_csrf")
        client.post("/api/v1/resources/scan", headers={"X-CSRF-Token": csrf})
        client.post("/api/v1/resources/serial:/dev/ttyUSB0/approve", headers={"X-CSRF-Token": csrf})
        payload = {
            "protocol": "modbus_rtu",
            "map_version": "deif-gempac-v1",
            "read_resources": [{"resource_id": "serial:/dev/ttyUSB0"}],
            "config": {"reader": {"port": "/dev/ttyUSB0"}},
        }
        first = client.post("/api/v1/vms", headers={"X-CSRF-Token": csrf}, json={"name": "lease-a", **payload}).json()
        second = client.post("/api/v1/vms", headers={"X-CSRF-Token": csrf}, json={"name": "lease-b", **payload}).json()
        assert client.post(f"/api/v1/vms/{first['id']}/start", headers={"X-CSRF-Token": csrf}).status_code == 200
        blocked = client.post(f"/api/v1/vms/{second['id']}/start", headers={"X-CSRF-Token": csrf})
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "resource_conflict"


def test_html_pages_render_with_current_starlette(tmp_path: Path):
    """The Starlette TemplateResponse request/name order must stay explicit."""
    with _client(tmp_path) as client:
        assert client.get("/login").status_code == 200
        form_login = client.post("/login", data={"username": "admin", "password": "admin-password"}, follow_redirects=False)
        assert form_login.status_code == 303
        csrf = client.cookies.get("bb_csrf")
        vm = client.post(
            "/api/v1/vms",
            json={"name": "page-sim", "protocol": "simulator", "map_version": "default-v1"},
            headers={"X-CSRF-Token": csrf},
        ).json()

        pages = [
            "/dashboard",
            "/vms",
            f"/vms/{vm['id']}",
            "/admin/vms",
            f"/admin/vms/{vm['id']}/edit",
            "/admin/maps",
            "/admin/resources",
            "/admin/logs",
        ]
        for path in pages:
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert "<html" in response.text.lower()


def test_batch_is_idempotent_and_parser_is_used(tmp_path: Path):
    with _client(tmp_path) as client:
        login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"})
        assert login.status_code == 200
        csrf = client.cookies.get("bb_csrf")
        vm = client.post("/api/v1/vms", json={"name": "sim-1", "protocol": "simulator", "map_version": "default-v1"}, headers={"X-CSRF-Token": csrf}).json()
        client.post(f"/api/v1/vms/{vm['id']}/start", headers={"X-CSRF-Token": csrf})
        # The internal endpoint is exercised through the ASGI application's
        # state, where Hub stores the per-VM bootstrap token.
        app = client._transport.app  # type: ignore[attr-defined]
        token = app.state.worker_tokens[vm["id"]]
        batch = RawBatch(vm_id=UUID(vm["id"]), protocol=VmProtocol.SIMULATOR, map_version="default-v1", seq_start=1, samples=[RawSample(seq=1, captured_at="2026-09-15T00:00:00Z", sources={"sim": [4, 1, 8]})])
        headers = {"X-Worker-Token": token}
        first = client.post("/api/v1/internal/workers/batches", json=batch.model_dump(mode="json"), headers=headers)
        second = client.post("/api/v1/internal/workers/batches", json=batch.model_dump(mode="json"), headers=headers)
        assert first.status_code == 200 and first.json()["count"] == 1
        assert second.status_code == 200 and second.json()["duplicate"] is True


def test_batch_idempotency_is_scoped_to_vm_and_sequence(tmp_path: Path):
    with _client(tmp_path) as client:
        client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"})
        csrf = client.cookies.get("bb_csrf")
        first = client.post("/api/v1/vms", json={"name": "batch-a", "protocol": "simulator", "map_version": "default-v1"}, headers={"X-CSRF-Token": csrf}).json()
        second = client.post("/api/v1/vms", json={"name": "batch-b", "protocol": "simulator", "map_version": "default-v1"}, headers={"X-CSRF-Token": csrf}).json()
        app = client._transport.app  # type: ignore[attr-defined]
        token_a = app.state.worker_tokens.setdefault(first["id"], "token-a")
        token_b = app.state.worker_tokens.setdefault(second["id"], "token-b")
        batch_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        def send(vm_id: str, token: str):
            batch = RawBatch(batch_id=batch_id, vm_id=UUID(vm_id), protocol=VmProtocol.SIMULATOR, map_version="default-v1", seq_start=1, samples=[RawSample(seq=1, captured_at="2026-09-15T00:00:00Z", sources={"sim": [1, 0, 0]})])
            return client.post("/api/v1/internal/workers/batches", json=batch.model_dump(mode="json"), headers={"X-Worker-Token": token})
        assert send(first["id"], token_a).status_code == 200
        assert send(second["id"], token_b).status_code == 200
        assert send(first["id"], token_a).json()["duplicate"] is True


def test_user_is_read_only_and_websocket_gets_snapshot(tmp_path: Path):
    with _client(tmp_path) as admin:
        login = admin.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"})
        assert login.status_code == 200
        app = admin._transport.app  # type: ignore[attr-defined]
        app.state.repo.create_user("reader", "reader-password", "user")
        csrf = admin.cookies.get("bb_csrf")
        vm = admin.post("/api/v1/vms", json={"name": "readonly-sim", "protocol": "simulator", "map_version": "default-v1"}, headers={"X-CSRF-Token": csrf}).json()

        # Reuse one lifespan and replace the browser cookies with a reader's
        # session; opening two lifespans on the same FastAPI object would share
        # the bounded asyncio queue across event loops.
        admin.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})
        admin.cookies.clear()
        assert admin.post("/api/v1/auth/login", json={"username": "reader", "password": "reader-password"}).status_code == 200
        assert admin.get("/api/v1/auth/me").json()["role"] == "user"
        assert admin.post(f"/api/v1/vms/{vm['id']}/start", headers={"X-CSRF-Token": admin.cookies.get("bb_csrf")}).status_code == 403
        assert admin.get("/api/v1/vms").status_code == 200
        with admin.websocket_connect("/ws/v1/events") as websocket:
            snapshot = websocket.receive_json()
            assert snapshot["type"] == "snapshot"
            assert "payload" in snapshot


def test_parser_rejects_attribute_escape():
    tags, quality, errors = parse_source_values({"fields": [{"name": "bad", "type": "expr", "expr": "().__class__.__mro__"}]}, {})
    assert tags["bad"] is None
    assert errors and quality.value == "degraded"


def test_modbus_reader_retries_fake_instrument():
    class FakeInstrument:
        def __init__(self):
            self.calls = 0

        def read_registers(self, address, count):
            self.calls += 1
            if self.calls == 1:
                raise OSError("temporary")
            return [12, 13][:count]

        def read_bits(self, address, count, functioncode):
            return [1][:count]

    instrument = FakeInstrument()
    assert ModbusReader(instrument, retries=2).read([{"name": "holding", "fc": 3, "address": 0, "count": 2}]) == {"holding": [12, 13]}


def test_map_version_is_immutable(tmp_path: Path):
    with _client(tmp_path) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
        csrf = client.cookies.get("bb_csrf")
        first = {"requests": [{"name": "sim", "fc": 3, "address": 0, "count": 1}], "fields": [{"name": "value", "type": "uint16", "source": "sim", "address": 0}]}
        second = {"requests": [{"name": "sim", "fc": 3, "address": 0, "count": 2}], "fields": [{"name": "value", "type": "uint16", "source": "sim", "address": 0}]}
        assert client.post("/api/v1/maps", json={"protocol": "simulator", "version": "immutable-v1", "document": first}, headers={"X-CSRF-Token": csrf}).status_code == 200
        conflict = client.post("/api/v1/maps", json={"protocol": "simulator", "version": "immutable-v1", "document": second}, headers={"X-CSRF-Token": csrf})
        assert conflict.status_code == 409 and conflict.json()["code"] == "map_immutable"


def test_get_map_document_by_version(tmp_path: Path):
    with _client(tmp_path) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
        csrf = client.cookies.get("bb_csrf")
        document = {"requests": [{"name": "sim", "fc": 3, "address": 0, "count": 1}], "fields": [{"name": "value", "type": "uint16", "source": "sim", "address": 0}]}
        assert client.post("/api/v1/maps", json={"protocol": "simulator", "version": "read-v1", "document": document}, headers={"X-CSRF-Token": csrf}).status_code == 200
        fetched = client.get("/api/v1/maps/read-v1", params={"protocol": "simulator"})
        assert fetched.status_code == 200
        body = fetched.json()
        assert body["version"] == "read-v1"
        assert body["protocol"] == "simulator"
        assert body["document"]["fields"][0]["name"] == "value"
        assert client.get("/api/v1/maps/missing-v1").status_code == 404


def test_refresh_rotation_and_logout(tmp_path: Path):
    with _client(tmp_path) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
        old_refresh = client.cookies.get("bb_refresh")
        rotated = client.post("/api/v1/auth/refresh")
        assert rotated.status_code == 200
        assert client.cookies.get("bb_refresh") != old_refresh
        assert client.get("/api/v1/auth/me").status_code == 200
        assert client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": client.cookies.get("bb_csrf")}).status_code == 200
        assert client.get("/api/v1/auth/me").status_code == 401
