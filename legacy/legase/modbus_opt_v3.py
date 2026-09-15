import minimalmodbus
import serial
import time
import copy
import os
from datetime import datetime
import csv
from pathlib import Path


def block1_poll():
    global modbus_errors
    raw = {}
    base = ADDRESS_OFFSET - 1
    try:
        regs = instrument.read_registers(base + 1, 90)
        raw['UgenL1L2'] = regs[0]
        raw['UgenL2L3'] = regs[1]
        raw['UgenL3L1'] = regs[2]
        raw['UgenL1N'] = regs[3]
        raw['UgenL2N'] = regs[4]
        raw['UgenL3N'] = regs[5]
        raw['Fgen'] = regs[6]
        raw['IL1'] = regs[7]
        raw['IL2'] = regs[8]
        raw['IL3'] = regs[9]
        raw['PF'] = regs[10]
        raw['Pgen'] = regs[11]
        raw['Qgen'] = regs[12]
        raw['UbusL1L2'] = regs[13]
        raw['Fbus'] = regs[14]
        raw['UbusL2L3'] = regs[30]
        raw['UbusL3L1'] = regs[31]
        raw['UbusL1N'] = regs[34]
        raw['UbusL2N'] = regs[35]
        raw['UbusL3N'] = regs[36]
        raw['Usupply'] = regs[46]
        raw['Sgen'] = regs[39]
        raw['RPM'] = regs[38]
        raw['PT100_1'] = regs[47]
        raw['PT100_2'] = regs[48]
        raw['Analog_input_E4'] = regs[58]
        raw['Egen'] = (regs[17] << 16) | regs[18]
        raw['Runhours_raw83'] = regs[82]
        raw['Runhours_raw84'] = regs[83]
        raw['Alarms_total'] = regs[26]
        raw['Alarms_non_ack'] = regs[27]
        raw['AlarmReg_20'] = regs[19]
        raw['AlarmReg_21'] = regs[20]
        raw['AlarmReg_22'] = regs[21]
        raw['AlarmReg_23'] = regs[22]
        raw['AlarmReg_26'] = regs[25]
        raw['AlarmReg_70'] = regs[69]
        raw['AlarmReg_71'] = regs[70]
        raw['AlarmReg_72'] = regs[71]
        raw['AlarmReg_73'] = regs[72]
        raw['AlarmReg_74'] = regs[73]
        raw['AlarmReg_79'] = regs[78] if len(regs) > 78 else 0
        raw['Gov.Reg.Value'] = regs[87]
        raw['AVR Reg.Value'] = regs[88]
    except Exception as e:
        modbus_errors += 1
        print(f"Holdings error: {type(e).__name__}: {e}")
    try:
        coils = instrument.read_bits(base + 16, 32, functioncode=1)
        raw['Engine_running'] = coils[0]
        raw['Engine_cooling_down'] = coils[1]
        raw['Engine_stopped'] = coils[2]
        raw['CB_Closed'] = coils[3]
        raw['CB_Opened'] = coils[4]
        raw['CB_Tripped'] = coils[5]
        raw['Warning'] = coils[6]
        raw['Shutdown'] = coils[7]
        raw['Avail_to_sync'] = coils[8]
        raw['Synchronizing'] = coils[9]
        raw['Auto_control_on'] = coils[11]
        raw['Local_control_on'] = coils[13]
        raw['ManMode'] = coils[26] if len(coils) > 26 else False
        raw['Peak Lopping'] = coils[27] if len(coils) > 27 else False
        raw['Base Load (P/PF)'] = coils[28] if len(coils) > 28 else False
        raw['Droop'] = coils[29] if len(coils) > 29 else False
        raw['Load Share'] = coils[30] if len(coils) > 30 else False
        raw['Base Load (P/var)'] = coils[31] if len(coils) > 31 else False
    except Exception as e:
        modbus_errors += 1
        print(f"Coils error: {type(e).__name__}: {e}")
    return raw


