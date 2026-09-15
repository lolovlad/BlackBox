# Platform contracts

Shared vNext message contracts, map documents and the safe legacy-map parser
live here. Runtime services import these modules through `bb_platform`, which
avoids colliding with Python's standard-library module named `platform` while
keeping the required repository tree.

The contracts are versioned Pydantic models. All timestamps are normalized to
UTC, maps are checksum-addressed and immutable after publication, and parser
expressions are restricted to an AST allowlist with no Python builtins.
