"""vNext platform package.

The repository tree is intentionally named ``platform`` to match the
architecture document.  We proxy the standard-library :mod:`platform`
module's public attributes so third-party packages importing it continue to
work while ``platform.contracts`` remains available to vNext code.
"""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sysconfig

_stdlib_path = Path(sysconfig.get_paths()["stdlib"]) / "platform.py"
_spec = spec_from_file_location("_blackbox_stdlib_platform", _stdlib_path)
if _spec and _spec.loader:
    _stdlib_platform = module_from_spec(_spec)
    _spec.loader.exec_module(_stdlib_platform)
    for _name in dir(_stdlib_platform):
        if not _name.startswith("_"):
            globals().setdefault(_name, getattr(_stdlib_platform, _name))

del _name, _spec, _stdlib_path, _stdlib_platform, module_from_spec, spec_from_file_location, sysconfig, Path

