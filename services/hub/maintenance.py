from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from .config import HubConfig
from .db import HubRepository


def compact_ingest(db_path: Path, *, hours: int = 24, vacuum: bool = False) -> tuple[int, int]:
    repo = HubRepository(db_path)
    removed = 0
    while True:
        count = repo.purge_ingest_batches(older_than_hours=hours, batch_size=100_000)
        removed += count
        if count == 0:
            break
    with repo.connect() as connection:
        remaining = int(connection.execute("SELECT COUNT(*) FROM ingest_batches").fetchone()[0])
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    if vacuum:
        connection = sqlite3.connect(db_path, timeout=300, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout=300000")
            connection.execute("VACUUM")
        finally:
            connection.close()
    return removed, remaining


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BlackBox Hub database maintenance")
    parser.add_argument("--ingest-hours", type=int, default=24)
    parser.add_argument("--vacuum", action="store_true", help="Reclaim file space; requires a maintenance window")
    args = parser.parse_args(argv)
    cfg = HubConfig.from_env()
    removed, remaining = compact_ingest(cfg.db_path, hours=max(1, args.ingest_hours), vacuum=args.vacuum)
    print(f"ingest_batches removed={removed} remaining={remaining} vacuum={args.vacuum}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
