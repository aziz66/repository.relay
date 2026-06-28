"""Launch another app from Kodi - platform helpers.

Android has the ``StartAndroidActivity`` builtin, but Kodi on iOS/tvOS exposes
no way for an add-on to open another app. tvOS *does* support URL schemes, and
``UIApplication.openURL:`` works - we just have to call it through the Obj-C
runtime via ctypes (and on the main thread, which UIKit requires). Verified
working on Apple TV (tvOS): ``open_url_scheme("stremio://")`` foregrounds Stremio.
"""

import ctypes


def open_url_scheme(url):
    """Open ``url`` (an app URL scheme, e.g. "stremio://") on iOS/tvOS via the
    Obj-C runtime. Returns True if the call was issued; never raises to callers
    that wrap it (it may raise if the runtime symbols are missing - caller guards)."""
    c = ctypes.CDLL(None)
    c.objc_getClass.restype = ctypes.c_void_p
    c.objc_getClass.argtypes = [ctypes.c_char_p]
    c.sel_registerName.restype = ctypes.c_void_p
    c.sel_registerName.argtypes = [ctypes.c_char_p]
    msg = c.objc_msgSend
    vp = ctypes.c_void_p

    def C(n):
        return c.objc_getClass(n)

    def S(n):
        return c.sel_registerName(n)

    msg.restype = vp
    msg.argtypes = [vp, vp, ctypes.c_char_p]
    s = msg(C(b"NSString"), S(b"stringWithUTF8String:"), url.encode("utf-8"))
    msg.argtypes = [vp, vp, vp]
    nsurl = msg(C(b"NSURL"), S(b"URLWithString:"), s)
    msg.argtypes = [vp, vp]
    app = msg(C(b"UIApplication"), S(b"sharedApplication"))
    if not (app and nsurl):
        return False
    # openURL: is a UIKit call -> must run on the main thread.
    msg.restype = None
    msg.argtypes = [vp, vp, vp, vp, ctypes.c_bool]
    msg(app, S(b"performSelectorOnMainThread:withObject:waitUntilDone:"),
        S(b"openURL:"), nsurl, False)
    return True
