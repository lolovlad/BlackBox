# Workers

Protocol-specific device agents live here. `simulator` produces deterministic
analog/discrete samples for CI and lab runs; `modbus_rtu` reads a minimalmodbus
instrument with retries and a fake-instrument-friendly adapter. Both use the
same register/heartbeat/command protocol, send batches to Hub and never write
telemetry storage or receive a Docker socket.
