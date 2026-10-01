from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import os
from pathlib import Path
import time
import zipfile

import pytest
from pydantic import ValidationError

from services.video.devices import h264_encoder_present, publish_host_video_devices
from services.video.motion import MotionTracker
from services.video.retention import measure_video_storage, purge_motion_over_quota
from services.video.settings import (
    CameraSettings,
    VideoConfig,
    build_buffer_argv,
    build_episode_argv,
    build_motion_argv,
    build_preview_argv,
    camera_estimate,
    estimate_mib,
)
from services.video.supervisor import Supervisor
from services.hub.db import HubRepository
from services.hub.recordings import RecordingError, list_recordings, recording_file
from tests.test_hub_vnext import _client


def _camera(**overrides) -> CameraSettings:
    payload = {
        "id": "cam1",
        "name": "Вход",
        "enabled": True,
        "url": "rtsp://10.0.0.8/stream",
        "resolution": "1920x1080",
        "bitrate_kbps": 2000,
        "audio": "none",
        "segment_sec": 60,
    }
    payload.update(overrides)
    return CameraSettings.model_validate(payload)


def test_fragment_size_matches_bitrate_formula():
    camera = _camera()
    expected = estimate_mib(2000, 0, 60)
    assert expected == 2000 * 60 / 8 / 1024
    estimate = camera_estimate(camera)
    assert estimate["fragment_mib"] == round(expected, 2)
    assert estimate["hour_mib"] == round(estimate_mib(2000, 0, 3600), 2)
    assert estimate["day_mib"] == round(estimate_mib(2000, 0, 86400), 2)


def test_ffmpeg_argv_copy_skips_scale_and_libx264_limits_bitrate(tmp_path: Path):
    copied = build_episode_argv(_camera(codec="copy", fps=25), tmp_path / "clip.mp4")
    assert "-vf" not in copied
    assert "scale" not in " ".join(copied)
    assert "segment" not in copied
    assert copied[copied.index("-c:v") + 1] == "copy"
    assert copied[copied.index("-t") + 1] == "60"
    assert copied[-1].endswith("clip.mp4")

    encoded = build_episode_argv(_camera(codec="libx264", fps=25), tmp_path / "clip.mp4")
    encoded_text = " ".join(encoded)
    assert "scale=1920:1080" in encoded_text
    assert "2000k" in encoded
    assert encoded[encoded.index("-c:v") + 1] == "libx264"
    assert encoded[encoded.index("-t") + 1] == "60"
    assert encoded[-1].endswith("clip.mp4")

    hardware = build_episode_argv(_camera(codec="h264_v4l2m2m", fps=25), tmp_path / "clip.mp4")
    hardware_text = " ".join(hardware)
    assert hardware[hardware.index("-c:v") + 1] == "h264_v4l2m2m"
    assert "format=yuv420p" in hardware_text
    assert "-maxrate" not in hardware
    assert "-preset" not in hardware

    buffer = build_buffer_argv(_camera(), tmp_path / "%Y%m%d_%H%M%S.mkv", 2)
    assert buffer[1] == "-nostats"
    assert buffer[buffer.index("-loglevel") + 1] == "warning"
    assert buffer[buffer.index("-f") + 1] == "segment"
    assert buffer[buffer.index("-segment_time") + 1] == "2"
    assert buffer[-1].endswith("%Y%m%d_%H%M%S.mkv")

    motion = build_motion_argv(_camera())
    assert motion[motion.index("-f") + 1] == "rawvideo"
    assert "fps=2,scale=320:180,format=gray" in motion
    assert motion[-1] == "pipe:1"

    preview = build_preview_argv(_camera(), tmp_path / "live.jpg")
    assert "scale=1920:1080" in " ".join(preview)
    assert preview[1] == "-y"
    assert preview[preview.index("-f") + 1] == "image2"
    plain = build_preview_argv(_camera(codec="copy"), tmp_path / "live.jpg")
    assert "scale=" not in " ".join(plain)


def test_storage_picks_a_resource_and_a_folder():
    assert VideoConfig().storage_resource_id == "storage:data"
    assert VideoConfig(video_subdir="").video_subdir == "video"
    assert VideoConfig(storage_resource_id="storage:nvme0n1p1", video_subdir="clips").video_subdir == "clips"
    with pytest.raises(ValidationError):
        VideoConfig(storage_resource_id="nvme")
    with pytest.raises(ValidationError):
        VideoConfig(video_subdir="../etc")


class _Proc:
    def __init__(self, argv: list[str]):
        self.argv = argv
        self.alive = True
        self.code = 0
        self.stderr = None

    def poll(self):
        return None if self.alive else self.code

    def terminate(self):
        self.alive = False

    def wait(self, timeout=None):
        self.alive = False


