"""Background scrobbler: Trakt + Stremio-account playback sync.

A resident xbmc.Player subclass that, for items played through this add-on
(and for external Stremio->Kodi playback once the subtitle service resolves
its id), reports:

  - Trakt: scrobble start/pause/stop (watch history + resume points)
  - Stremio account: continue-watching position + watched/advance-to-next
    (the Stremio app does NOT track external players at all - this fills that
    gap, so Kodi playback shows up in Stremio's Continue Watching row)

Identity comes from the stash written at resolve time (default.py:_stash_id),
or - for external playback - from the ``relay.external_*`` props the
subtitle service publishes after resolving the id via filename / capture / the Stremio account itself.
"""

from __future__ import annotations

import json
import re
import time

import xbmc
import xbmcgui
import xbmcaddon

from relay import trakt, stremio_api, introdb, ids as sids

ADDON = xbmcaddon.Addon()
HOME = xbmcgui.Window(10000)

# ~5 min between periodic Stremio pushes (pause/stop always push the exact
# position, so this is only crash insurance) - keeps api.strem.io traffic low.
SYNC_TICKS = 60


def _trakt_enabled():
    return ADDON.getSetting("trakt_scrobble") != "false"


def _stremio_enabled():
    return ADDON.getSetting("stremio_sync") != "false"


def _resume_enabled():
    return ADDON.getSetting("resume_prompt") != "false"


def _resume_default_start_over():
    """True when the user configured the resume prompt's timeout action to be
    'start from beginning' instead of resuming."""
    return ADDON.getSetting("resume_default") == "1"


def _skip_enabled():
    return ADDON.getSetting("skip_segments") != "false"


def _skip_auto():
    return ADDON.getSetting("skip_auto") == "true"


def _upnext_enabled():
    return ADDON.getSetting("upnext") != "false"


def _setting_int(key, default):
    try:
        return int(ADDON.getSetting(key))
    except (TypeError, ValueError):
        return default


def _upnext_tv_secs():
    return _setting_int("upnext_tv_secs", 40)


def _movie_end_enabled():
    return ADDON.getSetting("upnext_movie") == "true"


def _movie_end_secs():
    return _setting_int("upnext_movie_secs", 60)


def _kodi_setting(setting_id):
    """Read a Kodi *system* setting (e.g. services.webserver) via JSON-RPC.
    Returns None on any error."""
    try:
        resp = json.loads(xbmc.executeJSONRPC(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "Settings.GetSettingValue",
            "params": {"setting": setting_id}})))
        return resp.get("result", {}).get("value")
    except Exception:  # noqa
        return None


def _remote_client_active(webserver_port):
    """True if some client currently holds an ESTABLISHED TCP connection to
    Kodi's web server. Same-uid readable because Relay runs inside Kodi's
    process. Android-only signal - /proc/net/tcp doesn't exist on iOS/tvOS,
    where this just returns False (normal fallback runs)."""
    target = "%04X" % int(webserver_port)          # 8080 -> "1F90"
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as f:
                next(f, None)                      # skip header row
                for line in f:
                    cols = line.split()
                    if len(cols) < 4:
                        continue
                    local_addr, state = cols[1], cols[3]   # state 01 = ESTABLISHED
                    if state == "01" and local_addr.rsplit(":", 1)[-1].upper() == target:
                        return True
        except (OSError, ValueError):
            pass
    return False


def _arvio_remote_controlling(checks=3, gap=0.3):
    """True when an arvio Remote is actively driving Kodi from another device
    (it casts via Player.Open and keeps polling Kodi's web server while its
    Remote screen is open, including after Stop). Runs only at stop time - no
    background loop; the few retries absorb a momentary OkHttp socket reopen.
    No-ops to False when the web server is off (then there can be no remote)."""
    if _kodi_setting("services.webserver") is not True:
        return False
    port = _kodi_setting("services.webserverport") or 8080
    for i in range(checks):
        if _remote_client_active(port):
            return True
        if i < checks - 1:
            time.sleep(gap)
    return False


