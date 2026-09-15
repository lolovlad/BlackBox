#!/usr/bin/env python3
"""
DEIF GEMPAC: опрос Modbus и вывод в консоль (как legase/modbus_opt_v3.py, без CSV).

По умолчанию экран очищается и перерисовывается на каждом обновлении.
Для режима прокрутки (без очистки) используйте --no-clear.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

from modbus_acquire import build_instrument, convert_raw, poll_raw
from modbus_acquire.serial_cli import validate_serial_port_for_platform

# --- как в legase/modbus_opt_v3.py ---
DEFAULT_PORT = "/dev/ttyAMA0"
DEFAULT_SLAVE_ID = 1
DEFAULT_BAUDRATE = 9600
DEFAULT_ADDRESS_OFFSET = 1
DEFAULT_POLL_INTERVAL = 0.12
DEFAULT_DISPLAY_INTERVAL = 0.6
DEFAULT_TIMEOUT = 0.35

def _display(data: dict, last_poll_time: float, modbus_errors: int) -> None:
    print("=== DEIF GEMPAC — ВСЕ ПЕРЕМЕННЫЕ ===", datetime.now().strftime("%H:%M:%S"))
    print(f"Last poll time = {last_poll_time:.3f} сек | Modbus errors = {modbus_errors}")
    print("=" * 100)
    print("ИЗМЕРЕНИЯ")
    print(f" UgenL1L2 = {data.get('UgenL1L2', 0):5} В")
    print(f" UgenL2L3 = {data.get('UgenL2L3', 0):5} В")
    print(f" UgenL3L1 = {data.get('UgenL3L1', 0):5} В")
    print(f" UgenL1N = {data.get('UgenL1N', 0):5} В")
    print(f" UgenL2N = {data.get('UgenL2N', 0):5} В")
    print(f" UgenL3N = {data.get('UgenL3N', 0):5} В")
    print(f" UbusL1L2 = {data.get('UbusL1L2', 0):5} В")
    print(f" UbusL2L3 = {data.get('UbusL2L3', 0):5} В")
    print(f" UbusL3L1 = {data.get('UbusL3L1', 0):5} В")
    print(f" UbusL1N = {data.get('UbusL1N', 0):5} В")
    print(f" UbusL2N = {data.get('UbusL2N', 0):5} В")
    print(f" UbusL3N = {data.get('UbusL3N', 0):5} В")
    print(f" Usupply = {data.get('Usupply', 0):.1f} В")
    print(f" IL1 = {data.get('IL1', 0):.1f} А")
    print(f" IL2 = {data.get('IL2', 0):.1f} А")
    print(f" IL3 = {data.get('IL3', 0):.1f} А")
    print(f" Fgen = {data.get('Fgen', 0):.2f} Гц")
    print(f" Fbus = {data.get('Fbus', 0):.2f} Гц")
    print(f" Pgen = {data.get('Pgen', 0):.1f} кВт")
    print(f" Qgen = {data.get('Qgen', 0):.1f} кВар")
    print(f" Sgen = {data.get('Sgen', 0):.1f} кВА")
    print(f" PF = {data.get('PF', 0):.2f}")
    print(f" RPM = {data.get('RPM', 0)} об/мин")
    print(f" PT100_1 = {data.get('PT100_1', 0):.2f} °C")
    print(f" PT100_2 = {data.get('PT100_2', 0):.2f} °C")
    print(f" Egen = {data.get('Egen', 0):,} кВт·ч")
    print(f" Runhours = {data.get('Runhours_hours', 0):.0f} часов")
    print(f" Gov.Reg.Value = {data.get('Gov.Reg.Value', 0):.2f} %")
    print(f" AVR Reg.Value = {data.get('AVR Reg.Value', 0):.2f} %")
    print(f" Analog_input_E4 = {data.get('Analog_input_E4', 0)}")
    print("\nДИСКРЕТНЫЕ ПЕРЕМЕННЫЕ")
    for key in [
        "Engine_running",
        "Engine_cooling_down",
        "Engine_stopped",
        "CB_Closed",
        "CB_Opened",
        "CB_Tripped",
        "Warning",
        "Shutdown",
        "Avail_to_sync",
        "Synchronizing",
        "Auto_control_on",
        "Local_control_on",
        "ManMode",
        "Peak Lopping",
        "Base Load (P/PF)",
        "Droop",
        "Load Share",
        "Base Load (P/var)",
    ]:
        state = "ВКЛ" if data.get(key, False) else "ВЫКЛ"
        print(f" {key} = {state}")
    print("\nАКТИВНЫЕ АВАРИИ")
    print(f" Alarms_total = {data.get('Alarms_total', 0)} Alarms_non_ack = {data.get('Alarms_non_ack', 0)}")
    alarms = data.get("active_alarms", [])
    print(f" Активных аварий: {len(alarms)}")
    for a in alarms:
        print(f" • {a}")
    print("\nАКТИВНЫЕ СТАТУСЫ")
    for key in [
        "Sync.Start",
        "Mode1",
        "Mode2",
        "Mode3",
        "Mode4",
        "Mode5",
        "Mode6",
        "Alarm inhibit",
        "GB Pos On",
    ]:
        state = "ВКЛ" if data.get(key, False) else "ВЫКЛ"
        print(f" {key} = {state}")
    print(f"\nLast poll time = {last_poll_time:.3f} сек")
    print(f"Modbus errors = {modbus_errors}")
    print("=" * 100)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="DEIF GEMPAC — опрос Modbus, вывод в консоль (настройки как в legase)")
    p.add_argument(
        "--port",
        default=DEFAULT_PORT,
        help="Порт: Linux /dev/ttyUSB0, /dev/ttyAMA0 …; Windows COMn",
    )
    p.add_argument("--slave", type=int, default=DEFAULT_SLAVE_ID, help="Slave ID")
    p.add_argument("--baudrate", type=int, default=DEFAULT_BAUDRATE)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Таймаут serial, сек (легаси 0.35)")
    p.add_argument("--address-offset", type=int, default=DEFAULT_ADDRESS_OFFSET, dest="address_offset")
    p.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL, dest="poll_interval", help="Пауза между опросами (легаси 0.12)")
    p.add_argument(
        "--display-interval",
        type=float,
        default=DEFAULT_DISPLAY_INTERVAL,
        dest="display_interval",
        help="Период обновления экрана, сек (легаси 0.6)",
    )
    p.add_argument(
        "--no-clear",
        action="store_true",
        help="Режим прокрутки: без очистки экрана, каждое обновление добавляется вниз",
    )
    p.add_argument("--once", action="store_true", help="Один опрос и выход")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    validate_serial_port_for_platform(args.port)
    refresh = not args.no_clear

    modbus_errors = 0

    def on_error(_source: str, _exc: BaseException) -> None:
        nonlocal modbus_errors
        modbus_errors += 1

    instrument = build_instrument(
        {
            "port": args.port,
            "slave_id": args.slave,
            "baudrate": args.baudrate,
            "timeout": args.timeout,
            "bytesize": 8,
            "parity": "N",
            "stopbits": 1,
            "clear_buffers_before_each_transaction": True,
            "close_port_after_each_call": False,
        }
    )

    last_display = 0.0
    try:
        while True:
            start = time.perf_counter()
            raw = poll_raw(instrument, address_offset=args.address_offset, on_error=on_error)
            processed = convert_raw(raw)
            last_poll_time = time.perf_counter() - start

            if args.once:
                _display(processed, last_poll_time, modbus_errors)
                break

            now = time.time()
            if now - last_display >= args.display_interval:
                if refresh:
                    if sys.platform == "win32":
                        os.system("cls")
                    else:
                        os.system("clear")
                _display(processed, last_poll_time, modbus_errors)
                last_display = now

            time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        print("\n\nОстановлено.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
