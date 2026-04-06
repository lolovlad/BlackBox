#!/usr/bin/env python3
"""
DEIF GEMPAC: Modbus → почасовые CSV (analogs / discretes). Ошибки Modbus — только в лог.

Пример:
  python deif_modbus_csv_logger.py --port COM3 --output ./logs --prefix gempac1
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from blackbox.hourly_param_csv import HourlySplitCsvWriter
from modbus_acquire import (
    ANALOG_CSV_COLUMNS,
    DISCRETE_CSV_COLUMNS,
    analog_discrete_for_csv,
    build_instrument,
    convert_raw,
    poll_raw,
)

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="DEIF GEMPAC Modbus → почасовые CSV")
    p.add_argument("--port", default="/dev/ttyAMA0", help="Последовательный порт (Windows: COMn)")
    p.add_argument("--slave", type=int, default=1, help="Slave ID")
    p.add_argument("--baudrate", type=int, default=9600)
    p.add_argument("--timeout", type=float, default=0.35, help="Таймаут serial, сек")
    p.add_argument("--address-offset", type=int, default=1, dest="address_offset", help="Как в legase ADDRESS_OFFSET")
    p.add_argument("--interval", type=float, default=0.12, help="Пауза между опросами, сек")
    p.add_argument("--output", default="./modbus_logs", help="Каталог для analogs/ и discretes/")
    p.add_argument("--prefix", default="deif", help="Префикс имён файлов: {prefix}_{дата}_{час}.csv")
    p.add_argument("--verbose", action="store_true", help="Лог в консоль")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    def on_error(source: str, exc: BaseException) -> None:
        logger.warning("%s: %s: %s", source, type(exc).__name__, exc)

    instrument = build_instrument(
        {
            "port": args.port,
            "slave_id": args.slave,
            "baudrate": args.baudrate,
            "timeout": args.timeout,
            "clear_buffers_before_each_transaction": True,
            "close_port_after_each_call": False,
        }
    )

    writer = HourlySplitCsvWriter(
        Path(args.output),
        args.prefix,
        ANALOG_CSV_COLUMNS,
        DISCRETE_CSV_COLUMNS,
    )

    try:
        while True:
            raw = poll_raw(instrument, address_offset=args.address_offset, on_error=on_error)
            processed = convert_raw(raw)
            analog, discrete = analog_discrete_for_csv(processed)
            writer.write_sample(datetime.now(), analog, discrete)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("Останов по Ctrl+C")
    finally:
        writer.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
