# Presets

Versioned equipment definitions and parsing maps live here. Published map
documents are checksum-addressed and immutable; the Hub accepts the legacy
`requests` + `fields` JSON shape and normalizes it into a `MapDocument`.

Checked-in examples under `maps/` are imported into the Hub metadata database
on startup. The `/admin/maps` page opens their complete JSON, including the
Modbus requests and fields used by each worker.
