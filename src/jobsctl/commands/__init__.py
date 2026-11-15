"""Command handlers: one module per command (or command group), imported automatically."""

from __future__ import annotations

import importlib
import pkgutil


def load_all() -> None:
    """Import every module of this package so its `@command` registrations run."""
    for module in pkgutil.iter_modules(__path__):
        importlib.import_module(f"{__name__}.{module.name}")
