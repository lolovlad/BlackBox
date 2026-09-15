# Services

Runtime services for BlackBox vNext. The current implementation is the
FastAPI Hub in `hub/`; video and SCADA gateways remain reserved extension
points. Each service owns its runtime, dependencies, API and tests.
