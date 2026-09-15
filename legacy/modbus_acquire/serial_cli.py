"""Проверка имени COM-порта под текущую ОС (для CLI-скриптов)."""

from __future__ import annotations

import re
import sys

_WIN_COM = re.compile(r"^COM\d+$", re.IGNORECASE)


def validate_serial_port_for_platform(port: str) -> None:
    """
    На Linux/macOS имя вида COM3 не существует — даём явную подсказку до открытия pyserial.
    На Windows путь /dev/tty... обычно неверен.
    """
    p = port.strip()
    win = sys.platform == "win32"
    if win:
        if p.startswith("/dev/"):
            print(
                f"Указан Unix-порт «{p}», а система — Windows. "
                "Используйте COMn, например: --port COM3",
                file=sys.stderr,
            )
            raise SystemExit(2)
        return
    if _WIN_COM.match(p):
        print(
            f"Указан порт Windows «{p}», а ОС — не Windows (сейчас {sys.platform}).\n"
            "На Linux обычно: /dev/ttyUSB0, /dev/ttyACM0, /dev/ttyAMA0 или /dev/serial0.\n"
            "Пример: python deif_modbus_console.py --port /dev/ttyUSB0\n"
            "Список устройств: ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null; dmesg | tail",
            file=sys.stderr,
        )
        raise SystemExit(2)