def test_supervisor_records_one_episode_and_reports_when_it_ends(tmp_path: Path):
    started: list[_Proc] = []

    def spawn(argv):
        proc = _Proc(list(argv))
        started.append(proc)
        return proc

    supervisor = Supervisor(tmp_path, spawn=spawn)
    camera = _camera(url="rtsp://user:secret@10.0.0.8/stream")
    config = VideoConfig(cameras=[camera])
    started_buffer = supervisor.tick(config, [], [])
    assert started_buffer["statuses"] == [{"id": "cam1", "state": "buffering", "message": "Кольцевой буфер активен"}]
    assert len(started) == 1 and "segment" in " ".join(started[0].argv)
    assert any("Кольцевой буфер в памяти" in item["line"] for item in started_buffer["logs"])
    supervisor.buffers["cam1"]["lines"].extend(
        [
            "frame=14866 fps= 25 q=-1.0 size=N/A time=00:09:54.68 bitrate=N/A speed=   1x",
            "[segment @ 0x1] Opening '/host/mnt/nvme/video/.buffer/cam1/20260928_132710.mkv' for writing",
            "[segment @ 0x1] Failed to open segment: Permission denied",
        ]
    )
    quiet = supervisor.tick(config, [], [])
    logged = " ".join(item["line"] for item in quiet["logs"])
    assert "frame=" not in logged
    assert "Opening " not in logged
    assert any(item["level"] == "error" and "Permission denied" in item["line"] for item in quiet["logs"])
    assert supervisor.tick(VideoConfig(cameras=[_camera(enabled=False)]), [{"id": "ep0", "camera_id": "cam1", "state": "queued"}], [])["episodes"][0]["state"] == "error"
    assert len(started) == 1

    archive = tmp_path / "archive"
    job = {"id": "ep1", "camera_id": "cam1", "state": "queued"}
    first = supervisor.tick(config, [job], [camera], output_root=archive)
    assert len(started) == 3
    episode_proc = next(proc for proc in started if "-t" in proc.argv)
    assert "segment" not in episode_proc.argv
    assert str(archive) in episode_proc.argv[-1] or str(archive).replace("\\", "/") in episode_proc.argv[-1].replace("\\", "/")
    assert first["episodes"][0]["state"] == "recording"
    assert first["statuses"][0]["state"] == "recording"
    assert supervisor.previews == {}
    assert any("Эпизод ep1 запущен" in item["line"] and item["source"] == "hub" for item in first["logs"])
    assert "user:secret" not in " ".join(item["line"] for item in first["logs"])

    supervisor.episodes["ep1"]["lines"].append("Nothing was written into output file, because at least one of its streams received no packets.")
    logged = supervisor.tick(config, [{"id": "ep1", "camera_id": "cam1", "state": "recording"}], [])
    assert any(item["level"] == "error" and "Nothing was written" in item["line"] for item in logged["logs"])

    again = supervisor.tick(config, [{"id": "ep1", "camera_id": "cam1", "state": "recording"}], [])
    assert again["episodes"] == []
    assert len(started) == 3

    episode_proc.alive = False
    episode_proc.code = 0
    done = supervisor.tick(config, [{"id": "ep1", "camera_id": "cam1", "state": "recording"}], [])
    assert done["episodes"][0]["state"] == "finished"
    assert done["episodes"][0]["path"].endswith("ep1.mp4")
    assert any("Эпизод ep1 завершён" in item["line"] for item in done["logs"])

    lost = supervisor.tick(config, [{"id": "ep9", "camera_id": "cam1", "state": "recording"}], [])
    assert lost["episodes"][0]["state"] == "error"
    assert lost["episodes"][0]["message"] == "запись прервана"


def test_ring_buffer_stays_in_ram_and_closed_segments_are_copied_to_the_archive(tmp_path: Path):
    started: list[_Proc] = []

    def spawn(argv):
        proc = _Proc(list(argv))
        started.append(proc)
        return proc

    ram = tmp_path / "ram"
    archive = tmp_path / "archive"
    supervisor = Supervisor(tmp_path, spawn=spawn, buffer_root=ram)
    camera = _camera()
    config = VideoConfig(cameras=[camera], incident_pre_sec=10, incident_segment_sec=2)
    supervisor.tick(config, [], [], output_root=archive)
    buffer = next(proc for proc in started if "segment" in proc.argv)
    assert ram.as_posix() in Path(buffer.argv[-1]).as_posix() or str(ram) in buffer.argv[-1]
    assert ".buffer" not in buffer.argv[-1]
    assert not (archive / ".buffer").exists()
    assert (ram / "cam1").is_dir()

    segment = ram / "cam1" / "20260930_100000.mkv"
    segment.write_bytes(b"closed-segment")
    closed_at = time.time() - 4
    os.utime(segment, (closed_at, closed_at))
    moment = datetime.now(timezone.utc)
    capture_from = (moment - timedelta(seconds=10)).isoformat()
    result = supervisor.tick(
        config,
        [{
            "id": "ep-inc",
            "camera_id": "cam1",
            "incident_id": "inc1",
            "state": "queued",
            "capture_from": capture_from,
            "started_at": moment.isoformat(),
        }],
        [],
        output_root=archive,
    )
    copied = archive / "incidents" / "inc1" / "cam1" / segment.name
    assert copied.read_bytes() == b"closed-segment"
    assert segment.read_bytes() == b"closed-segment"
    assert result["episodes"][0]["state"] == "recording"
    assert str(copied) in result["episodes"][0]["paths"]

    stale = ram / "cam1" / "20260930_090000.mkv"
    stale.write_bytes(b"old")
    os.utime(stale, (time.time() - 120, time.time() - 120))
    supervisor.tick(config, [{
        "id": "ep-inc",
        "camera_id": "cam1",
        "incident_id": "inc1",
        "state": "recording",
        "capture_from": capture_from,
        "started_at": moment.isoformat(),
        "paths": [str(copied)],
    }], [], output_root=archive)
    assert not stale.exists()
    assert segment.exists()


