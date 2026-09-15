"""Deterministic simulator worker package.

The executable lives in :mod:`workers.simulator.main`.  Keep this package
initializer side-effect free so ``python -m workers.simulator.main`` does not
import the module twice (which produces a noisy runpy warning in containers).
"""
