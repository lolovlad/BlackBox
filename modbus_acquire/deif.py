"""
DEIF GEMPAC: пакетное чтение holding + coils (как legase/modbus_opt_v3.py).

Зависит только от minimalmodbus; для порта используйте modbus_acquire.instrument.build_instrument.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Dict, List, Optional

import minimalmodbus

ANALOG_CSV_COLUMNS: List[str] = [
    "UgenL1L2",
    "UgenL2L3",
    "UgenL3L1",
    "UgenL1N",
    "UgenL2N",
    "UgenL3N",
    "UbusL1L2",
    "UbusL2L3",
    "UbusL3L1",
    "UbusL1N",
    "UbusL2N",
    "UbusL3N",
    "Usupply",
    "IL1",
    "IL2",
    "IL3",
    "Fgen",
    "Fbus",
    "Pgen",
    "Qgen",
    "Sgen",
    "PF",
    "RPM",
    "PT100_1",
    "PT100_2",
    "Egen",
    "Runhours_hours",
    "Analog_input_E4",
    "Alarms_total",
    "Alarms_non_ack",
    "Gov.Reg.Value",
    "AVR Reg.Value",
]

DISCRETE_CSV_COLUMNS: List[str] = [
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
]

ALARM_BITS: Dict[int, List[tuple]] = {
    20: [
        ("1010 BUS High Volt 1", 0),
        ("1020 BUS High Volt 2", 1),
        ("1030 BUS Low Volt 1", 2),
        ("1040 BUS Low Volt 2", 3),
        ("1050 BUS High freq 1", 4),
        ("1060 BUS High freq 2", 5),
        ("1070 BUS Low freq 1", 6),
        ("1080 BUS Low freq 2", 7),
        ("1090 Reverse power", 8),
        ("1100 Over Current 1", 9),
        ("1110 Over Current 2", 10),
        ("1120 High power 1", 11),
        ("1130 High Power 2", 12),
        ("1220 Unbalance current", 13),
        ("1230 Unbalance voltage", 14),
    ],
    21: [
        ("Q import", 0),
        ("Q export", 1),
        ("df/dt", 2),
        ("1270 Vector jump", 3),
        ("2030 Sync. fail", 4),
        ("4220 Battery Low V", 5),
        ("CB close failure", 6),
        ("CB open failure", 7),
        ("CB position feedback failure", 8),
        ("Phase sequence error", 9),
        ("2170 GOV Reg.Fail", 10),
        ("AVR Reg.fail", 11),
        ("2181 Power Ramp Down", 13),
    ],
    22: [
        ("1310 Gen High Volt 1", 0),
        ("1320 Gen High Volt 2", 1),
        ("1330 Gen Low Volt 1", 2),
        ("1340 Gen Low Volt 2", 3),
        ("1350 Gen High freq 1", 4),
        ("1360 Gen High freq 2", 5),
        ("1370 Gen Low freq 1", 6),
        ("1380 Gen Low freq 2", 7),
        ("1400 Fast Overcurrent", 8),
        ("1410 High Overcurrent", 9),
    ],
    23: [
        ("4-20mA In.1 step 1", 0),
        ("4-20mA In.2 step 1", 1),
        ("4-20mA In.3 step 1", 2),
        ("4-20mA In.4 step 1", 3),
        ("1630 Overspeed 23", 6),
        ("Status relay DI4", 10),
        ("DI5", 11),
    ],
    70: [
        ("1510 AI1 level 1", 0),
        ("1520 AI1 level 2", 1),
        ("1530 AI2 level 1", 2),
        ("1540 AI2 level 2", 3),
        ("1550 Alt RTD3 Warn.", 4),
        ("1560 Alt RTD3 Shutd.", 5),
        ("1570 Low Fuel level", 6),
        ("1580 High Fuel Level", 7),
        ("AI1 Connect fail", 8),
        ("AI1 Sensor fail", 9),
        ("AI2 Connect fail", 10),
        ("AI2 Sensor fail", 11),
        ("RTD3 Connect fail", 12),
        ("RTD3 Sensor fail", 13),
        ("Fuel Level AI4 Connect fail", 14),
        ("Fuel Level AI4 Sensor fail", 15),
    ],
    71: [
        ("1800 AI1 L1", 0),
        ("1810 AI1 L2", 1),
        ("1820 AI2 L1", 2),
        ("1830 AI2 L2", 3),
        ("1840 AI3 L3", 4),
        ("1850 AI3 L3", 5),
        ("AI4 L1", 6),
        ("AI4 L2", 7),
        ("Fuel level Connect fail", 8),
        ("Fuel level Sensor fail", 9),
    ],
    72: [
        ("1590 PT100.1 L1", 0),
        ("1600 PT100.1 L2", 1),
        ("1610 PT100.2 L1", 2),
        ("1620 PT100.2 L2", 3),
        ("1630 Tacho overspeed L1", 4),
        ("1640 Tacho overspeed L2", 5),
        ("1650 Tacho underspeed L1", 6),
        ("1660 Tacho underspeed L2", 7),
    ],
    73: [
        ("1710 AVR Overvoltage DI45", 0),
        ("1720 Excitation Loss DI46", 1),
        ("1730 Fuel Spillage DI47", 2),
        ("1740 Fan Fail DI117", 3),
        ("1750 Spare DI118", 4),
        ("CB tripped DI23", 5),
        ("1670 EmergStop DI24", 6),
        ("1680 Earth Leakage DI43", 7),
        ("1690 Spare Term.44", 8),
        ("1700 Air Flops Close DI27", 9),
        ("Air flaps fail", 10),
        ("1760 High Water Temp DI91", 11),
        ("1770 Low Oil Press. DI92", 12),
        ("1780 Low Water Press. DI93", 13),
    ],
    74: [
        ("Engine stop failure", 0),
        ("Emergency stop failure", 1),
        ("Shutdown fail", 2),
        ("CB trip failure persist", 3),
        ("Start Fail", 4),
    ],
    79: [("External Communication Alarm", 0)],
}

STATUS_BITS: Dict[int, List[tuple]] = {
    20: [("2160 Sync Window", 12)],
    26: [
        ("Mode1", 0),
        ("Mode2", 1),
        ("Mode3", 2),
        ("Mode4", 3),
        ("Mode5", 4),
        ("Mode6", 5),
        ("Sync.Start", 7),
        ("Alarm inhibit", 8),
        ("GB Pos On", 9),
        ("Synchronising", 15),
    ],
}


def poll_raw(
    instrument: minimalmodbus.Instrument,
    address_offset: int = 1,
    on_error: Optional[Callable[[str, BaseException], None]] = None,
) -> Dict[str, Any]:
    raw: Dict[str, Any] = {}
    base = address_offset - 1
    try:
        regs = instrument.read_registers(base + 1, 90)
        raw["UgenL1L2"] = regs[0]
        raw["UgenL2L3"] = regs[1]
        raw["UgenL3L1"] = regs[2]
        raw["UgenL1N"] = regs[3]
        raw["UgenL2N"] = regs[4]
        raw["UgenL3N"] = regs[5]
        raw["Fgen"] = regs[6]
        raw["IL1"] = regs[7]
        raw["IL2"] = regs[8]
        raw["IL3"] = regs[9]
        raw["PF"] = regs[10]
        raw["Pgen"] = regs[11]
        raw["Qgen"] = regs[12]
        raw["UbusL1L2"] = regs[13]
        raw["Fbus"] = regs[14]
        raw["UbusL2L3"] = regs[30]
        raw["UbusL3L1"] = regs[31]
        raw["UbusL1N"] = regs[34]
        raw["UbusL2N"] = regs[35]
        raw["UbusL3N"] = regs[36]
        raw["Usupply"] = regs[46]
        raw["Sgen"] = regs[39]
        raw["RPM"] = regs[38]
        raw["PT100_1"] = regs[47]
        raw["PT100_2"] = regs[48]
        raw["Analog_input_E4"] = regs[58]
        raw["Egen"] = (regs[17] << 16) | regs[18]
        raw["Runhours_raw83"] = regs[82]
        raw["Runhours_raw84"] = regs[83]
        raw["Alarms_total"] = regs[26]
        raw["Alarms_non_ack"] = regs[27]
        raw["AlarmReg_20"] = regs[19]
        raw["AlarmReg_21"] = regs[20]
        raw["AlarmReg_22"] = regs[21]
        raw["AlarmReg_23"] = regs[22]
        raw["AlarmReg_26"] = regs[25]
        raw["AlarmReg_70"] = regs[69]
        raw["AlarmReg_71"] = regs[70]
        raw["AlarmReg_72"] = regs[71]
        raw["AlarmReg_73"] = regs[72]
        raw["AlarmReg_74"] = regs[73]
        raw["AlarmReg_79"] = regs[78] if len(regs) > 78 else 0
        raw["Gov.Reg.Value"] = regs[87]
        raw["AVR Reg.Value"] = regs[88]
    except BaseException as exc:
        if on_error:
            on_error("modbus_holdings", exc)

    try:
        coils = instrument.read_bits(base + 16, 32, functioncode=1)
        raw["Engine_running"] = coils[0]
        raw["Engine_cooling_down"] = coils[1]
        raw["Engine_stopped"] = coils[2]
        raw["CB_Closed"] = coils[3]
        raw["CB_Opened"] = coils[4]
        raw["CB_Tripped"] = coils[5]
        raw["Warning"] = coils[6]
        raw["Shutdown"] = coils[7]
        raw["Avail_to_sync"] = coils[8]
        raw["Synchronizing"] = coils[9]
        raw["Auto_control_on"] = coils[11]
        raw["Local_control_on"] = coils[13]
        raw["ManMode"] = coils[26] if len(coils) > 26 else False
        raw["Peak Lopping"] = coils[27] if len(coils) > 27 else False
        raw["Base Load (P/PF)"] = coils[28] if len(coils) > 28 else False
        raw["Droop"] = coils[29] if len(coils) > 29 else False
        raw["Load Share"] = coils[30] if len(coils) > 30 else False
        raw["Base Load (P/var)"] = coils[31] if len(coils) > 31 else False
    except BaseException as exc:
        if on_error:
            on_error("modbus_coils", exc)

    return raw


def convert_raw(raw: Dict[str, Any]) -> Dict[str, Any]:
    data = copy.deepcopy(raw)
    data["Fgen"] = raw.get("Fgen", 0) / 100.0
    data["Fbus"] = raw.get("Fbus", 0) / 100.0
    data["Usupply"] = raw.get("Usupply", 0) / 10.0
    data["IL1"] = raw.get("IL1", 0) / 1.0
    data["IL2"] = raw.get("IL2", 0) / 1.0
    data["IL3"] = raw.get("IL3", 0) / 1.0
    data["PF"] = raw.get("PF", 0) / 100.0
    data["Gov.Reg.Value"] = raw.get("Gov.Reg.Value", 0) / 10.0
    data["AVR Reg.Value"] = raw.get("AVR Reg.Value", 0) / 10.0
    data["Pgen"] = raw.get("Pgen", 0)
    data["Qgen"] = raw.get("Qgen", 0)
    data["Sgen"] = raw.get("Sgen", 0)
    data["PT100_1"] = raw.get("PT100_1", 0)
    data["PT100_2"] = raw.get("PT100_2", 0)
    data["Runhours_hours"] = raw.get("Runhours_raw84", 0) * 1000 + raw.get("Runhours_raw83", 0)

    active_alarms: List[str] = []
    for reg, bits in ALARM_BITS.items():
        val = raw.get(f"AlarmReg_{reg}", 0)
        for name, bit in bits:
            if val & (1 << bit):
                active_alarms.append(name)
    data["active_alarms"] = active_alarms

    active_status: List[str] = []
    for reg, bits in STATUS_BITS.items():
        val = raw.get(f"AlarmReg_{reg}", 0)
        for name, bit in bits:
            if val & (1 << bit):
                active_status.append(name)
    for name in active_status:
        data[name] = True
    return data


def analog_discrete_for_csv(processed: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    analog = {k: processed.get(k, "") for k in ANALOG_CSV_COLUMNS}
    discrete = {k: bool(processed.get(k, False)) for k in DISCRETE_CSV_COLUMNS}
    return analog, discrete


__all__ = [
    "ALARM_BITS",
    "STATUS_BITS",
    "ANALOG_CSV_COLUMNS",
    "DISCRETE_CSV_COLUMNS",
    "poll_raw",
    "convert_raw",
    "analog_discrete_for_csv",
]
