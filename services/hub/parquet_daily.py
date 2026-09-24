"""One Parquet file per VM per day, grown by appending a row group.

PyArrow has no append API. A new row group is written on its own, its footer
offsets are shifted, and the daily file's footer is rebuilt. Existing column
pages stay where they are, so a flush does not rewrite the whole day.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def _unavailable(message: str) -> None:
    from .storage import StorageUnavailable

    raise StorageUnavailable(message)

_STOP = 0
_BOOL_TRUE = 1
_BOOL_FALSE = 2
_BYTE = 3
_I16 = 4
_I32 = 5
_I64 = 6
_DOUBLE = 7
_BINARY = 8
_LIST = 9
_SET = 10
_MAP = 11
_STRUCT = 12

# ColumnMetaData page locations, plus optional column-index locations on ColumnChunk.
_OFFSET_FIELDS = {
    ("column_meta", 9),
    ("column_meta", 10),
    ("column_meta", 11),
    ("column_meta", 14),
    ("column_chunk", 4),
    ("column_chunk", 6),
}


def append_daily_file(path: Path, table) -> None:
    """Add ``table`` as a new row group of the daily file at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _rollback_pending(path)
    if not path.exists() or path.stat().st_size == 0:
        _write_fresh(path, table)
        return
    payload, footer, data_end = _spliced_tail(path, table)
    old_size = path.stat().st_size
    pending = _pending_path(path)
    with pending.open("wb") as handle:
        handle.write(old_size.to_bytes(8, "little"))
        handle.flush()
        os.fsync(handle.fileno())
    try:
        with path.open("r+b") as handle:
            handle.seek(data_end)
            handle.write(payload)
            handle.write(footer)
            handle.write(len(footer).to_bytes(4, "little"))
            handle.write(b"PAR1")
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
        pending.unlink(missing_ok=True)
    except Exception:
        _rollback_pending(path)
        raise


def recover_pending_files(root: Path) -> None:
    """Drop a daily-file append that did not finish before the process stopped."""
    if not root.is_dir():
        return
    for pending in root.rglob("*.parquet.pending"):
        target = Path(str(pending).removesuffix(".pending"))
        _rollback_pending(target)


def compact_legacy_partitions(root: Path) -> None:
    """Fold ``date=<day>/part-*.parquet`` directories into one ``date=<day>.parquet``."""
    if not root.is_dir():
        return
    for vm_dir in root.glob("vm_id=*"):
        if not vm_dir.is_dir():
            continue
        for day_dir in list(vm_dir.glob("date=*")):
            if not day_dir.is_dir():
                continue
            try:
                _compact_day(day_dir)
            except Exception as exc:
                logger.warning("skipped compacting %s: %s", day_dir, exc)


def _write_fresh(path: Path, table) -> None:
    import pyarrow.parquet as pq

    temporary = Path(str(path) + ".tmp")
    try:
        pq.write_table(table, temporary, compression="zstd")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _spliced_tail(path: Path, table) -> tuple[bytes, bytes, int]:
    import pyarrow as pa
    import pyarrow.parquet as pq
    import pyarrow._parquet as _parquet

    footer, _footer_len, data_end = _read_footer(path)
    sink = io.BytesIO()
    pq.write_table(table, sink, compression="zstd")
    fresh = sink.getvalue()
    fresh_footer, _fresh_len, fresh_end = _split_bytes(fresh)
    # The new file's page offsets count from its own leading PAR1. Once those
    # pages sit just after the existing pages, every offset grows by this delta.
    shifted = _shift_footer_offsets(fresh_footer, data_end - 4)
    merged = _parquet._reconstruct_filemetadata(pa.py_buffer(footer))
    added = _parquet._reconstruct_filemetadata(pa.py_buffer(shifted))
    merged.append_row_groups(added)
    merged_footer = _metadata_footer(merged)
    return fresh[4:fresh_end], merged_footer, data_end


def _metadata_footer(metadata) -> bytes:
    sink = io.BytesIO()
    metadata.write_metadata_file(sink)
    payload = sink.getvalue()
    footer, _length, _data_end = _split_bytes(payload)
    return footer


