# Hub

The Hub is the vNext FastAPI control plane. It owns the separate SQLite
metadata store, Argon2id/JWT browser sessions, role guards, immutable map
publication, resource discovery/approval, Docker lifecycle reconciliation,
bounded ingest, Tag Bus and WebSocket event stream.

Run locally with `uv run uvicorn services.hub.app:app --reload` or use
`deploy/compose.yaml`. Compose connects the Hub to Docker through
`docker-socket-proxy`; workers are created dynamically and receive no Docker
socket.

The `smoke` module is the same acceptance path used by `bbctl smoke`.
