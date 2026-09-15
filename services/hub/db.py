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
                    resources_json TEXT NOT NULL DEFAULT '[]', limits_json TEXT NOT NULL DEFAULT '{}',
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
                CREATE TABLE IF NOT EXISTS lifecycle_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, vm_id TEXT NOT NULL,
                    event TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT NOT NULL,
                    target TEXT, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ingest_batches (
                    batch_id TEXT PRIMARY KEY, vm_id TEXT NOT NULL, seq_start INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            c.executemany("INSERT OR IGNORE INTO roles(name) VALUES (?)", [("admin",), ("user",)])
            columns = {row[1] for row in c.execute("PRAGMA table_info(virtual_machines)").fetchall()}
            if "heartbeat_at" not in columns:
                c.execute("ALTER TABLE virtual_machines ADD COLUMN heartbeat_at TEXT")

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
        with self.connect() as c:
            c.execute(
                "INSERT INTO virtual_machines(id,name,description,protocol,preset_id,map_version,worker_image,desired_state,resources_json,limits_json,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (vm_id, values["name"], values.get("description", ""), values["protocol"], values.get("preset_id"), values["map_version"], values["worker_image"], "stopped", json.dumps(values.get("resources", [])), json.dumps(values.get("limits", {})), json.dumps(values.get("config", {})), now, now),
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
        allowed = {k: v for k, v in values.items() if k in {"name", "description", "desired_state", "map_version", "preset_id", "resources", "limits", "config", "lifecycle", "container_id", "last_error", "heartbeat_at", "worker_image"}}
        if not allowed:
            return self.get_vm(vm_id)
        sets: list[str] = []
        args: list[Any] = []
        for key, value in allowed.items():
            col = {"resources": "resources_json", "limits": "limits_json", "config": "config_json"}.get(key, key)
            if key in {"resources", "limits", "config"}:
                value = json.dumps(value)
            sets.append(f"{col}=?")
            args.append(value)
        if any(key in allowed for key in {"name", "description", "map_version", "preset_id", "resources", "limits", "config", "worker_image"}):
            sets.append("config_revision=config_revision+1")
        sets.append("updated_at=?")
        args.extend([datetime.now(timezone.utc).isoformat(), vm_id])
        with self.connect() as c:
            c.execute(f"UPDATE virtual_machines SET {','.join(sets)} WHERE id=?", args)
        return self.get_vm(vm_id)

    def delete_vm(self, vm_id: str) -> bool:
        with self.connect() as c:
            cur = c.execute("DELETE FROM virtual_machines WHERE id=?", (vm_id,))
        return cur.rowcount > 0

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
        for vm in self.list_vms():
            if exclude_vm_id and vm["id"] == exclude_vm_id:
                continue
            if vm.get("desired_state") != "running" and vm.get("lifecycle") not in {"created", "starting", "running", "stopping"}:
                continue
            for item in vm.get("resources", []):
                rid = item.get("resource_id") if isinstance(item, dict) else str(item)
                if rid in resource_ids and rid in exclusive:
                    conflicts.append(rid)
        return sorted(set(conflicts))

    def upsert_resources(self, resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as c:
            for r in resources:
                c.execute(
                    "INSERT INTO discovered_resources(resource_id,kind,name,path,address,metadata_json,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(resource_id) DO UPDATE SET available=1,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at",
                    (r["resource_id"], r["kind"], r["name"], r.get("path"), r.get("address"), json.dumps(r.get("metadata", {})), now),
                )
        return self.list_resources()

    def approve_resource(self, resource_id: str, user_id: int) -> bool:
        with self.connect() as c:
            cur = c.execute("UPDATE discovered_resources SET approved=1,approved_by=?,approved_at=? WHERE resource_id=?", (user_id, datetime.now(timezone.utc).isoformat(), resource_id))
        return cur.rowcount > 0

    @staticmethod
    def _vm_row(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        for key in ("resources", "limits", "config"):
            out[key] = json.loads(out.pop(f"{key}_json"))
        return out
