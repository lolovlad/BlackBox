"""Wire contracts shared by Hub and worker containers."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)
    schema_version: int = Field(default=1, ge=1)


class Quality(StrEnum):
    GOOD = "good"
    DEGRADED = "degraded"
    BAD = "bad"


class VmProtocol(StrEnum):
    MODBUS_RTU = "modbus_rtu"
    MODBUS_TCP = "modbus_tcp"
    CAN = "can"
    GPIO = "gpio"
    SIMULATOR = "simulator"


class VmLifecycle(StrEnum):
    PENDING = "pending"
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    UNKNOWN = "unknown"


TagValue = Any


class TagSample(Contract):
    vm_id: UUID
    seq: int = Field(ge=0)
    captured_at: datetime
    map_version: str = Field(min_length=1, max_length=128)
    tags: dict[str, TagValue] = Field(default_factory=dict)
    quality: Quality = Quality.GOOD
    protocol: VmProtocol
    source: str | None = Field(default=None, max_length=128)

    @field_validator("captured_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)


class RawSample(Contract):
    seq: int = Field(ge=0)
    captured_at: datetime
    sources: dict[str, list[Any]] = Field(default_factory=dict)
    quality: Quality = Quality.GOOD

    @field_validator("captured_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)


class RawBatch(Contract):
    batch_id: UUID = Field(default_factory=uuid4)
    vm_id: UUID
    protocol: VmProtocol
    map_version: str = Field(min_length=1, max_length=128)
    seq_start: int = Field(ge=0)
    samples: list[RawSample] = Field(min_length=1, max_length=10_000)


class VmCommand(Contract):
    command_id: UUID = Field(default_factory=uuid4)
    vm_id: UUID
    action: Literal["start", "stop", "restart", "apply_map"]
    config_revision: int = Field(default=0, ge=0)
    map_version: str | None = Field(default=None, max_length=128)


class VmStatus(Contract):
    vm_id: UUID
    lifecycle: VmLifecycle
    health: Literal["healthy", "unhealthy", "unknown"] = "unknown"
    container_id: str | None = None
    heartbeat_at: datetime | None = None
    map_version: str | None = None
    config_revision: int = 0
    last_error: str | None = None
    updated_at: datetime

    @field_validator("heartbeat_at", "updated_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)


class AlarmEvent(Contract):
    event_id: UUID = Field(default_factory=uuid4)
    vm_id: UUID
    timestamp: datetime
    severity: Literal["info", "warning", "critical"]
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=1024)
    active: bool
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)


class WorkerRegister(Contract):
    vm_id: UUID
    worker_id: str = Field(min_length=1, max_length=128)
    protocol: VmProtocol
    capabilities: list[str] = Field(default_factory=list)
    config_revision: int = Field(default=0, ge=0)


class WorkerHeartbeat(Contract):
    vm_id: UUID
    worker_id: str = Field(min_length=1, max_length=128)
    timestamp: datetime
    seq: int = Field(ge=0)
    health: Literal["healthy", "unhealthy"] = "healthy"

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)


class WorkerCommandAck(Contract):
    command_id: UUID
    vm_id: UUID
    accepted: bool
    message: str | None = None


class WorkerError(Contract):
    vm_id: UUID
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=1024)
    timestamp: datetime
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)


class MapDocument(Contract):
    map_id: UUID = Field(default_factory=uuid4)
    version: str = Field(min_length=1, max_length=128)
    protocol: VmProtocol
    preset_id: str | None = Field(default=None, max_length=128)
    checksum: str = Field(min_length=64, max_length=64)
    requests: list[dict[str, Any]] = Field(default_factory=list)
    fields: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    immutable: Literal[True] = True

    @model_validator(mode="after")
    def checksum_matches_document(self) -> "MapDocument":
        canonical = {
            "protocol": getattr(self.protocol, "value", self.protocol),
            "preset_id": self.preset_id,
            "version": self.version,
            "requests": self.requests,
            "fields": self.fields,
        }
        encoded = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        expected = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        if self.checksum != expected:
            raise ValueError("map checksum does not match document")
        return self


class ResourceKind(StrEnum):
    SERIAL = "serial"
    CAN = "can"
    GPIO = "gpio"
    TCP = "tcp"
    STORAGE = "storage"


class ResourceDescriptor(Contract):
    resource_id: str = Field(min_length=1, max_length=255)
    kind: ResourceKind
    name: str = Field(min_length=1, max_length=255)
    path: str | None = None
    address: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    available: bool = True


class EventEnvelope(Contract):
    event_id: UUID = Field(default_factory=uuid4)
    seq: int = Field(ge=0)
    topic: Literal["vm_status", "tags", "logs", "alarms"]
    payload: dict[str, Any]


__all__ = [
    "AlarmEvent",
    "Contract",
    "EventEnvelope",
    "MapDocument",
    "Quality",
    "RawBatch",
    "RawSample",
    "ResourceDescriptor",
    "ResourceKind",
    "TagSample",
    "VmCommand",
    "VmLifecycle",
    "VmProtocol",
    "VmStatus",
    "WorkerCommandAck",
    "WorkerError",
    "WorkerHeartbeat",
    "WorkerRegister",
]