def _read_footer(path: Path) -> tuple[bytes, int, int]:
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size < 12:
            _unavailable(f"parquet file is truncated: {path}")
        handle.seek(0)
        magic = handle.read(4)
        handle.seek(size - 8)
        tail = handle.read(8)
        if magic != b"PAR1" or tail[4:] != b"PAR1":
            _unavailable(f"parquet file is missing a footer: {path}")
        footer_len = int.from_bytes(tail[:4], "little")
        data_end = size - footer_len - 8
        if footer_len < 0 or data_end < 4:
            _unavailable(f"parquet footer is truncated: {path}")
        handle.seek(data_end)
        footer = handle.read(footer_len)
    if len(footer) != footer_len:
        _unavailable(f"parquet footer is truncated: {path}")
    return footer, footer_len, data_end


def _split_bytes(data: bytes) -> tuple[bytes, int, int]:
    if len(data) < 12 or data[:4] != b"PAR1" or data[-4:] != b"PAR1":
        _unavailable("parquet file is missing a footer")
    footer_len = int.from_bytes(data[-8:-4], "little")
    data_end = len(data) - footer_len - 8
    if footer_len < 0 or data_end < 4:
        _unavailable("parquet footer is truncated")
    return data[data_end : data_end + footer_len], footer_len, data_end


def _pending_path(path: Path) -> Path:
    return Path(str(path) + ".pending")


def _rollback_pending(path: Path) -> None:
    pending = _pending_path(path)
    if not pending.exists():
        return
    raw = pending.read_bytes()
    if len(raw) < 8:
        pending.unlink(missing_ok=True)
        return
    old_size = int.from_bytes(raw[:8], "little")
    if path.exists():
        with path.open("r+b") as handle:
            handle.truncate(old_size)
            handle.flush()
            os.fsync(handle.fileno())
    pending.unlink(missing_ok=True)


def _compact_day(day_dir: Path) -> None:
    import pyarrow.parquet as pq

    parts = [path for path in sorted(day_dir.glob("*.parquet")) if path.is_file() and _readable(path)]
    if not parts:
        return
    destination = day_dir.parent / f"{day_dir.name}.parquet"
    building = Path(str(destination) + ".building")
    if building.exists():
        building.unlink()
    sources: list[Path] = []
    if destination.is_file() and _readable(destination):
        sources.append(destination)
    sources.extend(parts)
    # Read before opening the writer. On Windows a ParquetFile keeps the part
    # locked, and the part directory cannot be removed until those handles close.
    tables = [pq.read_table(source) for source in sources]
    writer = None
    try:
        for table in tables:
            if writer is None:
                writer = pq.ParquetWriter(building, table.schema, compression="zstd")
            elif not table.schema.equals(writer.schema, check_metadata=False):
                table = table.cast(writer.schema)
            writer.write_table(table)
        if writer is None:
            return
        writer.close()
        writer = None
        building.replace(destination)
        shutil.rmtree(day_dir)
    finally:
        if writer is not None:
            writer.close()
        if building.exists():
            building.unlink(missing_ok=True)


def _readable(path: Path) -> bool:
    import pyarrow.parquet as pq

    try:
        pq.read_metadata(path)
    except Exception:
        return False
    return True


def _shift_footer_offsets(footer: bytes, delta: int) -> bytes:
    if delta == 0:
        return footer
    out = bytearray()
    cursor = _rewrite_struct(footer, 0, out, "file", delta)
    if cursor != len(footer):
        _unavailable("parquet footer could not be rewritten")
    return bytes(out)


def _child_kind(parent: str, field_id: int) -> str:
    if parent == "file" and field_id == 4:
        return "row_group"
    if parent == "row_group" and field_id == 1:
        return "column_chunk"
    if parent == "column_chunk" and field_id == 3:
        return "column_meta"
    return "other"


