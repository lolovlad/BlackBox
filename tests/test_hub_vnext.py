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
        self.devices = []

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
        self.running = False
        self.removed = True
        self.attrs["State"]["Status"] = "dead"

    def logs(self, stream=False, timestamps=True, tail=200):
        return b"2026-09-15T00:00:00Z worker ready\n"


class FakeContainers:
    def __init__(self):
        self.items = {}

    def create(self, image, name=None, **kwargs):
        item = FakeContainer(name or f"anon-{len(self.items)}", environment=kwargs.get("environment"))
        item.devices = list(kwargs.get("devices") or [])
        self.items[item.name] = item
        return item

    def run(self, image, name, **kwargs):
        item = self.create(image, name=name, **kwargs)
        item.start()
        return item

    def get(self, key):
        for item in self.items.values():
            if key in {item.id, item.name} and not getattr(item, "removed", False):
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
    docker = FakeDocker()
    client = TestClient(create_app(cfg, docker_client=docker))
    client.fake_docker = docker
    return client


SIM_DOCUMENT = {
    "requests": [{"name": "sim", "fc": 3, "address": 0, "count": 3}],
    "fields": [
        {"name": "counter", "type": "uint16", "source": "sim", "address": 0},
        {"name": "digital_1", "type": "bool", "source": "sim", "address": 1},
        {"name": "analog_1", "type": "expr", "expr": "counter * 0.5", "round": True},
    ],
}


