"""Protocol registry shared by API validation and the administration UI."""

from __future__ import annotations

from dataclasses import dataclass

from bb_platform.contracts import VmProtocol


@dataclass(frozen=True)
class ProtocolSpec:
    protocol: VmProtocol
    enabled: bool
    worker_kind: str
    description: str


PROTOCOLS: tuple[ProtocolSpec, ...] = (
    ProtocolSpec(VmProtocol.MODBUS_RTU, True, "modbus-rtu", "Modbus RTU worker"),
    ProtocolSpec(VmProtocol.MODBUS_TCP, True, "modbus-tcp", "Modbus TCP worker"),
    ProtocolSpec(VmProtocol.CAN, False, "can", "Contract/discovery only; adapter requires bench validation"),
    ProtocolSpec(VmProtocol.GPIO, True, "gpio", "Raspberry GPIO inputs with the legacy hold-time card"),
    ProtocolSpec(VmProtocol.SIMULATOR, True, "simulator", "Deterministic analog/discrete simulator"),
)


def protocol_spec(protocol: VmProtocol) -> ProtocolSpec:
    return next(spec for spec in PROTOCOLS if spec.protocol == protocol)


__all__ = ["PROTOCOLS", "ProtocolSpec", "protocol_spec"]
