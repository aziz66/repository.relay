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


# iOS/tvOS Kodi's Python has NO system CA bundle, so any default-context HTTPS
# via bare urllib (subtitle file downloads, Trakt, IntroDB, OpenSubtitles) fails
# TLS verification there - while requests works because it bundles certifi.
# Point ssl's default contexts at certifi's CA so bare-urllib HTTPS verifies too.
# No-op on platforms that already have a CA store (certifi is still a valid CA
# set). Safe-guarded so a missing certifi never breaks import.
try:
    import ssl as _ssl
    import certifi as _certifi

    _ca = _certifi.where()
    _orig_ctx = _ssl.create_default_context

    def _certifi_default_context(*a, **k):
        if not (k.get("cafile") or k.get("capath") or k.get("cadata")):
            k["cafile"] = _ca
        return _orig_ctx(*a, **k)

    _ssl.create_default_context = _certifi_default_context
    _ssl._create_default_https_context = _certifi_default_context
except Exception:  # noqa - never break import over a CA shim
    pass
