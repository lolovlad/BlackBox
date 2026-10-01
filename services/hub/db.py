from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID, uuid4

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError


def _episode_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "camera_id": str(row["camera_id"]),
        "vm_id": str(row["vm_id"] or "") if "vm_id" in row.keys() else "",
        "incident_id": str(row["incident_id"] or "") if "incident_id" in row.keys() else "",
        "state": str(row["state"]),
        "path": str(row["path"] or ""),
        "paths": json.loads(row["paths_json"] or "[]") if "paths_json" in row.keys() else ([str(row["path"])] if row["path"] else []),
        "message": str(row["message"] or ""),
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "stop_at": row["stop_at"] if "stop_at" in row.keys() else None,
        "capture_from": row["capture_from"] if "capture_from" in row.keys() else None,
        "duration_sec": int(row["duration_sec"]) if "duration_sec" in row.keys() and row["duration_sec"] is not None else None,
        "created_at": str(row["created_at"]),
    }


def _incident_dict(row: sqlite3.Row) -> dict[str, Any]:
    started_at = str(row["started_at"])
    pre_seconds = int(row["pre_seconds"] or 0)
    start_moment = _parse_iso(started_at)
    window_from = (start_moment - timedelta(seconds=pre_seconds)).isoformat() if start_moment is not None else started_at
    window_to = row["ended_at"] or row["stop_at"] or row["last_alert_at"]
    return {
        "id": str(row["id"]),
        "vm_id": str(row["vm_id"]),
        "state": str(row["state"]),
        "started_at": started_at,
        "last_alert_at": str(row["last_alert_at"]),
        "stop_at": row["stop_at"],
        "ended_at": row["ended_at"],
        "pre_seconds": pre_seconds,
        "post_seconds": int(row["post_seconds"] or 0),
        "created_at": str(row["created_at"]),
        "telemetry_from": window_from,
        "telemetry_to": window_to,
    }


def _recording_reason(kind: str, name: str) -> str:
    label = {"gpio": "сигнал GPIO", "emergency": "аварийное правило"}.get(kind, "авария")
    title = str(name or "").strip()
    return f"{label} «{title}»" if title else label


def _path_key(value: str) -> str:
    try:
        return str(Path(value).resolve(strict=False))
    except OSError:
        return str(value)


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


_SNAPSHOT_REVISION = re.compile(r"^(.+)@([1-9][0-9]*)$")


def parse_map_snapshot(version: str) -> tuple[str, int]:
    """Split a stored snapshot id into the stable map name and revision."""
    text = str(version or "").strip()
    match = _SNAPSHOT_REVISION.fullmatch(text)
    if not match:
        return text, 1
    return match.group(1), int(match.group(2))


def map_family_name(version: str) -> str:
    return parse_map_snapshot(version)[0]


def snapshot_version(name: str, revision: int) -> str:
    return name if int(revision) <= 1 else f"{name}@{int(revision)}"


