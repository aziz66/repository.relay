"""Stremio addon-protocol client shared across the Kodi proxy addons."""

from . import _compat  # noqa: F401 - inject _scproxy stub before urllib.request (iOS/tvOS)

__version__ = "1.0.9"
