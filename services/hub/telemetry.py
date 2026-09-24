"""Query Parquet telemetry with DuckDB and shape it for tables and charts."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable

from bb_platform.parser import field_channel

from .storage import ParquetStore, StorageUnavailable

PAGE_SIZE = 100
CHART_POINT_CAP = 3000
READ_ROW_CAP = 100_000
_COLUMNS = "vm_id, seq, captured_at, COALESCE(quality, '') AS quality, COALESCE(protocol, '') AS protocol, COALESCE(analog_json, '{}') AS analog_json, COALESCE(discrete_json, '{}') AS discrete_json"


class Measurement:
    __slots__ = ("vm_id", "captured_at", "seq", "analog", "discrete", "quality", "protocol")

    def __init__(self, vm_id: str, captured_at: datetime, seq: int, analog: dict[str, Any], discrete: dict[str, Any], quality: str, protocol: str) -> None:
        self.vm_id = vm_id
        self.captured_at = captured_at
        self.seq = seq
        self.analog = analog
        self.discrete = discrete
        self.quality = quality
        self.protocol = protocol

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.vm_id, self.seq, self.captured_at.isoformat())


def collect_roots(data_root: Path, resources: list[dict[str, Any]], vms: list[dict[str, Any]], store_roots: Iterable[Path]) -> list[Path]:
    found: list[Path] = [Path(data_root) / "telemetry", *store_roots]
    by_id = {str(item.get("resource_id")): item for item in resources if item.get("resource_id")}
    for resource in resources:
        if resource.get("kind") != "storage" or not resource.get("path"):
            continue
        found.append(Path(str(resource["path"])) / "telemetry")
    for vm in vms:
        storage = (vm.get("config") or {}).get("storage") or {}
        subdir = str(storage.get("telemetry_subdir") or "telemetry").strip().strip("/\\") or "telemetry"
        if ".." in Path(subdir).parts:
            continue
        target = str(storage.get("target_resource_id") or vm.get("storage_resource_id") or "storage:data")
        descriptor = by_id.get(target)
        if descriptor and descriptor.get("path"):
            found.append(Path(str(descriptor["path"])) / subdir)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in found:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def parse_bound(raw: str | None, *, end_of_day: bool = False) -> datetime | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    if end_of_day and parsed.timetz().replace(tzinfo=None) == time(0, 0, 0):
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed.astimezone(timezone.utc)


def format_timestamp(value: datetime) -> str:
    moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return moment.astimezone().strftime("%d.%m.%Y %H:%M:%S")


def format_cell(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


def query_measurements(
    roots: Iterable[Path],
    stores: Iterable[ParquetStore],
    *,
    vm_ids: set[str] | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    include_bad: bool = False,
    point_cap: int | None = None,
) -> tuple[list[Measurement], bool]:
    """Return matching samples. ``point_cap`` keeps an even time sample for charts."""
    if point_cap is not None and point_cap > 0:
        return _query_chart(
            roots,
            stores,
            vm_ids=vm_ids,
            date_from=date_from,
            date_to=date_to,
            include_bad=include_bad,
            point_cap=point_cap,
        )
    con, sql, bind = _open_scan(roots, stores, vm_ids=vm_ids, date_from=date_from, date_to=date_to, include_bad=include_bad)
    if sql is None:
        con.close()
        return [], False
    try:
        fetched = con.execute(
            f"{sql} ORDER BY captured_at DESC, seq DESC LIMIT ?",
            [*bind, READ_ROW_CAP + 1],
        ).fetchall()
    finally:
        con.close()
    truncated = len(fetched) > READ_ROW_CAP
    return _measurements(fetched[:READ_ROW_CAP]), truncated


def query_window(
    roots: Iterable[Path],
    stores: Iterable[ParquetStore],
    *,
    vm_ids: set[str] | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    include_bad: bool = False,
    page: int = 1,
    page_size: int = PAGE_SIZE,
    sort_desc: bool = True,
) -> tuple[list[Measurement], int, int]:
    """One table page. DuckDB applies the filter, order and limit."""
    size = max(1, page_size)
    scan_args = (roots, stores)
    scan_kwargs = dict(vm_ids=vm_ids, date_from=date_from, date_to=date_to, include_bad=include_bad)
    # Count and the page use separate connections. On one connection the count
    # warms DuckDB file metadata in a way that makes the following LIMIT slower
    # on every repeat (about 2s, then 4s, then 6s on a day of small parts).
    con, sql, bind = _open_scan(*scan_args, **scan_kwargs)
    if sql is None:
        con.close()
        return [], 0, 1
    try:
        total = int(con.execute(f"SELECT count(*) FROM ({_count_sql(sql)}) q", bind).fetchone()[0])
    finally:
        con.close()
    if total == 0:
        return [], 0, 1
    pages = max(1, (total + size - 1) // size)
    current = min(max(1, page), pages)
    order = "DESC" if sort_desc else "ASC"
    con, sql, bind = _open_scan(*scan_args, **scan_kwargs)
    try:
        fetched = con.execute(
            f"{sql} ORDER BY captured_at {order}, seq {order} LIMIT ? OFFSET ?",
            [*bind, size, (current - 1) * size],
        ).fetchall()
    finally:
        con.close()
    return _measurements(fetched), total, current


def page_table(
    rows: list[Measurement],
    *,
    tab: str,
    columns: list[dict[str, str]],
    vm_names: dict[str, str],
    sort_desc: bool,
    page: int,
    page_size: int = PAGE_SIZE,
    total_override: int | None = None,
    already_paged: bool = False,
) -> dict[str, Any]:
    size = max(1, page_size)
    if already_paged:
        window = list(rows)
        total = total_override if total_override is not None else len(window)
        total_pages = max(1, (total + size - 1) // size) if total else 1
        current = min(max(1, page), total_pages)
    else:
        ordered = sorted(rows, key=lambda item: (item.captured_at, item.seq), reverse=sort_desc)
        total = len(ordered)
        total_pages = max(1, (total + size - 1) // size) if total else 1
        current = min(max(1, page), total_pages)
        window = ordered[(current - 1) * size : current * size]
    channel = "discrete" if tab == "discrete" else "analog"
    out_rows = []
    for item in window:
        source = item.discrete if channel == "discrete" else item.analog
        out_rows.append(
            {
                "time": format_timestamp(item.captured_at),
                "ts": item.captured_at.isoformat(),
                "vm_id": item.vm_id,
                "vm_name": vm_names.get(item.vm_id, item.vm_id),
                "cells": [format_cell(source.get(column["key"])) for column in columns],
            }
        )
    return {
        "tab": tab,
        "columns": columns,
        "rows": out_rows,
        "page": current,
        "total_pages": total_pages,
        "total_rows": total,
        "page_size": size,
    }


def chart_payload(
    rows: list[Measurement],
    *,
    table: str,
    vm_ids: list[str],
    vm_names: dict[str, str],
    labels: dict[str, str],
    fields: list[str],
    realtime: bool,
) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda item: (item.captured_at, item.seq))
    if len(ordered) > CHART_POINT_CAP:
        step = len(ordered) / CHART_POINT_CAP
        picked = [ordered[min(len(ordered) - 1, int(index * step))] for index in range(CHART_POINT_CAP)]
        if picked[-1] is not ordered[-1]:
            picked[-1] = ordered[-1]
        ordered = picked
    series_fields = [field for field in fields if field]
    columns: list[str] = []
    column_labels: dict[str, str] = {}
    for vm_id in vm_ids:
        for field in series_fields:
            key = f"{vm_id}|{field}"
            columns.append(key)
            label = labels.get(field, field)
            column_labels[key] = f"{vm_names.get(vm_id, vm_id)} · {label}"
    points = []
    for item in ordered:
        source = item.discrete if table == "discrete" else item.analog
        values = {f"{item.vm_id}|{field}": source.get(field) for field in series_fields}
        points.append({"ts_ms": int(item.captured_at.timestamp() * 1000), "values": values})
    last_ts = ordered[-1].captured_at.isoformat() if ordered else None
    return {
        "table": table,
        "columns": columns,
        "column_labels": column_labels,
        "points": points,
        "row_count": len(points),
        "realtime": realtime,
        "last_ts": last_ts,
    }


def rows_as_csv(payload: dict[str, Any]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    headers = ["Время", "Источник", *[column["label"] for column in payload.get("columns", [])]]
    if payload.get("tab") in {"alarms", "gpio"}:
        headers = ["Время", "Источник", "Название", "Состояние"]
    writer.writerow(headers)
    for row in payload.get("rows", []):
        if payload.get("tab") in {"alarms", "gpio"}:
            writer.writerow([row.get("time", ""), row.get("vm_name", ""), row.get("name", ""), row.get("state_label", "")])
        else:
            writer.writerow([row.get("time", ""), row.get("vm_name", ""), *row.get("cells", [])])
    return buffer.getvalue()


def describe_sources(vms: list[dict[str, Any]], documents: dict[tuple[str, str], dict[str, Any]], live: dict[str, Any], roots: Iterable[Path]) -> list[dict[str, Any]]:
    described = []
    for vm in vms:
        vm_id = str(vm.get("id"))
        document = documents.get((str(vm.get("map_version") or ""), str(vm.get("protocol") or "")))
        analog, discrete = _fields_from_document(document)
        sample = live.get(vm_id)
        if sample is not None:
            _merge_keys(analog, getattr(sample, "analog", None) or {})
            _merge_keys(discrete, getattr(sample, "discrete", None) or {})
        disk_analog, disk_discrete = _keys_from_newest_file(roots, vm_id)
        _merge_keys(analog, {key: None for key in disk_analog})
        _merge_keys(discrete, {key: None for key in disk_discrete})
        described.append(
            {
                "id": vm_id,
                "name": str(vm.get("name") or vm_id),
                "protocol": str(vm.get("protocol") or ""),
                "analog": analog,
                "discrete": discrete,
            }
        )
    return described


def column_defs(sources: list[dict[str, Any]], tab: str, selected: list[str] | None) -> list[dict[str, str]]:
    channel = "discrete" if tab == "discrete" else "analog"
    ordered: list[dict[str, str]] = []
    seen: set[str] = set()
    for source in sources:
        for field in source.get(channel, []):
            key = str(field.get("key") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            ordered.append({"key": key, "label": str(field.get("label") or key)})
    if selected:
        wanted = set(selected)
        return [item for item in ordered if item["key"] in wanted] or [{"key": key, "label": key} for key in selected]
    return ordered


def _fields_from_document(document: dict[str, Any] | None) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    analog: list[dict[str, str]] = []
    discrete: list[dict[str, str]] = []
    if not document:
        return analog, discrete
    for field in document.get("fields") or []:
        if not isinstance(field, dict):
            continue
        name = str(field.get("name") or "").strip()
        if not name or field.get("system") or field.get("is_system") or field.get("internal"):
            continue
        kind = field_channel(field)
        label = str(field.get("label") or field.get("title") or field.get("description") or name)
        item = {"key": name, "label": label}
        if kind == "analog":
            analog.append(item)
        elif kind == "discrete":
            discrete.append(item)
    return analog, discrete


def _merge_keys(target: list[dict[str, str]], values: dict[str, Any]) -> None:
    seen = {item["key"] for item in target}
    for key in values:
        text = str(key).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        target.append({"key": text, "label": text})


def _keys_from_newest_file(roots: Iterable[Path], vm_id: str) -> tuple[set[str], set[str]]:
    entries = _day_entries(roots, {vm_id}, None, None)
    if not entries:
        return set(), set()
    newest = max(entries, key=lambda path: path.name)
    if newest.is_dir():
        candidates = [path for path in newest.glob("*.parquet") if path.is_file() and not path.name.endswith(".tmp")]
        if not candidates:
            return set(), set()
        newest = max(candidates, key=lambda path: path.name)
    analog: set[str] = set()
    discrete: set[str] = set()
    try:
        con = _duckdb()
        try:
            fetched = con.execute(
                "SELECT json_keys(analog_json), json_keys(discrete_json) FROM read_parquet(?)",
                [newest.resolve().as_posix()],
            ).fetchall()
        finally:
            con.close()
    except (OSError, StorageUnavailable, ValueError):
        return set(), set()
    for analog_keys, discrete_keys in fetched:
        analog.update(str(key) for key in (analog_keys or []) if str(key).strip())
        discrete.update(str(key) for key in (discrete_keys or []) if str(key).strip())
    return analog, discrete


def _day_entries(roots: Iterable[Path], vm_ids: set[str] | None, date_from: datetime | None, date_to: datetime | None) -> list[Path]:
    """Daily files, or legacy part directories when that day has no daily file yet."""
    start_day = date_from.astimezone(timezone.utc).date() if date_from is not None else None
    end_day = date_to.astimezone(timezone.utc).date() if date_to is not None else None
    found: list[Path] = []
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        directories = [root / f"vm_id={vm_id}" for vm_id in vm_ids] if vm_ids else [path for path in root.glob("vm_id=*") if path.is_dir()]
        for directory in directories:
            if not directory.is_dir():
                continue
            for candidate in directory.glob("date=*"):
                day_text = _day_token(candidate)
                if day_text is None:
                    continue
                try:
                    day = datetime.fromisoformat(day_text).date()
                except ValueError:
                    continue
                if start_day is not None and day < start_day:
                    continue
                if end_day is not None and day > end_day:
                    continue
                if candidate.is_dir() and (directory / f"date={day_text}.parquet").is_file():
                    continue
                found.append(candidate)
    return found


def _day_token(path: Path) -> str | None:
    name = path.name
    if path.is_file() and name.endswith(".parquet"):
        token = name[: -len(".parquet")]
    elif path.is_dir():
        token = name
    else:
        return None
    if not token.startswith("date="):
        return None
    return token.removeprefix("date=")


def _partition_globs(roots: Iterable[Path], vm_ids: set[str] | None, date_from: datetime | None, date_to: datetime | None) -> list[str]:
    globs: list[str] = []
    for entry in _day_entries(roots, vm_ids, date_from, date_to):
        if entry.is_file():
            globs.append(entry.resolve().as_posix())
        elif next(entry.glob("*.parquet"), None) is not None:
            globs.append((entry / "*.parquet").resolve().as_posix())
    return globs


def _parquet_files(roots: Iterable[Path], vm_ids: set[str] | None, date_from: datetime | None, date_to: datetime | None) -> list[Path]:
    files: list[Path] = []
    for entry in _day_entries(roots, vm_ids, date_from, date_to):
        if entry.is_file():
            files.append(entry)
        else:
            files.extend(path for path in entry.glob("*.parquet") if path.is_file() and not path.name.endswith(".tmp"))
    return files


def _duckdb():
    try:
        import duckdb
    except ImportError as exc:
        raise StorageUnavailable("duckdb is required to query Parquet telemetry") from exc
    return duckdb.connect(database=":memory:")


def _filter_clause(
    *,
    vm_ids: set[str] | None,
    date_from: datetime | None,
    date_to: datetime | None,
    include_bad: bool,
) -> tuple[str, list[Any]]:
    where = ["TRUE"]
    params: list[Any] = []
    if not include_bad:
        where.append("quality <> 'bad'")
    if vm_ids is not None:
        if not vm_ids:
            where.append("FALSE")
        else:
            where.append(f"vm_id IN ({','.join('?' for _ in vm_ids)})")
            params.extend(sorted(vm_ids))
    if date_from is not None:
        where.append("captured_at >= ?")
        params.append(_iso_utc(date_from))
    if date_to is not None:
        where.append("captured_at <= ?")
        params.append(_iso_utc(date_to))
    where.append("captured_at IS NOT NULL")
    where.append("captured_at <> ''")
    return " AND ".join(where), params


def _union_sql(globs: list[str], has_pending: bool, columns: str, clause: str, params: list[Any]) -> tuple[str, list[Any]]:
    # Each glob is a VARCHAR. A VARCHAR[] of the same patterns, and
    # union_by_name, both make DuckDB reopen every part and a one-day page
    # grows from about 2 seconds to about 8. Parts share one written schema.
    branches: list[str] = []
    bind: list[Any] = []
    for pattern in globs:
        branches.append(f"SELECT {columns} FROM read_parquet(?) WHERE {clause}")
        bind.append(pattern)
        bind.extend(params)
    if has_pending:
        branches.append(f"SELECT {columns} FROM pending_samples WHERE {clause}")
        bind.extend(params)
    return " UNION ALL ".join(branches), bind


def _count_sql(sql: str) -> str:
    """Count through the same filters without projecting JSON columns."""
    return sql.replace(_COLUMNS, "1 AS present")


def _open_scan(
    roots: Iterable[Path],
    stores: Iterable[ParquetStore],
    *,
    vm_ids: set[str] | None,
    date_from: datetime | None,
    date_to: datetime | None,
    include_bad: bool,
):
    con = _duckdb()
    globs = _partition_globs(roots, vm_ids, date_from, date_to)
    has_pending = _register_pending(con, stores)
    if not globs and not has_pending:
        return con, None, []
    clause, params = _filter_clause(vm_ids=vm_ids, date_from=date_from, date_to=date_to, include_bad=include_bad)
    sql, bind = _union_sql(globs, has_pending, _COLUMNS, clause, params)
    return con, sql, bind


def _query_chart(
    roots: Iterable[Path],
    stores: Iterable[ParquetStore],
    *,
    vm_ids: set[str] | None,
    date_from: datetime | None,
    date_to: datetime | None,
    include_bad: bool,
    point_cap: int,
) -> tuple[list[Measurement], bool]:
    """Even sample across part files. A row_number over a full day reads every 5-second part."""
    files = _parquet_files(roots, vm_ids, date_from, date_to)
    files.sort(key=lambda path: (path.parent.name, path.name))
    sampled = len(files) > point_cap
    if sampled:
        step = len(files) / point_cap
        files = [files[min(len(files) - 1, int(index * step))] for index in range(point_cap)]
    con = _duckdb()
    has_pending = _register_pending(con, stores)
    if not files and not has_pending:
        con.close()
        return [], False
    clause, params = _filter_clause(vm_ids=vm_ids, date_from=date_from, date_to=date_to, include_bad=include_bad)
    try:
        fetched: list[tuple[Any, ...]] = []
        if files and sampled:
            fetched = con.execute(
                f"""
                SELECT {_COLUMNS}
                FROM read_parquet(?, filename=true)
                WHERE {clause}
                QUALIFY row_number() OVER (PARTITION BY filename ORDER BY captured_at, seq) = 1
                ORDER BY captured_at, seq
                """,
                [[path.resolve().as_posix() for path in files], *params],
            ).fetchall()
        elif files:
            paths = [path.resolve().as_posix() for path in files]
            total = int(con.execute(f"SELECT count(*) FROM read_parquet(?) WHERE {clause}", [paths, *params]).fetchone()[0])
            if total > point_cap:
                stride = max(1, total // point_cap)
                fetched = con.execute(
                    f"""
                    SELECT vm_id, seq, captured_at, quality, protocol, analog_json, discrete_json
                    FROM (
                        SELECT {_COLUMNS}, row_number() OVER (ORDER BY captured_at, seq) AS n
                        FROM read_parquet(?)
                        WHERE {clause}
                    ) numbered
                    WHERE n = 1 OR n = ? OR ((n - 1) % ? = 0)
                    ORDER BY captured_at, seq
                    """,
                    [paths, *params, total, stride],
                ).fetchall()
                sampled = True
            else:
                fetched = con.execute(
                    f"""
                    SELECT {_COLUMNS}
                    FROM read_parquet(?)
                    WHERE {clause}
                    ORDER BY captured_at, seq
                    """,
                    [paths, *params],
                ).fetchall()
        pending: list[tuple[Any, ...]] = []
        if has_pending:
            pending = con.execute(
                f"SELECT {_COLUMNS} FROM pending_samples WHERE {clause} ORDER BY captured_at, seq",
                params,
            ).fetchall()
    finally:
        con.close()
    rows = _dedupe(_measurements([*fetched, *pending]))
    if len(rows) <= point_cap:
        return rows, sampled
    return _thin(rows, point_cap), True


def _dedupe(rows: list[Measurement]) -> list[Measurement]:
    rows.sort(key=lambda item: (item.captured_at, item.seq, item.vm_id))
    unique: list[Measurement] = []
    seen: set[tuple[str, int, str]] = set()
    for item in rows:
        if item.key in seen:
            continue
        seen.add(item.key)
        unique.append(item)
    return unique


def _thin(rows: list[Measurement], cap: int) -> list[Measurement]:
    if cap <= 1:
        return rows[-1:]
    step = (len(rows) - 1) / (cap - 1)
    return [rows[min(len(rows) - 1, round(index * step))] for index in range(cap)]


def _register_pending(con: Any, stores: Iterable[ParquetStore]) -> bool:
    payload: list[tuple[Any, ...]] = []
    for store in stores:
        for sample in store.pending_samples():
            item = _from_sample(sample)
            if item is None:
                continue
            payload.append(
                (
                    item.vm_id,
                    item.seq,
                    _iso_utc(item.captured_at),
                    item.quality,
                    item.protocol,
                    json.dumps(item.analog, ensure_ascii=False, default=str),
                    json.dumps(item.discrete, ensure_ascii=False, default=str),
                )
            )
    if not payload:
        return False
    con.execute(
        """
        CREATE TEMP TABLE pending_samples(
            vm_id VARCHAR, seq BIGINT, captured_at VARCHAR, quality VARCHAR,
            protocol VARCHAR, analog_json VARCHAR, discrete_json VARCHAR
        )
        """
    )
    con.executemany("INSERT INTO pending_samples VALUES (?,?,?,?,?,?,?)", payload)
    return True


def _measurements(fetched: list[tuple[Any, ...]]) -> list[Measurement]:
    rows: list[Measurement] = []
    for vm_id, seq, captured, quality, protocol, analog_json, discrete_json in fetched:
        moment = _parse_stored_time(captured)
        if moment is None:
            continue
        rows.append(
            Measurement(
                vm_id=str(vm_id or ""),
                captured_at=moment,
                seq=int(seq or 0),
                analog=_json_object(analog_json),
                discrete=_json_object(discrete_json),
                quality=str(quality or ""),
                protocol=str(protocol or ""),
            )
        )
    return rows


def _from_sample(sample: Any) -> Measurement | None:
    captured = getattr(sample, "captured_at", None)
    if not isinstance(captured, datetime):
        return None
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    analog = getattr(sample, "analog", None) or {}
    discrete = getattr(sample, "discrete", None) or {}
    return Measurement(
        vm_id=str(getattr(sample, "vm_id")),
        captured_at=captured.astimezone(timezone.utc),
        seq=int(getattr(sample, "seq", 0) or 0),
        analog=dict(analog) if isinstance(analog, dict) else {},
        discrete=dict(discrete) if isinstance(discrete, dict) else {},
        quality=str(getattr(getattr(sample, "quality", ""), "value", getattr(sample, "quality", ""))),
        protocol=str(getattr(getattr(sample, "protocol", ""), "value", getattr(sample, "protocol", ""))),
    )


def _accept(item: Measurement, vm_ids: set[str] | None, date_from: datetime | None, date_to: datetime | None, include_bad: bool) -> bool:
    if vm_ids is not None and item.vm_id not in vm_ids:
        return False
    if not include_bad and item.quality == "bad":
        return False
    moment = item.captured_at.astimezone(timezone.utc)
    if date_from is not None and moment < date_from:
        return False
    if date_to is not None and moment > date_to:
        return False
    return True


def _iso_utc(value: datetime) -> str:
    moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _parse_stored_time(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=timezone.utc)
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
