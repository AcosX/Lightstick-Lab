# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-control contributors
"""Historical protocol API; waveform definitions live in protocols._common."""

from .protocols import _common

__all__ = [name for name in dir(_common) if not name.startswith('_')]


def __getattr__(name):
    return getattr(_common, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_common)))
