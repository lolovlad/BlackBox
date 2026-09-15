"""Stable import namespace for contracts stored in the ``platform/`` tree.

Python imports the stdlib module named ``platform`` very early in several
entrypoints (pytest, uvicorn and multiprocessing).  ``bb_platform`` avoids
that collision while keeping the repository layout required by the vNext
architecture.
"""

from pathlib import Path

__path__ = [str(Path(__file__).resolve().parent.parent / "platform")]

