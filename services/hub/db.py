from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID, uuid4

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError


def _episode_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "camera_id": str(row["camera_id"]),
        "state": str(row["state"]),
        "path": str(row["path"] or ""),
        "message": str(row["message"] or ""),
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "created_at": str(row["created_at"]),
    }


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
                    created_at TEXT NOT NULL, UNIQUE(checksum)
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
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alarm_active (
                    vm_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    PRIMARY KEY (vm_id, kind, name)
                );
                CREATE INDEX IF NOT EXISTS idx_alarm_events_vm ON alarm_events(vm_id, kind, created_at);
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
                CREATE TABLE IF NOT EXISTS video_episodes (
                    id TEXT PRIMARY KEY,
                    camera_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    path TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    started_at TEXT,
                    ended_at TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            c.executemany("INSERT OR IGNORE INTO roles(name) VALUES (?)", [("admin",), ("user",)])
            columns = {row[1] for row in c.execute("PRAGMA table_info(virtual_machines)").fetchall()}
            if "heartbeat_at" not in columns:
                c.execute("ALTER TABLE virtual_machines ADD COLUMN heartbeat_at TEXT")
            if "read_resources_json" not in columns:
                c.execute("ALTER TABLE virtual_machines ADD COLUMN read_resources_json TEXT NOT NULL DEFAULT '[]'")
                c.execute("UPDATE virtual_machines SET read_resources_json=resources_json WHERE read_resources_json='[]' OR read_resources_json IS NULL")
            if "storage_resource_id" not in columns:
                c.execute("ALTER TABLE virtual_machines ADD COLUMN storage_resource_id TEXT")
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

    def sync_alarm_edges(self, vm_id: str, created_at: datetime, active_names: set[str] | list[str], *, kind: str = "alert") -> list[dict[str, Any]]:
        """Write one row when an alarm starts and one when it ends.

        Repeating the same active set does not insert anything, so a poll
        loop cannot fill the journal with unchanged values.
        """
        moment = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=timezone.utc)
        stamp = moment.astimezone(timezone.utc).isoformat()
        channel = "gpio" if kind == "gpio" else "alert"
        desired = {str(name).strip() for name in active_names if str(name).strip()}
        events: list[dict[str, Any]] = []
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                rows = c.execute("SELECT name FROM alarm_active WHERE vm_id=? AND kind=?", (vm_id, channel)).fetchall()
                current = {str(row["name"]) for row in rows}
                for name in sorted(desired - current):
                    c.execute(
                        "INSERT INTO alarm_events(vm_id,name,state,kind,created_at) VALUES(?,?,?,?,?)",
                        (vm_id, name, "active", channel, stamp),
                    )
                    c.execute(
                        "INSERT OR REPLACE INTO alarm_active(vm_id,kind,name,started_at) VALUES(?,?,?,?)",
                        (vm_id, channel, name, stamp),
                    )
                    events.append({"vm_id": vm_id, "name": name, "state": "active", "kind": channel, "created_at": stamp})
                for name in sorted(current - desired):
                    c.execute(
                        "INSERT INTO alarm_events(vm_id,name,state,kind,created_at) VALUES(?,?,?,?,?)",
                        (vm_id, name, "inactive", channel, stamp),
                    )
                    c.execute("DELETE FROM alarm_active WHERE vm_id=? AND kind=? AND name=?", (vm_id, channel, name))
                    events.append({"vm_id": vm_id, "name": name, "state": "inactive", "kind": channel, "created_at": stamp})
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        return events

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
        channel = "gpio" if kind == "gpio" else "alert"
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
                f"SELECT id,vm_id,name,state,kind,created_at FROM alarm_events WHERE {where} ORDER BY created_at {order}, id {order} LIMIT ? OFFSET ?",
                [*args, max(1, limit), max(0, offset)],
            ).fetchall()
        return [dict(row) for row in rows], total

    def save_map(self, document: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as c:
            existing = c.execute("SELECT checksum FROM map_versions WHERE version=? AND protocol=? LIMIT 1", (document["version"], document["protocol"])).fetchone()
            if existing is not None and existing["checksum"] != document["checksum"]:
                raise ValueError(f"map version {document['version']} is immutable")
            c.execute(
                "INSERT OR IGNORE INTO map_versions(id,version,protocol,preset_id,checksum,document_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (document["map_id"], document["version"], document["protocol"], document.get("preset_id"), document["checksum"], json.dumps(document), now),
            )

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
                "SELECT id,version,protocol,preset_id,checksum,created_at FROM map_versions ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def map_record(self, version: str, protocol: str | None = None) -> dict[str, Any] | None:
        with self.connect() as c:
            if protocol is None:
                row = c.execute(
                    "SELECT id,version,protocol,preset_id,checksum,document_json,created_at FROM map_versions WHERE version=? ORDER BY created_at DESC LIMIT 1",
                    (version,),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT id,version,protocol,preset_id,checksum,document_json,created_at FROM map_versions WHERE version=? AND protocol=? ORDER BY created_at DESC LIMIT 1",
                    (version, protocol),
                ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        payload["document"] = json.loads(payload.pop("document_json"))
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

    def enqueue_episode(self, camera_id: str) -> dict[str, Any]:
        episode_id = secrets.token_hex(8)
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as c:
            c.execute(
                "INSERT INTO video_episodes(id,camera_id,state,path,message,started_at,ended_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (episode_id, camera_id, "queued", "", "", None, None, now),
            )
        episode = self.get_episode(episode_id)
        if episode is None:
            raise RuntimeError("episode was not stored")
        return episode

    def get_episode(self, episode_id: str) -> dict[str, Any] | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM video_episodes WHERE id=?", (episode_id,)).fetchone()
        return _episode_dict(row) if row is not None else None

    def open_episodes(self) -> list[dict[str, Any]]:
        with self.connect() as c:
            rows = c.execute(
                "SELECT * FROM video_episodes WHERE state IN ('queued','recording') ORDER BY created_at"
            ).fetchall()
        return [_episode_dict(row) for row in rows]

    def list_episodes(self, *, limit: int = 40) -> list[dict[str, Any]]:
        cap = max(1, min(int(limit), 100))
        with self.connect() as c:
            rows = c.execute("SELECT * FROM video_episodes ORDER BY created_at DESC LIMIT ?", (cap,)).fetchall()
        return [_episode_dict(row) for row in rows]

    def apply_episode_events(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        changed: list[dict[str, Any]] = []
        with self.connect() as c:
            for item in items:
                episode_id = str(item.get("id") or "").strip()
                state = str(item.get("state") or "")
                if not episode_id or state not in {"recording", "finished", "error"}:
                    continue
                row = c.execute("SELECT * FROM video_episodes WHERE id=?", (episode_id,)).fetchone()
                if row is None or row["state"] in {"finished", "error"}:
                    continue
                if row["state"] == "recording" and state == "recording":
                    continue
                started = item.get("started_at") or row["started_at"]
                ended = item.get("ended_at") if state in {"finished", "error"} else None
                path = str(item.get("path") or row["path"] or "")
                message = str(item.get("message") or "")[:500]
                c.execute(
                    "UPDATE video_episodes SET state=?, path=?, message=?, started_at=?, ended_at=? WHERE id=?",
                    (state, path, message, started or None, ended or None, episode_id),
                )
                updated = c.execute("SELECT * FROM video_episodes WHERE id=?", (episode_id,)).fetchone()
                if updated is not None:
                    changed.append(_episode_dict(updated))
        return changed

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
