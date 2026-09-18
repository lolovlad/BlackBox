from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from bb_platform.contracts import EventEnvelope, TagSample, VmStatus


class EventBus:
    def __init__(self, *, log_limit: int = 10_000) -> None:
        self._seq = 0
        self._lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue[EventEnvelope]] = set()
        self._history: deque[EventEnvelope] = deque(maxlen=log_limit)
        self._latest_status: dict[str, VmStatus] = {}
        self._latest_tags: dict[str, TagSample] = {}
        self._logs: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=log_limit))
        self._alarms: deque[dict[str, Any]] = deque(maxlen=log_limit)

    async def publish(self, topic: str, payload: dict[str, Any]) -> EventEnvelope:
        async with self._lock:
            self._seq += 1
            event = EventEnvelope(seq=self._seq, topic=topic, payload=payload)
            self._history.append(event)
            if topic == "alarms":
                self._alarms.append(payload)
            for queue in list(self._subscribers):
                if not queue.full():
                    queue.put_nowait(event)
            return event

    async def publish_status(self, status: VmStatus) -> EventEnvelope:
        self._latest_status[str(status.vm_id)] = status
        return await self.publish("vm_status", status.model_dump(mode="json"))

    async def publish_tags(self, sample: TagSample) -> EventEnvelope:
        self._latest_tags[str(sample.vm_id)] = sample
        return await self.publish("tags", sample.model_dump(mode="json"))

    async def publish_log(self, vm_id: str, line: str, *, level: str = "info") -> EventEnvelope:
        item = {"vm_id": vm_id, "timestamp": datetime.now(timezone.utc).isoformat(), "level": level, "line": line}
        self._logs[vm_id].append(item)
        return await self.publish("logs", item)

    def logs_for(self, vm_id: str, *, limit: int = 2000) -> list[dict[str, Any]]:
        cap = max(1, min(int(limit), 10_000))
        return list(self._logs.get(str(vm_id), []))[-cap:]

    def latest_tags(self, vm_id: str) -> TagSample | None:
        return self._latest_tags.get(str(vm_id))

    def snapshot(self) -> dict[str, Any]:
        return {
            "seq": self._seq,
            "vm_status": [x.model_dump(mode="json") for x in self._latest_status.values()],
            "tags": [x.model_dump(mode="json") for x in self._latest_tags.values()],
            "logs": {key: list(value)[-2000:] for key, value in self._logs.items()},
            "alarms": list(self._alarms)[-500:],
        }

    async def subscribe(self, *, after_seq: int = 0) -> asyncio.Queue[EventEnvelope]:
        queue: asyncio.Queue[EventEnvelope] = asyncio.Queue(maxsize=512)
        for event in self._history:
            if event.seq > after_seq and not queue.full():
                queue.put_nowait(event)
        self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[EventEnvelope]) -> None:
        self._subscribers.discard(queue)

    def forget_vm(self, vm_id: str) -> None:
        key = str(vm_id)
        self._latest_status.pop(key, None)
        self._latest_tags.pop(key, None)
        self._logs.pop(key, None)
