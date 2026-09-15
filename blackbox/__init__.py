"""Compatibility namespace for the legacy data logger.

New vNext code must live under ``platform/``, ``services/``, ``workers/`` or
``ui/``.  The implementation remains available from ``legacy/blackbox`` until
the Hub and worker services replace it.
"""

from pathlib import Path

_LEGACY_PACKAGE = Path(__file__).resolve().parent.parent / "legacy" / "blackbox"
__path__ = [str(_LEGACY_PACKAGE)]

from .data_logger import DataLogger  # noqa: E402
from .config import DataLoggerConfig, AlarmCondition, DataFormat  # noqa: E402
from .discrete_inputs import DiscreteInputs  # noqa: E402
from .analog_inputs import AnalogInputs  # noqa: E402
from .data_writer import DataWriter, AlarmWriter  # noqa: E402

try:
    from modbus_acquire.instrument import read_all_data
except Exception:  # pragma: no cover - compatibility fallback
    def read_all_data(*args, **kwargs):
        raise RuntimeError(
            "Modbus-модуль недоступен. Установите зависимость 'minimalmodbus'."
        )


__version__ = "1.0.0"
__all__ = [
    "DataLogger",
    "DataLoggerConfig",
    "AlarmCondition",
    "DataFormat",
    "DiscreteInputs",
    "AnalogInputs",
    "DataWriter",
    "AlarmWriter",
    "read_all_data",
]