def block2_convert(raw):
    data = copy.deepcopy(raw)
    data['Fgen'] = raw.get('Fgen', 0) / 100.0
    data['Fbus'] = raw.get('Fbus', 0) / 100.0
    data['Usupply'] = raw.get('Usupply', 0) / 10.0
    data['IL1'] = raw.get('IL1', 0) / 1.0
    data['IL2'] = raw.get('IL2', 0) / 1.0
    data['IL3'] = raw.get('IL3', 0) / 1.0
    data['PF'] = raw.get('PF', 0) / 100.0
    data['Gov.Reg.Value'] = raw.get('Gov.Reg.Value', 0) / 10.0
    data['AVR Reg.Value'] = raw.get('AVR Reg.Value', 0) / 10.0
    data['Pgen'] = raw.get('Pgen', 0)
    data['Qgen'] = raw.get('Qgen', 0)
    data['Sgen'] = raw.get('Sgen', 0)
    data['PT100_1'] = raw.get('PT100_1', 0)
    data['PT100_2'] = raw.get('PT100_2', 0)
    data['Runhours_hours'] = raw.get('Runhours_raw84', 0) * 1000 + raw.get('Runhours_raw83', 0)
    active_alarms = []
    for reg, bits in alarm_bits.items():
        val = raw.get(f'AlarmReg_{reg}', 0)
        for name, bit in bits:
            if val & (1 << bit):
                active_alarms.append(name)
    data['active_alarms'] = active_alarms
    active_status = []
    status_bits = {
        20: [("2160 Sync Window", 12)],
        26: [("Mode1", 0), ("Mode2", 1), ("Mode3", 2), ("Mode4", 3), ("Mode5", 4), ("Mode6", 5), ("Sync.Start", 7),
             ("Alarm inhibit", 8), ("GB Pos On", 9), ("Synchronising", 15)]
    }
    for reg, bits in status_bits.items():
        val = raw.get(f'AlarmReg_{reg}', 0)
        for name, bit in bits:
            if val & (1 << bit):
                active_status.append(name)
    data['active_status'] = active_status
    for name in active_status:
        data[name] = True
    return data


def block3_display(data):
    os.system('clear')
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
    for key in ['Engine_running', 'Engine_cooling_down', 'Engine_stopped', 'CB_Closed', 'CB_Opened', 'CB_Tripped',
                'Warning', 'Shutdown', 'Avail_to_sync', 'Synchronizing', 'Auto_control_on', 'Local_control_on',
                'ManMode', 'Peak Lopping', 'Base Load (P/PF)', 'Droop',
                'Load Share', 'Base Load (P/var)']:
        state = "ВКЛ" if data.get(key, False) else "ВЫКЛ"
        print(f" {key} = {state}")
    print("\nАКТИВНЫЕ АВАРИИ")
    print(f" Alarms_total = {data.get('Alarms_total', 0)} Alarms_non_ack = {data.get('Alarms_non_ack', 0)}")
    alarms = data.get('active_alarms', [])
    print(f" Активных аварий: {len(alarms)}")
    for a in alarms:
        print(f" • {a}")
    print("\nАКТИВНЫЕ СТАТУСЫ")
    for key in ['Sync.Start', 'Mode1', 'Mode2', 'Mode3', 'Mode4', 'Mode5', 'Mode6', 'Alarm inhibit', 'GB Pos On']:
        state = "ВКЛ" if data.get(key, False) else "ВЫКЛ"
        print(f" {key} = {state}")
    print(f"\nLast poll time = {last_poll_time:.3f} сек")
    print(f"Modbus errors = {modbus_errors}")
    print("=" * 100)


def ensure_log_directories():
    for folder in ["analogs", "discretes", "alarms"]:
        Path(f"{LOG_BASE_PATH}/{folder}").mkdir(parents=True, exist_ok=True)


def get_csv_filename(category: str, dt: datetime) -> str:
    return f"{LOG_BASE_PATH}/{category}/{category}_{dt.strftime('%Y-%m-%d_%H')}.csv"


def open_new_csv_files(dt: datetime):
    global csv_files, csv_writers, current_hour_str
    hour_str = dt.strftime('%Y-%m-%d_%H')
    if hour_str == current_hour_str and all(csv_files.values()):
        return
    close_csv_files()
    current_hour_str = hour_str
    for category in ["analogs", "discretes", "alarms"]:
        filename = get_csv_filename(category, dt)
        file_exists = os.path.exists(filename)
        f = open(filename, 'a', newline='', encoding='utf-8')
        writer = csv.writer(f, delimiter=',')
        if not file_exists:
            if category == "analogs":
                writer.writerow(["date", "time", "UgenL1L2", "UgenL2L3", "UgenL3L1", "UgenL1N", "UgenL2N", "UgenL3N",
                                 "UbusL1L2", "UbusL2L3", "UbusL3L1", "UbusL1N", "UbusL2N", "UbusL3N", "Usupply",
                                 "IL1", "IL2", "IL3", "Fgen", "Fbus", "Pgen", "Qgen", "Sgen", "PF", "RPM",
                                 "PT100_1", "PT100_2", "Egen", "Runhours_hours", "Analog_input_E4",
                                 "Alarms_total", "Alarms_non_ack", "Gov.Reg.Value", "AVR Reg.Value"])
            elif category == "discretes":
                writer.writerow(["date", "time", "Engine_running", "Engine_cooling_down", "Engine_stopped", "CB_Closed",
                                 "CB_Opened", "CB_Tripped", "Warning", "Shutdown", "Avail_to_sync", "Synchronizing",
                                 "Auto_control_on", "Local_control_on", "ManMode", "Peak Lopping", "Base Load (P/PF)",
                                 "Droop", "Load Share", "Base Load (P/var)"])
            elif category == "alarms":
                writer.writerow(["date", "time"] + all_alarm_names)
        csv_files[category] = f
        csv_writers[category] = writer


