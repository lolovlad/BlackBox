"""How each protocol attaches to the outside world.

Worker images live in ``registry``.  This module is the link layer: what the
operator scans, what a VM must lease, and which live tests exist.  TCP is a
dialed endpoint, not a discovered device.
"""

from __future__ import annotations

from dataclasses import dataclass

from bb_platform.contracts import ResourceKind, VmProtocol


@dataclass(frozen=True)
class ConnectionProfile:
    protocol: str
    link: str
    discovers: bool
    exclusive: bool
    requires_resource: bool
    resource_kind: str | None
    probe_read: bool
    scan_label: str | None
    hint: str

    def as_dict(self) -> dict[str, object]:
        return {
            "protocol": self.protocol,
            "link": self.link,
            "discovers": self.discovers,
            "exclusive": self.exclusive,
            "requires_resource": self.requires_resource,
            "resource_kind": self.resource_kind,
            "probe_read": self.probe_read,
            "scan_label": self.scan_label,
            "hint": self.hint,
            "shows_link": self.link in {"serial", "network", "can"},
            "shows_ping": self.link == "network",
        }


PROFILES: dict[str, ConnectionProfile] = {
    VmProtocol.SIMULATOR.value: ConnectionProfile(
        protocol=VmProtocol.SIMULATOR.value,
        link="none",
        discovers=False,
        exclusive=False,
        requires_resource=False,
        resource_kind=None,
        probe_read=True,
        scan_label=None,
        hint="Симулятор не использует порт или IP — только карта и интервал опроса.",
    ),
    VmProtocol.MODBUS_RTU.value: ConnectionProfile(
        protocol=VmProtocol.MODBUS_RTU.value,
        link="serial",
        discovers=True,
        exclusive=True,
        requires_resource=True,
        resource_kind=ResourceKind.SERIAL.value,
        probe_read=True,
        scan_label="Найти порты",
        hint="UART на этом хосте. Найдите порты и выберите устройство.",
    ),
    VmProtocol.MODBUS_TCP.value: ConnectionProfile(
        protocol=VmProtocol.MODBUS_TCP.value,
        link="network",
        discovers=False,
        exclusive=False,
        requires_resource=False,
        resource_kind=None,
        probe_read=True,
        scan_label=None,
        hint="Укажите IP и TCP-порт прибора. Поиск устройств и подтверждение ресурсов не нужны.",
    ),
    VmProtocol.CAN.value: ConnectionProfile(
        protocol=VmProtocol.CAN.value,
        link="can",
        discovers=True,
        exclusive=True,
        requires_resource=True,
        resource_kind=ResourceKind.CAN.value,
        probe_read=True,
        scan_label="Найти интерфейсы",
        hint="CAN-интерфейс на этом хосте. Найдите интерфейсы и выберите шину.",
    ),
    VmProtocol.GPIO.value: ConnectionProfile(
        protocol=VmProtocol.GPIO.value,
        link="gpio",
        discovers=False,
        exclusive=False,
        requires_resource=False,
        resource_kind=None,
        probe_read=False,
        scan_label=None,
        hint="Одна GPIO панель. Сканирование ресурсов её не ищет: Hub только проверяет, что панель есть.",
    ),
}


def connection_profile(protocol: str | VmProtocol) -> ConnectionProfile:
    key = getattr(protocol, "value", protocol)
    return PROFILES.get(str(key), PROFILES[VmProtocol.SIMULATOR.value])


def inventory_kinds() -> tuple[str, ...]:
    """Resource kinds that belong in Hub inventory and the Ресурсы page."""
    kinds: list[str] = []
    seen: set[str] = set()
    for profile in PROFILES.values():
        kind = profile.resource_kind
        if profile.discovers and kind and kind not in seen:
            seen.add(kind)
            kinds.append(kind)
    return tuple(kinds)


__all__ = ["ConnectionProfile", "PROFILES", "connection_profile", "inventory_kinds"]
