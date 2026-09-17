# Workers

Protocol-specific device agents live here. `simulator` produces deterministic
analog/discrete samples for CI and lab runs; `modbus_rtu` reads a minimalmodbus
instrument and `modbus_tcp` reads the same map over the Modbus TCP MBAP
protocol. Both use the same register/heartbeat/command protocol, send batches
to Hub and never write telemetry storage or receive a Docker socket.
