# BlackBox vNext

BlackBox is being migrated from the legacy Flask/collector monolith to a
service-oriented platform. All new product code belongs in one of the vNext
trees:

- `platform/` - shared contracts and libraries;
- `services/` - Hub, Video and SCADA services;
- `workers/` - isolated device/protocol workers;
- `ui/` - the WebSocket-first user interface;
- `presets/` - versioned equipment and parsing maps;
- `deploy/` - Compose deployment and operational tooling.

The previous application is frozen under [`legacy/`](legacy/README.md). It is
kept runnable during the migration, but it accepts bug fixes only. Do not add
vNext features there.

## vNext Hub

The combined Phase 1+2 implementation is a FastAPI Hub with a separate
SQLite metadata database, Docker socket-proxy, hardened dynamic workers and
Parquet telemetry. The legacy application remains isolated under `legacy/` and
is not imported by the Hub.

Start the development stack:

```sh
chmod +x bbctl
./bbctl up
./bbctl smoke
```

Use `./bbctl help` for all available lifecycle commands. On Windows, run the
equivalent `./bbctl.ps1` commands from PowerShell. The Hub is available at
`http://127.0.0.1:8080` (override with `BB_HUB_PORT`). The bootstrap admin is
controlled by `BB_BOOTSTRAP_ADMIN_USERNAME` and
`BB_BOOTSTRAP_ADMIN_PASSWORD`; set a long random `BB_JWT_SECRET` outside the
lab default.

The UI is served by Jinja2/HTMX and vanilla JavaScript. Admins can create and
operate simulator or Modbus RTU VMs, publish immutable JSON maps, scan and
approve resources, and inspect worker logs. Users receive read-only dashboard,
status, tags and log access. Live status, tags, logs and alarms use
`/ws/v1/events`; no polling is used for those channels.

The internal worker API is under `/api/v1/internal/workers`. Workers send raw
batches to the bounded ingest queue; the Hub parses them and atomically writes
ZSTD-compressed Parquet under
`/data/telemetry/vm_id=<id>/date=<UTC-date>/`. SQLite stores metadata only.

## Migration status

- [x] Phase 0: repository split and deployment skeleton
- [x] Phase 1+2: versioned contracts, FastAPI Hub, roles, Docker orchestration, simulator and Modbus RTU worker
- [x] Phase 3: bounded ingest and Parquet/ZSTD storage
- [x] Phase 4: WebSocket UI, VM/resource/log administration
- [ ] Phase 5: Video, SCADA and equipment presets
- [ ] Phase 6: remove `legacy/`