def _publish_map(client: TestClient, csrf: str, *, protocol: str = "simulator", version: str = "default-v1", document: dict | None = None) -> str:
    body: dict = {"protocol": protocol, "version": version, "document": document or SIM_DOCUMENT}
    if protocol != "simulator" and document is None:
        payload = json.loads((Path(__file__).resolve().parent.parent / "presets" / "maps" / protocol / "deif-gempac-v1.json").read_text(encoding="utf-8"))
        protocol = payload.pop("protocol", protocol)
        version = payload.pop("version", version)
        preset_id = payload.pop("preset_id", None)
        body = {"protocol": protocol, "version": version, "document": payload}
        if preset_id:
            body["preset_id"] = preset_id
    response = client.post("/api/v1/maps", json=body, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    return version


def test_hub_login_crud_and_role_guard(tmp_path: Path):
    with _client(tmp_path) as client:
        assert client.get("/healthz").json()["status"] == "ok"
        login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"})
        assert login.status_code == 200
        assert client.get("/api/v1/auth/me").json()["role"] == "admin"
        csrf = client.cookies.get("bb_csrf")
        _publish_map(client, csrf)
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
        logs = client.get(f"/api/v1/vms/{vm_id}/logs").json()
        assert logs["lines"]
        blob = "\n".join(logs["lines"]).lower()
        assert "старт" in blob
        assert "worker ready" in blob
        assert any(item.get("kind") == "lifecycle" for item in logs["entries"])
        stopped = client.post(f"/api/v1/vms/{vm_id}/stop", headers={"X-CSRF-Token": csrf})
        assert stopped.status_code == 200
        after_stop = client.get(f"/api/v1/vms/{vm_id}/logs").json()
        stop_blob = "\n".join(after_stop["lines"]).lower()
        assert "останов" in stop_blob
        restarted = client.post(f"/api/v1/vms/{vm_id}/restart", headers={"X-CSRF-Token": csrf})
        assert restarted.status_code == 200
        after_restart = client.get(f"/api/v1/vms/{vm_id}/logs").json()
        restart_blob = "\n".join(after_restart["lines"]).lower()
        assert "перезапуск" in restart_blob
        reading = client.get(f"/api/v1/vms/{vm_id}/reading").json()
        assert reading["vm_id"] == vm_id
        assert "fields" in reading
        assert "tags" in reading
        assert "analog" in reading and "discrete" in reading and "alerts" in reading
        assert reading["diagnosis"]["code"] == "no_sample"


def test_read_timeout_keeps_vm_running_and_preserves_error(tmp_path: Path):
    with _client(tmp_path) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
        csrf = client.cookies.get("bb_csrf")
        _publish_map(client, csrf)
        created = client.post(
            "/api/v1/vms",
            json={"name": "sim-timeout", "protocol": "simulator", "map_version": "default-v1"},
            headers={"X-CSRF-Token": csrf},
        ).json()
        vm_id = created["id"]
        assert client.post(f"/api/v1/vms/{vm_id}/start", headers={"X-CSRF-Token": csrf}).status_code == 200
        app = client._transport.app  # type: ignore[attr-defined]
        token = app.state.worker_tokens[vm_id]
        headers = {"X-Worker-Token": token}
        reported = client.post(
            "/api/v1/internal/workers/error",
            json={
                "vm_id": vm_id,
                "code": "read_failed",
                "message": "Modbus request hr failed after 3 retries — No communication with the instrument (no answer)",
                "timestamp": "2026-09-21T14:35:13Z",
            },
            headers=headers,
        )
        assert reported.status_code == 200, reported.text
        state = client.get(f"/api/v1/vms/{vm_id}").json()
        assert state["lifecycle"] == "running"
        assert "No communication" in state["last_error"]
        journal = "\n".join(client.get(f"/api/v1/vms/{vm_id}/logs").json()["lines"])
        assert "Чтение не удалось" in journal
        assert "running → failed" not in journal
        heartbeat = client.post(
            "/api/v1/internal/workers/heartbeat",
            json={"vm_id": vm_id, "worker_id": "worker", "timestamp": "2026-09-21T14:35:14Z", "seq": 2, "health": "unhealthy"},
            headers=headers,
        )
        assert heartbeat.status_code == 200
        after_hb = client.get(f"/api/v1/vms/{vm_id}").json()
        assert after_hb["lifecycle"] == "running"
        assert "No communication" in after_hb["last_error"]
        batch = RawBatch(
            vm_id=UUID(vm_id),
            protocol=VmProtocol.SIMULATOR,
            map_version="default-v1",
            seq_start=1,
            samples=[RawSample(seq=1, captured_at="2026-09-21T14:36:00Z", sources={"sim": [4, 1, 8]})],
        )
        ingested = client.post("/api/v1/internal/workers/batches", json=batch.model_dump(mode="json"), headers=headers)
        assert ingested.status_code == 200, ingested.text
        recovered = client.get(f"/api/v1/vms/{vm_id}").json()
        assert not recovered.get("last_error")


def test_reading_splits_channels_and_separates_link_from_device_alerts(tmp_path: Path):
    document = {
        "requests": [
            {"name": "hr", "fc": 3, "address": 0, "count": 20},
            {"name": "coils", "fc": 1, "address": 0, "count": 4},
        ],
        "fields": [
            {"name": "RPM", "type": "uint16", "source": "hr", "address": 0, "kind": "analog"},
            {"name": "Engine_running", "type": "bool", "source": "coils", "address": 0, "kind": "discrete"},
            {
                "name": "active_alarms",
                "type": "bitfield",
                "source": "hr",
                "address": 19,
                "kind": "alert",
                "bits": {"0": "BUS High Volt", "1": "Overspeed"},
            },
        ],
    }
    with _client(tmp_path) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
        csrf = client.cookies.get("bb_csrf")
        _publish_map(client, csrf, version="channels-v1", document=document)
        vm = client.post(
            "/api/v1/vms",
            json={"name": "channels", "protocol": "simulator", "map_version": "channels-v1"},
            headers={"X-CSRF-Token": csrf},
        ).json()
        vm_id = vm["id"]
        assert client.post(f"/api/v1/vms/{vm_id}/start", headers={"X-CSRF-Token": csrf}).status_code == 200
        app = client._transport.app  # type: ignore[attr-defined]
        token = app.state.worker_tokens[vm_id]
        headers = {"X-Worker-Token": token}
        silent = client.post(
            "/api/v1/internal/workers/error",
            json={
                "vm_id": vm_id,
                "code": "read_failed",
                "message": "Modbus request hr failed after 3 retries — No communication with the instrument (no answer)",
                "timestamp": "2026-09-21T14:35:13Z",
            },
            headers=headers,
        )
        assert silent.status_code == 200, silent.text
        before_link = client.get(f"/api/v1/vms/{vm_id}/reading").json()
        assert before_link["diagnosis"]["code"] == "no_answer"
        assert before_link["diagnosis"]["can_read_alerts"] is False
        holding = [0] * 20
        holding[0] = 1500
        holding[19] = 1
        good = RawBatch(
            vm_id=UUID(vm_id),
            protocol=VmProtocol.SIMULATOR,
            map_version="channels-v1",
            seq_start=1,
            samples=[RawSample(seq=1, captured_at="2026-09-21T14:36:00Z", sources={"hr": holding, "coils": [1, 0, 0, 0]})],
        )
        assert client.post("/api/v1/internal/workers/batches", json=good.model_dump(mode="json"), headers=headers).status_code == 200
        live = client.get(f"/api/v1/vms/{vm_id}/reading").json()
        assert live["diagnosis"]["code"] == "device_alerts"
        assert live["diagnosis"]["can_read_alerts"] is True
        analog = {row["name"]: row["value"] for row in live["analog"]}
        discrete = {row["name"]: row["value"] for row in live["discrete"]}
        alerts = {row["name"]: row["active"] for row in live["alerts"]}
        assert analog["RPM"] == 1500
        assert discrete["Engine_running"] is True
        assert alerts["BUS High Volt"] is True
        assert alerts["Overspeed"] is False
        journal = "\n".join(client.get(f"/api/v1/vms/{vm_id}/logs").json()["lines"])
        assert "Алерт прибора: BUS High Volt" in journal
        lost = RawBatch(
            vm_id=UUID(vm_id),
            protocol=VmProtocol.SIMULATOR,
            map_version="channels-v1",
            seq_start=2,
            samples=[RawSample(seq=2, captured_at="2026-09-21T14:37:00Z", sources={}, quality="bad")],
        )
        client.post(
            "/api/v1/internal/workers/error",
            json={
                "vm_id": vm_id,
                "code": "read_failed",
                "message": "Modbus request hr failed after 3 retries — No communication with the instrument (no answer)",
                "timestamp": "2026-09-21T14:37:00Z",
            },
            headers=headers,
        )
        assert client.post("/api/v1/internal/workers/batches", json=lost.model_dump(mode="json"), headers=headers).status_code == 200
        stale = client.get(f"/api/v1/vms/{vm_id}/reading").json()
        assert stale["diagnosis"]["code"] == "no_answer"
        assert stale["alerts_stale"] is True
        assert stale["quality"] == "bad"
        assert {row["name"]: row["value"] for row in stale["analog"]}["RPM"] == 1500
        assert {row["name"]: row["active"] for row in stale["alerts"]}["BUS High Volt"] is True
        zeros = RawBatch(
            vm_id=UUID(vm_id),
            protocol=VmProtocol.SIMULATOR,
            map_version="channels-v1",
            seq_start=3,
            samples=[RawSample(seq=3, captured_at="2026-09-21T14:38:00Z", sources={"hr": [0] * 20, "coils": [0, 0, 0, 0]})],
        )
        assert client.post("/api/v1/internal/workers/batches", json=zeros.model_dump(mode="json"), headers=headers).status_code == 200
        quiet = client.get(f"/api/v1/vms/{vm_id}/reading").json()
        assert quiet["diagnosis"]["code"] == "ok"
        assert {row["name"]: row["value"] for row in quiet["analog"]}["RPM"] == 0
        assert all(not row["active"] for row in quiet["alerts"])


def test_modbus_vm_persists_reader_and_storage_settings(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BB_DISCOVERY_SERIAL_PATHS", "/dev/ttyUSB0")
    with _client(tmp_path) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
        csrf = client.cookies.get("bb_csrf")
        scanned = client.post("/api/v1/resources/scan", headers={"X-CSRF-Token": csrf})
        assert any(item["resource_id"] == "serial:/dev/ttyUSB0" for item in scanned.json()["items"])
        assert client.post("/api/v1/resources/serial:/dev/ttyUSB0/approve", headers={"X-CSRF-Token": csrf}).status_code == 200
        _publish_map(client, csrf, protocol="modbus_rtu")
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


def test_rtu_binds_approved_serial_path_when_form_sends_default_port(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BB_DISCOVERY_SERIAL_PATHS", "/dev/ttyAMA0")
    with _client(tmp_path) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
        csrf = client.cookies.get("bb_csrf")
        client.post("/api/v1/resources/scan", headers={"X-CSRF-Token": csrf})
        assert client.post("/api/v1/resources/serial:/dev/ttyAMA0/approve", headers={"X-CSRF-Token": csrf}).status_code == 200
        _publish_map(client, csrf, protocol="modbus_rtu")
        created = client.post(
            "/api/v1/vms",
            headers={"X-CSRF-Token": csrf},
            json={
                "name": "rtu-tty",
                "protocol": "modbus_rtu",
                "map_version": "deif-gempac-v1",
                "read_resources": [{"resource_id": "serial:/dev/ttyAMA0"}],
                "config": {"reader": {"port": "/dev/ttyUSB0"}},
            },
        )
        assert created.status_code == 200, created.text
        vm = created.json()
        assert vm["read_resources"][0]["path"] == "/dev/ttyAMA0"
        assert vm["config"]["reader"]["port"] == "/dev/ttyAMA0"


def test_controlling_tty_is_not_a_uart():
    from fastapi import HTTPException

    from services.hub.app import _is_usable_serial_port, _validate_reader_allowlist

    assert not _is_usable_serial_port("/dev/tty")
    assert not _is_usable_serial_port("/dev/tty0")
    assert not _is_usable_serial_port("/dev/console")
    assert _is_usable_serial_port("/dev/ttyAMA0")
    assert _is_usable_serial_port("/dev/ttyUSB0")
    assert _is_usable_serial_port("/dev/serial0")
    assert _is_usable_serial_port("COM3")
    assert _is_usable_serial_port("/dev/serial/by-id/usb-FTDI-if00")
    reader = {"port": "/dev/ttyAMA0"}
    try:
        _validate_reader_allowlist(None, "modbus_rtu", {"reader": reader}, [{"kind": "serial", "path": "/dev/tty"}])
        raise AssertionError("expected 409")
    except HTTPException as exc:
        assert exc.status_code == 409
        assert exc.detail["code"] == "read_resource_required"


def test_serial_scan_skips_controlling_tty(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BB_DISCOVERY_SERIAL_PATHS", "/dev/tty,/dev/ttyAMA0")
    with _client(tmp_path) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
        csrf = client.cookies.get("bb_csrf")
        scanned = client.post("/api/v1/resources/scan", headers={"X-CSRF-Token": csrf})
        ids = {item["resource_id"] for item in scanned.json()["items"]}
        assert "serial:/dev/tty" not in ids
        assert "serial:/dev/ttyAMA0" in ids


def test_scan_lists_forced_physical_resources_for_each_protocol(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BB_DISCOVERY_SERIAL_PATHS", "/dev/ttyUSB0")
    monkeypatch.setenv("BB_DISCOVERY_CAN_IFACES", "can0")
    monkeypatch.setenv("BB_DISCOVERY_GPIO_PATHS", "/dev/gpiochip0")
    monkeypatch.setenv("BB_DISCOVERY_TCP_ENDPOINTS", "10.0.0.8:502")
    monkeypatch.setenv("BB_DISCOVERY_TCP_SCAN", "0")
    with _client(tmp_path) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
        csrf = client.cookies.get("bb_csrf")
        scanned = client.post("/api/v1/resources/scan", headers={"X-CSRF-Token": csrf})
        payload = scanned.json()
        ids = {item["resource_id"] for item in payload["items"]}
        assert ids >= {"serial:/dev/ttyUSB0", "can:can0", "gpio:/dev/gpiochip0", "tcp:10.0.0.8:502", "storage:data"}
        assert payload["summary"]["serial"] >= 1
        assert payload["summary"]["can"] >= 1
        assert payload["summary"]["gpio"] >= 1
        assert payload["summary"]["tcp"] >= 1
        html = client.get("/admin/resources").text
        assert "Serial · Modbus RTU" in html
        assert "TCP · Modbus TCP" in html
        assert "CAN" in html
        assert "GPIO" in html
        assert "USB serial (ttyUSB0)" in html or "ttyUSB0" in html


def test_device_mappings_include_serial_aliases_and_reader_port():
    from services.hub.docker_manager import DockerManager

    mapped = DockerManager.device_mappings(
        {
            "read_resources": [
                {
                    "path": "/dev/ttyAMA10",
                    "metadata": {"aliases": ["/dev/serial0", "/dev/ttyAMA10"]},
                }
            ],
            "config": {"reader": {"port": "/dev/ttyAMA10"}},
        }
    )
    assert "/dev/ttyAMA10:/dev/ttyAMA10:rwm" in mapped
    assert "/dev/serial0:/dev/serial0:rwm" in mapped
    assert not any("/dev/tty:" in item or item.startswith("/dev/tty:") for item in mapped)


def test_rtu_worker_container_gets_new_uart_after_port_change(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BB_DISCOVERY_SERIAL_PATHS", "/dev/ttyUSB0,/dev/ttyAMA10")
    with _client(tmp_path) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
        csrf = client.cookies.get("bb_csrf")
        client.post("/api/v1/resources/scan", headers={"X-CSRF-Token": csrf})
        assert client.post("/api/v1/resources/serial:/dev/ttyUSB0/approve", headers={"X-CSRF-Token": csrf}).status_code == 200
        assert client.post("/api/v1/resources/serial:/dev/ttyAMA10/approve", headers={"X-CSRF-Token": csrf}).status_code == 200
        _publish_map(client, csrf, protocol="modbus_rtu")
        created = client.post(
            "/api/v1/vms",
            headers={"X-CSRF-Token": csrf},
            json={
                "name": "rtu-port-change",
                "protocol": "modbus_rtu",
                "map_version": "deif-gempac-v1",
                "read_resources": [{"resource_id": "serial:/dev/ttyUSB0"}],
            },
        )
        assert created.status_code == 200, created.text
        vm_id = created.json()["id"]
        started = client.post(f"/api/v1/vms/{vm_id}/start", headers={"X-CSRF-Token": csrf})
        assert started.status_code == 200, started.text
        container = client.fake_docker.containers.get(f"bb-vm-{vm_id}")
        assert any("ttyUSB0" in item for item in container.devices)
        patched = client.patch(
            f"/api/v1/vms/{vm_id}",
            headers={"X-CSRF-Token": csrf},
            json={"read_resources": [{"resource_id": "serial:/dev/ttyAMA10"}]},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["config"]["reader"]["port"] == "/dev/ttyAMA10"
        container = client.fake_docker.containers.get(f"bb-vm-{vm_id}")
        assert any("ttyAMA10" in item for item in container.devices)
        assert not any("ttyUSB0" in item for item in container.devices)


def test_modbus_tcp_vm_persists_host_and_port(tmp_path: Path):
    with _client(tmp_path) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
        csrf = client.cookies.get("bb_csrf")
        version = _publish_map(client, csrf, protocol="modbus_tcp", version="tcp-v1", document=SIM_DOCUMENT)
        created = client.post(
            "/api/v1/vms",
            headers={"X-CSRF-Token": csrf},
            json={
                "name": "tcp-loopback",
                "protocol": "modbus_tcp",
                "map_version": version,
                "config": {"reader": {"host": "127.0.0.1", "tcp_port": 1502, "unit_id": 4}},
            },
        )
        assert created.status_code == 200, created.text
        vm = created.json()
        assert vm["config"]["reader"]["host"] == "127.0.0.1"
        assert vm["config"]["reader"]["tcp_port"] == 1502
        assert vm["config"]["reader"]["unit_id"] == 4
        assert vm["storage_resource_id"] == "storage:data"
        remote = client.post(
            "/api/v1/vms",
            headers={"X-CSRF-Token": csrf},
            json={
                "name": "tcp-remote",
                "protocol": "modbus_tcp",
                "map_version": version,
                "config": {"reader": {"host": "192.168.1.10", "tcp_port": 502}},
            },
        )
        assert remote.status_code == 409
        assert remote.json()["code"] == "read_resource_required"


def test_admin_vm_form_has_protocol_specific_settings(tmp_path: Path):
    with _client(tmp_path) as client:
        client.post("/login", data={"username": "admin", "password": "admin-password"})
        html = client.get("/vms").text
        assert 'data-vm-workspace' in html
        assert 'bb-vm-card' in html or 'Нет виртуальных машин' in html
        assert 'Добавить' in html
        assert 'id="create-vm"' in html
        assert 'data-protocol-panel="simulator"' in html
        assert 'data-protocol-panel="modbus_rtu"' in html
        assert 'data-protocol-panel="modbus_tcp"' in html
        assert 'data-protocol-panel="can"' in html
        assert 'name="serial_resource_id"' in html
        assert 'data-device-list="modbus_rtu"' in html
        assert '<span class="bb-step-num">1</span> Имя' in html
        assert '<span class="bb-step-num">2</span> Протокол' in html
        assert '<span class="bb-step-num">3</span> Подключение' in html
        assert '<span class="bb-step-num">4</span> Карта и проверка' in html
        assert '<span class="bb-step-num">5</span> Хранение' in html
        assert html.find('name="name"') < html.find('name="protocol"')
        assert html.find('data-probe-scan') < html.find('data-probe-read')
        assert html.find('data-probe-read') < html.find('name="storage_resource_id"')
        assert "Найти устройства" in html
        assert "Проверить чтение" in html
        assert 'name="host"' in html
        assert 'name="tcp_port"' in html
        assert 'name="can_bitrate"' in html
        assert 'name="storage_resource_id"' in html
        assert 'name="min_free_mb"' in html
        assert 'name="description"' in html
        assert 'bb-field-hint' in html
        assert 'bb-protocol-pick' in html
        assert 'name="preset_id"' not in html
        assert 'name="read_resource_id"' not in html
        assert 'Админ ВМ' not in html


def test_exclusive_read_resource_conflict_is_rejected_on_second_start(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BB_DISCOVERY_SERIAL_PATHS", "/dev/ttyUSB0")
    with _client(tmp_path) as client:
        client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"})
        csrf = client.cookies.get("bb_csrf")
        client.post("/api/v1/resources/scan", headers={"X-CSRF-Token": csrf})
        client.post("/api/v1/resources/serial:/dev/ttyUSB0/approve", headers={"X-CSRF-Token": csrf})
        _publish_map(client, csrf, protocol="modbus_rtu")
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


def test_scan_candidates_and_create_auto_approves_serial(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BB_DISCOVERY_SERIAL_PATHS", "/dev/ttyUSB0")
    with _client(tmp_path) as client:
        client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"})
        csrf = client.cookies.get("bb_csrf")
        scanned = client.post("/api/v1/resources/scan?network=false", headers={"X-CSRF-Token": csrf})
        assert scanned.status_code == 200, scanned.text
        serials = scanned.json()["candidates"]["modbus_rtu"]
        assert any(item["resource_id"] == "serial:/dev/ttyUSB0" for item in serials)
        listed = client.get("/api/v1/resources/candidates", params={"protocol": "modbus_rtu"}).json()["items"]
        assert any(item["resource_id"] == "serial:/dev/ttyUSB0" for item in listed)
        before = {item["resource_id"]: item["approved"] for item in client.get("/api/v1/resources").json()["items"]}
        assert before.get("serial:/dev/ttyUSB0") is False
        _publish_map(client, csrf, protocol="modbus_rtu")
        created = client.post(
            "/api/v1/vms",
            headers={"X-CSRF-Token": csrf},
            json={
                "name": "auto-serial",
                "protocol": "modbus_rtu",
                "map_version": "deif-gempac-v1",
                "read_resources": [{"resource_id": "serial:/dev/ttyUSB0"}],
                "config": {"reader": {"port": "/dev/ttyUSB0"}},
            },
        )
        assert created.status_code == 200, created.text
        after = {item["resource_id"]: item["approved"] for item in client.get("/api/v1/resources").json()["items"]}
        assert after.get("serial:/dev/ttyUSB0") is True


def test_probe_simulator_returns_split_channels(tmp_path: Path):
    with _client(tmp_path) as client:
        client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"})
        csrf = client.cookies.get("bb_csrf")
        _publish_map(client, csrf)
        probe = client.post(
            "/api/v1/vms/probe",
            headers={"X-CSRF-Token": csrf},
            json={"protocol": "simulator", "map_version": "default-v1", "config": {"reader": {"poll_interval_sec": 0.12}}},
        )
        assert probe.status_code == 200, probe.text
        body = probe.json()
        assert body["ok"] is True
        assert body["diagnosis"]["code"] in {"ok", "device_alerts"}
        analog = {row["name"]: row["value"] for row in body["analog"]}
        discrete = {row["name"]: row["value"] for row in body["discrete"]}
        assert analog["counter"] == 1
        assert analog["analog_1"] == 0.5
        assert discrete["digital_1"] is True


def test_probe_rtu_reads_map_or_explains_silence(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BB_DISCOVERY_SERIAL_PATHS", "/dev/ttyUSB0")
    with _client(tmp_path) as client:
        client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"})
        csrf = client.cookies.get("bb_csrf")
        client.post("/api/v1/resources/scan?network=false", headers={"X-CSRF-Token": csrf})
        _publish_map(client, csrf, protocol="modbus_rtu")
        silent = client.post(
            "/api/v1/vms/probe",
            headers={"X-CSRF-Token": csrf},
            json={
                "protocol": "modbus_rtu",
                "map_version": "deif-gempac-v1",
                "read_resources": [{"resource_id": "serial:/dev/ttyUSB0"}],
                "config": {"reader": {"port": "/dev/ttyUSB0", "slave_id": 1, "baudrate": 9600}},
            },
        )
        assert silent.status_code == 200, silent.text
        assert silent.json()["ok"] is False
        assert silent.json()["diagnosis"]["code"] == "no_answer"
        assert silent.json()["diagnosis"]["can_read_alerts"] is False

        def fake_read(reader, requests):
            holding = [0] * 90
            holding[38] = 1500
            holding[19] = 1
            coils = [0] * 32
            coils[0] = 1
            return {"holding": holding, "coils": coils}, None

        monkeypatch.setattr("services.hub.probe.read_rtu_sources", fake_read)
        live = client.post(
            "/api/v1/vms/probe",
            headers={"X-CSRF-Token": csrf},
            json={
                "protocol": "modbus_rtu",
                "map_version": "deif-gempac-v1",
                "read_resources": [{"resource_id": "serial:/dev/ttyUSB0"}],
                "config": {"reader": {"port": "/dev/ttyUSB0", "slave_id": 1, "baudrate": 9600}},
            },
        )
        assert live.status_code == 200, live.text
        body = live.json()
        assert body["ok"] is True
        analog = {row["name"]: row["value"] for row in body["analog"]}
        discrete = {row["name"]: row["value"] for row in body["discrete"]}
        alerts = {row["name"]: row["active"] for row in body["alerts"]}
        assert analog["RPM"] == 1500
        assert discrete["Engine_running"] is True
        assert alerts["BUS High Volt"] is True
        assert body["diagnosis"]["code"] == "device_alerts"


def test_probe_busy_when_serial_is_leased(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BB_DISCOVERY_SERIAL_PATHS", "/dev/ttyUSB0")
    with _client(tmp_path) as client:
        client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"})
        csrf = client.cookies.get("bb_csrf")
        client.post("/api/v1/resources/scan?network=false", headers={"X-CSRF-Token": csrf})
        _publish_map(client, csrf, protocol="modbus_rtu")
        payload = {
            "protocol": "modbus_rtu",
            "map_version": "deif-gempac-v1",
            "read_resources": [{"resource_id": "serial:/dev/ttyUSB0"}],
            "config": {"reader": {"port": "/dev/ttyUSB0"}},
        }
        vm = client.post("/api/v1/vms", headers={"X-CSRF-Token": csrf}, json={"name": "holder", **payload}).json()
        assert client.post(f"/api/v1/vms/{vm['id']}/start", headers={"X-CSRF-Token": csrf}).status_code == 200
        probe = client.post(
            "/api/v1/vms/probe",
            headers={"X-CSRF-Token": csrf},
            json={
                "protocol": "modbus_rtu",
                "map_version": "deif-gempac-v1",
                "read_resources": [{"resource_id": "serial:/dev/ttyUSB0"}],
                "config": {"reader": {"port": "/dev/ttyUSB0"}},
            },
        )
        assert probe.status_code == 200, probe.text
        assert probe.json()["diagnosis"]["code"] == "busy"
        assert probe.json()["ok"] is False


def test_html_pages_render_with_current_starlette(tmp_path: Path):
    """The Starlette TemplateResponse request/name order must stay explicit."""
    with _client(tmp_path) as client:
        assert client.get("/login").status_code == 200
        login_html = client.get("/login").text
        assert "AGK" in login_html
        assert '2.0.17' in login_html
        assert "bb-login" in login_html
        app_js = login_html.find("/static/app.js")
        alpine_js = login_html.find("/static/vendor/alpine.min.js")
        assert 0 <= app_js < alpine_js
        form_login = client.post("/login", data={"username": "admin", "password": "admin-password"}, follow_redirects=False)
        assert form_login.status_code == 303
        csrf = client.cookies.get("bb_csrf")
        _publish_map(client, csrf)
        vm = client.post(
            "/api/v1/vms",
            json={"name": "page-sim", "protocol": "simulator", "map_version": "default-v1"},
            headers={"X-CSRF-Token": csrf},
        ).json()

        pages = [
            "/dashboard",
            "/vms",
            f"/vms/{vm['id']}",
            f"/admin/vms/{vm['id']}/edit",
            "/admin/maps",
            "/admin/resources",
        ]
        for path in pages:
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert "<html" in response.text.lower()
            assert "AGK" in response.text
            assert "2.0.17" in response.text
            if path in {"/vms", f"/vms/{vm['id']}", f"/admin/vms/{vm['id']}/edit"}:
                assert 'data-vm-action="delete"' in response.text
                assert "Удалить" in response.text
            if path in {"/vms", f"/vms/{vm['id']}"}:
                assert "bb-vm-card" in response.text
                assert 'data-vm-action="start"' in response.text
                assert "bb-icon-btn-play" in response.text
                assert "Аналоги" in response.text
                assert "Дискреты" in response.text
                assert "Алерты прибора" in response.text
                assert "Найти устройства" in response.text
                assert "Проверить чтение" in response.text
        for path in ("/admin/vms", "/admin/logs"):
            redirected = client.get(path, follow_redirects=False)
            assert redirected.status_code == 303, path
            assert redirected.headers.get("location") == "/vms"


def test_admin_deletes_vm_container_and_related_files(tmp_path: Path):
    with _client(tmp_path) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
        csrf = client.cookies.get("bb_csrf")
        _publish_map(client, csrf)
        keep = client.post(
            "/api/v1/vms",
            json={"name": "keep-sim", "protocol": "simulator", "map_version": "default-v1"},
            headers={"X-CSRF-Token": csrf},
        ).json()
        victim = client.post(
            "/api/v1/vms",
            json={"name": "drop-sim", "protocol": "simulator", "map_version": "default-v1", "config": {"buffer": {"ram_rows": 1}}},
            headers={"X-CSRF-Token": csrf},
        ).json()
        started = client.post(f"/api/v1/vms/{victim['id']}/start", headers={"X-CSRF-Token": csrf})
        assert started.status_code == 200, started.text
        telemetry = tmp_path / "data" / "telemetry" / f"vm_id={victim['id']}" / "date=2026-09-18"
        telemetry.mkdir(parents=True)
        (telemetry / "part-test.parquet").write_bytes(b"data")
        logs = tmp_path / "data" / "logs" / f"vm_id={victim['id']}"
        logs.mkdir(parents=True)
        (logs / "worker.log").write_text("line\n", encoding="utf-8")
        keep_dir = tmp_path / "data" / "telemetry" / f"vm_id={keep['id']}"
        keep_dir.mkdir(parents=True)
        (keep_dir / "keep.parquet").write_bytes(b"keep")
        js = (Path(__file__).resolve().parent.parent / "ui" / "static" / "app.js").read_text(encoding="utf-8")
        assert "все связанные с этой ВМ файлы" in js
        deleted = client.delete(f"/api/v1/vms/{victim['id']}", headers={"X-CSRF-Token": csrf})
        assert deleted.status_code == 200, deleted.text
        body = deleted.json()
        assert body["ok"] is True
        assert any(f"vm_id={victim['id']}" in path for path in body["deleted_paths"])
        assert client.get(f"/api/v1/vms/{victim['id']}").status_code == 404
        remaining = client.get("/api/v1/vms").json()["items"]
        assert [item["name"] for item in remaining] == ["keep-sim"]
        assert not (tmp_path / "data" / "telemetry" / f"vm_id={victim['id']}").exists()
        assert not logs.exists()
        assert (keep_dir / "keep.parquet").read_bytes() == b"keep"
        app = client._transport.app  # type: ignore[attr-defined]
        assert victim["id"] not in app.state.worker_tokens
        with app.state.repo.connect() as connection:
            assert connection.execute("SELECT 1 FROM ingest_batches WHERE vm_id=?", (victim["id"],)).fetchone() is None
            assert connection.execute("SELECT 1 FROM lifecycle_events WHERE vm_id=?", (victim["id"],)).fetchone() is None
            assert connection.execute("SELECT 1 FROM resource_leases WHERE vm_id=?", (victim["id"],)).fetchone() is None
        try:
            app.state.docker.client.containers.get(f"bb-vm-{victim['id']}")
            raise AssertionError("container should be removed")
        except KeyError:
            pass
        never_started = client.post(
            "/api/v1/vms",
            json={"name": "never-sim", "protocol": "simulator", "map_version": "default-v1"},
            headers={"X-CSRF-Token": csrf},
        ).json()
        assert client.delete(f"/api/v1/vms/{never_started['id']}", headers={"X-CSRF-Token": csrf}).status_code == 200
        missing = client.delete("/api/v1/vms/00000000-0000-0000-0000-000000000000", headers={"X-CSRF-Token": csrf})
        assert missing.status_code == 404


def test_batch_is_idempotent_and_parser_is_used(tmp_path: Path):
    with _client(tmp_path) as client:
        login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"})
        assert login.status_code == 200
        csrf = client.cookies.get("bb_csrf")
        _publish_map(client, csrf)
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
        _publish_map(client, csrf)
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
        _publish_map(admin, csrf)
        vm = admin.post("/api/v1/vms", json={"name": "readonly-sim", "protocol": "simulator", "map_version": "default-v1"}, headers={"X-CSRF-Token": csrf}).json()

        # Reuse one lifespan and replace the browser cookies with a reader's
        # session; opening two lifespans on the same FastAPI object would share
        # the bounded asyncio queue across event loops.
        admin.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})
        admin.cookies.clear()
        assert admin.post("/api/v1/auth/login", json={"username": "reader", "password": "reader-password"}).status_code == 200
        assert admin.get("/api/v1/auth/me").json()["role"] == "user"
        assert admin.post(f"/api/v1/vms/{vm['id']}/start", headers={"X-CSRF-Token": admin.cookies.get("bb_csrf")}).status_code == 403
        assert admin.delete(f"/api/v1/vms/{vm['id']}", headers={"X-CSRF-Token": admin.cookies.get("bb_csrf")}).status_code == 403
        assert admin.delete("/api/v1/maps/default-v1", params={"protocol": "simulator"}, headers={"X-CSRF-Token": admin.cookies.get("bb_csrf")}).status_code == 403
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


def test_hub_does_not_seed_default_maps(tmp_path: Path):
    with _client(tmp_path) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
        assert client.get("/api/v1/maps").json()["items"] == []
        missing = client.post(
            "/api/v1/vms",
            json={"name": "no-map", "protocol": "simulator", "map_version": "default-v1"},
            headers={"X-CSRF-Token": client.cookies.get("bb_csrf")},
        )
        assert missing.status_code == 422
        assert missing.json()["code"] == "map_not_found"


def test_maps_page_opens_version_studio(tmp_path: Path):
    with _client(tmp_path) as client:
        client.post("/login", data={"username": "admin", "password": "admin-password"})
        html = client.get("/admin/maps").text
        assert "bb-maps-page" in html
        assert "bb-studio" in html
        assert "Новая версия" in html
        assert "Опубликовать версию" in html
        assert "Удалить" in html
        start = html.find('id="bb-maps-payload">')
        end = html.find("</script>", start)
        payload = html[start + len('id="bb-maps-payload">') : end]
        maps = json.loads(payload)
        assert maps == []


def test_map_edits_publish_as_a_new_immutable_version(tmp_path: Path):
    with _client(tmp_path) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
        csrf = client.cookies.get("bb_csrf")
        original = {"requests": [{"name": "sim", "fc": 3, "address": 0, "count": 1}], "fields": [{"name": "value", "type": "uint16", "source": "sim", "address": 0}]}
        edited = {"requests": [{"name": "sim", "fc": 3, "address": 0, "count": 2}], "fields": [{"name": "value", "type": "uint16", "source": "sim", "address": 0}]}
        assert client.post("/api/v1/maps", json={"protocol": "simulator", "version": "edit-v1", "document": original}, headers={"X-CSRF-Token": csrf}).status_code == 200
        created = client.post("/api/v1/maps", json={"protocol": "simulator", "version": "edit-v2", "document": edited}, headers={"X-CSRF-Token": csrf})
        assert created.status_code == 200
        assert created.json()["version"] == "edit-v2"
        first = client.get("/api/v1/maps/edit-v1", params={"protocol": "simulator"}).json()
        second = client.get("/api/v1/maps/edit-v2", params={"protocol": "simulator"}).json()
        assert first["document"]["requests"][0]["count"] == 1
        assert second["document"]["requests"][0]["count"] == 2


def test_admin_can_delete_unused_map_but_not_assigned_map(tmp_path: Path):
    with _client(tmp_path) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
        csrf = client.cookies.get("bb_csrf")
        _publish_map(client, csrf, version="keep-v1")
        _publish_map(client, csrf, version="drop-v1")
        vm = client.post(
            "/api/v1/vms",
            json={"name": "mapped", "protocol": "simulator", "map_version": "keep-v1"},
            headers={"X-CSRF-Token": csrf},
        ).json()
        blocked = client.delete("/api/v1/maps/keep-v1", params={"protocol": "simulator"}, headers={"X-CSRF-Token": csrf})
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "map_in_use"
        assert vm["id"] in blocked.json()["details"]
        removed = client.delete("/api/v1/maps/drop-v1", params={"protocol": "simulator"}, headers={"X-CSRF-Token": csrf})
        assert removed.status_code == 200, removed.text
        assert client.get("/api/v1/maps/drop-v1", params={"protocol": "simulator"}).status_code == 404
        assert client.get("/api/v1/maps/keep-v1", params={"protocol": "simulator"}).status_code == 200
        missing = client.delete("/api/v1/maps/drop-v1", params={"protocol": "simulator"}, headers={"X-CSRF-Token": csrf})
        assert missing.status_code == 404
        assert client.delete("/api/v1/maps/keep-v1", headers={"X-CSRF-Token": csrf}).status_code == 422


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