def test_missing_pi_encoder_records_with_libx264(tmp_path: Path):
    started: list[_Proc] = []

    def spawn(argv):
        proc = _Proc(list(argv))
        started.append(proc)
        return proc

    supervisor = Supervisor(tmp_path, spawn=spawn, hardware_h264=lambda: False)
    camera = _camera(codec="h264_v4l2m2m")
    result = supervisor.tick(VideoConfig(cameras=[camera]), [{"id": "ep1", "camera_id": "cam1", "state": "queued"}], [])
    episode = next(proc for proc in started if "-c:v" in proc.argv)
    assert episode.argv[episode.argv.index("-c:v") + 1] == "libx264"
    assert "h264_v4l2m2m" not in episode.argv
    assert any("Аппаратный кодер H.264 не найден" in item["line"] and item["source"] == "hub" for item in result["logs"])

    ready = Supervisor(tmp_path, spawn=spawn, hardware_h264=lambda: True)
    again = ready.tick(VideoConfig(cameras=[camera]), [{"id": "ep2", "camera_id": "cam1", "state": "queued"}], [])
    hardware_episode = [proc for proc in started if "-c:v" in proc.argv][-1]
    assert hardware_episode.argv[hardware_episode.argv.index("-c:v") + 1] == "h264_v4l2m2m"
    assert "format=yuv420p" in " ".join(hardware_episode.argv)
    assert not any("не найден" in item["line"] for item in again["logs"])


def test_video_device_publish_ignores_ordinary_files(tmp_path: Path):
    host = tmp_path / "host"
    dest = tmp_path / "dev"
    host.mkdir()
    dest.mkdir()
    (host / "video0").write_text("not a device", encoding="utf-8")
    (host / "sda").write_text("disk", encoding="utf-8")
    assert publish_host_video_devices(host, dest) == 0
    assert list(dest.iterdir()) == []
    assert h264_encoder_present(tmp_path / "missing") is False


def test_preview_restarts_when_the_picture_settings_change(tmp_path: Path):
    started: list[_Proc] = []

    def spawn(argv):
        proc = _Proc(list(argv))
        started.append(proc)
        return proc

    supervisor = Supervisor(tmp_path, spawn=spawn)
    camera = _camera()
    config = VideoConfig(cameras=[camera])
    first = supervisor.tick(config, [], [camera])
    assert first["statuses"][0]["state"] == "preview"
    assert len(started) == 2
    preview = next(proc for proc in started if "image2" in proc.argv)
    assert preview.argv[preview.argv.index("-f") + 1] == "image2"
    supervisor.tick(config, [], [camera])
    assert len(started) == 2

    changed = _camera(resolution="640x480")
    supervisor.tick(config, [], [changed])
    assert len(started) == 3
    assert preview.alive is False
    assert any("scale=640:480" in " ".join(proc.argv) and "image2" in proc.argv for proc in started[2:])


def test_alarm_edges_merge_into_one_incident_and_schedule_video_stop(tmp_path: Path):
    repo = HubRepository(tmp_path / "hub.db")
    first = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    incident = repo.register_incident_start(
        "vm-1",
        name="Oil pressure",
        kind="alert",
        created_at=first,
        camera_ids=["cam1", "cam2"],
        post_seconds=15,
    )
    incident_id = incident["id"]
    extended = repo.register_incident_start(
        "vm-1",
        name="Temperature",
        kind="alert",
        created_at=first + timedelta(seconds=3),
        camera_ids=["cam1", "cam2"],
        post_seconds=15,
    )
    assert extended["id"] == incident_id
    assert len(extended["episodes"]) == 2
    reasons = [item["line"] for item in repo.list_camera_logs("cam1")]
    assert "Запись начата. Причина: авария «Oil pressure»." in reasons
    assert "Запись продолжается. Причина: авария «Temperature»." in reasons
    repo.sync_alarm_edges(
        "vm-1", first + timedelta(seconds=4), {"Another alert"}, kind="alert", incident_names={"Another alert"}
    )
    repo.register_incident_end(
        "vm-1",
        name="Oil pressure",
        kind="alert",
        created_at=first + timedelta(seconds=5),
        post_seconds=15,
        has_active=False,
    )
    still_open = repo.get_incident(incident_id)
    assert still_open["state"] == "active"
    assert still_open["stop_at"] is None
    assert all(item["duration_sec"] == 86400 for item in still_open["episodes"])
    assert all(item["state"] == "queued" for item in still_open["episodes"])


def test_startup_recovers_incident_from_active_full_alarm(tmp_path: Path):
    repo = HubRepository(tmp_path / "hub.db")
    started = datetime.now(timezone.utc) - timedelta(seconds=5)
    repo.sync_alarm_edges(
        "vm-recovery",
        started,
        {"Emergency stop"},
        incident_names={"Emergency stop"},
    )
    with _client(tmp_path) as client:
        incidents = client.app.state.repo.list_incidents(vm_ids=["vm-recovery"])
        assert len(incidents) == 1
        assert incidents[0]["state"] == "active"
        assert [row["name"] for row in incidents[0]["alerts"]] == ["Emergency stop"]


def test_incident_episode_updates_keep_all_segment_paths(tmp_path: Path):
    repo = HubRepository(tmp_path / "hub.db")
    moment = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    incident = repo.register_incident_start("vm-1", name="Alarm", kind="alert", created_at=moment, camera_ids=["cam1"])
    episode = incident["episodes"][0]
    paths = [str(tmp_path / "segment-1.mkv"), str(tmp_path / "segment-2.mkv")]
    repo.apply_episode_events([{"id": episode["id"], "state": "recording", "paths": paths, "started_at": moment.isoformat()}])
    saved = repo.get_incident(incident["id"])["episodes"][0]
    assert saved["paths"] == paths


