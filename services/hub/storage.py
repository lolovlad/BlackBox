from __future__ import annotations

import json
import os
import shutil
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable
from uuid import UUID, uuid4

from bb_platform.contracts import TagSample


class StorageUnavailable(RuntimeError):
    pass


class ParquetStore:
    """Buffered, atomic Parquet writer partitioned by VM and UTC date."""

    def __init__(self, root: Path, *, flush_rows: int = 500, flush_seconds: float = 5.0, min_free_bytes: int = 64 * 1024 * 1024, quota_bytes: int | None = None) -> None:
        self.root = root
        self.flush_rows = flush_rows
        self.flush_seconds = flush_seconds
        self.min_free_bytes = max(0, min_free_bytes)
        self.quota_bytes = quota_bytes
        self._buffers: dict[tuple[str, str], list[TagSample]] = defaultdict(list)
        self._last_flush = datetime.now().timestamp()
        self._lock = threading.RLock()

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
        if not self._buffers:
            return 0
        pa, pq = self._arrow()
        total = 0
        self.root.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(self.root)
        if usage.free < self.min_free_bytes:
            raise StorageUnavailable(f"free space below telemetry threshold: {usage.free} bytes")
        if self.quota_bytes is not None:
            used = sum(path.stat().st_size for path in self.root.rglob("*.parquet") if path.is_file())
            if used >= self.quota_bytes:
                raise StorageUnavailable("telemetry quota exceeded")
        with self._lock:
            buffers, self._buffers = self._buffers, defaultdict(list)
            completed: set[tuple[str, str]] = set()
            try:
                for key, samples in buffers.items():
                    vm_id, date = key
                    if not samples:
                        completed.add(key)
                        continue
                    directory = self.root / f"vm_id={vm_id}" / f"date={date}"
                    directory.mkdir(parents=True, exist_ok=True)
                    rows = [
                        {
                            "vm_id": str(sample.vm_id),
                            "seq": sample.seq,
                            "captured_at": sample.captured_at.isoformat(),
                            "map_version": sample.map_version,
                            "protocol": getattr(sample.protocol, "value", sample.protocol),
                            "quality": getattr(sample.quality, "value", sample.quality),
                            "tags_json": json.dumps(sample.tags, ensure_ascii=False, default=str),
                        }
                        for sample in samples
                    ]
                    table = pa.Table.from_pylist(rows)
                    # A worker sequence restarts after a container restart. Add
                    # an opaque suffix so a new batch can never overwrite an
                    # older partition file with the same first sequence number.
                    final = directory / f"part-{samples[0].seq:020d}-{os.getpid()}-{uuid4().hex[:12]}.parquet"
                    tmp = final.with_suffix(".tmp")
                    try:
                        pq.write_table(table, tmp, compression="zstd")
                        tmp.replace(final)
                    finally:
                        # A failed write must not leave a misleading .tmp file
                        # that is mistaken for a committed partition.
                        if tmp.exists():
                            try:
                                tmp.unlink()
                            except OSError:
                                pass
                    total += len(samples)
                    completed.add(key)
                self._last_flush = datetime.now().timestamp()
            except Exception:
                # Keep only partitions that were not committed. This allows a
                # later flush after an SSD is remounted/space is freed without
                # duplicating files that were already atomically renamed.
                for key, samples in buffers.items():
                    if key not in completed:
                        self._buffers[key][0:0] = samples
                raise
        return total

    def discard_vm(self, vm_id: str) -> None:
        """Drop unflushed rows for a VM so delete does not rewrite its files."""
        key_id = str(vm_id)
        with self._lock:
            for key in [item for item in self._buffers if item[0] == key_id]:
                self._buffers.pop(key, None)


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
