"""Cross-platform compatibility shims. Import this BEFORE any ``urllib.request``.

iOS/tvOS Kodi's bundled Python has no ``_scproxy`` module, yet CPython's
``urllib/request.py`` does a *module-level* ``from _scproxy import ...`` on Apple
platforms. So merely importing ``urllib.request`` raises
``ModuleNotFoundError: No module named '_scproxy'`` and crashes the whole add-on
on the Apple TV. Inject a harmless no-op stub so urllib imports cleanly - we
never use macOS system-proxy settings. On macOS the real module is used; on
Android/Linux the stub is injected but never consulted (their urllib doesn't
import _scproxy), so it's a no-op there.
"""

import sys

if "_scproxy" not in sys.modules:
    try:
        import _scproxy  # noqa: F401 - present on macOS, absent on iOS/tvOS
    except ImportError:
        import types

        _stub = types.ModuleType("_scproxy")
        _stub._get_proxies = lambda *a, **k: {}
        _stub._get_proxy_settings = lambda *a, **k: {"exclude_simple": True,
                                                     "exceptions": []}
        sys.modules["_scproxy"] = _stub