def _ask(title, subtitle, primary, secondary, timeout_ms, back="secondary",
         focus="primary", thumb=""):
    """Playback prompt -> True when the PRIMARY action is chosen (primary is
    always the timeout default; ``focus`` may highlight the secondary so one
    OK press is the override). Routes to the minimal bottom-right overlay or
    the classic skin yesno per the popup_style setting; any popup failure
    falls back to classic so a broken skin file can never kill a prompt."""
    if ADDON.getSetting("popup_style") != "1":
        try:
            import popup
            return popup.ask(title, subtitle, primary, secondary,
                             timeout_ms, back, focus, thumb)
        except Exception as exc:  # noqa
            xbmc.log("[relay] popup failed (%r) - classic fallback" % exc,
                     xbmc.LOGWARNING)
    # Classic: autoclose always returns False = the No button, so the timeout
    # default (primary) lives on No; defaultbutton only moves the FOCUS.
    line = title + ("\n" + subtitle if subtitle else "")
    return not xbmcgui.Dialog().yesno(
        "Relay", line, yeslabel=secondary, nolabel=primary,
        autoclose=timeout_ms,
        defaultbutton=(getattr(xbmcgui, "DLG_YESNO_NO_BTN", 10)
                       if focus == "primary"
                       else getattr(xbmcgui, "DLG_YESNO_YES_BTN", 11)))


