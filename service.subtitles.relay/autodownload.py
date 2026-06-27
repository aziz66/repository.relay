"""Background auto-download service for Relay Subtitles.

Kodi has NO built-in "search subtitles on playback start" trigger for
xbmc.subtitle.module add-ons (verified against Kodi source: a subtitle search
only runs when the subtitle *dialog* is opened). So a passive subtitle module
can do manual search/download but never auto-downloads. Maintained add-ons
(e.g. a4kSubtitles) solve this with a background xbmc.service that watches
playback and fetches a subtitle itself. This is that service.

On each playing file, if the user's preferred subtitle language isn't already
present, it resolves the Stremio id (waiting for info labels to populate),
queries the configured subtitle add-ons, downloads the best match and loads it
via Player().setSubtitles().
"""

from __future__ import annotations

import json
import time

import xbmc
import xbmcaddon

from relay import router, store
import service as svc  # reuse current_video / _playing_filename / download_to_temp

ADDON = xbmcaddon.Addon()


def _enabled():
    return ADDON.getSetting("autodownload") != "false"


def _preferred_langs():
    """English names of the languages from 'Languages to download subtitles for'."""
    q = {"jsonrpc": "2.0", "id": 1, "method": "Settings.GetSettingValue",
         "params": {"setting": "subtitles.languages"}}
    try:
        res = json.loads(xbmc.executeJSONRPC(json.dumps(q)))
        vals = res.get("result", {}).get("value", []) or []
    except Exception:  # noqa
        vals = []
    out = set()
    for v in vals:
        out.add(xbmc.convertLanguage(v, xbmc.ENGLISH_NAME) or v)
    return out


def _have_preferred(player, pref):
    try:
        streams = player.getAvailableSubtitleStreams() or []
    except Exception:  # noqa
        streams = []
    for s in streams:
        if (xbmc.convertLanguage(s, xbmc.ENGLISH_NAME) or s) in pref:
            return True
    return False


def _resolve_id(mon, ticks):
    """Poll for a usable Stremio id; info labels lag a second or two at start."""
    for _ in range(ticks):
        ctype, cid = svc.current_video()
        if cid:
            return ctype, cid
        if mon.waitForAbort(1):
            break
    return None, None


def _fetch(player, ctype, cid, pref):
    subs = router.get_subtitles(ctype, cid, extra=svc.sub_extra(),
                                verify_ssl=svc.verify_ssl())

    def rank(s):
        eng = svc._english_name(s.get("lang", ""))
        return (0 if (eng and eng in pref) else 1, 0 if s.get("sync") else 1)

    for s in sorted(subs, key=rank):
        eng = svc._english_name(s.get("lang", ""))
        if pref and eng and eng not in pref:
            continue
        if not s.get("url"):
            continue
        path = svc.download_to_temp(s["url"], s.get("lang", ""))
        if path:
            player.setSubtitles(path)
            svc.log("auto-downloaded subtitle (%s)" % (eng or s.get("lang", "?")))
            return True
    return False


def _prefetch_next(ctype, cid):
    """Warm the next episode's stream cache so binge playback starts instantly.
    Resolves the real next id from meta (handles season boundaries) and skips
    episodes that haven't aired yet (no point prefetching, and it avoids the
    spurious next-episode AIOStreams/capture traffic)."""
    if ctype != "series" or ADDON.getSetting("prefetch_next") == "false":
        return
    try:
        from relay import stremio_api, ids as sids
        meta = router.get_meta("series", sids.base_id(cid),
                               verify_ssl=svc.verify_ssl())
        nxt = stremio_api._next_episode_id(cid, meta)
        if not nxt or not stremio_api.is_released(meta, nxt):
            return  # last episode, or the next one hasn't aired
    except Exception as exc:  # noqa
        svc.log("prefetch resolve error: %r" % exc)
        return
    # Mark what we're about to prefetch BEFORE querying: the prefetch can
    # reach a configured capture helper, whose last.json would otherwise name
    # the NEXT episode and mis-identify the current playback (echo guard in
    # service._is_prefetch_echo reads these props).
    svc.HOME.setProperty("relay.prefetch_id", nxt)
    svc.HOME.setProperty("relay.prefetch_ts", str(time.time()))
    try:
        router.get_streams("series", nxt, verify_ssl=svc.verify_ssl())
        svc.log("prefetched next episode %s" % nxt)
    except Exception as exc:  # noqa
        svc.log("prefetch error: %r" % exc)