def _rewrite_struct(buf: bytes, index: int, out: bytearray, kind: str, delta: int) -> int:
    previous = 0
    while True:
        if index >= len(buf):
            _unavailable("parquet footer ended inside a struct")
        type_byte = buf[index]
        if type_byte == _STOP:
            out.append(_STOP)
            return index + 1
        index += 1
        field_type = type_byte & 0x0F
        field_delta = type_byte >> 4
        if field_delta == 0:
            field_id, index = _read_zigzag(buf, index)
        else:
            field_id = previous + field_delta
        _write_header(out, previous, field_id, field_type)
        previous = field_id
        if field_type in (_BOOL_TRUE, _BOOL_FALSE):
            continue
        if field_type == _I64 and (kind, field_id) in _OFFSET_FIELDS:
            value, index = _read_zigzag(buf, index)
            _write_zigzag(out, value + delta)
            continue
        index = _rewrite_value(buf, index, out, field_type, _child_kind(kind, field_id), delta)


def _rewrite_value(buf: bytes, index: int, out: bytearray, field_type: int, kind: str, delta: int) -> int:
    if field_type == _BYTE:
        out.append(buf[index])
        return index + 1
    if field_type in (_I16, _I32, _I64):
        value, index = _read_zigzag(buf, index)
        _write_zigzag(out, value)
        return index
    if field_type == _DOUBLE:
        out.extend(buf[index : index + 8])
        return index + 8
    if field_type == _BINARY:
        length, index = _read_varint(buf, index)
        if index + length > len(buf):
            _unavailable("parquet footer has a bad binary field")
        _write_varint(out, length)
        out.extend(buf[index : index + length])
        return index + length
    if field_type == _STRUCT:
        return _rewrite_struct(buf, index, out, kind, delta)
    if field_type in (_LIST, _SET):
        return _rewrite_list(buf, index, out, kind, delta)
    if field_type == _MAP:
        return _rewrite_map(buf, index, out, delta)
    _unavailable(f"unsupported parquet footer type {field_type}")


def _rewrite_list(buf: bytes, index: int, out: bytearray, kind: str, delta: int) -> int:
    header = buf[index]
    index += 1
    element_type = header & 0x0F
    size = header >> 4
    if size == 15:
        size, index = _read_varint(buf, index)
    if size < 15:
        out.append((size << 4) | element_type)
    else:
        out.append(0xF0 | element_type)
        _write_varint(out, size)
    if element_type in (_BOOL_TRUE, _BOOL_FALSE):
        out.extend(buf[index : index + size])
        return index + size
    for _ in range(size):
        index = _rewrite_value(buf, index, out, element_type, kind, delta)
    return index


def _rewrite_map(buf: bytes, index: int, out: bytearray, delta: int) -> int:
    size, index = _read_varint(buf, index)
    if size == 0:
        out.append(0)
        return index
    types = buf[index]
    index += 1
    _write_varint(out, size)
    out.append(types)
    key_type = types >> 4
    value_type = types & 0x0F
    for _ in range(size):
        index = _rewrite_value(buf, index, out, key_type, "other", delta)
        index = _rewrite_value(buf, index, out, value_type, "other", delta)
    return index


def _write_header(out: bytearray, previous: int, field_id: int, field_type: int) -> None:
    gap = field_id - previous
    if 1 <= gap <= 15:
        out.append((gap << 4) | field_type)
        return
    out.append(field_type)
    _write_zigzag(out, field_id)


def _read_varint(buf: bytes, index: int) -> tuple[int, int]:
    shift = 0
    value = 0
    while True:
        if index >= len(buf) or shift > 63:
            _unavailable("parquet footer varint is truncated")
        byte = buf[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, index
        shift += 7


def _read_zigzag(buf: bytes, index: int) -> tuple[int, int]:
    value, index = _read_varint(buf, index)
    return (value >> 1) ^ -(value & 1), index


def _write_varint(out: bytearray, value: int) -> None:
    if value < 0:
        _unavailable("parquet footer varint is negative")
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return


def _write_zigzag(out: bytearray, value: int) -> None:
    encoded = value << 1 if value >= 0 else ((-value - 1) << 1) | 1
    _write_varint(out, encoded)