class Scrobbler(xbmc.Player):
    def __init__(self):
        super().__init__()
        self.cid = None              # playing item's stremio id (any prefix)
        self.ctype = None
        self.meta = None             # cached meta (name/poster/videos) for sync
        self.trakt_on = False
        self.stremio_on = False
        self.progress = 0.0          # percent
        self.cur_time = 0.0          # seconds
        self.cur_total = 0.0
        self._abnormal_stop = False  # last stop looked like a broken/partial stream
        self._ticks = 0
        self._last_pushed = -1       # last synced position (skip no-op pushes)
        self._resume_pending = None  # (cid, pct) awaiting a settled player
        self._resume_offer = False   # show the per-stream resume prompt once
        self._resumed_to = None      # seconds we resumed to (mutes passed skips)
        self._external = False       # playback launched by the Stremio app
        self.segments = None         # introdb {intro/recap/outro: {start,end}}
        self._seg_fetch = False      # segments still to be fetched (run loop)
        self._seg_done = set()       # segment kinds already skipped/offered
        self._upnext_on = False
        self._upnext_shown = False
        self._upnext_declined = False
        self._movie_end_on = False
        self._movie_end_shown = False

    # -- identity -----------------------------------------------------------

    def _fresh_stash(self):
        ts = HOME.getProperty("relay.playing_ts")
        try:
            if not ts or time.time() - float(ts) > 120:
                return None, None
        except ValueError:
            return None, None
        # File binding: a stash written for another file must never identify
        # this one (launching B externally while A plays leaves A's stash live
        # until onAVStarted - seconds in which it would mislabel B).
        pf = HOME.getProperty("relay.playing_file")
        if pf:
            try:
                cur = (self.getPlayingFile() or "").split("|")[0]
            except Exception:  # noqa
                cur = ""
            if cur and pf.split("|")[0] != cur:
                return None, None
        return (HOME.getProperty("relay.playing_type"),
                HOME.getProperty("relay.playing_id"))

    @staticmethod
    def _clear_stash():
        """Drop the playing_* stash so the subtitle service (and we) can never
        reuse a previous item's id for a different playback."""
        for p in ("playing_id", "playing_type", "playing_filename",
                  "playing_videosize", "playing_file", "playing_ts"):
            HOME.clearProperty("relay." + p)

    @staticmethod
    def _clear_external(max_age=None):
        """Drop the external_* props. With ``max_age`` set, props younger than
        that survive: slow streams can buffer so long that the subtitle service
        resolves+publishes the id BEFORE onAVStarted fires - wiping those fresh
        props there killed adoption (no skip/resume/sync) for that playback."""
        if max_age is not None:
            try:
                ts = float(HOME.getProperty("relay.external_ts") or 0)
                if time.time() - ts <= max_age:
                    return  # freshly published for the starting file - keep
            except ValueError:
                pass
        for p in ("external_id", "external_type", "external_conf", "external_ts"):
            HOME.clearProperty("relay." + p)

    # -- player state -------------------------------------------------------

    def _pct(self):
        try:
            total = self.getTotalTime()
            return 100.0 * self.getTime() / total if total > 0 else self.progress
        except Exception:  # noqa - getTime invalid when not playing
            return self.progress

    def _snap(self):
        """Refresh progress/time/total while the player is still valid."""
        if self.isPlayingVideo():
            try:
                self.progress = self._pct()
                self.cur_time = self.getTime()
                self.cur_total = self.getTotalTime()
            except Exception:  # noqa
                pass

    def _expected_total(self):
        """Title runtime in seconds from meta (parses '1 h 47 min' / '23 min' /
        bare minutes); 0 if unknown. Used to sanity-check the stream's own
        reported duration."""
        rt = str((self.meta or {}).get("runtime") or "")
        if not rt:
            return 0
        h = re.search(r"(\d+)\s*h", rt)
        m = re.search(r"(\d+)\s*m", rt)
        secs = (int(h.group(1)) * 3600 if h else 0) + (int(m.group(1)) * 60 if m else 0)
        if not secs:
            d = re.search(r"\d+", rt)   # bare number = minutes
            secs = int(d.group(0)) * 60 if d else 0
        return secs

    def _is_abnormal_stop(self):
        """True when the stream's reported duration is far short of the title's
        real runtime - i.e. a partial/broken debrid stream that died early, not a
        genuine finish. Guards against false 'watched' + a spurious return-to-app.
        No-op (False) when the runtime is unknown."""
        exp = self._expected_total()
        return bool(exp and 0 < self.cur_total < exp * 0.6)

    # -- resume prompt (per stream, both local AND external playback) --------

    def _resume_progress(self):
        """Saved progress percent for the current item: Trakt first, Stremio
        account as fallback (works even without Trakt)."""
        pct = None
        try:
            if trakt.is_authorized():
                pct = trakt.playback_progress(self.ctype, self.cid)
        except Exception:  # noqa
            pct = None
        if pct is None:
            try:
                pct = stremio_api.playback_progress_pct(self.ctype, self.cid)
            except Exception:  # noqa
                pct = None
        return pct

    def _maybe_offer_resume(self):
        """Shown once per stream while it plays: 'Resume from N%?' with a 10s
        auto-close that defaults to RESUME (Kodi's yesno returns False on
        autoclose, so Resume is mapped to the No button + default focus)."""
        self._resume_offer = False
        cid_at = self.cid
        pct = self._resume_progress()
        if not pct or pct < 1.0 or pct >= 90.0:
            return  # nothing meaningful to resume / already basically watched
        if pct <= self._pct() + 1.5:
            return  # saved position is where we already are (often our own
                    # just-pushed progress echoed back) - nothing to gain
        xbmc.log("[relay] resume prompt %s @%.0f%%" % (cid_at, pct),
                 xbmc.LOGINFO)
        # The configured timeout default rides the primary button; the FOCUS
        # sits on the other one (the timer covers the default, the highlight
        # is the one-press override). Back always means "no seek".
        if _resume_default_start_over():
            resumed = not _ask("Resume from %d%%" % round(pct),
                               "Continue watching", "Start over", "Resume",
                               10000, back="primary", focus="secondary")
        else:
            resumed = _ask("Resume from %d%%" % round(pct),
                           "Continue watching", "Resume", "Start over",
                           10000, focus="secondary")
        if self.cid != cid_at:
            return  # playback changed/stopped while the prompt was up
        if not resumed:
            xbmc.log("[relay] resume: start-over for %s" % cid_at,
                     xbmc.LOGINFO)
            return
        self._resume_pending = (cid_at, float(pct))

    def _do_resume_seek(self):
        """Seek only once playback has settled (video rolling, duration known,
        >1s in) - never inside a player callback mid-surface-init."""
        if not self._resume_pending:
            return
        _rid, pct = self._resume_pending
        try:
            if not self.isPlayingVideo():
                return
            total = self.getTotalTime()
            if not total or total <= 0 or self.getTime() < 1.0:
                return
            target = total * pct / 100.0
            self.seekTime(target)
            # Resuming jumps past earlier segments - never offer to "skip" an
            # intro/recap the seek already cleared (popup right after the
            # resume prompt is pure noise). A resume INTO a segment keeps it.
            self._resumed_to = target
            self._mute_passed_segments()
            xbmc.log("[relay] resumed %s to %.0f%% (%ds of %ds)"
                     % (_rid, pct, target, total), xbmc.LOGINFO)
        except Exception:  # noqa
            pass
        self._resume_pending = None

    def _mute_passed_segments(self):
        """Mark intro/recap segments that end before the resumed position as
        already handled (called after the resume seek AND after a late
        segment fetch, whichever happens second)."""
        if self._resumed_to is None or not self.segments:
            return
        for kind in ("intro", "recap"):
            seg = self.segments.get(kind)
            if seg and seg["end"] <= self._resumed_to + 5:
                self._seg_done.add(kind)

    # -- IntroDB skip + Up Next ----------------------------------------------

    def _load_segments(self):
        """Fetch the episode's intro/recap/outro timestamps (cached on disk)."""
        self._seg_fetch = False
        segs = introdb.segments(self.cid or "")
        self.segments = segs or {}
        self._mute_passed_segments()  # resume seek may have run before fetch

    def _seg_near(self):
        """True while a segment boundary is imminent -> tighten loop to 1s."""
        if not self.segments or not self.cid:
            return False
        pos = self.cur_time
        for kind in ("intro", "recap"):
            seg = self.segments.get(kind)
            if seg and kind not in self._seg_done \
                    and seg["start"] - 12 <= pos < seg["end"]:
                return True
        outro = self.segments.get("outro")
        if outro and self._upnext_on and not self._upnext_shown \
                and outro["start"] - 12 <= pos:
            return True
        if self._movie_end_on and not self._movie_end_shown \
                and self.cur_total > 300 \
                and pos >= self.cur_total - _movie_end_secs() - 12:
            return True
        return False

    def _skip_segment(self, kind, seg):
        """Skip prompt (or auto-skip): seek past the segment's end."""
        if _skip_auto():
            try:
                self.seekTime(seg["end"])
                xbmcgui.Dialog().notification("Relay",
                                              "Skipped %s" % kind, time=2500)
                xbmc.log("[relay] auto-skipped %s (%ds-%ds)"
                         % (kind, seg["start"], seg["end"]), xbmc.LOGINFO)
            except Exception:  # noqa
                pass
            return
        cid_at = self.cid
        # Timeout default = Skip (primary); highlight sits on "Keep watching"
        # so one OK press cancels the skip, or wait it out to skip.
        skip = _ask("Skip %s" % kind, "%d:%02d - %d:%02d"
                    % (seg["start"] // 60, seg["start"] % 60,
                       seg["end"] // 60, seg["end"] % 60),
                    "Skip", "Keep watching", 8000, focus="secondary")
        if not skip or self.cid != cid_at or not self.isPlayingVideo():
            return
        try:
            if self.getTime() < seg["end"]:  # still inside - jump past it
                self.seekTime(seg["end"])
                xbmc.log("[relay] skipped %s -> %ds" % (kind, seg["end"]),
                         xbmc.LOGINFO)
        except Exception:  # noqa
            pass

    def _next_ep(self):
        """(next_episode_id, display_label) from the cached meta, or (None, '')."""
        try:
            nid = stremio_api._next_episode_id(self.cid, self.meta)
        except Exception:  # noqa
            nid = None
        if not nid:
            return None, ""
        try:  # don't offer/binge an episode that hasn't aired yet
            if not stremio_api.is_released(self.meta, nid):
                xbmc.log("[relay] next ep %s not aired - no Up Next" % nid,
                         xbmc.LOGINFO)
                return None, ""
        except Exception:  # noqa
            pass
        _b, s, e = sids.split_series_id(nid)
        label = (self.meta or {}).get("name") or "Next episode"
        if s is not None:
            label = "%s S%02dE%02d" % (label, s, e)
        for v in (self.meta or {}).get("videos") or []:
            if v.get("id") == nid and (v.get("title") or v.get("name")):
                label += " - " + (v.get("title") or v.get("name"))
                break
        return nid, label

    def _next_ep_art(self, nid):
        """(thumbnail, overview) for the next episode from cached meta."""
        for v in (self.meta or {}).get("videos") or []:
            if v.get("id") == nid:
                ov = (v.get("overview") or "").strip()
                if len(ov) > 110:
                    ov = ov[:107].rstrip() + "…"
                return (v.get("thumbnail") or ""), ov
        return "", ""

    def _offer_upnext(self):
        """Up Next popup at outro start (or near the end): 15s -> play next.
        Shows the next episode's thumbnail (dimmed backdrop) + overview."""
        self._upnext_shown = True
        nid, label = self._next_ep()
        if not nid:
            return
        thumb, overview = self._next_ep_art(nid)
        cid_at = self.cid
        # Timeout default = Play next (auto-binge); highlight sits on "Dismiss"
        # so one OK press stops the auto-advance, or wait it out to continue.
        play = _ask(label, overview or "Up Next", "Play next", "Dismiss",
                    15000, focus="secondary", thumb=thumb)
        if self.cid != cid_at:
            return
        if not play:
            self._upnext_declined = True
            return
        self._play_next(nid, label)

    def _play_next(self, nid, label):
        """Hand the next episode to our own plugin (streams are prefetched, so
        this is near-instant) - works for external playback too. Passes the
        current stream's bingeGroup so the next episode keeps the same
        source/quality/release-group."""
        self._external = False  # the next play is ours - don't bounce mid-binge
        self._upnext_declined = True  # one trigger per playback
        bg = HOME.getProperty("relay.playing_bingegroup")
        from urllib.parse import urlencode
        params = {"action": "play", "type": "series", "id": nid, "title": label}
        if bg:
            params["bg"] = bg
        url = "plugin://plugin.video.relay/?" + urlencode(params)
        xbmc.log("[relay] up next -> %s (bg=%s)" % (nid, bool(bg)),
                 xbmc.LOGINFO)
        xbmc.executebuiltin('PlayMedia(%s)' % url)

    def _watch_segments(self):
        """Per-tick: trigger skip prompts and the Up Next popup."""
        if not self.cid:
            return
        if self._resume_offer or self._resume_pending:
            return  # resume flow unresolved - the seek may jump past segments
        pos = self.cur_time
        if self.segments:
            for kind in ("recap", "intro"):
                seg = self.segments.get(kind)
                if not seg or kind in self._seg_done:
                    continue
                if seg["start"] <= pos < seg["end"] - 2:
                    self._seg_done.add(kind)
                    if _skip_enabled():
                        self._skip_segment(kind, seg)
        if self._upnext_on and not self._upnext_shown and self.cur_total > 120:
            outro = (self.segments or {}).get("outro")
            trigger = outro["start"] if outro \
                else self.cur_total - _upnext_tv_secs()
            if pos >= trigger:
                self._offer_upnext()
        if self._movie_end_on and not self._movie_end_shown \
                and self.cur_total > 300 \
                and pos >= self.cur_total - _movie_end_secs():
            self._movie_end_shown = True
            self._offer_movie_end()

    def _offer_movie_end(self):
        """Movies at the credits: offer to stop now (timer -> stop). Stopping
        marks watched (>=70%) and triggers return-to-Stremio if external."""
        cid_at = self.cid
        # Timeout default = Stop now (primary); highlight sits on "Keep
        # watching" so one OK press keeps playing, or wait it out to stop.
        stop = _ask("Movie ending", "Stop and mark watched",
                    "Stop now", "Keep watching", 15000, focus="secondary")
        if not stop or self.cid != cid_at:
            return
        xbmc.log("[relay] movie-end stop %s" % cid_at, xbmc.LOGINFO)
        try:
            self.stop()
        except Exception:  # noqa
            pass

    # -- begin / end --------------------------------------------------------

    def _fetch_meta(self):
        """Meta for the playing item (name/poster for item creation, videos for
        next-episode advance). Served from the shared disk cache normally."""
        try:
            from relay import router
            self.meta = router.get_meta(
                "series" if self.ctype == "series" else "movie",
                sids.base_id(self.cid)) or {}
        except Exception:  # noqa
            self.meta = {}

    def _begin(self, ctype, cid):
        self.ctype = ctype or ("series" if cid.count(":") >= 2 else "movie")
        self.cid = cid
        self._abnormal_stop = False
        self.progress = self._pct()
        self.cur_time = self.cur_total = 0.0
        self._ticks = 0
        self._last_pushed = -1
        self._resume_pending = None
        self._resumed_to = None
        self._resume_offer = _resume_enabled()  # per-stream prompt (toggle)
        self.trakt_on = (_trakt_enabled() and cid.startswith("tt")
                         and trakt.is_authorized())
        self.stremio_on = _stremio_enabled() and stremio_api.authorized()
        if self.ctype == "series" and cid.count(":") < 2:
            # Episode unresolved (bare show id from account identification):
            # never write show-level junk into watch history - marking a bare
            # series id watched would drop the whole show from Continue
            # Watching as "finished".
            self.trakt_on = self.stremio_on = False
        stremio_api.reset_session()  # start from the server's current state
        is_episode = self.ctype == "series" and cid.startswith("tt") \
            and cid.count(":") >= 2
        self.segments = None
        self._seg_done = set()
        self._seg_fetch = is_episode and (_skip_enabled() or _upnext_enabled())
        self._upnext_on = is_episode and _upnext_enabled()
        self._upnext_shown = self._upnext_declined = False
        self._movie_end_on = (self.ctype != "series") and _movie_end_enabled()
        self._movie_end_shown = False
        if self.stremio_on or self._upnext_on:
            self._fetch_meta()  # name/poster for sync + next-episode lookup
        if self.trakt_on:
            trakt.scrobble("start", self.ctype, cid, self.progress)

    def adopt_external(self, ctype, cid, conf="exact"):
        """Adopt an externally-resolved id (published by the subtitle service)
        so external Stremio playback gets Trakt + Stremio sync too.

        conf='guess' means the EPISODE was inferred (account identification
        names only the show for external launches) - confirm it with the user
        before any watch-history write, then republish the corrected id as the
        playing_* stash so subtitle searches pick it up too."""
        if self.cid or not cid:
            return
        if conf == "guess" and (ctype or "") == "series":
            cid = self._confirm_episode(cid)
            if not self.isPlayingVideo():
                return  # playback ended while the dialog was up
        # Republish as the playing_* stash (EVERY adoption, not just corrected
        # guesses): later subtitle searches then reuse this id for the whole
        # playback - the account's click record expires after ~2 min. Bound to
        # the playing file so it can never leak onto the next playback.
        try:
            cur_file = self.getPlayingFile() or ""
        except Exception:  # noqa
            cur_file = ""
        HOME.setProperty("relay.playing_id", cid)
        HOME.setProperty("relay.playing_type", ctype or "")
        HOME.setProperty("relay.playing_file", cur_file)
        HOME.setProperty("relay.playing_ts", str(time.time()))
        self._begin(ctype, cid)
        xbmc.log("[relay] adopted external playback %s %s (%s)"
                 % (self.ctype, cid, conf), xbmc.LOGINFO)

    def _confirm_episode(self, cid):
        """'Tracking as <Show> SxxEyy - correct?' 10s timeout accepts the guess
        (timeout/No-button = Correct, matching the other prompts); 'Choose
        episode' opens a picker built from the meta's episode list."""
        try:
            from relay import router
            meta = router.get_meta("series", sids.base_id(cid)) or {}
        except Exception:  # noqa
            meta = {}
        name = meta.get("name") or sids.base_id(cid)
        _b, s, e = sids.split_series_id(cid)
        label = ("%s S%02dE%02d" % (name, s, e)) if s is not None \
            else "%s (episode unknown)" % name
        # Back accepts the guess here (back must never open the picker).
        correct = _ask("Tracking as", label, "Correct", "Choose episode",
                       10000, back="primary")
        if correct:
            return cid
        def num(v, k):
            x = v.get(k, 0)
            return int(x) if str(x).isdigit() else 0
        vids = [v for v in (meta.get("videos") or [])
                if isinstance(v, dict) and num(v, "season") > 0]
        vids.sort(key=lambda v: (num(v, "season"), num(v, "episode")))
        if not vids:
            return cid
        labels, pre = [], 0
        for i, v in enumerate(vids):
            lbl = "S%02dE%02d" % (num(v, "season"), num(v, "episode"))
            t = v.get("title") or v.get("name")
            labels.append(lbl + (" - " + t if t else ""))
            if v.get("id") == cid:
                pre = i
        sel = xbmcgui.Dialog().select("Which episode?", labels, preselect=pre)
        if sel < 0:
            return cid
        v = vids[sel]
        return v.get("id") or "%s:%d:%d" % (sids.base_id(cid),
                                            num(v, "season"), num(v, "episode"))

    def _push_progress(self):
        if self.stremio_on and self.cid and self.cur_total > 0 \
                and 1.0 <= self.progress < 70.0:
            if int(self.cur_time) == self._last_pushed:
                return  # paused: same position - don't re-push every 2 min
            try:
                stremio_api.sync_progress(self.ctype, self.cid, self.cur_time,
                                          self.cur_total, self.meta)
                self._last_pushed = int(self.cur_time)
            except Exception:  # noqa
                pass

    def _stop(self, snap=True):
        # snap=False when called from onAVStarted: the NEW video is already
        # playing there, so snapping would overwrite the PREVIOUS item's final
        # progress with the new video's ~0% (the run loop keeps it 5s-fresh).
        if snap:
            self._snap()
        # A partial/broken debrid stream (still caching, wrong container) reports
        # a duration far short of the title's real runtime and can stop/"end"
        # early. Treat that as a stream failure, not a genuine finish: don't mark
        # watched, don't write a bogus resume, and don't bounce to the return-app.
        self._abnormal_stop = self._is_abnormal_stop()
        if self._abnormal_stop:
            xbmc.log("[relay] abnormal stop: stream total %ds << expected %ds - "
                     "skipping watched/progress/return"
                     % (int(self.cur_total), self._expected_total()), xbmc.LOGINFO)
        if self.trakt_on:
            trakt.scrobble("stop", self.ctype, self.cid, self.progress)
        if self.stremio_on and self.cid and not self._abnormal_stop:
            try:
                if self.progress >= stremio_api.WATCHED_COEF * 100.0:
                    stremio_api.mark_watched(self.ctype, self.cid, self.meta)
                else:
                    self._push_progress()
            except Exception:  # noqa
                pass
        self.trakt_on = self.stremio_on = False
        self.cid = None
        self.meta = None

    # -- player callbacks ----------------------------------------------------

    def onAVStarted(self):
        if self.cid:
            self._stop(snap=False)  # close out the previous item (no stale "watching")
        ctype, cid = self._fresh_stash()
        # External Stremio playback = an http(s) stream we didn't start.
        self._external = bool(not cid and self._is_stremio_url())
        if not cid:
            self._clear_stash()        # stale stash - never reuse
            self._clear_external(90)   # keep props published while buffering
            return
        self._begin(ctype, cid)

    def _is_stremio_url(self):
        # Covers every Stremio source (Comet :8000/playback, StremThru :8081,
        # AIOStreams...). Our own playback has a stash; library files are local
        # paths - neither lands here.
        try:
            f = self.getPlayingFile() or ""
        except Exception:  # noqa
            return False
        return f.startswith("http://") or f.startswith("https://")

    def onPlayBackPaused(self):
        self._snap()
        if self.trakt_on:
            trakt.scrobble("pause", self.ctype, self.cid, self.progress)
        self._push_progress()

    def onPlayBackResumed(self):
        if self.trakt_on:
            self.progress = self._pct()
            trakt.scrobble("start", self.ctype, self.cid, self.progress)

    def _return_to_stremio(self):
        if ADDON.getSetting("return_to_stremio") == "false" or self._abnormal_stop:
            # never bounce to the app on a broken/partial-stream stop (it looks
            # like the stream "ended" but the user is mid-watch).
            self._external = False
            return
        app = ADDON.getSetting("return_app").strip()
        if xbmc.getCondVisibility("System.Platform.Android"):
            # Android: only bounce back when Kodi was launched as an EXTERNAL
            # player by the Stremio app (the handoff scenario)...
            # ...but NOT when an arvio Remote is driving Kodi from a phone/tablet
            # (Cast mode also looks "external", yet returning to the app on the
            # Shield would yank Kodi away from the still-controlling remote).
            if self._external and not _arvio_remote_controlling():
                xbmc.executebuiltin("StartAndroidActivity(%s)"
                                    % (app or "com.stremio.one"))
        else:
            # iOS/tvOS: no external-player handoff exists (playback is in Relay),
            # so return after any stop. Kodi exposes no app-launch builtin here -
            # open the app's URL scheme via the Obj-C runtime (osapp).
            scheme = app if "://" in app else "stremio://"
            try:
                from relay import osapp
                osapp.open_url_scheme(scheme)
            except Exception as exc:  # noqa
                xbmc.log("[relay] return-to-app (tvOS) failed: %r" % exc,
                         xbmc.LOGWARNING)
        self._external = False

    def onPlayBackStopped(self):
        self._resume_pending = None
        self._resume_offer = False
        self._stop()
        self._clear_stash()
        self._clear_external()
        self._return_to_stremio()

    def onPlayBackEnded(self):
        # Natural end (credits finished): continue the binge automatically -
        # unless the user declined the Up Next prompt for this episode.
        nxt = label = None
        if self._upnext_on and not self._upnext_declined and self.cid:
            nxt, label = self._next_ep()
        self._resume_pending = None
        self._resume_offer = False
        self.progress = 100.0
        self._stop()
        self._clear_stash()
        self._clear_external()
        if nxt and not self._abnormal_stop:  # don't auto-binge off a broken stream
            self._play_next(nxt, label)
            return  # don't bounce to Stremio mid-binge
        self._return_to_stremio()


def run():
    monitor = xbmc.Monitor()
    player = Scrobbler()
    while not monitor.abortRequested():
        # Idle-light: 5s normally; 1s only while a resume offer/seek is pending
        # or a skip/Up Next boundary is imminent.
        busy = (player._resume_pending or player._resume_offer
                or player._seg_near())
        if monitor.waitForAbort(1 if busy else 5):
            break
        if player._resume_pending:
            player._do_resume_seek()
        if not player.isPlayingVideo():
            continue
        if player._resume_offer and player.cid:
            player._maybe_offer_resume()  # per-stream prompt (10s -> Resume)
        if player._seg_fetch and player.cid:
            player._load_segments()       # introdb intro/recap/outro (cached)
        if not player.cid:
            # External playback: adopt the id once the subtitle service has
            # resolved it (filename / capture helper / Stremio account).
            eid = HOME.getProperty("relay.external_id")
            ets = HOME.getProperty("relay.external_ts")
            try:
                fresh = bool(eid) and time.time() - float(ets or 0) < 600
            except ValueError:
                fresh = False
            # Only adopt for genuinely external (http, stash-less) playback -
            # otherwise a click in the Stremio app on the PHONE while Kodi
            # plays an unrelated local/id-less file would poison its identity.
            if fresh and player._external:
                etype = HOME.getProperty("relay.external_type") or None
                conf = HOME.getProperty("relay.external_conf") or "exact"
                for p in ("external_id", "external_type", "external_conf",
                          "external_ts"):
                    HOME.clearProperty("relay." + p)
                try:
                    player.adopt_external(etype, eid, conf)
                except Exception:  # noqa - junk meta must never kill the loop
                    pass
        else:
            player._snap()  # keep progress fresh for stop/skip/up-next timing
            player._watch_segments()      # skip prompts + Up Next popup
            if player.trakt_on or player.stremio_on:
                player._ticks += 1
                if player._ticks % SYNC_TICKS == 0:
                    player._push_progress()  # periodic continue-watching update
    del player


if __name__ == "__main__":
    run()
