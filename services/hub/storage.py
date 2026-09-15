from __future__ import annotations

import json
import os
import shutil
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

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

    def append(self, samples: Iterable[TagSample]) -> None:
        now = datetime.now().timestamp()
        with self._lock:
            for sample in samples:
                key = (str(sample.vm_id), sample.captured_at.date().isoformat())
                self._buffers[key].append(sample)
            if sum(len(x) for x in self._buffers.values()) >= self.flush_rows or now - self._last_flush >= self.flush_seconds:
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
            for (vm_id, date), samples in buffers.items():
                if not samples:
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
                final = directory / f"part-{samples[0].seq:020d}-{os.getpid()}.parquet"
                tmp = final.with_suffix(".tmp")
                pq.write_table(table, tmp, compression="zstd")
                tmp.replace(final)
                total += len(samples)
            self._last_flush = datetime.now().timestamp()
        return total
