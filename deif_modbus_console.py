#!/usr/bin/env python3
"""
DEIF GEMPAC: опрос Modbus и вывод в консоль (как legase/modbus_opt_v3.py, без CSV).

В обычном режиме экран обновляется «на месте» (ANSI clear + один кадр текста), как docker stats,
а не бесконечной прокруткой. Для лога в файл используйте --no-clear.

Настройки по умолчанию совпадают с легаси:
  PORT=/dev/ttyAMA0, SLAVE_ID=1, BAUDRATE=9600, timeout=0.35, ADDRESS_OFFSET=1,
  POLL_INTERVAL=0.12, DISPLAY_INTERVAL=0.6, 8N1, clear_buffers=True

Пример (Linux, USB‑адаптер RS‑485):
  python deif_modbus_console.py --port /dev/ttyUSB0

Пример (Windows):
  python deif_modbus_console.py --port COM3
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

# Полный сброс видимой области и курсор в (1,1) — как docker stats / htop (без прокрутки вниз)
_ANSI_CLEAR_HOME = "\033[2J\033[H"


def _enable_windows_vt() -> bool:
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        handle = ctypes.windll.kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        if ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING):
            return True
    except Exception:
        pass
    return False


def _terminal_inplace_capable() -> bool:
    """TTY + ANSI очистка экрана (один кадр без прокрутки), на Windows — включение VT100."""
    if not sys.stdout.isatty():
        return False
    if sys.platform == "win32":
        return _enable_windows_vt()
    return os.environ.get("TERM", "xterm") != "dumb"


def _format_dashboard(data: dict, last_poll_time: float, modbus_errors: int) -> str:
    lines: list[str] = []
    lines.append("=== DEIF GEMPAC — ВСЕ ПЕРЕМЕННЫЕ === " + datetime.now().strftime("%H:%M:%S"))
    lines.append(f"Last poll time = {last_poll_time:.3f} сек | Modbus errors = {modbus_errors}")
    lines.append("=" * 100)
    lines.append("ИЗМЕРЕНИЯ")
    lines.append(f" UgenL1L2 = {data.get('UgenL1L2', 0):5} В")
    lines.append(f" UgenL2L3 = {data.get('UgenL2L3', 0):5} В")
    lines.append(f" UgenL3L1 = {data.get('UgenL3L1', 0):5} В")
    lines.append(f" UgenL1N = {data.get('UgenL1N', 0):5} В")
    lines.append(f" UgenL2N = {data.get('UgenL2N', 0):5} В")
    lines.append(f" UgenL3N = {data.get('UgenL3N', 0):5} В")
    lines.append(f" UbusL1L2 = {data.get('UbusL1L2', 0):5} В")
    lines.append(f" UbusL2L3 = {data.get('UbusL2L3', 0):5} В")
    lines.append(f" UbusL3L1 = {data.get('UbusL3L1', 0):5} В")
    lines.append(f" UbusL1N = {data.get('UbusL1N', 0):5} В")
    lines.append(f" UbusL2N = {data.get('UbusL2N', 0):5} В")
    lines.append(f" UbusL3N = {data.get('UbusL3N', 0):5} В")
    lines.append(f" Usupply = {data.get('Usupply', 0):.1f} В")
    lines.append(f" IL1 = {data.get('IL1', 0):.1f} А")
    lines.append(f" IL2 = {data.get('IL2', 0):.1f} А")
    lines.append(f" IL3 = {data.get('IL3', 0):.1f} А")
    lines.append(f" Fgen = {data.get('Fgen', 0):.2f} Гц")
    lines.append(f" Fbus = {data.get('Fbus', 0):.2f} Гц")
    lines.append(f" Pgen = {data.get('Pgen', 0):.1f} кВт")
    lines.append(f" Qgen = {data.get('Qgen', 0):.1f} кВар")
    lines.append(f" Sgen = {data.get('Sgen', 0):.1f} кВА")
    lines.append(f" PF = {data.get('PF', 0):.2f}")
    lines.append(f" RPM = {data.get('RPM', 0)} об/мин")
    lines.append(f" PT100_1 = {data.get('PT100_1', 0):.2f} °C")
    lines.append(f" PT100_2 = {data.get('PT100_2', 0):.2f} °C")
    lines.append(f" Egen = {data.get('Egen', 0):,} кВт·ч")
    lines.append(f" Runhours = {data.get('Runhours_hours', 0):.0f} часов")
    lines.append(f" Gov.Reg.Value = {data.get('Gov.Reg.Value', 0):.2f} %")
    lines.append(f" AVR Reg.Value = {data.get('AVR Reg.Value', 0):.2f} %")
    lines.append(f" Analog_input_E4 = {data.get('Analog_input_E4', 0)}")
    lines.append("")
    lines.append("ДИСКРЕТНЫЕ ПЕРЕМЕННЫЕ")
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
        lines.append(f" {key} = {state}")
    lines.append("")
    lines.append("АКТИВНЫЕ АВАРИИ")
    lines.append(f" Alarms_total = {data.get('Alarms_total', 0)} Alarms_non_ack = {data.get('Alarms_non_ack', 0)}")
    alarms = data.get("active_alarms", [])
    lines.append(f" Активных аварий: {len(alarms)}")
    for a in alarms:
        lines.append(f" • {a}")
    lines.append("")
    lines.append("АКТИВНЫЕ СТАТУСЫ")
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
        lines.append(f" {key} = {state}")
    lines.append("")
    lines.append(f"Last poll time = {last_poll_time:.3f} сек")
    lines.append(f"Modbus errors = {modbus_errors}")
    lines.append("=" * 100)
    return "\n".join(lines)


def _display(
    data: dict,
    last_poll_time: float,
    modbus_errors: int,
    *,
    refresh: bool,
    inplace: bool,
) -> None:
    text = _format_dashboard(data, last_poll_time, modbus_errors)
    if not refresh:
        print(text)
        return
    if inplace:
        sys.stdout.write(_ANSI_CLEAR_HOME + text + "\n")
        sys.stdout.flush()
        return
    if sys.platform == "win32":
        os.system("cls")
    else:
        os.system("clear")
    print(text)


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
        help="Режим прокрутки: каждое обновление добавляет текст вниз (без очистки экрана)",
    )
    p.add_argument("--once", action="store_true", help="Один опрос и выход")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    validate_serial_port_for_platform(args.port)
    refresh = not args.no_clear
    inplace = refresh and _terminal_inplace_capable()

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
                _display(processed, last_poll_time, modbus_errors, refresh=refresh, inplace=inplace)
                break

            now = time.time()
            if now - last_display >= args.display_interval:
                _display(processed, last_poll_time, modbus_errors, refresh=refresh, inplace=inplace)
                last_display = now

            time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        print("\n\nОстановлено.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
