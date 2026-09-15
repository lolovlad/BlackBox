"""Compatibility package for the legacy Flask application.

The implementation was moved to :mod:`legacy.src` as part of the vNext
repository split.  Keeping this small namespace shim preserves the existing
``src.*`` import paths while the lab deployment is migrated.
"""

from pathlib import Path

_LEGACY_SRC = Path(__file__).resolve().parent.parent / "legacy" / "src"
__path__ = [str(_LEGACY_SRC)]