def close_csv_files():
    for f in csv_files.values():
        if f and not f.closed:
            f.close()


def log_data_to_csv(data: dict, dt: datetime):
    date_str = dt.strftime("%Y-%m-%d")
    time_str = dt.strftime("%H:%M:%S.%f")[:12]
    open_new_csv_files(dt)
    # ANALOGS
    row = [date_str, time_str]
    analog_keys = ["UgenL1L2", "UgenL2L3", "UgenL3L1", "UgenL1N", "UgenL2N", "UgenL3N", "UbusL1L2", "UbusL2L3",
                   "UbusL3L1",
                   "UbusL1N", "UbusL2N", "UbusL3N", "Usupply", "IL1", "IL2", "IL3", "Fgen", "Fbus", "Pgen", "Qgen",
                   "Sgen",
                   "PF", "RPM", "PT100_1", "PT100_2", "Egen", "Runhours_hours", "Analog_input_E4", "Alarms_total",
                   "Alarms_non_ack", "Gov.Reg.Value", "AVR Reg.Value"]
    for k in analog_keys:
        row.append(data.get(k, ''))
    csv_writers["analogs"].writerow(row)
    # DISCRETES
    row = [date_str, time_str]
    discrete_keys = ["Engine_running", "Engine_cooling_down", "Engine_stopped", "CB_Closed", "CB_Opened", "CB_Tripped",
                     "Warning", "Shutdown", "Avail_to_sync", "Synchronizing", "Auto_control_on", "Local_control_on",
                     "ManMode", "Peak Lopping", "Base Load (P/PF)", "Droop", "Load Share", "Base Load (P/var)"]
    for k in discrete_keys:
        row.append(1 if data.get(k, False) else 0)
    csv_writers["discretes"].writerow(row)
    # ALARMS
    row = [date_str, time_str]
    active_set = set(data.get('active_alarms', []))
    for name in all_alarm_names:
        row.append(1 if name in active_set else 0)
    csv_writers["alarms"].writerow(row)
    for f in csv_files.values():
        if f:
            f.flush()


PORT = "/dev/ttyAMA0"
SLAVE_ID = 1
BAUDRATE = 9600
ADDRESS_OFFSET = 1
POLL_INTERVAL = 0.12
DISPLAY_INTERVAL = 0.6
instrument = minimalmodbus.Instrument(PORT, SLAVE_ID, mode=minimalmodbus.MODE_RTU)
instrument.serial.baudrate = BAUDRATE
instrument.serial.bytesize = 8
instrument.serial.parity = serial.PARITY_NONE
instrument.serial.stopbits = 1
instrument.serial.timeout = 0.35
instrument.clear_buffers_before_each_transaction = True
LOG_BASE_PATH = "/mnt/nvme"
modbus_errors = 0
last_poll_time = 0.0