def test_incident_pages_and_export_routes(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BB_VIDEO_TOKEN", "video-secret")
    with _client(tmp_path) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
        assert client.get("/incidents").status_code == 200
        missing = client.get("/incidents/not-found", follow_redirects=False)
        assert missing.status_code == 303
        assert client.get("/api/v1/telemetry/export-package").status_code == 422
        workbook = client.get("/api/v1/telemetry/export-package", params={
            "date_from": "2026-09-28T00:00", "date_to": "2026-09-28T23:59", "format": "xlsx", "include": "analog",
        })
        assert workbook.status_code == 200
        with zipfile.ZipFile(io.BytesIO(workbook.content)) as package:
            assert "xl/workbook.xml" in package.namelist()


def test_incident_download_and_bundle_contain_video(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BB_VIDEO_TOKEN", "video-secret")
    def fake_merge(paths, output, **_kwargs):
        output.write_bytes(b"merged-video")
        return {"state": "included", "segments": [str(path) for path in paths], "omitted": [], "gaps": []}
    monkeypatch.setattr("services.hub.app.merge_camera_segments", fake_merge)
    with _client(tmp_path) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
        csrf = client.cookies.get("bb_csrf")
        repo = client.app.state.repo
        moment = datetime.now(timezone.utc).replace(microsecond=0)
        incident = repo.register_incident_start(
            "vm-test", name="Alarm", kind="alert", created_at=moment, camera_ids=["cam1"]
        )
        episode = incident["episodes"][0]
        video_path = tmp_path / "data" / "video" / "incidents" / incident["id"] / "cam1" / "part.mkv"
        video_path.parent.mkdir(parents=True)
        video_path.write_bytes(b"video-data")
        repo.apply_episode_events([{
            "id": episode["id"], "state": "finished", "path": str(video_path), "paths": [str(video_path)],
            "started_at": moment.isoformat(), "ended_at": moment.isoformat(),
        }])
        response = client.get(f"/api/v1/video/incidents/{incident['id']}/videos/{episode['id']}/0")
        assert response.status_code == 200
        assert response.content == b"video-data"
        archive = client.get(f"/api/v1/video/incidents/{incident['id']}/export")
        assert archive.status_code == 200
        with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
            assert "videos/cam1.mp4" in bundle.namelist()
            assert "videos/manifest.json" in bundle.namelist()
            assert "telemetry/analog.csv" in bundle.namelist()
            assert "charts/analog.json" in bundle.namelist()
        segments = repo.list_video_segments(
            date_from=(moment - timedelta(seconds=1)).isoformat(),
            date_to=(moment + timedelta(seconds=3)).isoformat(),
            camera_ids=["cam1"],
        )
        assert [row["path"] for row in segments] == [str(video_path)]

        queued = client.post(
            "/api/v1/export-jobs",
            json={"kind": "incident", "incident_id": incident["id"]},
            headers={"X-CSRF-Token": csrf},
        )
        assert queued.status_code == 202
        job = queued.json()
        for _ in range(50):
            job = client.get(f"/api/v1/export-jobs/{job['id']}").json()
            if job["state"] not in {"queued", "running"}:
                break
            time.sleep(0.02)
        assert job["state"] == "completed"
        assert client.get(f"/api/v1/export-jobs/{job['id']}/download").status_code == 200


def test_supervisor_stops_incident_episode_at_stop_at(tmp_path: Path):
    started: list[_Proc] = []

    def spawn(argv):
        proc = _Proc(list(argv))
        started.append(proc)
        return proc

    supervisor = Supervisor(tmp_path, spawn=spawn, hardware_h264=lambda: False)
    camera = _camera()
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    first = supervisor.tick(
        VideoConfig(cameras=[camera]),
        [{"id": "ep1", "camera_id": "cam1", "state": "queued", "duration_sec": 86400, "stop_at": future}],
        [],
    )
    assert first["episodes"][0]["state"] == "recording"
    episode_proc = next(proc for proc in started if "-t" in proc.argv)
    assert episode_proc.argv[episode_proc.argv.index("-t") + 1] == "86400"
    due = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    supervisor.tick(
        VideoConfig(cameras=[camera]),
        [{"id": "ep1", "camera_id": "cam1", "state": "recording", "duration_sec": 86400, "stop_at": due}],
        [],
    )
    assert episode_proc.alive is False


def test_camera_settings_roundtrip(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BB_VIDEO_TOKEN", "video-secret")
    with _client(tmp_path) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
        page = client.get("/cameras")
        assert page.status_code == 200
        assert "Добавить камеру" in page.text
        assert 'data-field="storage_resource_id"' in page.text
        assert "Носитель" in page.text
        assert "Журнал" in page.text
        assert "Пауза после движения" in page.text
        assert "Лимит каталога" in page.text
        assert 'data-camera-tab="motion"' in page.text
        assert "bb-vm-step" in page.text
        assert "/static/cameras.js" in page.text

        csrf = client.cookies.get("bb_csrf")
        disk = tmp_path / "nvme"
        disk.mkdir()
        client.app.state.repo.upsert_resources(
            [
                {
                    "resource_id": "storage:nvme0n1p1",
                    "kind": "storage",
                    "name": "NVMe SSD",
                    "path": str(disk),
                    "available": True,
                    "metadata": {},
                }
            ]
        )
        assert client.app.state.repo.approve_resource("storage:nvme0n1p1", None)
        body = {"storage_resource_id": "storage:nvme0n1p1", "video_subdir": "clips", "cameras": [_camera().model_dump(mode="json")]}
        saved = client.put("/api/v1/cameras", json=body, headers={"X-CSRF-Token": csrf})
        assert saved.status_code == 200
        payload = saved.json()
        assert payload["config"]["storage_resource_id"] == "storage:nvme0n1p1"
        assert payload["config"]["video_subdir"] == "clips"
        assert Path(payload["storage_dir"]) == disk / "clips"
        assert any(item["resource_id"] == "storage:nvme0n1p1" for item in payload["storage_resources"])
        assert payload["config"]["cameras"][0]["resolution"] == "1920x1080"
        assert payload["config"]["cameras"][0]["bitrate_kbps"] == 2000
        assert payload["estimates"]["cameras"][0]["fragment_mib"] == round(2000 * 60 / 8 / 1024, 2)
        assert client.put("/api/v1/cameras", json={"storage_resource_id": "storage:missing", "cameras": []}, headers={"X-CSRF-Token": csrf}).status_code == 409
        assert client.put("/api/v1/cameras", json={"video_subdir": "../etc", "cameras": []}, headers={"X-CSRF-Token": csrf}).status_code == 422

        loaded = client.get("/api/v1/cameras")
        assert loaded.json()["config"]["cameras"][0]["codec"] == "libx264"

        assert client.get("/api/v1/internal/video/config").status_code == 401
        internal = client.get("/api/v1/internal/video/config", headers={"X-Video-Token": "video-secret"})
        assert internal.status_code == 200
        assert internal.json()["config"]["cameras"][0]["id"] == "cam1"
        assert Path(internal.json()["storage_dir"]) == disk / "clips"
        assert internal.json()["episodes"] == []

        started = client.post("/api/v1/cameras/cam1/episodes", headers={"X-CSRF-Token": csrf})
        assert started.status_code == 200
        episode_id = started.json()["id"]
        assert started.json()["state"] == "queued"
        assert client.post("/api/v1/cameras/cam1/episodes", headers={"X-CSRF-Token": csrf}).status_code == 409
        queued = client.get("/api/v1/internal/video/config", headers={"X-Video-Token": "video-secret"})
        assert queued.json()["episodes"][0]["id"] == episode_id

        recording = client.post(
            "/api/v1/internal/video/episodes",
            headers={"X-Video-Token": "video-secret"},
            json={"items": [{"id": episode_id, "state": "recording", "path": "/mnt/nvme/video/cam1/clip.mp4", "started_at": "2026-09-25T12:00:00+00:00"}]},
        )
        assert recording.status_code == 200
        assert recording.json()["items"][0]["state"] == "recording"
        repeat = client.post(
            "/api/v1/internal/video/episodes",
            headers={"X-Video-Token": "video-secret"},
            json={"items": [{"id": episode_id, "state": "recording", "path": "/mnt/nvme/video/cam1/clip.mp4", "started_at": "2026-09-25T12:00:00+00:00"}]},
        )
        assert repeat.json()["items"] == []
        finished = client.post(
            "/api/v1/internal/video/episodes",
            headers={"X-Video-Token": "video-secret"},
            json={"items": [{"id": episode_id, "state": "finished", "path": "/mnt/nvme/video/cam1/clip.mp4", "started_at": "2026-09-25T12:00:00+00:00", "ended_at": "2026-09-25T12:01:00+00:00"}]},
        )
        assert finished.status_code == 200
        assert finished.json()["items"][0]["state"] == "finished"
        assert finished.json()["items"][0]["path"].endswith("clip.mp4")
        listed = client.get("/api/v1/cameras").json()["episodes"]
        assert listed[0]["id"] == episode_id
        assert listed[0]["state"] == "finished"
        assert client.get("/api/v1/internal/video/config", headers={"X-Video-Token": "video-secret"}).json()["episodes"] == []
        assert client.post("/api/v1/internal/video/episodes", json={"items": []}).status_code == 401
        assert client.post("/api/v1/internal/video/logs", json={"items": []}).status_code == 401
        written = client.post(
            "/api/v1/internal/video/logs",
            headers={"X-Video-Token": "video-secret"},
            json={"items": [{"camera_id": "cam1", "level": "error", "source": "ffmpeg", "line": "Nothing was written into output file"}]},
        )
        assert written.status_code == 200
        journal = client.get("/api/v1/cameras/cam1/logs")
        assert journal.status_code == 200
        assert journal.json()["entries"][-1]["line"] == "Nothing was written into output file"
        assert journal.json()["entries"][-1]["level"] == "error"
        assert journal.json()["entries"][-1]["source"] == "ffmpeg"

        watched = client.post(
            "/api/v1/cameras/cam1/preview",
            headers={"X-CSRF-Token": csrf},
            json={"active": True, "camera": _camera().model_dump(mode="json")},
        )
        assert watched.status_code == 200
        previewing = client.get("/api/v1/internal/video/config", headers={"X-Video-Token": "video-secret"})
        assert previewing.json()["previews"][0]["url"] == "rtsp://10.0.0.8/stream"
        assert client.get("/api/v1/cameras/cam1/preview.jpg").status_code == 404
        jpeg = tmp_path / "data" / "video" / ".preview" / "cam1.jpg"
        jpeg.parent.mkdir(parents=True)
        jpeg.write_bytes(b"\xff\xd8\xff\xd9")
        image = client.get("/api/v1/cameras/cam1/preview.jpg")
        assert image.status_code == 200
        assert image.headers["content-type"].startswith("image/jpeg")
        assert client.post("/api/v1/cameras/cam1/preview", headers={"X-CSRF-Token": csrf}, json={"active": False}).status_code == 200
        assert client.get("/api/v1/internal/video/config", headers={"X-Video-Token": "video-secret"}).json()["previews"] == []

        posted = client.post(
            "/api/v1/internal/video/status",
            headers={"X-Video-Token": "video-secret"},
            json={"items": [{"id": "cam1", "state": "error", "message": "connection refused"}]},
        )
        assert posted.status_code == 200
        assert client.get("/api/v1/cameras").json()["status"]["cam1"]["message"] == "connection refused"

        bad = _camera().model_dump(mode="json")
        bad["url"] = "http://not-rtsp"
        rejected = client.put("/api/v1/cameras", json={"cameras": [bad]}, headers={"X-CSRF-Token": csrf})
        assert rejected.status_code == 422


def test_motion_tracker_holds_through_a_quiet_gap_and_ignores_one_frame():
    tracker = MotionTracker(noise=32, threshold_pct=2, min_frames=2, gap_sec=10)
    still = bytes(100)
    moved = bytes([255]) * 100
    moved_more = bytes([128]) * 100
    settled = bytes([200]) * 100
    assert tracker.push(still, 0) is None
    assert tracker.push(moved, 1) is None
    assert tracker.push(moved_more, 2) == "start"
    assert tracker.push(moved_more, 3) is None
    assert tracker.push(settled, 8) is None
    assert tracker.push(settled, 9) is None
    assert tracker.push(settled, 18) is None
    assert tracker.push(settled, 19) == "stop"

    noisy = MotionTracker(noise=32, threshold_pct=50, min_frames=1, gap_sec=0)
    base = bytes(100)
    nudge = bytes([10]) * 100
    assert noisy.push(base, 0) is None
    assert noisy.push(nudge, 1) is None


def test_motion_analysis_reports_an_edge_and_copies_buffer_segments(tmp_path: Path):
    started: list[_Proc] = []

    def spawn(argv):
        proc = _Proc(list(argv))
        started.append(proc)
        return proc

    ram = tmp_path / "ram"
    archive = tmp_path / "archive"
    supervisor = Supervisor(tmp_path, spawn=spawn, spawn_raw=spawn, buffer_root=ram)
    camera = _camera(motion=True, motion_gap_sec=10, motion_min_frames=2)
    config = VideoConfig(cameras=[camera])
    first = supervisor.tick(config, [], [], output_root=archive)
    analyze = next(proc for proc in started if "rawvideo" in proc.argv)
    assert "scale=320:180" in " ".join(analyze.argv)
    assert any("Детектор движения запущен" in item["line"] for item in first["logs"])

    still = bytes(80)
    moved = bytes([255]) * 80
    moved_more = bytes([128]) * 80
    supervisor._push_frame("cam1", still, now=1_000)
    supervisor._push_frame("cam1", moved, now=1_001)
    assert supervisor.tick(config, [], [], output_root=archive)["motion"] == []
    supervisor._push_frame("cam1", moved_more, now=1_002)
    edge = supervisor.tick(config, [], [], output_root=archive)
    assert edge["motion"] == [{"camera_id": "cam1", "state": "start", "at": datetime.fromtimestamp(1_002, timezone.utc).isoformat()}]

    segment = ram / "cam1" / "20260930_100000.mkv"
    segment.write_bytes(b"motion-segment")
    closed_at = time.time() - 4
    os.utime(segment, (closed_at, closed_at))
    moment = datetime.now(timezone.utc)
    result = supervisor.tick(
        config,
        [{
            "id": "mot1",
            "camera_id": "cam1",
            "state": "queued",
            "capture_from": (moment - timedelta(seconds=10)).isoformat(),
            "started_at": moment.isoformat(),
        }],
        [],
        output_root=archive,
    )
    copied = archive / "motion" / "cam1" / "mot1" / segment.name
    assert copied.read_bytes() == b"motion-segment"
    assert result["statuses"][0]["state"] == "recording"
    assert result["statuses"][0]["message"] == "Запись по движению"
    assert not any("-t" in proc.argv for proc in started)


def test_motion_edge_continues_one_clip_until_the_gap_closes(tmp_path: Path):
    repo = HubRepository(tmp_path / "hub.db")
    moment = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
    opened = repo.apply_motion_edge("cam1", "start", moment.isoformat(), pre_seconds=10)
    assert opened is not None
    assert opened["capture_from"] == (moment - timedelta(seconds=10)).isoformat()
    assert opened["incident_id"] == ""
    assert opened["duration_sec"] == 86400
    again = repo.apply_motion_edge("cam1", "start", (moment + timedelta(seconds=3)).isoformat(), pre_seconds=10)
    assert again is not None and again["id"] == opened["id"]
    repo.enqueue_episode("cam2")
    assert repo.apply_motion_edge("cam2", "start", moment.isoformat(), pre_seconds=10) is None
    stopped = repo.apply_motion_edge("cam1", "stop", (moment + timedelta(seconds=20)).isoformat(), pre_seconds=10)
    assert stopped is not None and stopped["stop_at"] == (moment + timedelta(seconds=20)).isoformat()
    resumed = repo.apply_motion_edge("cam1", "start", (moment + timedelta(seconds=21)).isoformat(), pre_seconds=10)
    assert resumed is not None and resumed["id"] == opened["id"] and not resumed["stop_at"]
    repo.apply_episode_events([{"id": opened["id"], "state": "finished", "path": "clip.mkv", "ended_at": moment.isoformat()}])
    fresh = repo.apply_motion_edge("cam1", "start", (moment + timedelta(seconds=40)).isoformat(), pre_seconds=10)
    assert fresh is not None and fresh["id"] != opened["id"]


def test_incident_replaces_an_open_motion_clip_instead_of_stacking(tmp_path: Path):
    repo = HubRepository(tmp_path / "hub.db")
    moment = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
    motion = repo.apply_motion_edge("cam1", "start", moment.isoformat(), pre_seconds=10)
    incident = repo.register_incident_start(
        "vm-1",
        name="Давление",
        kind="alert",
        created_at=moment + timedelta(seconds=5),
        camera_ids=["cam1"],
    )
    stored = repo.get_episode(motion["id"])
    assert stored["state"] == "finished"
    assert stored["message"] == "Сохранено: начался инцидент"
    episode = incident["episodes"][0]
    assert episode["id"] != motion["id"]
    assert episode["incident_id"] == incident["id"]
    assert {item["id"] for item in repo.open_episodes()} == {episode["id"]}
    blocked = repo.apply_motion_edge("cam1", "start", (moment + timedelta(seconds=6)).isoformat(), pre_seconds=10)
    assert blocked is None
    assert {item["id"] for item in repo.open_episodes()} == {episode["id"]}
    repo.apply_episode_events([{"id": episode["id"], "state": "finished", "ended_at": (moment + timedelta(seconds=20)).isoformat()}])
    fresh = repo.apply_motion_edge("cam1", "start", (moment + timedelta(seconds=21)).isoformat(), pre_seconds=10)
    assert fresh is not None and fresh["id"] != motion["id"]
    lines = [item["line"] for item in repo.list_camera_logs("cam1")]
    assert any("Запись движения сохранена" in line and "Давление" in line for line in lines)
    assert any("Отдельная запись не начата" in line for line in lines)


def test_supervisor_keeps_one_copy_when_motion_and_incident_overlap(tmp_path: Path):
    started: list[_Proc] = []

    def spawn(argv):
        proc = _Proc(list(argv))
        started.append(proc)
        return proc

    ram = tmp_path / "ram"
    archive = tmp_path / "archive"
    supervisor = Supervisor(tmp_path, spawn=spawn, spawn_raw=spawn, buffer_root=ram)
    camera = _camera(motion=True)
    config = VideoConfig(cameras=[camera], incident_pre_sec=30, incident_segment_sec=2)
    supervisor.tick(config, [], [], output_root=archive)
    segment = ram / "cam1" / "clip.mkv"
    segment.parent.mkdir(parents=True, exist_ok=True)
    segment.write_bytes(b"same-seconds")
    closed_at = time.time() - 4
    os.utime(segment, (closed_at, closed_at))
    moment = datetime.now(timezone.utc)
    capture = (moment - timedelta(seconds=20)).isoformat()
    supervisor.tick(
        config,
        [
            {"id": "mot1", "camera_id": "cam1", "state": "recording", "capture_from": capture, "started_at": capture},
            {"id": "inc-ep", "camera_id": "cam1", "incident_id": "inc1", "state": "recording", "capture_from": capture, "started_at": moment.isoformat()},
        ],
        [],
        output_root=archive,
    )
    assert (archive / "incidents" / "inc1" / "cam1" / "clip.mkv").read_bytes() == b"same-seconds"
    assert not (archive / "motion" / "cam1" / "mot1").exists()
    assert "mot1" not in supervisor.incident_episodes
    assert "inc-ep" in supervisor.incident_episodes
    saved = next(item for item in supervisor.closed.values() if item["id"] == "mot1")
    assert saved["state"] == "finished"
    assert saved["message"] == "Сохранено: начался инцидент"
    motion_procs = [proc for proc in started if "rawvideo" in proc.argv]
    assert motion_procs and motion_procs[-1].alive is False
    resumed = supervisor.tick(config, [], [], output_root=archive)
    assert any("Детектор движения снова включён" in item["line"] for item in resumed["logs"])
    assert any("rawvideo" in proc.argv and proc.alive for proc in started)


def test_motion_endpoint_ignores_cameras_without_the_flag(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BB_VIDEO_TOKEN", "video-secret")
    with _client(tmp_path) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
        csrf = client.cookies.get("bb_csrf")
        camera = _camera(motion=True).model_dump(mode="json")
        quiet = _camera(id="cam2", name="Двор").model_dump(mode="json")
        assert client.put("/api/v1/cameras", json={"cameras": [camera, quiet]}, headers={"X-CSRF-Token": csrf}).status_code == 200
        assert client.post("/api/v1/internal/video/motion", json={"items": []}).status_code == 401
        response = client.post(
            "/api/v1/internal/video/motion",
            headers={"X-Video-Token": "video-secret"},
            json={"items": [
                {"camera_id": "cam1", "state": "start", "at": "2026-09-30T10:00:00+00:00"},
                {"camera_id": "cam2", "state": "start", "at": "2026-09-30T10:00:00+00:00"},
            ]},
        )
        assert response.status_code == 200
        items = response.json()["items"]
        assert [item["camera_id"] for item in items] == ["cam1"]
        assert items[0]["capture_from"] == "2026-09-30T09:59:50+00:00"
        logs = client.get("/api/v1/cameras/cam1/logs").json()["entries"]
        assert any("Причина: движение в кадре." in entry["line"] for entry in logs)


def test_motion_quota_deletes_old_clips_and_keeps_incidents(tmp_path: Path):
    root = tmp_path / "video"
    incident = root / "incidents" / "inc1" / "cam1" / "keep.mkv"
    old = root / "motion" / "cam1" / "old-ep" / "old.mkv"
    newer = root / "motion" / "cam1" / "new-ep" / "new.mkv"
    live = root / "motion" / "cam1" / "live-ep" / "live.mkv"
    manual = root / "cam1" / "manual.mp4"
    for path, payload in (
        (incident, b"i" * 1000),
        (old, b"o" * 800),
        (newer, b"n" * 800),
        (live, b"l" * 800),
        (manual, b"m" * 500),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    now = time.time()
    os.utime(old, (now - 30, now - 30))
    os.utime(newer, (now - 10, now - 10))
    removed = purge_motion_over_quota(root, 2300, {("cam1", "live-ep")})
    assert {path.name for path in removed} == {"old.mkv", "new.mkv"}
    assert not old.exists() and not newer.exists()
    assert incident.read_bytes() == b"i" * 1000
    assert live.read_bytes() == b"l" * 800
    assert manual.read_bytes() == b"m" * 500
    assert measure_video_storage(root) == {"used_bytes": 2300, "motion_bytes": 800}
    assert purge_motion_over_quota(root, 2300, {("cam1", "live-ep")}) == []
    assert purge_motion_over_quota(root, 0, set()) == []

    repo = HubRepository(tmp_path / "hub.db")
    moment = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
    motion = repo.apply_motion_edge("cam1", "start", moment.isoformat(), pre_seconds=0)
    motion_id = motion["id"]
    repo.apply_episode_events([
        {"id": motion_id, "state": "finished", "path": str(old), "paths": [str(old), str(newer)], "ended_at": moment.isoformat()},
    ])
    incident_row = repo.register_incident_start("vm-1", name="Alarm", kind="alert", created_at=moment, camera_ids=["cam1"])
    incident_episode = incident_row["episodes"][0]["id"]
    repo.apply_episode_events([
        {"id": incident_episode, "state": "finished", "path": str(incident), "paths": [str(incident)], "ended_at": moment.isoformat()},
    ])
    assert repo.note_purged_motion_files([str(old), str(newer)]) == 1
    stored = repo.get_episode(motion_id)
    assert stored["paths"] == []
    assert stored["message"] == "Удалено: каталог видео превысил лимит"
    kept = repo.get_episode(incident_episode)
    assert kept["paths"] == [str(incident)]
    assert kept["message"] == ""


def test_recording_library_lists_saved_clips_and_rejects_escape(tmp_path: Path):
    root = tmp_path / "video"
    clip = root / "motion" / "cam1" / "clip.mkv"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"abc")
    secret = root / ".buffer" / "cam1" / "secret.mkv"
    secret.parent.mkdir(parents=True)
    secret.write_bytes(b"no")
    listed = list_recordings(root, "")
    assert [item["name"] for item in listed["entries"]] == ["motion"]
    nested = list_recordings(root, "motion/cam1")
    assert nested["place"] == "motion"
    assert nested["entries"][0]["name"] == "clip.mkv"
    assert nested["entries"][0]["size"] == 3
    assert recording_file(root, "motion/cam1/clip.mkv").read_bytes() == b"abc"
    with pytest.raises(RecordingError):
        list_recordings(root, "../hub.db")
    with pytest.raises(RecordingError):
        list_recordings(root, ".buffer/cam1")
    with pytest.raises(RecordingError):
        recording_file(root, "motion/cam1/notes.txt")


def test_recordings_page_lists_and_downloads_incident_and_motion_files(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BB_VIDEO_TOKEN", "video-secret")
    with _client(tmp_path) as client:
        assert client.get("/api/v1/video/files").status_code == 401
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
        page = client.get("/recordings")
        assert page.status_code == 200
        assert "bb-explorer" in page.text
        assert "запись по детектору" in page.text
        assert "/static/recordings.js" in page.text
        cameras = client.get("/cameras")
        assert "Файлы записей" in cameras.text

        csrf = client.cookies.get("bb_csrf")
        saved = client.put(
            "/api/v1/cameras",
            json={"cameras": [_camera().model_dump(mode="json")]},
            headers={"X-CSRF-Token": csrf},
        )
        assert saved.status_code == 200
        moment = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
        incident = client.app.state.repo.register_incident_start(
            "vm-1", name="Давление", kind="alert", created_at=moment, camera_ids=["cam1"]
        )
        video = tmp_path / "data" / "video"
        motion = video / "motion" / "cam1" / "ep1" / "20260930_100000.mkv"
        alarm = video / "incidents" / incident["id"] / "cam1" / "20260930_100002.mkv"
        manual = video / "cam1" / "manual.mp4"
        hidden = video / ".buffer" / "cam1" / "secret.mkv"
        note = motion.parent / "notes.txt"
        for path, payload in (
            (motion, b"motion-clip"),
            (alarm, b"alarm-clip"),
            (manual, b"manual-clip"),
            (hidden, b"secret"),
            (note, b"nope"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)

        root = client.get("/api/v1/video/files").json()
        labels = {item["name"]: item["label"] for item in root["entries"]}
        assert labels["incidents"] == "Инциденты"
        assert labels["motion"] == "Движение"
        assert labels["cam1"] == "Вход"
        assert ".buffer" not in labels
        opened = client.get("/api/v1/video/files", params={"path": f"incidents/{incident['id']}"})
        assert opened.status_code == 200
        assert "Давление" in opened.json()["crumbs"][-1]["label"]
        motion_dir = client.get("/api/v1/video/files", params={"path": "motion/cam1/ep1"}).json()
        assert [item["name"] for item in motion_dir["entries"]] == ["20260930_100000.mkv"]
        assert motion_dir["place"] == "motion"
        downloaded = client.get("/api/v1/video/files/download", params={"path": "motion/cam1/ep1/20260930_100000.mkv"})
        assert downloaded.status_code == 200
        assert downloaded.content == b"motion-clip"
        assert "attachment" in downloaded.headers["content-disposition"]
        assert client.get("/api/v1/video/files", params={"path": "../hub.db"}).status_code == 404
        assert client.get("/api/v1/video/files/download", params={"path": "motion/cam1/ep1/notes.txt"}).status_code == 404
        assert client.get("/api/v1/video/files", params={"path": ".buffer/cam1"}).status_code == 404