def map_body_checksum(document: dict[str, Any]) -> str:
    """Checksum of the readable map, ignoring the snapshot id."""
    protocol = document.get("protocol")
    if hasattr(protocol, "value"):
        protocol = protocol.value
    canonical = {
        "protocol": protocol,
        "preset_id": document.get("preset_id"),
        "requests": document.get("requests") or [],
        "fields": document.get("fields") or [],
    }
    encoded = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class HubRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.hasher = PasswordHasher()
        self.init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS roles (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL, role TEXT NOT NULL REFERENCES roles(name),
                    disabled INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS refresh_sessions (
                    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, token_hash TEXT UNIQUE NOT NULL,
                    expires_at TEXT NOT NULL, revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS virtual_machines (
                    id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT NOT NULL DEFAULT '',
                    protocol TEXT NOT NULL, preset_id TEXT, map_version TEXT NOT NULL,
                    worker_image TEXT NOT NULL, desired_state TEXT NOT NULL DEFAULT 'stopped',
                    resources_json TEXT NOT NULL DEFAULT '[]',
                    read_resources_json TEXT NOT NULL DEFAULT '[]',
                    storage_resource_id TEXT,
                    limits_json TEXT NOT NULL DEFAULT '{}',
                    config_json TEXT NOT NULL DEFAULT '{}', config_revision INTEGER NOT NULL DEFAULT 1,
                    lifecycle TEXT NOT NULL DEFAULT 'pending', container_id TEXT, last_error TEXT, heartbeat_at TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS map_versions (
                    id TEXT PRIMARY KEY, version TEXT NOT NULL, protocol TEXT NOT NULL,
                    preset_id TEXT, checksum TEXT NOT NULL, document_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, name TEXT, revision INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(checksum)
                );
                CREATE TABLE IF NOT EXISTS discovered_resources (
                    resource_id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
                    path TEXT, address TEXT, metadata_json TEXT NOT NULL DEFAULT '{}',
                    available INTEGER NOT NULL DEFAULT 1, approved INTEGER NOT NULL DEFAULT 0,
                    approved_by INTEGER, approved_at TEXT, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resource_leases (
                    resource_id TEXT PRIMARY KEY, vm_id TEXT NOT NULL, leased_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lifecycle_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, vm_id TEXT NOT NULL,
                    event TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT NOT NULL,
                    target TEXT, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ingest_batches (
                    batch_id TEXT NOT NULL, vm_id TEXT NOT NULL, seq_start INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(vm_id, batch_id, seq_start)
                );
                CREATE TABLE IF NOT EXISTS alarm_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vm_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    state TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'alert',
                    triggers_incident INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alarm_active (
                    vm_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    triggers_incident INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (vm_id, kind, name)
                );
                CREATE INDEX IF NOT EXISTS idx_alarm_events_vm ON alarm_events(vm_id, kind, created_at);
                CREATE TABLE IF NOT EXISTS emergency_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vm_id TEXT,
                    name TEXT NOT NULL,
                    expression TEXT NOT NULL,
                    is_deleted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_emergency_rules_name_active ON emergency_rules(name) WHERE is_deleted=0;
                CREATE TABLE IF NOT EXISTS emergency_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vm_id TEXT NOT NULL,
                    rule_id INTEGER NOT NULL,
                    rule_name TEXT NOT NULL,
                    expression TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    incident_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_emergency_events_vm_time ON emergency_events(vm_id, started_at);
                CREATE TABLE IF NOT EXISTS emergency_active (
                    vm_id TEXT NOT NULL,
                    rule_id INTEGER NOT NULL,
                    event_id INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    PRIMARY KEY(vm_id, rule_id)
                );
                CREATE TABLE IF NOT EXISTS camera_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    document_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS camera_status (
                    camera_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS camera_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    camera_id TEXT NOT NULL,
                    level TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'ffmpeg',
                    line TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_camera_logs_camera ON camera_logs(camera_id, id);
                CREATE TABLE IF NOT EXISTS video_episodes (
                    id TEXT PRIMARY KEY,
                    camera_id TEXT NOT NULL,
                    vm_id TEXT,
                    incident_id TEXT,
                    state TEXT NOT NULL,
                    path TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    started_at TEXT,
                    ended_at TEXT,
                    stop_at TEXT,
                    duration_sec INTEGER,
                    paths_json TEXT NOT NULL DEFAULT '[]',
                    capture_from TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS video_episode_segments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    episode_id TEXT NOT NULL,
                    camera_id TEXT NOT NULL,
                    path TEXT NOT NULL UNIQUE,
                    started_at TEXT,
                    ended_at TEXT,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'ready',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_video_segments_time
                    ON video_episode_segments(camera_id, started_at, ended_at);
                CREATE TABLE IF NOT EXISTS export_jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    params_json TEXT NOT NULL DEFAULT '{}',
                    progress INTEGER NOT NULL DEFAULT 0,
                    result_path TEXT,
                    filename TEXT,
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_export_jobs_expiry ON export_jobs(expires_at);
                CREATE TABLE IF NOT EXISTS video_incidents (
                    id TEXT PRIMARY KEY,
                    vm_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    last_alert_at TEXT NOT NULL,
                    stop_at TEXT,
                    ended_at TEXT,
                    pre_seconds INTEGER NOT NULL DEFAULT 0,
                    post_seconds INTEGER NOT NULL DEFAULT 15,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_video_incidents_vm ON video_incidents(vm_id, state, started_at);
                CREATE TABLE IF NOT EXISTS video_incident_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id TEXT NOT NULL,
                    vm_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_video_incident_alerts_incident ON video_incident_alerts(incident_id, created_at);
                """
            )
            episode_columns = {row[1] for row in c.execute("PRAGMA table_info(video_episodes)").fetchall()}
            for name, definition in (
                ("vm_id", "TEXT"),
                ("incident_id", "TEXT"),
                ("stop_at", "TEXT"),
                ("duration_sec", "INTEGER"),
                ("paths_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("capture_from", "TEXT"),
            ):
                if name not in episode_columns:
                    c.execute(f"ALTER TABLE video_episodes ADD COLUMN {name} {definition}")
            c.execute("CREATE INDEX IF NOT EXISTS idx_video_episodes_incident ON video_episodes(incident_id, state)")
            rule_columns = {row[1] for row in c.execute("PRAGMA table_info(emergency_rules)").fetchall()}
            if "vm_id" not in rule_columns:
                c.execute("ALTER TABLE emergency_rules ADD COLUMN vm_id TEXT")
            c.executemany("INSERT OR IGNORE INTO roles(name) VALUES (?)", [("admin",), ("user",)])
            columns = {row[1] for row in c.execute("PRAGMA table_info(virtual_machines)").fetchall()}
            if "heartbeat_at" not in columns:
                c.execute("ALTER TABLE virtual_machines ADD COLUMN heartbeat_at TEXT")
            if "read_resources_json" not in columns:
                c.execute("ALTER TABLE virtual_machines ADD COLUMN read_resources_json TEXT NOT NULL DEFAULT '[]'")
                c.execute("UPDATE virtual_machines SET read_resources_json=resources_json WHERE read_resources_json='[]' OR read_resources_json IS NULL")
            if "storage_resource_id" not in columns:
                c.execute("ALTER TABLE virtual_machines ADD COLUMN storage_resource_id TEXT")
            alarm_event_columns = {row[1] for row in c.execute("PRAGMA table_info(alarm_events)").fetchall()}
            if "triggers_incident" not in alarm_event_columns:
                c.execute("ALTER TABLE alarm_events ADD COLUMN triggers_incident INTEGER NOT NULL DEFAULT 0")
            alarm_active_columns = {row[1] for row in c.execute("PRAGMA table_info(alarm_active)").fetchall()}
            if "triggers_incident" not in alarm_active_columns:
                c.execute("ALTER TABLE alarm_active ADD COLUMN triggers_incident INTEGER NOT NULL DEFAULT 0")
            map_columns = {row[1] for row in c.execute("PRAGMA table_info(map_versions)").fetchall()}
            if "name" not in map_columns:
                c.execute("ALTER TABLE map_versions ADD COLUMN name TEXT")
            if "revision" not in map_columns:
                c.execute("ALTER TABLE map_versions ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
            for row in c.execute("SELECT id, version, name FROM map_versions").fetchall():
                if str(row["name"] or "").strip():
                    continue
                family, revision = parse_map_snapshot(str(row["version"]))
                c.execute("UPDATE map_versions SET name=?, revision=? WHERE id=?", (family, revision, row["id"]))
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_map_family_revision ON map_versions(protocol, name, revision)")
            ingest_info = c.execute("PRAGMA table_info(ingest_batches)").fetchall()
            ingest_columns = {row[1] for row in ingest_info}
            ingest_pk = [row[1] for row in ingest_info if row[5]]
            # Pre-vNext databases used batch_id as the sole primary key.  A
            # batch id is only unique within a VM/sequence in the worker
            # contract, so rebuild that small metadata table with the proper
            # composite primary key while preserving existing rows.
            if ingest_pk == ["batch_id"] or "idempotency_key" not in ingest_columns:
                c.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ingest_batches_v2 (
                        batch_id TEXT NOT NULL, vm_id TEXT NOT NULL, seq_start INTEGER NOT NULL,
                        idempotency_key TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY(vm_id, batch_id, seq_start)
                    )
                    """
                )
                if "idempotency_key" in ingest_columns:
                    c.execute(
                        """
                        INSERT OR IGNORE INTO ingest_batches_v2(batch_id,vm_id,seq_start,idempotency_key,created_at)
                        SELECT batch_id,vm_id,seq_start,
                               COALESCE(idempotency_key, vm_id || ':' || batch_id || ':' || seq_start),
                               created_at
                        FROM ingest_batches
                        """
                    )
                else:
                    c.execute(
                        """
                        INSERT OR IGNORE INTO ingest_batches_v2(batch_id,vm_id,seq_start,idempotency_key,created_at)
                        SELECT batch_id,vm_id,seq_start,
                               vm_id || ':' || batch_id || ':' || seq_start,
                               created_at
                        FROM ingest_batches
                        """
                    )
                c.execute("DROP TABLE ingest_batches")
                c.execute("ALTER TABLE ingest_batches_v2 RENAME TO ingest_batches")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_ingest_batches_idempotency ON ingest_batches(idempotency_key)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_ingest_batches_created_at ON ingest_batches(created_at)")

    def bootstrap_admin(self, username: str, password: str) -> None:
        if not password:
            return
        with self.connect() as c:
            exists = c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
            if exists is None:
                c.execute(
                    "INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?)",
                    (username, self.hasher.hash(password), "admin", datetime.now(timezone.utc).isoformat()),
                )

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM users WHERE username=? AND disabled=0", (username,)).fetchone()
        if row is None:
            return None
        try:
            self.hasher.verify(row["password_hash"], password)
        except (VerifyMismatchError, ValueError):
            return None
        return dict(row)

    def user_by_id(self, user_id: int) -> dict[str, Any] | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM users WHERE id=? AND disabled=0", (user_id,)).fetchone()
        return None if row is None else dict(row)

    def create_user(self, username: str, password: str, role: str = "user") -> dict[str, Any]:
        if role not in {"admin", "user"}:
            raise ValueError("invalid role")
        with self.connect() as c:
            c.execute("INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?)", (username, self.hasher.hash(password), role, datetime.now(timezone.utc).isoformat()))
            row = c.execute("SELECT id,username,role,disabled,created_at FROM users WHERE username=?", (username,)).fetchone()
        return dict(row)

    def list_users(self) -> list[dict[str, Any]]:
        with self.connect() as c:
            rows = c.execute("SELECT id,username,role,disabled,created_at FROM users ORDER BY username").fetchall()
        return [dict(row) for row in rows]

    def create_refresh_session(self, user_id: int, token_hash: str, expires_at: str, sid: str | None = None) -> str:
        sid = sid or str(uuid4())
        with self.connect() as c:
            c.execute("INSERT INTO refresh_sessions VALUES(?,?,?,?,NULL)", (sid, user_id, token_hash, expires_at))
        return sid

    def refresh_token_matches(self, sid: str, token_hash: str) -> bool:
        with self.connect() as c:
            row = c.execute("SELECT token_hash,expires_at,revoked_at FROM refresh_sessions WHERE id=?", (sid,)).fetchone()
        if row is None or row["revoked_at"] is not None or row["token_hash"] != token_hash:
            return False
        try:
            return datetime.fromisoformat(row["expires_at"]) > datetime.now(timezone.utc)
        except ValueError:
            return False

    def refresh_session(self, sid: str) -> dict[str, Any] | None:
        with self.connect() as c:
            row = c.execute(
                "SELECT s.*,u.username,u.role,u.disabled FROM refresh_sessions s JOIN users u ON u.id=s.user_id WHERE s.id=? AND s.revoked_at IS NULL",
                (sid,),
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        # Keep the session primary key in ``id`` only inside the database
        # layer; token issuance expects the user's numeric id.
        data["id"] = data["user_id"]
        return data

    def revoke_session(self, sid: str) -> None:
        with self.connect() as c:
            c.execute("UPDATE refresh_sessions SET revoked_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), sid))

    def record_audit(self, user_id: int | None, action: str, target: str | None = None, payload: dict[str, Any] | None = None) -> None:
        with self.connect() as c:
            c.execute(
                "INSERT INTO audit_events(user_id,action,target,payload_json,created_at) VALUES(?,?,?,?,?)",
                (user_id, action, target, json.dumps(payload or {}, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
            )

    def list_audit_events(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as c:
            rows = c.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (max(1, min(limit, 1000)),)).fetchall()
        return [dict(row) | {"payload": json.loads(row["payload_json"])} for row in rows]

    def create_vm(self, values: dict[str, Any]) -> dict[str, Any]:
        vm_id = str(values.get("id") or uuid4())
        now = datetime.now(timezone.utc).isoformat()
        read_resources = values.get("read_resources", values.get("resources", []))
        with self.connect() as c:
            c.execute(
                "INSERT INTO virtual_machines(id,name,description,protocol,preset_id,map_version,worker_image,desired_state,resources_json,read_resources_json,storage_resource_id,limits_json,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    vm_id,
                    values["name"],
                    values.get("description", ""),
                    values["protocol"],
                    values.get("preset_id"),
                    values["map_version"],
                    values["worker_image"],
                    values.get("desired_state") if values.get("desired_state") in {"running", "stopped"} else "stopped",
                    json.dumps(read_resources),
                    json.dumps(read_resources),
                    values.get("storage_resource_id"),
                    json.dumps(values.get("limits", {})),
                    json.dumps(values.get("config", {})),
                    now,
                    now,
                ),
            )
        return self.get_vm(vm_id)  # type: ignore[return-value]

    def list_vms(self) -> list[dict[str, Any]]:
        with self.connect() as c:
            rows = c.execute("SELECT * FROM virtual_machines ORDER BY name").fetchall()
        return [self._vm_row(row) for row in rows]

    def get_vm(self, vm_id: str) -> dict[str, Any] | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM virtual_machines WHERE id=?", (vm_id,)).fetchone()
        return None if row is None else self._vm_row(row)

    def update_vm(self, vm_id: str, values: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {k: v for k, v in values.items() if k in {"name", "description", "desired_state", "map_version", "preset_id", "resources", "read_resources", "storage_resource_id", "limits", "config", "lifecycle", "container_id", "last_error", "heartbeat_at", "worker_image"}}
        if not allowed:
            return self.get_vm(vm_id)
        sets: list[str] = []
        args: list[Any] = []
        for key, value in allowed.items():
            col = {"resources": "resources_json", "read_resources": "read_resources_json", "limits": "limits_json", "config": "config_json"}.get(key, key)
            if key in {"resources", "limits", "config"}:
                value = json.dumps(value)
            if key == "resources":
                sets.append("read_resources_json=?")
                args.append(value)
            if key == "read_resources":
                value = json.dumps(value)
                # Keep the old field in sync for clients written before the
                # explicit read/storage split.
                sets.append("resources_json=?")
                args.append(value)
            sets.append(f"{col}=?")
            args.append(value)
        if any(key in allowed for key in {"name", "description", "map_version", "preset_id", "resources", "read_resources", "storage_resource_id", "limits", "config", "worker_image"}):
            sets.append("config_revision=config_revision+1")
        sets.append("updated_at=?")
        args.extend([datetime.now(timezone.utc).isoformat(), vm_id])
        with self.connect() as c:
            previous = c.execute("SELECT lifecycle FROM virtual_machines WHERE id=?", (vm_id,)).fetchone()
            c.execute(f"UPDATE virtual_machines SET {','.join(sets)} WHERE id=?", args)
            if previous is not None and "lifecycle" in allowed and previous["lifecycle"] != allowed["lifecycle"]:
                c.execute(
                    "INSERT INTO lifecycle_events(vm_id,event,payload_json,created_at) VALUES(?,?,?,?)",
                    (
                        vm_id,
                        str(allowed["lifecycle"]),
                        json.dumps({"from": previous["lifecycle"], "to": allowed["lifecycle"]}, ensure_ascii=False),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
        return self.get_vm(vm_id)

    def list_lifecycle_events(self, vm_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as c:
            rows = c.execute(
                "SELECT * FROM lifecycle_events WHERE vm_id=? ORDER BY id DESC LIMIT ?",
                (vm_id, max(1, min(limit, 5000))),
            ).fetchall()
        return [dict(row) | {"payload": json.loads(row["payload_json"])} for row in rows]

    def delete_vm(self, vm_id: str) -> bool:
        with self.connect() as c:
            c.execute("DELETE FROM resource_leases WHERE vm_id=?", (vm_id,))
            c.execute("DELETE FROM ingest_batches WHERE vm_id=?", (vm_id,))
            c.execute("DELETE FROM lifecycle_events WHERE vm_id=?", (vm_id,))
            c.execute("DELETE FROM alarm_events WHERE vm_id=?", (vm_id,))
            c.execute("DELETE FROM alarm_active WHERE vm_id=?", (vm_id,))
            cur = c.execute("DELETE FROM virtual_machines WHERE id=?", (vm_id,))
        return cur.rowcount > 0

    def sync_alarm_edges(
        self,
        vm_id: str,
        created_at: datetime,
        active_names: set[str] | list[str],
        *,
        kind: str = "alert",
        incident_names: set[str] | list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Write one row when an alarm starts and one when it ends.

        Repeating the same active set does not insert anything, so a poll
        loop cannot fill the journal with unchanged values.
        """
        moment = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=timezone.utc)
        stamp = moment.astimezone(timezone.utc).isoformat()
        channel = kind if kind in {"gpio", "emergency"} else "alert"
        desired = {str(name).strip() for name in active_names if str(name).strip()}
        triggers = {str(name).strip() for name in (incident_names or []) if str(name).strip()} & desired
        events: list[dict[str, Any]] = []
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                rows = c.execute(
                    "SELECT name,triggers_incident FROM alarm_active WHERE vm_id=? AND kind=?",
                    (vm_id, channel),
                ).fetchall()
                current = {str(row["name"]) for row in rows}
                current_triggers = {str(row["name"]) for row in rows if bool(row["triggers_incident"])}
                for name in sorted(desired - current):
                    trigger = name in triggers
                    c.execute(
                        "INSERT INTO alarm_events(vm_id,name,state,kind,triggers_incident,created_at) VALUES(?,?,?,?,?,?)",
                        (vm_id, name, "active", channel, int(trigger), stamp),
                    )
                    c.execute(
                        "INSERT OR REPLACE INTO alarm_active(vm_id,kind,name,started_at,triggers_incident) VALUES(?,?,?,?,?)",
                        (vm_id, channel, name, stamp, int(trigger)),
                    )
                    events.append({"vm_id": vm_id, "name": name, "state": "active", "kind": channel, "created_at": stamp, "triggers_incident": trigger})
                for name in sorted(current - desired):
                    trigger = name in current_triggers
                    c.execute(
                        "INSERT INTO alarm_events(vm_id,name,state,kind,triggers_incident,created_at) VALUES(?,?,?,?,?,?)",
                        (vm_id, name, "inactive", channel, int(trigger), stamp),
                    )
                    c.execute("DELETE FROM alarm_active WHERE vm_id=? AND kind=? AND name=?", (vm_id, channel, name))
                    events.append({"vm_id": vm_id, "name": name, "state": "inactive", "kind": channel, "created_at": stamp, "triggers_incident": trigger})
                for name in sorted(desired & current):
                    was_trigger = name in current_triggers
                    is_trigger = name in triggers
                    if was_trigger == is_trigger:
                        continue
                    c.execute(
                        "UPDATE alarm_active SET triggers_incident=? WHERE vm_id=? AND kind=? AND name=?",
                        (int(is_trigger), vm_id, channel, name),
                    )
                    events.append({
                        "vm_id": vm_id,
                        "name": name,
                        "state": "active" if is_trigger else "inactive",
                        "kind": channel,
                        "created_at": stamp,
                        "triggers_incident": True,
                        "classification_changed": True,
                    })
                has_active = c.execute(
                    "SELECT 1 FROM alarm_active WHERE vm_id=? AND triggers_incident=1 UNION ALL SELECT 1 FROM emergency_active WHERE vm_id=? LIMIT 1",
                    (vm_id, vm_id),
                ).fetchone() is not None
                for event in events:
                    event["has_active"] = has_active
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        return events

    def close_vm_alarms(self, vm_id: str, created_at: datetime) -> list[dict[str, Any]]:
        """Close every open device, GPIO and rule alarm for a stopped VM.

        Incidents stay open while any of these rows remain. Stop and restart
        must drop them, otherwise a cleared machine keeps recording.
        """
        events = []
        for kind in ("alert", "gpio"):
            events.extend(self.sync_alarm_edges(vm_id, created_at, set(), kind=kind))
        events.extend(self._close_emergency_alarms(vm_id, created_at))
        return events

    def _close_emergency_alarms(self, vm_id: str, created_at: datetime) -> list[dict[str, Any]]:
        moment = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=timezone.utc)
        stamp = moment.astimezone(timezone.utc).isoformat()
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                rows = c.execute(
                    "SELECT a.event_id,e.rule_name,e.expression FROM emergency_active a "
                    "JOIN emergency_events e ON e.id=a.event_id WHERE a.vm_id=?",
                    (vm_id,),
                ).fetchall()
                if rows:
                    c.execute("UPDATE emergency_events SET ended_at=? WHERE id IN (SELECT event_id FROM emergency_active WHERE vm_id=?)", (stamp, vm_id))
                    c.execute("DELETE FROM emergency_active WHERE vm_id=?", (vm_id,))
                has_active = c.execute(
                    "SELECT 1 FROM alarm_active WHERE vm_id=? AND triggers_incident=1 UNION ALL SELECT 1 FROM emergency_active WHERE vm_id=? LIMIT 1",
                    (vm_id, vm_id),
                ).fetchone() is not None
                events = [
                    {
                        "vm_id": vm_id,
                        "name": str(row["rule_name"]),
                        "state": "inactive",
                        "kind": "emergency",
                        "created_at": stamp,
                        "triggers_incident": True,
                        "has_active": has_active,
                    }
                    for row in rows
                ]
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        return events

    def list_active_alarms(
        self,
        vm_ids: list[str] | None,
        *,
        kind: str,
        date_from: str | None = None,
        date_to: str | None = None,
        sort_desc: bool = True,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[list[dict[str, Any]], int]:
        """Alarms that are open right now. Cleared names are not returned."""
        channel = kind if kind in {"gpio", "emergency"} else "alert"
        clauses = ["kind=?"]
        args: list[Any] = [channel]
        if vm_ids:
            placeholders = ",".join("?" for _ in vm_ids)
            clauses.append(f"vm_id IN ({placeholders})")
            args.extend(vm_ids)
        if date_from:
            clauses.append("started_at>=?")
            args.append(date_from)
        if date_to:
            clauses.append("started_at<=?")
            args.append(date_to)
        where = " AND ".join(clauses)
        order = "DESC" if sort_desc else "ASC"
        with self.connect() as c:
            total = int(c.execute(f"SELECT COUNT(*) FROM alarm_active WHERE {where}", args).fetchone()[0])
            rows = c.execute(
                f"SELECT vm_id,name,kind,triggers_incident,started_at FROM alarm_active WHERE {where} ORDER BY started_at {order} LIMIT ? OFFSET ?",
                [*args, max(1, limit), max(0, offset)],
            ).fetchall()
        return [dict(row) | {"state": "active", "created_at": row["started_at"]} for row in rows], total

    def list_incident_triggers(self) -> list[dict[str, Any]]:
        """Active controller alarms and rules that must keep incidents open."""
        with self.connect() as c:
            alarms = [
                dict(row) | {"source_kind": "alert"}
                for row in c.execute(
                    "SELECT vm_id,name,started_at FROM alarm_active WHERE triggers_incident=1 ORDER BY started_at"
                ).fetchall()
            ]
            rules = [
                dict(row) | {"name": str(row["rule_name"]), "source_kind": "emergency"}
                for row in c.execute(
                    """
                    SELECT a.vm_id,a.started_at,r.name AS rule_name
                    FROM emergency_active a JOIN emergency_rules r ON r.id=a.rule_id
                    ORDER BY a.started_at
                    """
                ).fetchall()
            ]
        return alarms + rules

    def list_alarm_events(
        self,
        vm_ids: list[str] | None,
        *,
        kind: str,
        date_from: str | None = None,
        date_to: str | None = None,
        sort_desc: bool = True,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[list[dict[str, Any]], int]:
        channel = kind if kind in {"gpio", "emergency"} else "alert"
        clauses = ["kind=?"]
        args: list[Any] = [channel]
        if vm_ids:
            placeholders = ",".join("?" for _ in vm_ids)
            clauses.append(f"vm_id IN ({placeholders})")
            args.extend(vm_ids)
        if date_from:
            clauses.append("created_at>=?")
            args.append(date_from)
        if date_to:
            clauses.append("created_at<=?")
            args.append(date_to)
        where = " AND ".join(clauses)
        order = "DESC" if sort_desc else "ASC"
        with self.connect() as c:
            total = int(c.execute(f"SELECT COUNT(*) FROM alarm_events WHERE {where}", args).fetchone()[0])
            rows = c.execute(
                f"SELECT id,vm_id,name,state,kind,triggers_incident,created_at FROM alarm_events WHERE {where} ORDER BY created_at {order}, id {order} LIMIT ? OFFSET ?",
                [*args, max(1, limit), max(0, offset)],
            ).fetchall()
        return [dict(row) for row in rows], total

    def list_emergency_rules(self) -> list[dict[str, Any]]:
        with self.connect() as c:
            rows = c.execute("SELECT * FROM emergency_rules WHERE is_deleted=0 ORDER BY id").fetchall()
        return [dict(row) | {"is_deleted": bool(row["is_deleted"])} for row in rows]

    def save_emergency_rule(self, *, name: str, expression: str, vm_id: str | None = None) -> dict[str, Any]:
        stamp = datetime.now(timezone.utc).isoformat()
        with self.connect() as c:
            cur = c.execute(
                "INSERT INTO emergency_rules(vm_id,name,expression,is_deleted,created_at,updated_at) VALUES(?,?,?,0,?,?)",
                (vm_id, name.strip(), expression.strip(), stamp, stamp),
            )
            row = c.execute("SELECT * FROM emergency_rules WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row) | {"is_deleted": False}

    def update_emergency_rule(self, rule_id: int, *, name: str, expression: str, vm_id: str | None = None) -> dict[str, Any] | None:
        stamp = datetime.now(timezone.utc).isoformat()
        with self.connect() as c:
            cur = c.execute(
                "UPDATE emergency_rules SET vm_id=?,name=?,expression=?,updated_at=? WHERE id=? AND is_deleted=0",
                (vm_id, name.strip(), expression.strip(), stamp, int(rule_id)),
            )
            row = c.execute("SELECT * FROM emergency_rules WHERE id=? AND is_deleted=0", (int(rule_id),)).fetchone() if cur.rowcount else None
        return (dict(row) | {"is_deleted": False}) if row is not None else None

    def delete_emergency_rule(self, rule_id: int) -> bool:
        with self.connect() as c:
            cur = c.execute(
                "UPDATE emergency_rules SET is_deleted=1,updated_at=? WHERE id=? AND is_deleted=0",
                (datetime.now(timezone.utc).isoformat(), int(rule_id)),
            )
        return cur.rowcount > 0

    def close_deleted_emergency_rule(self, rule_id: int, created_at: datetime) -> list[dict[str, Any]]:
        moment = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=timezone.utc)
        stamp = moment.astimezone(timezone.utc).isoformat()
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                rows = c.execute(
                    "SELECT a.vm_id,a.event_id,e.rule_name,e.expression FROM emergency_active a "
                    "JOIN emergency_events e ON e.id=a.event_id WHERE a.rule_id=?",
                    (int(rule_id),),
                ).fetchall()
                c.execute("UPDATE emergency_events SET ended_at=? WHERE id IN (SELECT event_id FROM emergency_active WHERE rule_id=?)", (stamp, int(rule_id)))
                c.execute("DELETE FROM emergency_active WHERE rule_id=?", (int(rule_id),))
                transitions = []
                for row in rows:
                    vm_id = str(row["vm_id"])
                    has_active = c.execute(
                        "SELECT 1 FROM alarm_active WHERE vm_id=? UNION ALL SELECT 1 FROM emergency_active WHERE vm_id=? LIMIT 1",
                        (vm_id, vm_id),
                    ).fetchone() is not None
                    transitions.append({
                        "vm_id": vm_id,
                        "id": int(row["event_id"]),
                        "name": str(row["rule_name"]),
                        "expression": str(row["expression"]),
                        "state": "inactive",
                        "created_at": stamp,
                        "has_active": has_active,
                    })
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        return transitions

    def evaluate_emergency_rules(
        self,
        vm_id: str,
        created_at: datetime,
        values: dict[str, Any],
        *,
        rule_scope: str = "all",
    ) -> list[dict[str, Any]]:
        from .emergency_rules import evaluate_rule_expression

        moment = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=timezone.utc)
        stamp = moment.astimezone(timezone.utc).isoformat()
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                if rule_scope == "scoped":
                    rules = c.execute(
                        "SELECT * FROM emergency_rules WHERE is_deleted=0 AND vm_id=? ORDER BY id",
                        (vm_id,),
                    ).fetchall()
                elif rule_scope == "global":
                    rules = c.execute(
                        "SELECT * FROM emergency_rules WHERE is_deleted=0 AND vm_id IS NULL ORDER BY id"
                    ).fetchall()
                else:
                    rules = c.execute(
                        "SELECT * FROM emergency_rules WHERE is_deleted=0 AND (vm_id IS NULL OR vm_id=?) ORDER BY id",
                        (vm_id,),
                    ).fetchall()
                active_rows = c.execute("SELECT rule_id,event_id FROM emergency_active WHERE vm_id=?", (vm_id,)).fetchall()
                active = {int(row["rule_id"]): int(row["event_id"]) for row in active_rows}
                desired: dict[int, tuple[sqlite3.Row, bool | None]] = {}
                for rule in rules:
                    fired, error = evaluate_rule_expression(str(rule["expression"]), values)
                    desired[int(rule["id"])] = (rule, None if error else fired)
                transitions: list[dict[str, Any]] = []
                for rule_id, (rule, fired) in desired.items():
                    if fired is None:
                        continue
                    event_id = active.get(rule_id)
                    if fired and event_id is None:
                        cur = c.execute(
                            "INSERT INTO emergency_events(vm_id,rule_id,rule_name,expression,started_at,ended_at) VALUES(?,?,?,?,?,?)",
                            (vm_id, rule_id, rule["name"], rule["expression"], stamp, stamp),
                        )
                        c.execute(
                            "INSERT INTO emergency_active(vm_id,rule_id,event_id,started_at) VALUES(?,?,?,?)",
                            (vm_id, rule_id, cur.lastrowid, stamp),
                        )
                        transitions.append({"id": int(cur.lastrowid), "rule_id": rule_id, "name": str(rule["name"]), "expression": str(rule["expression"]), "state": "active", "created_at": stamp})
                    elif fired and event_id is not None:
                        c.execute("UPDATE emergency_events SET ended_at=? WHERE id=?", (stamp, event_id))
                    elif not fired and event_id is not None:
                        c.execute("UPDATE emergency_events SET ended_at=? WHERE id=?", (stamp, event_id))
                        c.execute("DELETE FROM emergency_active WHERE vm_id=? AND rule_id=?", (vm_id, rule_id))
                        transitions.append({"id": event_id, "rule_id": rule_id, "name": str(rule["name"]), "expression": str(rule["expression"]), "state": "inactive", "created_at": stamp})
                for rule_id, event_id in active.items():
                    if rule_id in desired:
                        continue
                    c.execute("UPDATE emergency_events SET ended_at=? WHERE id=?", (stamp, event_id))
                    c.execute("DELETE FROM emergency_active WHERE vm_id=? AND rule_id=?", (vm_id, rule_id))
                    row = c.execute("SELECT rule_name,expression FROM emergency_events WHERE id=?", (event_id,)).fetchone()
                    if row is not None:
                        transitions.append({"id": event_id, "rule_id": rule_id, "name": str(row["rule_name"]), "expression": str(row["expression"]), "state": "inactive", "created_at": stamp})
                has_active = c.execute(
                    "SELECT 1 FROM alarm_active WHERE vm_id=? UNION ALL SELECT 1 FROM emergency_active WHERE vm_id=? LIMIT 1",
                    (vm_id, vm_id),
                ).fetchone() is not None
                for event in transitions:
                    event["has_active"] = has_active
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        return transitions

    def link_emergency_event_incident(self, event_id: int, incident_id: str) -> None:
        with self.connect() as c:
            c.execute("UPDATE emergency_events SET incident_id=? WHERE id=?", (incident_id, int(event_id)))

    def list_emergency_events(
        self,
        vm_ids: list[str] | None,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        sort_desc: bool = True,
        limit: int = 100000,
    ) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        args: list[Any] = []
        if vm_ids:
            clauses.append(f"vm_id IN ({','.join('?' for _ in vm_ids)})")
            args.extend(vm_ids)
        if date_from:
            clauses.append("started_at>=?")
            args.append(date_from)
        if date_to:
            clauses.append("started_at<=?")
            args.append(date_to)
        order = "DESC" if sort_desc else "ASC"
        args.append(max(1, min(int(limit), 1_000_000)))
        with self.connect() as c:
            rows = c.execute(
                f"SELECT * FROM emergency_events WHERE {' AND '.join(clauses)} ORDER BY started_at {order},id {order} LIMIT ?",
                args,
            ).fetchall()
        return [dict(row) for row in rows]

    def save_map(self, document: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        name = str(document.get("name") or map_family_name(str(document["version"])))
        revision = int(document.get("revision") or parse_map_snapshot(str(document["version"]))[1])
        stored = dict(document)
        stored["name"] = name
        stored["revision"] = revision
        with self.connect() as c:
            existing = c.execute(
                "SELECT checksum FROM map_versions WHERE version=? AND protocol=? LIMIT 1",
                (document["version"], document["protocol"]),
            ).fetchone()
            if existing is not None:
                if existing["checksum"] == document["checksum"]:
                    return
                raise ValueError(f"Снимок «{document['version']}» уже занят другой картой.")
            try:
                c.execute(
                    "INSERT INTO map_versions(id,version,protocol,preset_id,checksum,document_json,created_at,name,revision) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        document["map_id"],
                        document["version"],
                        document["protocol"],
                        document.get("preset_id"),
                        document["checksum"],
                        json.dumps(stored),
                        now,
                        name,
                        revision,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"Снимок «{document['version']}» уже занят другой картой.") from exc

    def maps_in_family(self, name: str, protocol: str) -> list[dict[str, Any]]:
        with self.connect() as c:
            rows = c.execute(
                """
                SELECT id,version,name,revision,protocol,preset_id,checksum,document_json,created_at
                FROM map_versions
                WHERE protocol=? AND (name=? OR version=? OR version GLOB ?)
                ORDER BY revision, created_at
                """,
                (protocol, name, name, name + "@[0-9]*"),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["document"] = json.loads(payload.pop("document_json"))
            payload["name"] = str(payload.get("name") or map_family_name(str(payload["version"])))
            payload["revision"] = int(payload.get("revision") or 1)
            items.append(payload)
        return items

    def map_by_version(self, version: str, protocol: str | None = None) -> dict[str, Any] | None:
        with self.connect() as c:
            if protocol is None:
                row = c.execute("SELECT document_json FROM map_versions WHERE version=? ORDER BY created_at DESC LIMIT 1", (version,)).fetchone()
            else:
                row = c.execute("SELECT document_json FROM map_versions WHERE version=? AND protocol=? ORDER BY created_at DESC LIMIT 1", (version, protocol)).fetchone()
        return None if row is None else json.loads(row[0])

    def list_maps(self) -> list[dict[str, Any]]:
        with self.connect() as c:
            rows = c.execute(
                "SELECT id,version,name,revision,protocol,preset_id,checksum,created_at FROM map_versions ORDER BY created_at DESC"
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["name"] = str(payload.get("name") or map_family_name(str(payload["version"])))
            payload["revision"] = int(payload.get("revision") or 1)
            items.append(payload)
        return items

    def map_record(self, version: str, protocol: str | None = None) -> dict[str, Any] | None:
        with self.connect() as c:
            if protocol is None:
                row = c.execute(
                    "SELECT id,version,name,revision,protocol,preset_id,checksum,document_json,created_at FROM map_versions WHERE version=? ORDER BY created_at DESC LIMIT 1",
                    (version,),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT id,version,name,revision,protocol,preset_id,checksum,document_json,created_at FROM map_versions WHERE version=? AND protocol=? ORDER BY created_at DESC LIMIT 1",
                    (version, protocol),
                ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        payload["document"] = json.loads(payload.pop("document_json"))
        payload["name"] = str(payload.get("name") or map_family_name(str(payload["version"])))
        payload["revision"] = int(payload.get("revision") or 1)
        return payload

    def vms_using_map(self, version: str, protocol: str) -> list[dict[str, Any]]:
        with self.connect() as c:
            rows = c.execute(
                "SELECT id,name,lifecycle FROM virtual_machines WHERE map_version=? AND protocol=? ORDER BY name",
                (version, protocol),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_map(self, version: str, protocol: str) -> bool:
        with self.connect() as c:
            cur = c.execute("DELETE FROM map_versions WHERE version=? AND protocol=?", (version, protocol))
        return cur.rowcount > 0

    def list_resources(self) -> list[dict[str, Any]]:
        with self.connect() as c:
            rows = c.execute("SELECT * FROM discovered_resources ORDER BY kind,name").fetchall()
        return [dict(row) | {"metadata": json.loads(row["metadata_json"]), "approved": bool(row["approved"]), "available": bool(row["available"])} for row in rows]

    def resource_by_id(self, resource_id: str) -> dict[str, Any] | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM discovered_resources WHERE resource_id=?", (resource_id,)).fetchone()
        if row is None:
            return None
        return dict(row) | {"metadata": json.loads(row["metadata_json"]), "approved": bool(row["approved"]), "available": bool(row["available"])}

    def resource_conflicts(self, resource_ids: list[str], *, exclude_vm_id: str | None = None) -> list[str]:
        if not resource_ids:
            return []
        approved = {row["resource_id"] for row in self.list_resources() if row["approved"] and row["available"]}
        conflicts = [rid for rid in resource_ids if rid not in approved]
        exclusive = {row["resource_id"] for row in self.list_resources() if row["kind"] in {"serial", "can", "gpio"}}
        with self.connect() as c:
            leased_rows = c.execute("SELECT resource_id,vm_id FROM resource_leases WHERE resource_id IN (%s)" % ",".join("?" for _ in resource_ids), resource_ids).fetchall()
        for row in leased_rows:
            if row["resource_id"] in exclusive and (not exclude_vm_id or row["vm_id"] != exclude_vm_id):
                conflicts.append(row["resource_id"])
        for vm in self.list_vms():
            if exclude_vm_id and vm["id"] == exclude_vm_id:
                continue
            if vm.get("desired_state") != "running" and vm.get("lifecycle") not in {"starting", "running", "stopping"}:
                continue
            for item in vm.get("read_resources", vm.get("resources", [])):
                rid = item.get("resource_id") if isinstance(item, dict) else str(item)
                if rid in resource_ids and rid in exclusive:
                    conflicts.append(rid)
        return sorted(set(conflicts))

    def acquire_resource_leases(self, vm_id: str, resource_ids: list[str]) -> list[str]:
        """Atomically lease exclusive physical resources for a running VM."""
        ids = sorted(set(str(rid) for rid in resource_ids if rid))
        exclusive = {
            row["resource_id"]
            for row in self.list_resources()
            if row["resource_id"] in ids and row["kind"] in {"serial", "can", "gpio"}
        }
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            conflicts: list[str] = []
            for rid in sorted(exclusive):
                row = c.execute("SELECT vm_id FROM resource_leases WHERE resource_id=?", (rid,)).fetchone()
                if row is not None and row["vm_id"] != vm_id:
                    conflicts.append(rid)
            if conflicts:
                c.rollback()
                return conflicts
            # A VM may have been edited while running.  Replace its complete
            # lease set in the same transaction so a removed serial/CAN/GPIO
            # resource is released immediately and never remains stale.
            if exclusive:
                placeholders = ",".join("?" for _ in exclusive)
                c.execute(
                    f"DELETE FROM resource_leases WHERE vm_id=? AND resource_id NOT IN ({placeholders})",
                    [vm_id, *sorted(exclusive)],
                )
            else:
                c.execute("DELETE FROM resource_leases WHERE vm_id=?", (vm_id,))
            for rid in sorted(exclusive):
                c.execute(
                    "INSERT INTO resource_leases(resource_id,vm_id,leased_at) VALUES(?,?,?) ON CONFLICT(resource_id) DO UPDATE SET vm_id=excluded.vm_id,leased_at=excluded.leased_at",
                    (rid, vm_id, now),
                )
            c.commit()
        return []

    def release_resource_leases(self, vm_id: str, resource_ids: list[str] | None = None) -> None:
        with self.connect() as c:
            if resource_ids:
                ids = sorted(set(str(rid) for rid in resource_ids if rid))
                if not ids:
                    return
                placeholders = ",".join("?" for _ in ids)
                c.execute(
                    f"DELETE FROM resource_leases WHERE vm_id=? AND resource_id IN ({placeholders})",
                    [vm_id, *ids],
                )
            else:
                c.execute("DELETE FROM resource_leases WHERE vm_id=?", (vm_id,))

    @staticmethod
    def ingest_idempotency_key(batch_id: str, vm_id: str, seq_start: int) -> str:
        return f"{vm_id}:{batch_id}:{int(seq_start)}"

    def claim_ingest_batch(self, batch_id: str, vm_id: str, seq_start: int) -> bool:
        """Reserve a batch key before parsing/writing to close duplicate races."""
        key = self.ingest_idempotency_key(batch_id, vm_id, seq_start)
        with self.connect() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO ingest_batches(batch_id,vm_id,seq_start,idempotency_key,created_at) VALUES(?,?,?,?,?)",
                (batch_id, vm_id, int(seq_start), key, datetime.now(timezone.utc).isoformat()),
            )
        return cur.rowcount == 1

    def release_ingest_batch(self, batch_id: str, vm_id: str, seq_start: int) -> None:
        key = self.ingest_idempotency_key(batch_id, vm_id, seq_start)
        with self.connect() as c:
            c.execute("DELETE FROM ingest_batches WHERE idempotency_key=?", (key,))

    def purge_ingest_batches(self, *, older_than_hours: int = 24, batch_size: int = 10_000) -> int:
        """Bound idempotency metadata without holding a long SQLite write lock."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, int(older_than_hours)))).isoformat()
        limit = max(1, min(int(batch_size), 100_000))
        with self.connect() as c:
            cur = c.execute(
                """
                DELETE FROM ingest_batches
                WHERE rowid IN (
                    SELECT rowid FROM ingest_batches
                    WHERE created_at < ?
                    ORDER BY created_at
                    LIMIT ?
                )
                """,
                (cutoff, limit),
            )
        return max(0, int(cur.rowcount))

    def upsert_resources(self, resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as c:
            seen_ids = [str(r["resource_id"]) for r in resources if r.get("resource_id")]
            if seen_ids:
                placeholders = ",".join("?" for _ in seen_ids)
                c.execute(
                    f"UPDATE discovered_resources SET available=0,updated_at=? WHERE resource_id NOT IN ({placeholders})",
                    [now, *seen_ids],
                )
            else:
                c.execute("UPDATE discovered_resources SET available=0,updated_at=?", (now,))
            for r in resources:
                available = 1 if r.get("available", True) else 0
                c.execute(
                    "INSERT INTO discovered_resources(resource_id,kind,name,path,address,metadata_json,available,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(resource_id) DO UPDATE SET kind=excluded.kind,name=excluded.name,path=excluded.path,address=excluded.address,available=excluded.available,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at",
                    (r["resource_id"], r["kind"], r["name"], r.get("path"), r.get("address"), json.dumps(r.get("metadata", {})), available, now),
                )
        return self.list_resources()

    def camera_document(self) -> dict[str, Any]:
        with self.connect() as c:
            row = c.execute("SELECT document_json FROM camera_settings WHERE id=1").fetchone()
        if row is None:
            return {"cameras": []}
        try:
            payload = json.loads(row[0])
        except json.JSONDecodeError:
            return {"cameras": []}
        return payload if isinstance(payload, dict) else {"cameras": []}

    def save_camera_document(self, document: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as c:
            c.execute(
                "INSERT INTO camera_settings(id, document_json, updated_at) VALUES(1, ?, ?) ON CONFLICT(id) DO UPDATE SET document_json=excluded.document_json, updated_at=excluded.updated_at",
                (json.dumps(document, ensure_ascii=False), now),
            )
        return self.camera_document()

    def camera_status(self) -> dict[str, dict[str, Any]]:
        with self.connect() as c:
            rows = c.execute("SELECT camera_id, state, message, updated_at FROM camera_status").fetchall()
        return {
            str(row["camera_id"]): {"state": row["state"], "message": row["message"], "updated_at": row["updated_at"]}
            for row in rows
        }

    def save_camera_status(self, items: list[dict[str, Any]]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as c:
            c.execute("DELETE FROM camera_status")
            for item in items:
                camera_id = str(item.get("id") or "").strip()
                if not camera_id:
                    continue
                c.execute(
                    "INSERT INTO camera_status(camera_id, state, message, updated_at) VALUES(?,?,?,?)",
                    (camera_id, str(item.get("state") or "stopped"), str(item.get("message") or "")[:500], now),
                )

    def append_camera_logs(self, items: list[dict[str, Any]]) -> int:
        now = datetime.now(timezone.utc).isoformat()
        stored = 0
        cameras: set[str] = set()
        with self.connect() as c:
            for item in items:
                camera_id = str(item.get("camera_id") or "").strip()
                line = str(item.get("line") or "").strip()
                if not camera_id or not line:
                    continue
                level = "error" if str(item.get("level") or "") == "error" else "info"
                source = str(item.get("source") or "ffmpeg")[:32] or "ffmpeg"
                c.execute(
                    "INSERT INTO camera_logs(camera_id,level,source,line,created_at) VALUES(?,?,?,?,?)",
                    (camera_id, level, source, line[:2000], now),
                )
                cameras.add(camera_id)
                stored += 1
            for camera_id in cameras:
                kept = c.execute(
                    "SELECT id FROM camera_logs WHERE camera_id=? ORDER BY id DESC LIMIT 2000",
                    (camera_id,),
                ).fetchall()
                if len(kept) < 2000:
                    continue
                cutoff = int(kept[-1]["id"])
                c.execute("DELETE FROM camera_logs WHERE camera_id=? AND id < ?", (camera_id, cutoff))
        return stored

    def list_camera_logs(self, camera_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        cap = max(1, min(int(limit), 2000))
        with self.connect() as c:
            rows = c.execute(
                "SELECT id,camera_id,level,source,line,created_at FROM camera_logs WHERE camera_id=? ORDER BY id DESC LIMIT ?",
                (camera_id, cap),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "camera_id": str(row["camera_id"]),
                "level": str(row["level"]),
                "source": str(row["source"]),
                "line": str(row["line"]),
                "timestamp": str(row["created_at"]),
            }
            for row in reversed(rows)
        ]

    def enqueue_episode(
        self,
        camera_id: str,
        *,
        vm_id: str | None = None,
        incident_id: str | None = None,
        duration_sec: int | None = None,
        stop_at: str | None = None,
    ) -> dict[str, Any]:
        episode_id = secrets.token_hex(8)
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as c:
            c.execute(
                "INSERT INTO video_episodes(id,camera_id,vm_id,incident_id,state,path,message,started_at,ended_at,stop_at,duration_sec,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (episode_id, camera_id, vm_id, incident_id, "queued", "", "", None, None, stop_at, duration_sec, now),
            )
        episode = self.get_episode(episode_id)
        if episode is None:
            raise RuntimeError("episode was not stored")
        return episode

    def apply_motion_edge(self, camera_id: str, state: str, at: str, *, pre_seconds: int) -> dict[str, Any] | None:
        """Open, extend, or schedule the end of one motion clip for a camera.

        A start while the previous clip is still open continues that clip.
        A stop sets stop_at; the video service closes the file after the gap
        has already elapsed in the detector.
        """
        moment = _parse_iso(at) or datetime.now(timezone.utc)
        stamp = moment.astimezone(timezone.utc).isoformat()
        pre_seconds = max(0, min(int(pre_seconds), 3600))
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute(
                "SELECT * FROM video_episodes WHERE camera_id=? AND state IN ('queued','recording','stopping') ORDER BY created_at DESC",
                (camera_id,),
            ).fetchall()
            incident = next((row for row in rows if row["incident_id"]), None)
            if state == "start" and incident is not None:
                c.execute("COMMIT")
                self.append_camera_logs(
                    [{
                        "camera_id": camera_id,
                        "level": "info",
                        "source": "motion",
                        "line": "Движение есть. Отдельная запись не начата: уже идёт инцидент.",
                    }]
                )
                return None
            motion = next((row for row in rows if row["capture_from"] and not row["incident_id"]), None)
            other = next((row for row in rows if motion is None or row["id"] != motion["id"]), None)
            if state == "start":
                if motion is not None:
                    if motion["stop_at"]:
                        c.execute("UPDATE video_episodes SET stop_at=NULL WHERE id=?", (motion["id"],))
                    episode_id = str(motion["id"])
                elif other is not None:
                    c.execute("COMMIT")
                    return None
                else:
                    episode_id = secrets.token_hex(8)
                    capture_from = (moment - timedelta(seconds=pre_seconds)).isoformat()
                    c.execute(
                        "INSERT INTO video_episodes(id,camera_id,vm_id,incident_id,state,path,message,started_at,ended_at,stop_at,duration_sec,paths_json,capture_from,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (episode_id, camera_id, None, None, "queued", "", "", stamp, None, None, 86400, "[]", capture_from, stamp),
                    )
            elif state == "stop":
                if motion is None:
                    c.execute("COMMIT")
                    return None
                episode_id = str(motion["id"])
                c.execute(
                    "UPDATE video_episodes SET stop_at=? WHERE id=? AND state IN ('queued','recording')",
                    (stamp, episode_id),
                )
            else:
                c.execute("COMMIT")
                return None
            c.execute("COMMIT")
        return self.get_episode(episode_id)

    def note_purged_motion_files(self, removed: list[str]) -> int:
        """Drop deleted motion paths from episodes. Incident rows are left unchanged."""
        removed_keys = {_path_key(item) for item in removed if str(item).strip()}
        if not removed_keys:
            return 0
        updated = 0
        with self.connect() as c:
            rows = c.execute("SELECT id, incident_id, path, paths_json, state, message FROM video_episodes").fetchall()
            for row in rows:
                if row["incident_id"]:
                    continue
                paths = json.loads(row["paths_json"] or "[]")
                if not isinstance(paths, list):
                    paths = []
                if not paths and row["path"]:
                    paths = [str(row["path"])]
                kept = [item for item in paths if _path_key(str(item)) not in removed_keys]
                if len(kept) == len(paths):
                    continue
                message = str(row["message"] or "")
                if not kept and str(row["state"] or "") in {"finished", "error"}:
                    message = "Удалено: каталог видео превысил лимит"
                c.execute(
                    "UPDATE video_episodes SET path=?, paths_json=?, message=? WHERE id=?",
                    (str(kept[0]) if kept else "", json.dumps(kept), message, row["id"]),
                )
                updated += 1
        return updated

    def get_episode(self, episode_id: str) -> dict[str, Any] | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM video_episodes WHERE id=?", (episode_id,)).fetchone()
        return _episode_dict(row) if row is not None else None

    def open_episodes(self) -> list[dict[str, Any]]:
        with self.connect() as c:
            rows = c.execute(
                "SELECT * FROM video_episodes WHERE state IN ('queued','recording','stopping') ORDER BY created_at"
            ).fetchall()
        return [_episode_dict(row) for row in rows]

    def list_episodes(self, *, limit: int = 40) -> list[dict[str, Any]]:
        cap = max(1, min(int(limit), 100))
        with self.connect() as c:
            rows = c.execute("SELECT * FROM video_episodes ORDER BY created_at DESC LIMIT ?", (cap,)).fetchall()
        return [_episode_dict(row) for row in rows]

    def apply_episode_events(self, items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        changed: list[dict[str, Any]] = []
        closed_incidents: list[str] = []
        with self.connect() as c:
            for item in items:
                episode_id = str(item.get("id") or "").strip()
                state = str(item.get("state") or "")
                if not episode_id or state not in {"recording", "finished", "error"}:
                    continue
                row = c.execute("SELECT * FROM video_episodes WHERE id=?", (episode_id,)).fetchone()
                if row is None or row["state"] in {"finished", "error"}:
                    continue
                self._upsert_episode_segments(c, row, item)
                if row["state"] == "recording" and state == "recording":
                    paths = item.get("paths") or ([str(item.get("path"))] if item.get("path") else [])
                    c.execute(
                        "UPDATE video_episodes SET path=?, paths_json=?, started_at=? WHERE id=?",
                        (str(item.get("path") or row["path"] or ""), json.dumps(paths), item.get("started_at") or row["started_at"], episode_id),
                    )
                    continue
                started = item.get("started_at") or row["started_at"]
                ended = item.get("ended_at") if state in {"finished", "error"} else None
                path = str(item.get("path") or row["path"] or "")
                message = str(item.get("message") or "")[:500]
                c.execute(
                    "UPDATE video_episodes SET state=?, path=?, message=?, started_at=?, ended_at=?, paths_json=? WHERE id=?",
                    (state, path, message, started or None, ended or None, json.dumps(item.get("paths") or ([path] if path else [])), episode_id),
                )
                updated = c.execute("SELECT * FROM video_episodes WHERE id=?", (episode_id,)).fetchone()
                if updated is not None:
                    changed.append(_episode_dict(updated))
                    incident_id = str(updated["incident_id"] or "") if "incident_id" in updated.keys() else ""
                    if incident_id and state in {"finished", "error"}:
                        if self._finish_incident_if_complete(c, incident_id, ended or datetime.now(timezone.utc).isoformat()):
                            closed_incidents.append(incident_id)
        return changed, closed_incidents

    @staticmethod
    def _upsert_episode_segments(c: sqlite3.Connection, episode: sqlite3.Row, item: dict[str, Any]) -> None:
        raw_segments = item.get("segments") if isinstance(item.get("segments"), list) else []
        metadata = {
            str(segment.get("path")): segment
            for segment in raw_segments
            if isinstance(segment, dict) and segment.get("path")
        }
        paths = item.get("paths") or ([item.get("path")] if item.get("path") else [])
        now = datetime.now(timezone.utc).isoformat()
        for raw in paths:
            path = str(raw or "").strip()
            if not path:
                continue
            segment = metadata.get(path, {})
            started_at = segment.get("started_at")
            ended_at = segment.get("ended_at")
            size = int(segment.get("size_bytes") or 0)
            if not started_at:
                try:
                    start = datetime.strptime(Path(path).stem[:15], "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
                    started_at = start.isoformat()
                    ended_at = ended_at or (start + timedelta(seconds=2)).isoformat()
                except ValueError:
                    started_at = episode["capture_from"] or episode["started_at"]
            if size <= 0:
                try:
                    size = Path(path).stat().st_size
                except OSError:
                    size = 0
            c.execute(
                """
                INSERT INTO video_episode_segments(episode_id,camera_id,path,started_at,ended_at,size_bytes,state,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET
                    episode_id=excluded.episode_id,camera_id=excluded.camera_id,
                    started_at=COALESCE(excluded.started_at,video_episode_segments.started_at),
                    ended_at=COALESCE(excluded.ended_at,video_episode_segments.ended_at),
                    size_bytes=excluded.size_bytes,state=excluded.state
                """,
                (episode["id"], episode["camera_id"], path, started_at, ended_at, size, "ready", now),
            )

    def list_video_segments(
        self,
        *,
        date_from: str,
        date_to: str,
        camera_ids: list[str] | None = None,
        vm_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["COALESCE(s.started_at,e.created_at) <= ?", "COALESCE(s.ended_at,e.ended_at,e.stop_at,e.started_at) >= ?"]
        args: list[Any] = [date_to, date_from]
        if camera_ids:
            clauses.append("s.camera_id IN (%s)" % ",".join("?" for _ in camera_ids))
            args.extend(camera_ids)
        if vm_ids:
            clauses.append("(e.vm_id IN (%s) OR e.vm_id IS NULL)" % ",".join("?" for _ in vm_ids))
            args.extend(vm_ids)
        with self.connect() as c:
            rows = c.execute(
                f"""
                SELECT s.*,e.vm_id,e.incident_id,e.capture_from,e.stop_at
                FROM video_episode_segments s JOIN video_episodes e ON e.id=s.episode_id
                WHERE {' AND '.join(clauses)}
                ORDER BY s.camera_id,COALESCE(s.started_at,e.created_at),s.id
                """,
                args,
            ).fetchall()
        return [dict(row) for row in rows]

    def backfill_video_segments(self) -> int:
        """Catalog paths written by versions that predate the segment table."""
        with self.connect() as c:
            episodes = c.execute(
                """
                SELECT e.* FROM video_episodes e
                WHERE COALESCE(e.paths_json,'[]')!='[]' OR COALESCE(e.path,'')!=''
                """
            ).fetchall()
            before = int(c.execute("SELECT COUNT(*) FROM video_episode_segments").fetchone()[0])
            for episode in episodes:
                paths = json.loads(episode["paths_json"] or "[]")
                if not paths and episode["path"]:
                    paths = [episode["path"]]
                self._upsert_episode_segments(c, episode, {"paths": paths})
            after = int(c.execute("SELECT COUNT(*) FROM video_episode_segments").fetchone()[0])
        return after - before

    def create_export_job(self, kind: str, params: dict[str, Any], *, ttl_hours: int = 24) -> dict[str, Any]:
        job_id = str(uuid4())
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=max(1, ttl_hours))
        with self.connect() as c:
            c.execute(
                """
                INSERT INTO export_jobs(id,kind,state,params_json,created_at,updated_at,expires_at)
                VALUES(?,?, 'queued', ?,?,?,?)
                """,
                (job_id, kind, json.dumps(params, ensure_ascii=False), now.isoformat(), now.isoformat(), expires.isoformat()),
            )
        return self.get_export_job(job_id) or {}

    def get_export_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM export_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["params"] = json.loads(result.pop("params_json") or "{}")
        return result

    def update_export_job(self, job_id: str, **values: Any) -> dict[str, Any] | None:
        allowed = {"state", "progress", "result_path", "filename", "message"}
        changes = {key: value for key, value in values.items() if key in allowed}
        if not changes:
            return self.get_export_job(job_id)
        changes["updated_at"] = datetime.now(timezone.utc).isoformat()
        with self.connect() as c:
            c.execute(
                f"UPDATE export_jobs SET {','.join(f'{key}=?' for key in changes)} WHERE id=?",
                [*changes.values(), job_id],
            )
        return self.get_export_job(job_id)

    def cleanup_export_jobs(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as c:
            paths = [
                str(row["result_path"])
                for row in c.execute("SELECT result_path FROM export_jobs WHERE expires_at<? AND result_path IS NOT NULL", (now,))
            ]
            cur = c.execute("DELETE FROM export_jobs WHERE expires_at<?", (now,))
        for raw in paths:
            try:
                Path(raw).unlink(missing_ok=True)
            except OSError:
                pass
        return int(cur.rowcount)

    def register_incident_start(
        self,
        vm_id: str,
        *,
        name: str,
        kind: str,
        created_at: datetime,
        camera_ids: list[str],
        pre_seconds: int = 10,
        post_seconds: int = 15,
    ) -> dict[str, Any]:
        """Open or extend one incident and create one long-running episode per camera."""
        moment = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=timezone.utc)
        stamp = moment.astimezone(timezone.utc).isoformat()
        pre_seconds = max(0, min(int(pre_seconds), 3600))
        post_seconds = max(0, min(int(post_seconds), 3600))
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM video_incidents WHERE vm_id=? AND state IN ('active','finishing') ORDER BY started_at DESC LIMIT 1",
                (vm_id,),
            ).fetchone()
            if row is not None and row["state"] == "finishing":
                stop_at = _parse_iso(row["stop_at"])
                if stop_at is None or moment <= stop_at:
                    c.execute(
                        "UPDATE video_incidents SET state='active', last_alert_at=?, stop_at=NULL WHERE id=?",
                        (stamp, row["id"]),
                    )
                    c.execute(
                        "UPDATE video_episodes SET stop_at=NULL WHERE incident_id=? AND state IN ('queued','recording')",
                        (row["id"],),
                    )
                    row = c.execute("SELECT * FROM video_incidents WHERE id=?", (row["id"],)).fetchone()
                else:
                    row = None
            if row is None:
                incident_id = secrets.token_hex(10)
                c.execute(
                    "INSERT INTO video_incidents(id,vm_id,state,started_at,last_alert_at,stop_at,ended_at,pre_seconds,post_seconds,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (incident_id, vm_id, "active", stamp, stamp, None, None, pre_seconds, post_seconds, stamp),
                )
                row = c.execute("SELECT * FROM video_incidents WHERE id=?", (incident_id,)).fetchone()
            else:
                c.execute(
                    "UPDATE video_incidents SET state='active', last_alert_at=?, stop_at=NULL WHERE id=?",
                    (stamp, row["id"]),
                )
                row = c.execute("SELECT * FROM video_incidents WHERE id=?", (row["id"],)).fetchone()
            incident_id = str(row["id"])
            c.execute(
                "INSERT INTO video_incident_alerts(incident_id,vm_id,name,kind,state,created_at) VALUES(?,?,?,?,?,?)",
                (incident_id, vm_id, str(name), kind if kind in {"gpio", "emergency"} else "alert", "active", stamp),
            )
            wanted = {str(item).strip() for item in camera_ids if str(item).strip()}
            yielded: list[str] = []
            for motion in c.execute(
                """SELECT id, camera_id FROM video_episodes
                   WHERE state IN ('queued','recording','stopping') AND IFNULL(incident_id,'')='' AND IFNULL(capture_from,'')<>''"""
            ).fetchall():
                camera_id = str(motion["camera_id"])
                if camera_id not in wanted:
                    continue
                c.execute(
                    "UPDATE video_episodes SET state='finished', ended_at=?, stop_at=NULL, message=? WHERE id=?",
                    (stamp, "Сохранено: начался инцидент", motion["id"]),
                )
                yielded.append(camera_id)
            existing = {
                str(item["camera_id"])
                for item in c.execute(
                    "SELECT camera_id FROM video_episodes WHERE incident_id=? AND state <> 'error'",
                    (incident_id,),
                ).fetchall()
            }
            busy = {
                str(item["camera_id"])
                for item in c.execute(
                    "SELECT camera_id FROM video_episodes WHERE state IN ('queued','recording','stopping')"
                ).fetchall()
            }
            started = sorted(wanted - existing - busy)
            for camera_id in started:
                c.execute(
                    "INSERT INTO video_episodes(id,camera_id,vm_id,incident_id,state,path,message,started_at,ended_at,stop_at,duration_sec,paths_json,capture_from,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (secrets.token_hex(8), camera_id, vm_id, incident_id, "queued", "", "", stamp, None, None, 86400, "[]", (moment - timedelta(seconds=pre_seconds)).isoformat(), stamp),
                )
            c.execute("COMMIT")
        reason = _recording_reason(kind, name)
        self.append_camera_logs(
            [{"camera_id": camera_id, "level": "info", "source": "hub", "line": f"Запись движения сохранена. Начата запись инцидента, {reason}."} for camera_id in yielded]
            + [{"camera_id": camera_id, "level": "info", "source": "hub", "line": f"Запись начата. Причина: {reason}."} for camera_id in started]
            + [{"camera_id": camera_id, "level": "info", "source": "hub", "line": f"Запись продолжается. Причина: {reason}."} for camera_id in sorted(existing)]
        )
        return self.get_incident(incident_id) or {}

    def register_incident_end(
        self,
        vm_id: str,
        *,
        name: str | None = None,
        kind: str = "alert",
        created_at: datetime,
        post_seconds: int = 15,
        has_active: bool | None = None,
    ) -> dict[str, Any] | None:
        moment = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=timezone.utc)
        stamp = moment.astimezone(timezone.utc).isoformat()
        post_seconds = max(0, min(int(post_seconds), 3600))
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            active = c.execute(
                """
                SELECT 1 FROM alarm_active WHERE vm_id=? AND triggers_incident=1
                UNION ALL
                SELECT 1 FROM emergency_active WHERE vm_id=?
                LIMIT 1
                """,
                (vm_id, vm_id),
            ).fetchone() is not None
            row = c.execute(
                "SELECT * FROM video_incidents WHERE vm_id=? AND state IN ('active','finishing') ORDER BY started_at DESC LIMIT 1",
                (vm_id,),
            ).fetchone()
            if row is not None and name:
                c.execute(
                    "INSERT INTO video_incident_alerts(incident_id,vm_id,name,kind,state,created_at) VALUES(?,?,?,?,?,?)",
                    (row["id"], vm_id, str(name), kind if kind in {"gpio", "emergency"} else "alert", "inactive", stamp),
                )
            if bool(active) or row is None:
                c.execute("COMMIT")
                return _incident_dict(row) if row is not None else None
            stop_at = (moment + timedelta(seconds=post_seconds)).isoformat()
            c.execute(
                "UPDATE video_incidents SET state='finishing', stop_at=?, post_seconds=? WHERE id=?",
                (stop_at, post_seconds, row["id"]),
            )
            c.execute(
                "UPDATE video_episodes SET stop_at=? WHERE incident_id=? AND state IN ('queued','recording')",
                (stop_at, row["id"]),
            )
            cameras = [
                str(item["camera_id"])
                for item in c.execute(
                    "SELECT camera_id FROM video_episodes WHERE incident_id=? AND state IN ('queued','recording')",
                    (row["id"],),
                ).fetchall()
            ]
            if c.execute("SELECT 1 FROM video_episodes WHERE incident_id=? LIMIT 1", (row["id"],)).fetchone() is None:
                c.execute("UPDATE video_incidents SET state='closed', ended_at=?, stop_at=NULL WHERE id=?", (stamp, row["id"]))
            c.execute("COMMIT")
        reason = _recording_reason(kind, name or "")
        self.append_camera_logs(
            [{"camera_id": camera_id, "level": "info", "source": "hub", "line": f"Причина снята: {reason}. Запись закроется после паузы."} for camera_id in cameras]
        )
        return self.get_incident(str(row["id"]))

    def get_incident(self, incident_id: str) -> dict[str, Any] | None:
        with self.connect() as c:
            row = c.execute(
                """
                SELECT i.*,
                    (SELECT json_group_array(json_object('id',e.id,'camera_id',e.camera_id,'vm_id',e.vm_id,'incident_id',e.incident_id,'state',e.state,'path',e.path,'paths_json',e.paths_json,'message',e.message,'started_at',e.started_at,'ended_at',e.ended_at,'stop_at',e.stop_at,'capture_from',e.capture_from,'duration_sec',e.duration_sec,'created_at',e.created_at)) FROM video_episodes e WHERE e.incident_id=i.id) AS episodes_json,
                    (SELECT json_group_array(json_object('id',a.id,'vm_id',a.vm_id,'name',a.name,'kind',a.kind,'state',a.state,'created_at',a.created_at)) FROM video_incident_alerts a WHERE a.incident_id=i.id) AS alerts_json
                FROM video_incidents i WHERE i.id=?
                """,
                (incident_id,),
            ).fetchone()
            if row is None:
                return None
            result = _incident_dict(row)
            episodes = json.loads(row["episodes_json"] or "[]")
            for episode in episodes:
                episode["paths"] = json.loads(episode.pop("paths_json") or "[]")
            result["episodes"] = episodes
            result["alerts"] = json.loads(row["alerts_json"] or "[]")
            return result

    def list_incidents(self, *, vm_ids: list[str] | None = None, limit: int = 100) -> list[dict[str, Any]]:
        cap = max(1, min(int(limit), 500))
        if vm_ids is not None and not vm_ids:
            return []
        with self.connect() as c:
            if vm_ids is not None:
                placeholders = ",".join("?" for _ in vm_ids)
                where = f"i.vm_id IN ({placeholders})"
                args = [*vm_ids, cap]
            else:
                where = "1=1"
                args = [cap]
            rows = c.execute(
                f"""
                SELECT i.*,
                    (SELECT json_group_array(json_object('id',e.id,'camera_id',e.camera_id,'vm_id',e.vm_id,'incident_id',e.incident_id,'state',e.state,'path',e.path,'paths_json',e.paths_json,'message',e.message,'started_at',e.started_at,'ended_at',e.ended_at,'stop_at',e.stop_at,'capture_from',e.capture_from,'duration_sec',e.duration_sec,'created_at',e.created_at)) FROM video_episodes e WHERE e.incident_id=i.id) AS episodes_json,
                    (SELECT json_group_array(json_object('id',a.id,'vm_id',a.vm_id,'name',a.name,'kind',a.kind,'state',a.state,'created_at',a.created_at)) FROM video_incident_alerts a WHERE a.incident_id=i.id) AS alerts_json
                FROM video_incidents i WHERE {where} ORDER BY i.started_at DESC LIMIT ?
                """,
                args,
            ).fetchall()
            results = []
            for row in rows:
                incident = _incident_dict(row)
                incident["episodes"] = json.loads(row["episodes_json"] or "[]")
                for episode in incident["episodes"]:
                    episode["paths"] = json.loads(episode.pop("paths_json") or "[]")
                incident["alerts"] = json.loads(row["alerts_json"] or "[]")
                results.append(incident)
            return results

    def list_incidents_overlapping(self, *, vm_ids: list[str], date_from: str, date_to: str) -> list[dict[str, Any]]:
        if not vm_ids:
            return []
        placeholders = ",".join("?" for _ in vm_ids)
        with self.connect() as c:
            ids = [
                str(row["id"])
                for row in c.execute(
                    f"""
                    SELECT id FROM video_incidents
                    WHERE vm_id IN ({placeholders})
                      AND started_at<=?
                      AND COALESCE(telemetry_to,ended_at,stop_at,last_alert_at,started_at)>=?
                    ORDER BY started_at
                    """,
                    [*vm_ids, date_to, date_from],
                ).fetchall()
            ]
        return [incident for incident_id in ids if (incident := self.get_incident(incident_id)) is not None]

    @staticmethod
    def _finish_incident_if_complete(c: sqlite3.Connection, incident_id: str, ended_at: str) -> bool:
        row = c.execute("SELECT state FROM video_incidents WHERE id=?", (incident_id,)).fetchone()
        if row is None or row["state"] == "closed":
            return False
        active = c.execute("SELECT 1 FROM video_episodes WHERE incident_id=? AND state IN ('queued','recording','stopping') LIMIT 1", (incident_id,)).fetchone()
        if active is not None:
            return False
        c.execute("UPDATE video_incidents SET state='closed', ended_at=?, stop_at=NULL WHERE id=?", (ended_at, incident_id))
        return True

    def replace_incident_camera_file(self, incident_id: str, camera_id: str, merged_path: str) -> None:
        """Point the incident camera at one merged file and drop the chunk catalog."""
        with self.connect() as c:
            episodes = c.execute(
                "SELECT id, started_at, ended_at, capture_from FROM video_episodes WHERE incident_id=? AND camera_id=? ORDER BY created_at",
                (incident_id, camera_id),
            ).fetchall()
            if not episodes:
                return
            ids = [str(row["id"]) for row in episodes]
            placeholders = ",".join("?" for _ in ids)
            spans = c.execute(
                f"SELECT MIN(started_at) AS started_at, MAX(ended_at) AS ended_at FROM video_episode_segments WHERE episode_id IN ({placeholders})",
                ids,
            ).fetchone()
            c.execute(f"DELETE FROM video_episode_segments WHERE episode_id IN ({placeholders})", ids)
            primary = ids[0]
            try:
                size = Path(merged_path).stat().st_size
            except OSError:
                size = 0
            started = (spans["started_at"] if spans is not None else None) or episodes[0]["capture_from"] or episodes[0]["started_at"]
            ended = (spans["ended_at"] if spans is not None else None) or episodes[0]["ended_at"]
            now = datetime.now(timezone.utc).isoformat()
            c.execute(
                "UPDATE video_episodes SET path=?, paths_json=? WHERE id=?",
                (merged_path, json.dumps([merged_path]), primary),
            )
            for extra in ids[1:]:
                c.execute("UPDATE video_episodes SET path='', paths_json='[]' WHERE id=?", (extra,))
            c.execute(
                """
                INSERT INTO video_episode_segments(episode_id,camera_id,path,started_at,ended_at,size_bytes,state,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (primary, camera_id, merged_path, started, ended, size, "ready", now),
            )


    def list_resource_leases(self) -> dict[str, str]:
        with self.connect() as c:
            rows = c.execute("SELECT resource_id, vm_id FROM resource_leases").fetchall()
        return {str(row["resource_id"]): str(row["vm_id"]) for row in rows}

    def approve_resource(self, resource_id: str, user_id: int | None = None) -> bool:
        with self.connect() as c:
            cur = c.execute("UPDATE discovered_resources SET approved=1,approved_by=?,approved_at=? WHERE resource_id=?", (user_id, datetime.now(timezone.utc).isoformat(), resource_id))
        return cur.rowcount > 0

    @staticmethod
    def _vm_row(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        for key in ("resources", "read_resources", "limits", "config"):
            storage_key = f"{key}_json"
            if storage_key not in out:
                continue
            raw = out.pop(storage_key) or ("[]" if key in {"resources", "read_resources"} else "{}")
            out[key] = json.loads(raw)
        if not out.get("read_resources"):
            out["read_resources"] = list(out.get("resources", []))
        if "resources" not in out:
            out["resources"] = list(out.get("read_resources", []))
        return out