alarm_bits = {
       20: [
           ("1010 BUS High Volt 1",0),
           ("1020 BUS High Volt 2",1),
           ("1030 BUS Low Volt 1",2),
           ("1040 BUS Low Volt 2",3),
           ("1050 BUS High freq 1",4),
           ("1060 BUS High freq 2",5),
           ("1070 BUS Low freq 1",6),
           ("1080 BUS Low freq 2", 7),
           ("1090 Reverse power",8),
           ("1100 Over Current 1",9),
           ("1110 Over Current 2",10),
           ("1120 High power 1",11),
           ("1130 High Power 2",12),
           ("1220 Unbalance current",13),
           ("1230 Unbalance voltage",14)
       ],
        21: [("Q import",0),("Q export",1),("df/dt",2),("1270 Vector jump",3),("2030 Sync. fail",4),("4220 Battery Low V",5),("CB close failure",6),("CB open failure",7),("CB position feedback failure",8),("Phase sequence error",9),
             ("2170 GOV Reg.Fail",10),("AVR Reg.fail",11),("2181 Power Ramp Down",13)],
        22: [("1310 Gen High Volt 1",0),("1320 Gen High Volt 2",1),("1330 Gen Low Volt 1",2),("1340 Gen Low Volt 2",3),("1350 Gen High freq 1",4),("1360 Gen High freq 2",5),("1370 Gen Low freq 1",6),("1380 Gen Low freq 2",7),
             ("1400 Fast Overcurrent",8),("1410 High Overcurrent",9)],
        23: [("4-20mA In.1 step 1",0),("4-20mA In.2 step 1",1),("4-20mA In.3 step 1",2),("4-20mA In.4 step 1",3),("1630 Overspeed 23",6),("Status relay DI4",10),("DI5",11)],
        #26: [("Mode1",0),("Mode2",1),("Mode3",2),("Mode4",3),("Mode5",4),("Mode6",5),("Sync.Start",7),("Alarm inhibit",8),("GB Pos On",9),("Synchronising",15)],
        70: [("1510 AI1 level 1",0),("1520 AI1 level 2",1),("1530 AI2 level 1",2),("1540 AI2 level 2",3),("1550 Alt RTD3 Warn.",4),("1560 Alt RTD3 Shutd.",5),("1570 Low Fuel level",6),("1580 High Fuel Level",7),
             ("AI1 Connect fail",8),("AI1 Sensor fail",9),("AI2 Connect fail",10),("AI2 Sensor fail",11),("RTD3 Connect fail",12),("RTD3 Sensor fail",13),("Fuel Level AI4 Connect fail",14),("Fuel Level AI4 Sensor fail",15)],
        71: [("1800 AI1 L1",0),("1810 AI1 L2",1),("1820 AI2 L1",2),("1830 AI2 L2",3),("1840 AI3 L3",4),("1850 AI3 L3",5),("AI4 L1",6),("AI4 L2",7),("Fuel level Connect fail",8), ("Fuel level Sensor fail",9)],
        72: [("1590 PT100.1 L1",0),("1600 PT100.1 L2",1),("1610 PT100.2 L1",2),("1620 PT100.2 L2",3),("1630 Tacho overspeed L1",4),("1640 Tacho overspeed L2",5), 
             ("1650 Tacho underspeed L1",6),("1660 Tacho underspeed L2",7)],
        73: [("1710 AVR Overvoltage DI45",0), ("1720 Excitation Loss DI46",1),("1730 Fuel Spillage DI47",2),("1740 Fan Fail DI117",3),("1750 Spare DI118",4), ("CB tripped DI23",5),
             ("1670 EmergStop DI24",6),("1680 Earth Leakage DI43",7),("1690 Spare Term.44",8),("1700 Air Flops Close DI27",9), ("Air flaps fail",10),("1760 High Water Temp DI91",11),
             ("1770 Low Oil Press. DI92",12),("1780 Low Water Press. DI93",13)],
        74: [("Engine stop failure",0),("Emergency stop failure",1),("Shutdown fail",2),("CB trip failure persist",3),("Start Fail",4)],
        79: [("External Communication Alarm",0)]
}
print("=== ОТЛАДКА alarm_bits ===")
for key, bits in alarm_bits.items():
    print(f"\nКлюч: {key}")
    print(f"  Тип: {type(bits)}")
    for i, item in enumerate(bits):
        print(f"  [{i}] {item} | тип: {type(item).__name__} | длина: {len(item) if hasattr(item, '__len__') else 'N/A'}")
all_alarm_names = [name for bits in alarm_bits.values() for name, _ in bits]

ensure_log_directories()
csv_files = {"analogs": None, "discretes": None, "alarms": None}
csv_writers = {"analogs": None, "discretes": None, "alarms": None}
current_hour_str = None

print("DEIF GEMPAC — CSV wide-format + Last poll time + Modbus errors")
time.sleep(1)
last_display = 0
try:
    while True:
        start_time = time.time()
        raw = block1_poll()
        processed = block2_convert(raw)
        last_poll_time = time.time() - start_time
        if time.time() - last_display > DISPLAY_INTERVAL:
            block3_display(processed)
            last_display = time.time()
        log_data_to_csv(processed, datetime.now())
        time.sleep(POLL_INTERVAL)
except KeyboardInterrupt:
    close_csv_files()
    print("\n\nОстановлено.")
