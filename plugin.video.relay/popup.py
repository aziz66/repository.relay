"""Relay minimal overlay popup (bottom-right card).

One reusable WindowXMLDialog behind every playback prompt (resume, skip
intro/recap, up next, movie end, tracking confirm) so they all share the same
small, media-friendly look instead of the skin's centered modal dialogs.

Semantics match the classic prompts: the PRIMARY button is the default - the
countdown bar drains over ``timeout_ms`` and selects it on expiry. Back/ESC
picks the passive option (``back`` = "primary"/"secondary").
"""

from __future__ import annotations

import threading

import xbmc
import xbmcaddon
import xbmcgui

ADDON = xbmcaddon.Addon()

# Back/ESC/Nav-back/Stop-ish actions that should dismiss the popup.
_CLOSE_ACTIONS = {9, 10, 92, 216, 247, 257, 275, 61467, 61448}
_BAR_WIDTH = 530  # must match DialogRelayPopup.xml


class RelayPopup(xbmcgui.WindowXMLDialog):
    def __init__(self, *args, **kwargs):
        super().__init__(*args)
        self._title = kwargs.get("title", "")
        self._subtitle = kwargs.get("subtitle", "")
        self._primary = kwargs.get("primary", "OK")
        self._secondary = kwargs.get("secondary", "Cancel")
        self._timeout = max(1, int(kwargs.get("timeout_ms", 10000)))
        self._back = kwargs.get("back", "secondary")
        self._focus = kwargs.get("focus", "primary")
        self._thumb = kwargs.get("thumb", "")
        self.result = True  # primary is the default (timeout) outcome
        self._done = threading.Event()

    # -- lifecycle -----------------------------------------------------------

    def onInit(self):
        self.getControl(101).setLabel(self._title)
        self.getControl(102).setLabel(self._subtitle)
        self.getControl(201).setLabel(self._primary)
        self.getControl(202).setLabel(self._secondary)
        if self._thumb:  # dimmed episode-still backdrop (Up Next), no resize
            try:
                img = self.getControl(105)
                img.setImage(self._thumb)
                img.setVisible(True)
            except Exception:  # noqa - control absent / image load failure
                pass
        # Focus may sit on the NON-default button (the timer already covers
        # the default, so the highlight is the one-press override).
        self.setFocusId(201 if self._focus == "primary" else 202)
        threading.Thread(target=self._countdown, daemon=True).start()

    def _countdown(self):
        """Drain the bottom bar; timeout selects the primary (default)."""
        mon = xbmc.Monitor()
        step = 0.1
        elapsed = 0.0
        total = self._timeout / 1000.0
        bar = self.getControl(301)
        while not self._done.is_set() and elapsed < total:
            if mon.waitForAbort(step):
                break
            elapsed += step
            try:
                bar.setWidth(max(0, int(_BAR_WIDTH * (1 - elapsed / total))))
            except Exception:  # noqa - window torn down mid-update
                return
        if not self._done.is_set():
            self.result = True  # timeout -> default
            self._finish()

    def _finish(self):
        self._done.set()
        try:
            self.close()
        except Exception:  # noqa
            pass

    # -- input ----------------------------------------------------------------

    def onClick(self, control_id):
        if control_id in (201, 202):
            self.result = control_id == 201
            self._finish()

    def onAction(self, action):
        if action.getId() in _CLOSE_ACTIONS:
            self.result = self._back == "primary"
            self._finish()


def ask(title, subtitle, primary, secondary, timeout_ms,
        back="secondary", focus="primary", thumb=""):
    """Show the popup; True = primary chosen (also on timeout)."""
    win = RelayPopup("DialogRelayPopup.xml", ADDON.getAddonInfo("path"),
                     "default", "1080i",
                     title=title, subtitle=subtitle, primary=primary,
                     secondary=secondary, timeout_ms=timeout_ms, back=back,
                     focus=focus, thumb=thumb)
    win.doModal()
    result = bool(win.result)
    del win
    return result
