from __future__ import annotations

import json
import os
import time
import urllib.request
from urllib.error import HTTPError, URLError
from datetime import datetime, timezone
from uuid import UUID

from bb_platform.contracts import RawBatch, RawSample, VmProtocol, WorkerCommandAck, WorkerError, WorkerHeartbeat, WorkerRegister


def format_exception(exc: BaseException) -> str:
    parts: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current).strip() or type(current).__name__
        if not parts or text not in parts[-1]:
            parts.append(text)
        nxt = current.__cause__
        if nxt is None and not getattr(current, "__suppress_context__", False):
            nxt = current.__context__
        current = nxt
    return " — ".join(parts)


class WorkerClient:
    """Small stdlib HTTP client used inside hardened worker containers."""

    def __init__(self, *, hub_url: str | None = None, vm_id: str | None = None, token: str | None = None, protocol: VmProtocol) -> None:
        self.hub_url = (hub_url or os.getenv("BB_HUB_URL", "http://hub:8080")).rstrip("/")
        self.vm_id = vm_id or os.environ["BB_VM_ID"]
        self.token = token or os.environ["BB_BOOTSTRAP_TOKEN"]
        self.protocol = protocol
        self.worker_id = os.getenv("HOSTNAME", "worker")
        self.seq = 0

    def _request(self, path: str, *, method: str, payload: dict | None = None) -> dict:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        headers = {"X-Worker-Token": self.token}
        if data is not None:
            headers["Content-Type"] = "application/json"
        last_error: Exception | None = None
        for attempt in range(3):
            request = urllib.request.Request(f"{self.hub_url}{path}", data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return json.loads(response.read().decode())
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.25 * (attempt + 1))
        raise RuntimeError(f"Hub request failed: {last_error}") from last_error

    def post(self, path: str, payload: dict) -> dict:
        return self._request(path, method="POST", payload=payload)

    def get(self, path: str) -> dict:
        return self._request(path, method="GET")

    def register(self) -> dict:
        return self.post("/api/v1/internal/workers/register", WorkerRegister(vm_id=UUID(self.vm_id), worker_id=self.worker_id, protocol=self.protocol, capabilities=["batch", "heartbeat"]).model_dump(mode="json"))

    def heartbeat(self, *, health: str = "healthy") -> dict:
        self.seq += 1
        return self.post(
            "/api/v1/internal/workers/heartbeat",
            WorkerHeartbeat(
                vm_id=UUID(self.vm_id),
                worker_id=self.worker_id,
                timestamp=datetime.now(timezone.utc),
                seq=self.seq,
                health=health,  # type: ignore[arg-type]
            ).model_dump(mode="json"),
        )

    def commands(self) -> list[dict]:
        return list(self.get(f"/api/v1/internal/workers/{self.vm_id}/commands").get("items", []))

    def configuration(self) -> dict:
        """Fetch the current VM settings and immutable map from Hub."""
        return self.get(f"/api/v1/internal/workers/{self.vm_id}/config")

    def acknowledge(self, command: dict, *, accepted: bool = True, message: str | None = None) -> dict:
        payload = WorkerCommandAck(command_id=UUID(str(command["command_id"])), vm_id=UUID(self.vm_id), accepted=accepted, message=message)
        return self.post("/api/v1/internal/workers/command-ack", payload.model_dump(mode="json"))

    def report_error(self, code: str, message: str | BaseException, details: dict | None = None) -> dict:
        text = format_exception(message) if isinstance(message, BaseException) else str(message)
        payload = WorkerError(vm_id=UUID(self.vm_id), code=code, message=text, timestamp=datetime.now(timezone.utc), details=details or {})
        return self.post("/api/v1/internal/workers/error", payload.model_dump(mode="json"))

    def batch(self, sources: dict[str, list], map_version: str, *, quality: str = "good") -> dict:
        self.seq += 1
        sample = RawSample(seq=self.seq, captured_at=datetime.now(timezone.utc), sources=sources, quality=quality)
        batch = RawBatch(vm_id=UUID(self.vm_id), protocol=self.protocol, map_version=map_version, seq_start=self.seq, samples=[sample])
        return self.post("/api/v1/internal/workers/batches", batch.model_dump(mode="json"))
