from __future__ import annotations

import json
import shutil
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable
from uuid import UUID

import pyarrow as pa

from bb_platform.contracts import TagSample

from .parquet_daily import append_daily_file, compact_legacy_partitions, recover_pending_files


class StorageUnavailable(RuntimeError):
    pass


class ParquetStore:
    """Buffered Parquet writer: one file per VM per UTC day.

    Flushes still happen every few seconds, but each flush appends a row group
    to that day's file instead of creating another small part.
    """

    def __init__(self, root: Path, *, flush_rows: int = 500, flush_seconds: float = 5.0, min_free_bytes: int = 64 * 1024 * 1024, quota_bytes: int | None = None) -> None:
        self.root = root
        self.flush_rows = flush_rows
        self.flush_seconds = flush_seconds
        self.min_free_bytes = max(0, min_free_bytes)
        self.quota_bytes = quota_bytes
        self._buffers: dict[tuple[str, str], list[TagSample]] = defaultdict(list)
        self._last_flush = datetime.now().timestamp()
        self._lock = threading.RLock()
        self._legacy_compacted = False
        recover_pending_files(root)

    @staticmethod
    def _arrow():
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
            return pa, pq
        except ImportError as exc:
            raise StorageUnavailable("pyarrow is required for Parquet telemetry storage") from exc

    def append(self, samples: Iterable[TagSample], *, flush_rows: int | None = None, flush_seconds: float | None = None) -> None:
        now = datetime.now().timestamp()
        with self._lock:
            for sample in samples:
                key = (str(sample.vm_id), sample.captured_at.date().isoformat())
                self._buffers[key].append(sample)
            row_limit = max(1, int(flush_rows or self.flush_rows))
            time_limit = max(0.1, float(flush_seconds or self.flush_seconds))
            if sum(len(x) for x in self._buffers.values()) >= row_limit or now - self._last_flush >= time_limit:
                self.flush()

    def flush(self) -> int:
        with self._lock:
            if not self._legacy_compacted:
                compact_legacy_partitions(self.root)
                self._legacy_compacted = True
            if not self._buffers:
                return 0
            self._arrow()
            total = 0
            self.root.mkdir(parents=True, exist_ok=True)
            usage = shutil.disk_usage(self.root)
            if usage.free < self.min_free_bytes:
                raise StorageUnavailable(f"free space below telemetry threshold: {usage.free} bytes")
            if self.quota_bytes is not None:
                used = sum(path.stat().st_size for path in self.root.rglob("*.parquet") if path.is_file())
                if used >= self.quota_bytes:
                    raise StorageUnavailable("telemetry quota exceeded")
            buffers, self._buffers = self._buffers, defaultdict(list)
            completed: set[tuple[str, str]] = set()
            try:
                for key, samples in buffers.items():
                    vm_id, date = key
                    if not samples:
                        completed.add(key)
                        continue
                    rows = [_row(sample) for sample in samples]
                    table = pa.Table.from_pylist(rows, schema=_DAILY_SCHEMA)
                    target = self.root / f"vm_id={vm_id}" / f"date={date}.parquet"
                    append_daily_file(target, table)
                    total += len(samples)
                    completed.add(key)
                self._last_flush = datetime.now().timestamp()
            except Exception:
                # Keep rows whose daily file was not updated. A finished append
                # is already in that day's file and must not be written again.
                for key, samples in buffers.items():
                    if key not in completed:
                        self._buffers[key][0:0] = samples
                raise
            return total

    def pending_samples(self) -> list[TagSample]:
        """Rows accepted by ingest but not yet flushed into the daily file."""
        with self._lock:
            return [sample for rows in self._buffers.values() for sample in rows]

    def discard_vm(self, vm_id: str) -> None:
        """Drop unflushed rows for a VM so delete does not rewrite its files."""
        key_id = str(vm_id)
        with self._lock:
            for key in [item for item in self._buffers if item[0] == key_id]:
                self._buffers.pop(key, None)


_DAILY_SCHEMA = pa.schema(
    [
        pa.field("vm_id", pa.string()),
        pa.field("seq", pa.int64()),
        pa.field("captured_at", pa.string()),
        pa.field("map_version", pa.string()),
        pa.field("protocol", pa.string()),
        pa.field("quality", pa.string()),
        pa.field("tags_json", pa.string()),
        pa.field("analog_json", pa.string()),
        pa.field("discrete_json", pa.string()),
        pa.field("alerts_json", pa.string()),
    ]
)


def _row(sample: TagSample) -> dict[str, object]:
    return {
        "vm_id": str(sample.vm_id),
        "seq": int(sample.seq),
        "captured_at": sample.captured_at.isoformat(),
        "map_version": sample.map_version or "",
        "protocol": str(getattr(sample.protocol, "value", sample.protocol) or ""),
        "quality": str(getattr(sample.quality, "value", sample.quality) or ""),
        "tags_json": json.dumps(sample.tags, ensure_ascii=False, default=str),
        "analog_json": json.dumps(sample.analog, ensure_ascii=False, default=str),
        "discrete_json": json.dumps(sample.discrete, ensure_ascii=False, default=str),
        "alerts_json": json.dumps(sample.alerts, ensure_ascii=False, default=str),
    }


def purge_vm_directories(vm_id: str, roots: Iterable[Path], *, extra_subdirs: Iterable[str] = ()) -> list[str]:
    """Delete only ``vm_id=<uuid>`` partitions under approved storage roots."""
    UUID(str(vm_id))
    partition = f"vm_id={vm_id}"
    subdirs = {"telemetry", "alarms", "backup", "logs", *(str(item) for item in extra_subdirs if item)}
    deleted: list[str] = []
    seen: set[str] = set()
    for raw_root in roots:
        try:
            base = Path(raw_root).resolve()
        except OSError:
            continue
        if not base.exists() or not base.is_dir():
            continue
        candidates = [base / partition]
        for sub in subdirs:
            text = str(sub).replace("\\", "/").strip().strip("/")
            parts = Path(text).parts
            if not text or ".." in parts or Path(text).is_absolute():
                continue
            candidates.append(base / text / partition)
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                resolved.relative_to(base)
            except (ValueError, OSError):
                continue
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            if resolved.is_dir() and resolved.name == partition:
                shutil.rmtree(resolved)
                deleted.append(key)
    return deleted
