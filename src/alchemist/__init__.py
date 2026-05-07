"""Alchemist — issue-driven transmuter for the Autumn Garage family."""

from __future__ import annotations

try:
    from alchemist._version import __version__
except ImportError:  # editable install before `hatch build` ran
    __version__ = "0.0.0+local"

__all__ = ["__version__"]
