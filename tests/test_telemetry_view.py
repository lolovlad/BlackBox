from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from bb_platform.contracts import RawBatch, RawSample, TagSample, VmProtocol
from services.hub.db import HubRepository
from services.hub.monitor import collect_system_monitor
from services.hub.storage import ParquetStore
from services.hub.telemetry import query_measurements
from tests.test_hub_vnext import _client, _publish_map


CHANNEL_DOCUMENT = {
    "requests": [
        {"name": "hr", "fc": 3, "address": 0, "count": 20},
        {"name": "coils", "fc": 1, "address": 0, "count": 4},
    ],
    "fields": [
        {"name": "RPM", "type": "uint16", "source": "hr", "address": 0, "kind": "analog", "label": "Обороты"},
        {"name": "Engine_running", "type": "bool", "source": "coils", "address": 0, "kind": "discrete", "label": "Двигатель"},
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


def test_alarm_edges_do_not_repeat_unchanged_state(tmp_path: Path) -> None:
    repo = HubRepository(tmp_path / "hub.db")
    moment = datetime(2026, 9, 21, 14, 36, tzinfo=timezone.utc)
    started = repo.sync_alarm_edges("vm-1", moment, {"BUS High Volt"})
    assert [item["state"] for item in started] == ["active"]
    assert repo.sync_alarm_edges("vm-1", moment, {"BUS High Volt"}) == []
    ended = repo.sync_alarm_edges("vm-1", moment.replace(minute=38), set())
    assert [item["state"] for item in ended] == ["inactive"]
    rows, total = repo.list_alarm_events(["vm-1"], kind="alert")
    assert total == 2
    assert [row["state"] for row in rows] == ["inactive", "active"]


def test_parquet_roundtrip_includes_buffer_and_flushed_parts(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "telemetry", flush_rows=1)
    vm_id = uuid4()
    sample = TagSample(
        vm_id=vm_id,
        seq=3,
        captured_at=datetime(2026, 9, 21, 14, 36, tzinfo=timezone.utc),
        map_version="channels-v1",
        protocol=VmProtocol.SIMULATOR,
        analog={"RPM": 1500},
        discrete={"Engine_running": True},
        alerts=["BUS High Volt"],
    )
    store.append([sample], flush_rows=1)
    rows, truncated = query_measurements([store.root], [store], vm_ids={str(vm_id)})
    assert truncated is False
    assert len(rows) == 1
    assert rows[0].analog["RPM"] == 1500
    assert rows[0].discrete["Engine_running"] is True

    pending = ParquetStore(tmp_path / "other", flush_rows=100, flush_seconds=10_000)
    pending.append(
        [
            TagSample(
                vm_id=vm_id,
                seq=4,
                captured_at=datetime(2026, 9, 21, 14, 37, tzinfo=timezone.utc),
                map_version="channels-v1",
                protocol=VmProtocol.SIMULATOR,
                analog={"RPM": 1510},
                discrete={"Engine_running": True},
            )
        ],
        flush_rows=100,
        flush_seconds=10_000,
    )
    buffered, _truncated = query_measurements([pending.root], [pending], vm_ids={str(vm_id)})
    assert [item.seq for item in buffered] == [4]


def test_one_parquet_file_per_day(tmp_path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    root = tmp_path / "telemetry"
    store = ParquetStore(root, flush_rows=1)
    vm_id = uuid4()
    moment = datetime(2026, 9, 21, 14, 36, tzinfo=timezone.utc)
    for seq in (1, 2, 3):
        store.append([_sample(vm_id, seq, moment.replace(minute=36 + seq))], flush_rows=1)
    store.append([_sample(vm_id, 4, moment.replace(day=22))], flush_rows=1)
    files = sorted(path.relative_to(root).as_posix() for path in root.rglob("*.parquet"))
    assert files == [
        f"vm_id={vm_id}/date=2026-09-21.parquet",
        f"vm_id={vm_id}/date=2026-09-22.parquet",
    ]
    assert pq.read_metadata(root / files[0]).num_row_groups == 3
    rows, _truncated = query_measurements([root], [store], vm_ids={str(vm_id)})
    assert [item.seq for item in rows] == [4, 3, 2, 1]

    legacy = root / "vm_id=legacy" / "date=2026-09-20"
    legacy.mkdir(parents=True)
    table = pa.table(
        {
            "vm_id": ["legacy", "legacy"],
            "seq": pa.array([7, 8], type=pa.int64()),
            "captured_at": ["2026-09-20T01:00:00+00:00", "2026-09-20T02:00:00+00:00"],
            "map_version": ["channels-v1", "channels-v1"],
            "protocol": ["simulator", "simulator"],
            "quality": ["good", "good"],
            "tags_json": ["{}", "{}"],
            "analog_json": ['{"RPM": 1}', '{"RPM": 2}'],
            "discrete_json": ["{}", "{}"],
            "alerts_json": ["[]", "[]"],
        }
    )
    pq.write_table(table.slice(0, 1), legacy / "part-a.parquet", compression="zstd")
    pq.write_table(table.slice(1, 1), legacy / "part-b.parquet", compression="zstd")
    ParquetStore(root, flush_rows=100, flush_seconds=10_000).flush()
    assert not legacy.exists()
    merged = root / "vm_id=legacy" / "date=2026-09-20.parquet"
    assert merged.is_file()
    legacy_rows, _truncated = query_measurements([root], [], vm_ids={"legacy"})
    assert [item.seq for item in legacy_rows] == [8, 7]


def _sample(vm_id, seq: int, moment: datetime):
    return TagSample(
        vm_id=vm_id,
        seq=seq,
        captured_at=moment,
        map_version="channels-v1",
        protocol=VmProtocol.SIMULATOR,
        analog={"RPM": 1500},
        discrete={"Engine_running": True},
    )


def test_system_monitor_has_host_panel_fields(tmp_path: Path) -> None:
    stats = collect_system_monitor(tmp_path)
    assert set(stats) >= {"disk", "cpu", "memory", "process", "disks"}
    assert "percent" in stats["disk"]
    assert stats["process"]["pid"]


def test_values_charts_and_alarm_journal_for_many_sources(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
        csrf = client.cookies.get("bb_csrf")
        _publish_map(client, csrf, version="channels-v1", document=CHANNEL_DOCUMENT)
        vm_ids = []
        for name in ("gen-1", "gen-2"):
            created = client.post(
                "/api/v1/vms",
                json={"name": name, "protocol": "simulator", "map_version": "channels-v1"},
                headers={"X-CSRF-Token": csrf},
            )
            assert created.status_code == 200, created.text
            vm_id = created.json()["id"]
            assert client.post(f"/api/v1/vms/{vm_id}/start", headers={"X-CSRF-Token": csrf}).status_code == 200
            vm_ids.append(vm_id)
            _ingest(client, vm_id, seq=1, when="2026-09-21T14:36:00Z", rpm=1500, alarm_bit=1, running=1)
            _ingest(client, vm_id, seq=2, when="2026-09-21T14:36:30Z", rpm=1500, alarm_bit=1, running=1)
            _ingest(client, vm_id, seq=3, when="2026-09-21T14:38:00Z", rpm=0, alarm_bit=0, running=0)

        app = client._transport.app  # type: ignore[attr-defined]
        for target in app.state.storage_stores.values():
            target.flush()

        alarms = client.get("/api/v1/telemetry/rows", params=[("tab", "alarms"), ("vm_id", vm_ids[0]), ("vm_id", vm_ids[1])]).json()
        assert alarms["total_rows"] == 4
        states = {(row["vm_name"], row["name"], row["state"]) for row in alarms["rows"]}
        assert ("gen-1", "BUS High Volt", "active") in states
        assert ("gen-1", "BUS High Volt", "inactive") in states
        assert ("gen-2", "BUS High Volt", "active") in states
        assert ("gen-2", "BUS High Volt", "inactive") in states

        analogs = client.get("/api/v1/telemetry/rows", params=[("tab", "analog"), ("vm_id", vm_ids[0]), ("column", "RPM")]).json()
        assert analogs["columns"][0]["label"] == "Обороты"
        assert {row["cells"][0] for row in analogs["rows"]} == {"1500", "0"}
        assert {row["vm_name"] for row in analogs["rows"]} == {"gen-1"}

        catalog = client.get("/api/v1/telemetry/catalog", params=[("vm_id", vm_ids[0]), ("vm_id", vm_ids[1])]).json()
        assert {item["name"] for item in catalog["sources"]} == {"gen-1", "gen-2"}

        series = client.get(
            "/api/v1/telemetry/series",
            params=[("table", "analog"), ("vm_id", vm_ids[0]), ("vm_id", vm_ids[1]), ("column", "RPM"), ("date_from", "2026-09-21T00:00"), ("date_to", "2026-09-21T23:59")],
        ).json()
        assert series["row_count"] == 6
        assert any(column.endswith("|RPM") for column in series["columns"])
        assert "gen-1 · Обороты" in series["column_labels"].values()

        dashboard = client.get("/dashboard").text
        assert "Главная панель" in dashboard
        assert "GPIO (Raspberry)" in dashboard
        assert "Мониторинг устройства" in dashboard
        assert "gen-1" in dashboard and "gen-2" in dashboard
        data_page = client.get("/data").text
        assert "Аналоги" in data_page and "Аварии" in data_page and "gen-1" in data_page
        charts = client.get("/charts").text
        assert "echarts.min.js" in charts
        assert "Построить график" in charts

        with client.websocket_connect("/ws/v1/events") as socket:
            snapshot = socket.receive_json()
        assert snapshot["type"] == "snapshot"
        assert "system" in snapshot["payload"]
        assert "tags" in snapshot["payload"]


def _ingest(client: TestClient, vm_id: str, *, seq: int, when: str, rpm: int, alarm_bit: int, running: int) -> None:
    app = client._transport.app  # type: ignore[attr-defined]
    token = app.state.worker_tokens[vm_id]
    holding = [0] * 20
    holding[0] = rpm
    holding[19] = alarm_bit
    batch = RawBatch(
        vm_id=UUID(vm_id),
        protocol=VmProtocol.SIMULATOR,
        map_version="channels-v1",
        seq_start=seq,
        samples=[RawSample(seq=seq, captured_at=when, sources={"hr": holding, "coils": [running, 0, 0, 0]})],
    )
    response = client.post("/api/v1/internal/workers/batches", json=batch.model_dump(mode="json"), headers={"X-Worker-Token": token})
    assert response.status_code == 200, response.text