def _prefetch_on():
    return ADDON.getSetting("prefetch_next") != "false"


ID_RETRY_WINDOW = 30   # s: keep retrying identification while services warm up


def run():
    mon = xbmc.Monitor()
    player = xbmc.Player()
    cur_file = None        # the file currently being tracked
    seen_at = 0.0          # when we first saw cur_file (bounds the retry window)
    done = False           # finished processing cur_file (identified + sub settled)
    warmed = False         # OSD enrich + next-episode prefetch already done
    oshash_done = False    # moviehash subtitle fallback already tried for cur_file
    last_gen = None
    while not mon.abortRequested():
        if mon.waitForAbort(2):
            break
        if not player.isPlayingVideo():
            cur_file = None
            continue
        if not (_enabled() or _prefetch_on()):  # both features off - idle
            cur_file = None
            continue
        if not xbmc.getCondVisibility("Player.HasDuration"):
            continue
        try:
            cur = player.getPlayingFile()
        except Exception:  # noqa
            continue

        if cur != cur_file:               # a new playback started
            cur_file = cur
            seen_at = time.time()
            done = False
            warmed = False
            oshash_done = False
        if done:
            continue

        # Only re-resolve the add-on list when it actually changed (add/remove/
        # toggle bumps the generation) - avoids a thread-pool spin-up every play.
        gen = store.generation()
        if gen != last_gen:
            router.reset_memo()
            last_gen = gen

        ctype, cid = _resolve_id(mon, 2)
        if cid:
            if not warmed:   # once per file: OSD enrich + next-episode prefetch
                warmed = True
                try:  # push real title + art to the OSD for external playback
                    svc.enrich_osd(player, ctype, cid)
                except Exception as exc:  # noqa
                    svc.log("enrich_osd error: %r" % exc)
                _prefetch_next(ctype, cid)  # warm next episode for instant binge
            if not _enabled():
                done = True
                continue
            pref = _preferred_langs()
            if not pref or _have_preferred(player, pref):
                done = True          # nothing to fetch, or already satisfied
                continue
            got = False
            try:
                got = _fetch(player, ctype, cid, pref)
            except Exception as exc:  # noqa - never kill the loop
                svc.log("autodownload error: %r" % exc)
            # RETRY until a preferred sub actually loads (or the window elapses).
            # On a binge into the next episode the subtitle add-on can be a beat
            # behind, so the FIRST get_subtitles can return nothing usable -
            # without this retry the episode keeps Kodi's embedded (often forced)
            # auto-pick instead of the user's language.
            if got or time.time() - seen_at >= ID_RETRY_WINDOW:
                done = True
            continue

        # No id yet. Run the OpenSubtitles moviehash subtitle fallback ONCE, then
        # keep retrying identification for a short window: a stream launched
        # seconds after Kodi booted can start before the add-on/account services
        # are ready, and we don't want to give up on that first miss.
        if not oshash_done:
            oshash_done = True
            if _enabled():
                pref = _preferred_langs()
                if pref and not _have_preferred(player, pref):
                    try:
                        svc.oshash_autodownload(player, pref)
                    except Exception as exc:  # noqa
                        svc.log("oshash autodownload error: %r" % exc)
        if time.time() - seen_at >= ID_RETRY_WINDOW:
            done = True   # window elapsed - stop retrying this file


if __name__ == "__main__":
    run()
