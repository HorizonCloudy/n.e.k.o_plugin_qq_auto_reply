"""Scoped fake clocks for plugin tests.

Patch the importing module's ``time`` name instead of mutating the process-wide
stdlib module, because the host test suite runs background tasks in parallel.
"""

import time as _real_time


class _ScopedTime:
    def __init__(self, **overrides):
        self._overrides = overrides

    def __getattr__(self, name):
        try:
            return self._overrides[name]
        except KeyError:
            return getattr(_real_time, name)


def patch_module_clock(monkeypatch, module, **overrides):
    """Replace only ``module.time`` with a clock having the given overrides."""

    monkeypatch.setattr(module, "time", _ScopedTime(**overrides))
