"""Stremio account API client: login, library (continue watching / watched)
sync, and the account's installed add-on collection.

Endpoints + data model verified against stremio-core (api.strem.io):
  - POST /api/login              {type:"Login", email, password} -> {result:{authKey,user}}
  - POST /api/datastoreGet       {authKey, collection:"libraryItem", ids, all}
  - POST /api/datastorePut       {authKey, collection:"libraryItem", changes:[item]}
  - POST /api/addonCollectionGet {type:"AddonCollectionGet", authKey, update}

Continue Watching = any libraryItem with state.timeOffset > 0. Watched threshold
is 70% (stremio-core WATCHED_THRESHOLD_COEF). Finishing an episode "advances"
the item: video_id = next episode, timeOffset = 1 (keeps the NEXT episode in
Continue Watching) - mirroring LibraryItem::advance_to_video.

The authKey is stored 0600 in the add-on profile; the password is never stored.
Nothing here ever raises into a player callback - all network errors return
empty/False results.
"""

from __future__ import annotations

import base64
import json
import os
import time
import zlib
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from . import store, ids, client

try:
    import xbmc
    import xbmcgui
    def log(msg, level=1):
        xbmc.log("[relay.stremio] " + msg, level)

    def _record_own_write(item):
        """Remember the last libraryItem WE wrote (id + its lastWatched) in a
        window prop, so recent_click can tell our own sync echo apart from a
        genuine app-side stream click (cross-addon: the subtitle service reads
        what the scrobbler wrote)."""
        try:
            lw = ((item.get("state") or {}).get("lastWatched")) or ""
            xbmcgui.Window(10000).setProperty(
                "relay.own_write", "%s|%s" % (item.get("_id") or "", lw))
        except Exception:  # noqa
            pass

    def _own_write():
        """(item_id, lastWatched_datetime) of our most recent write, or (None, None)."""
        try:
            raw = xbmcgui.Window(10000).getProperty("relay.own_write")
        except Exception:  # noqa
            return None, None
        if "|" not in raw:
            return None, None
        oid, _sep, lw = raw.partition("|")
        return (oid or None), _parse_iso(lw)
except ImportError:  # outside Kodi (tests)
    def log(msg, level=1):
        print("[stremio] " + msg)

    def _record_own_write(item):
        pass

    def _own_write():
        return None, None

API = "https://api.strem.io/api/"
TIMEOUT = 15
WATCHED_COEF = 0.7  # stremio-core WATCHED_THRESHOLD_COEF

# Stremio's preinstalled/official add-ons - skipped on import so e.g. Cinemeta
# doesn't duplicate every catalog AIOMetadata already provides.
OFFICIAL_IDS = {
    "com.linvo.cinemeta", "org.stremio.local", "org.stremio.opensubtitles",
    "org.stremio.opensubtitlesv3", "org.stremio.opensubtitles-pro",
    "com.linvo.stremiochannels", "org.stremio.watchhub",
}

_ADDONS_CACHE_KEY = "stremio_account_addons"


# ---------------------------------------------------------------------------
# Token storage (authKey only - the password is exchanged once and discarded)
# ---------------------------------------------------------------------------

def _token_path():
    return os.path.join(store._store_dir(), "stremio.json")


def _load():
    try:
        with open(_token_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save(tok):
    p = _token_path()
    tmp = p + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(tok, fh)
        os.replace(tmp, p)
    except OSError:
        pass


def authorized():
    return bool(_load().get("authKey"))


def auth_key():
    return _load().get("authKey") or ""


def account_email():
    return _load().get("email") or ""


def logout():
    try:
        os.remove(_token_path())
    except OSError:
        pass
    client.disk_set(_ADDONS_CACHE_KEY, None, 0)  # drop the account add-on cache


# ---------------------------------------------------------------------------
# HTTP (urllib; never raises)
# ---------------------------------------------------------------------------

def _post(path, body):
    try:
        data = json.dumps(body).encode("utf-8")
        req = Request(API + path, data=data,
                      headers={"Content-Type": "application/json",
                               "User-Agent": "Kodi-Relay/1.0"})
        with urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8", "replace") or "{}")
        except Exception:  # noqa
            return {}
    except (URLError, OSError, ValueError):
        return {}


def login(email, password):
    """Exchange credentials for an authKey. Returns (ok, error_message)."""
    resp = _post("login", {"type": "Login", "email": email, "password": password})
    result = resp.get("result") or {}
    key = result.get("authKey")
    if key:
        _save({"authKey": key,
               "email": (result.get("user") or {}).get("email") or email})
        log("signed in as %s" % (account_email() or "?"))
        return True, ""
    err = (resp.get("error") or {}).get("message") or "login failed (network?)"
    log("login failed: %s" % err, 3)
    return False, err


# ---------------------------------------------------------------------------
# Account add-on collection
# ---------------------------------------------------------------------------

def get_account_addons(force=False):
    """[{transportUrl, id, name}] for the account's installed add-ons
    (official defaults skipped). Disk-cached 1h; [] when signed out/offline."""
    if not authorized():
        return []
    if not force:
        hit, val = client.disk_get(_ADDONS_CACHE_KEY)
        if hit and val is not None:
            return val
    resp = _post("addonCollectionGet",
                 {"type": "AddonCollectionGet", "authKey": auth_key(),
                  "update": True})
    result = resp.get("result")
    if not isinstance(result, dict):
        return []  # transient failure - don't cache
    out = []
    for a in result.get("addons") or []:
        m = a.get("manifest") or {}
        url = a.get("transportUrl")
        if not url or m.get("id") in OFFICIAL_IDS:
            continue
        out.append({"transportUrl": url, "id": m.get("id", ""),
                    "name": m.get("name", "")})
    client.disk_set(_ADDONS_CACHE_KEY, out, 3600)
    log("account add-ons: %d (after skipping official)" % len(out))
    return out


# ---------------------------------------------------------------------------
# Library sync (continue watching + watched)
# ---------------------------------------------------------------------------

def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _get_item(lib_id):
    """(ok, item_or_None). ok=False means a transient API failure - in that case
    callers must SKIP the write, or they'd clobber server state with a fresh item."""
    resp = _post("datastoreGet", {"authKey": auth_key(),
                                  "collection": "libraryItem",
                                  "ids": [lib_id], "all": False})
    result = resp.get("result")
    if not isinstance(result, list):
        return False, None
    for it in result:
        if isinstance(it, dict) and it.get("_id") == lib_id:
            return True, it
    return True, None


def _put_item(item):
    resp = _post("datastorePut", {"authKey": auth_key(),
                                  "collection": "libraryItem",
                                  "changes": [item]})
    ok = "result" in resp
    if ok:
        _record_own_write(item)  # lets recent_click skip our own echo
    return ok


def _new_item(lib_id, ctype, meta):
    """A fresh library item the way Stremio itself creates continue-watching
    entries for un-libraried titles: temp + removed (shows in Continue Watching
    without adding to the visible library list)."""
    now = _now_iso()
    meta = meta or {}
    return {
        "_id": lib_id,
        "name": meta.get("name") or lib_id,
        "type": "series" if ctype == "series" else "movie",
        "poster": meta.get("poster"),
        "posterShape": "poster",
        "removed": True,
        "temp": True,
        "_ctime": now,
        "_mtime": now,
        "state": {
            "lastWatched": now, "timeWatched": 0, "timeOffset": 0,
            "overallTimeWatched": 0, "timesWatched": 0, "flaggedWatched": 0,
            "duration": 0, "video_id": None, "watched": None, "noNotif": False,
        },
        "behaviorHints": {},
    }


# Per-process item cache for the resident scrobbler: fetch the library item
# ONCE per playback and reuse it for every subsequent push - halves the API
# requests (no GET before each PUT). Single-slot (one playback at a time).
# MUST be reset at each playback start (reset_session) or a copy cached days
# ago could overwrite progress made on another device in the meantime.
_SESSION_ITEM = {}


def reset_session():
    """Forget the cached library item - call at every playback start so each
    playback begins from the server's current state."""
    _SESSION_ITEM.clear()


def sync_progress(ctype, content_id, time_s, total_s, meta=None):
    """Push the resume position -> the item appears in Continue Watching."""
    if not authorized() or not total_s or total_s <= 0:
        return False
    base = ids.base_id(content_id)
    item = _SESSION_ITEM.get(base)
    if item is None:
        ok, item = _get_item(base)
        if not ok:
            log("sync skipped (api unreachable) %s" % content_id, 2)
            return False
        if item is None:
            item = _new_item(base, ctype, meta)
        _SESSION_ITEM.clear()
        _SESSION_ITEM[base] = item
    st = item.setdefault("state", {})
    offset_ms, dur_ms = int(time_s * 1000), int(total_s * 1000)
    prev = int(st.get("timeOffset") or 0)
    if offset_ms > prev:  # accumulate forward progress like stremio-core does
        st["timeWatched"] = int(st.get("timeWatched") or 0) + (offset_ms - prev)
        st["overallTimeWatched"] = (int(st.get("overallTimeWatched") or 0)
                                    + (offset_ms - prev))
    st["timeOffset"] = max(offset_ms, 1)
    st["duration"] = dur_ms
    st["video_id"] = content_id if ":" in content_id else base
    st["lastWatched"] = _now_iso()
    item["_mtime"] = _now_iso()
    done = _put_item(item)
    _invalidate_library()
    log("sync %s @%d/%ds -> %s" % (content_id, time_s, total_s,
                                   "ok" if done else "fail"))
    return done


def _next_episode_id(content_id, meta):
    """The next episode's id from the meta's videos (None = it was the last)."""
    base, s, e = ids.split_series_id(content_id)
    if s is None:
        return None
    def num(v, k):
        x = v.get(k, 0)
        return int(x) if str(x).isdigit() else 0
    vids = [v for v in ((meta or {}).get("videos") or []) if num(v, "season") > 0]
    vids.sort(key=lambda v: (num(v, "season"), num(v, "episode")))
    cur = next((i for i, v in enumerate(vids)
                if v.get("id") == content_id
                or (num(v, "season") == s and num(v, "episode") == e)), None)
    if cur is None:
        return "%s:%d:%d" % (base, s, e + 1)  # meta unknown - naive fallback
    if cur + 1 < len(vids):
        v = vids[cur + 1]
        return v.get("id") or "%s:%d:%d" % (base, num(v, "season"), num(v, "episode"))
    return None  # finished the last available episode


_LIB_CACHE_KEY = "stremio_library"


def _invalidate_library():
    client.disk_set(_LIB_CACHE_KEY, None, 0)


def library_items(force=False):
    """The account's full libraryItem list (disk-cached 120s; [] when signed
    out/offline). Powers the Continue Watching row and watched indicators."""
    if not authorized():
        return []
    if not force:
        hit, val = client.disk_get(_LIB_CACHE_KEY)
        if hit and val is not None:
            return val
    resp = _post("datastoreGet", {"authKey": auth_key(),
                                  "collection": "libraryItem",
                                  "ids": [], "all": True})
    res = resp.get("result")
    if not isinstance(res, list):
        return []  # transient - don't cache
    client.disk_set(_LIB_CACHE_KEY, res, 300)  # 5 min: stay light on the API
    return res


def library_map():
    """{meta_id: libraryItem} for quick watched/progress lookups."""
    return {it.get("_id"): it for it in library_items()
            if isinstance(it, dict) and it.get("_id")}


def continue_watching(limit=30):
    """In-progress items, newest first (mirrors is_in_continue_watching)."""
    out = []
    for it in library_items():
        if not isinstance(it, dict) or it.get("type") == "other":
            continue
        if it.get("removed") and not it.get("temp"):
            continue
        if int((it.get("state") or {}).get("timeOffset") or 0) <= 0:
            continue
        out.append(it)
    out.sort(key=lambda i: str((i.get("state") or {}).get("lastWatched") or ""),
             reverse=True)
    return out[:limit]


def dismiss_cw(lib_id):
    """Remove an item from Continue Watching (timeOffset -> 0)."""
    ok, item = _get_item(lib_id)
    if not ok or item is None:
        return False
    item.setdefault("state", {})["timeOffset"] = 0
    item["_mtime"] = _now_iso()
    done = _put_item(item)
    _invalidate_library()
    return done


def watched_video_ids(item, ordered_video_ids):
    """Per-episode watched set from the item's WatchedField bitfield.

    Format (stremio-core): ``{anchor_video_id}:{anchor_length}:{b64(zlib(bytes))}``
    - the anchor id itself contains colons, so parse from the RIGHT. Bit i is
    ``bytes[i//8] >> (i%8) & 1``; a video at index j maps to bit ``j + offset``
    where ``offset = anchor_length - index_of(anchor_video) - 1``."""
    field = ((item or {}).get("state") or {}).get("watched")
    if not field or not ordered_video_ids:
        return set()
    try:
        parts = str(field).split(":")
        if len(parts) < 3:
            return set()
        raw = zlib.decompress(base64.b64decode(parts[-1]))
        anchor_length = int(parts[-2])
        anchor_video = ":".join(parts[:-2])
        if anchor_video not in ordered_video_ids:
            return set()
        offset = anchor_length - ordered_video_ids.index(anchor_video) - 1
        out = set()
        for j, vid in enumerate(ordered_video_ids):
            k = j + offset
            if 0 <= k < len(raw) * 8 and (raw[k // 8] >> (k % 8)) & 1:
                out.add(vid)
        return out
    except Exception:  # noqa - malformed field -> no checkmarks, never crash
        return set()


def _encode_watched_field(ordered_video_ids, watched_set):
    """Serialize a WatchedBitField exactly like stremio-core's
    ``WatchedBitField::Display`` + ``BitField8`` (zlib level 6 + base64):
    ``{anchor_video_id}:{anchor_len}:{b64(zlib(bytes))}`` where the anchor is
    the LAST watched video and ``anchor_len`` is its index+1. Verified to
    reproduce real account fields byte-for-byte and to round-trip through
    ``watched_video_ids`` (the anchor lets the reader offset-correct for any
    ordering shift). Returns None if nothing is watched / no videos."""
    if not ordered_video_ids:
        return None
    n = len(ordered_video_ids)
    vals = bytearray((n + 7) // 8)
    last = -1
    for i, vid in enumerate(ordered_video_ids):
        if vid in watched_set:
            vals[i // 8] |= 1 << (i % 8)
            last = i
    if last < 0:
        return None
    anchor = ordered_video_ids[last]
    packed = base64.b64encode(zlib.compress(bytes(vals), 6)).decode("ascii")
    return "%s:%d:%s" % (anchor, last + 1, packed)


def _ordered_episode_ids(content_id, meta):
    """Series episode ids in the canonical (season, episode) order Relay uses
    everywhere for the watched bitfield. Guarantees ``content_id`` is present
    (appended if the meta doesn't list it) so its bit can be set."""
    vids = [v for v in ((meta or {}).get("videos") or [])
            if isinstance(v, dict) and _num(v, "season") > 0 and v.get("id")]
    vids.sort(key=lambda v: (_num(v, "season"), _num(v, "episode")))
    ordered = [v["id"] for v in vids]
    if content_id not in ordered:
        ordered.append(content_id)
    return ordered


def _mark_video_watched(state, content_id, meta):
    """Set the per-episode watched bit in ``state['watched']`` (the bitfield
    that drives Stremio's episode checkmarks) - the piece stremio-core writes
    via ``watched.set_video(video_id, true)`` and Relay previously never did,
    so finished episodes never showed as watched. Preserves all existing bits."""
    ordered = _ordered_episode_ids(content_id, meta)
    seen = watched_video_ids({"state": state}, ordered)
    if state.get("watched") and not seen:
        # We have an existing bitfield but couldn't decode any bits with this
        # ordering (anchor video missing from a stale/partial meta). Re-encoding
        # now would WIPE the show's prior checkmarks - skip rather than destroy
        # history; this episode just misses its checkmark this once.
        return
    seen.add(content_id)
    field = _encode_watched_field(ordered, seen)
    if field:
        state["watched"] = field


def _parse_iso(s):
    """UTC datetime from a Stremio timestamp ('...T05:19:33.519849954Z' - the
    fraction can be 9 digits, which fromisoformat chokes on). None on garbage."""
    try:
        s = str(s).rstrip("Z")
        if "." in s:
            head, frac = s.split(".", 1)
            s = head + "." + (frac + "000000")[:6]
            return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f").replace(
                tzinfo=timezone.utc)
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def recent_click(max_age=120):
    """The library item the Stremio app touched within the last ``max_age``
    seconds - i.e. the title whose stream was JUST clicked.

    Verified live: the Stremio Android app syncs a library item to the account
    the instant a stream is clicked, even for external-player launches (movies
    AND series; for series only the show is recorded - state.video_id stays
    empty, so the episode must be resolved separately). This is what lets Relay
    identify external playback with no capture service and no usable filename.

    Always a FRESH datastoreGet (one request per external adoption; staleness
    here means misidentifying the playing title). Returns the raw libraryItem
    dict (newest lastWatched in the window) or None."""
    if not authorized():
        return None
    resp = _post("datastoreGet", {"authKey": auth_key(),
                                  "collection": "libraryItem",
                                  "ids": [], "all": True})
    res = resp.get("result")
    if not isinstance(res, list):
        return None
    now = datetime.now(timezone.utc)
    own_id, own_ts = _own_write()  # our scrobbler's last sync write
    best, best_ts = None, None
    for it in res:
        if not isinstance(it, dict) or it.get("type") not in ("movie", "series"):
            continue
        ts = _parse_iso((it.get("state") or {}).get("lastWatched"))
        if ts is None or not (-300 <= (now - ts).total_seconds() <= max_age):
            continue
        # Skip the echo of our OWN datastorePut (e.g. the just-stopped item's
        # final progress/watched write) - otherwise stopping item A right when
        # the user clicks item B in the app would adopt B's playback as A.
        # A LATER app-side click on the same item still wins (newer timestamp).
        if own_id and it.get("_id") == own_id and own_ts is not None \
                and ts <= own_ts:
            continue
        if best_ts is None or ts > best_ts:
            best, best_ts = it, ts
    return best


def likely_episode(item, meta):
    """(episode_id, exact) best guess for which episode of ``item`` the user
    just launched (the app never syncs the episode for external playback):

      1. the item's continue-watching pointer (state.video_id) - that is the
         exact episode Stremio's UI launches when continuing a show -> exact
         (only while timeOffset >= 1: offset 0 means finished/dismissed, where
         the pointer is the LAST episode, not the next launch)
      2. the first unwatched episode per the watched bitfield (same rule
         Stremio uses to pre-select an episode on the detail page) -> guess
      3. the meta's behaviorHints.defaultVideoId (the episode Stremio opens by
         default) -> guess
      4. the first regular episode (new show) -> guess

    (None, False) when the meta has no usable episode list."""
    st = (item or {}).get("state") or {}
    vid = str(st.get("video_id") or "")
    try:
        off = int(st.get("timeOffset") or 0)
    except (TypeError, ValueError):
        off = 0
    if vid and ":" in vid and off >= 1:
        return vid, True
    def num(v, k):
        x = v.get(k, 0)
        return int(x) if str(x).isdigit() else 0
    vids = [v for v in ((meta or {}).get("videos") or [])
            if isinstance(v, dict) and num(v, "season") > 0]
    vids.sort(key=lambda v: (num(v, "season"), num(v, "episode")))
    ordered = [v.get("id") for v in vids if v.get("id")]
    if not ordered:
        return None, False
    seen = watched_video_ids(item, ordered)
    for v in ordered:
        if v not in seen:
            return v, False
    # all watched -> the meta's default episode if it names one, else episode 1
    default_vid = ((meta or {}).get("behaviorHints") or {}).get("defaultVideoId")
    if default_vid in ordered:
        return default_vid, False
    return ordered[0], False


def playback_progress_pct(ctype, content_id):
    """Saved Continue-Watching percent for this exact item/episode, or None.
    Used as the resume-prompt fallback when Trakt has no progress. Reads from
    the shared (5-min cached) library fetch - no dedicated API request."""
    if not authorized():
        return None
    base = ids.base_id(content_id)
    item = library_map().get(base)
    if not item:
        return None
    st = item.get("state") or {}
    wanted = content_id if ":" in content_id else base
    if (st.get("video_id") or base) != wanted:
        return None  # the CW pointer is on a different episode
    dur, off = int(st.get("duration") or 0), int(st.get("timeOffset") or 0)
    if dur <= 0 or off <= 1:  # 1 = "advanced to next ep" marker, not progress
        return None
    return 100.0 * off / dur


def mark_watched(ctype, content_id, meta=None):
    """>=70% watched: movies get flagged watched and leave Continue Watching;
    episodes advance the pointer so the NEXT episode shows in Continue Watching
    (stremio-core advance_to_video semantics: video_id=next, timeOffset=1)."""
    if not authorized():
        return False
    base = ids.base_id(content_id)
    item = _SESSION_ITEM.pop(base, None)  # reuse the playback's cached item
    if item is None:
        ok, item = _get_item(base)
        if not ok:
            log("watched skipped (api unreachable) %s" % content_id, 2)
            return False
        if item is None:
            item = _new_item(base, ctype, meta)
    st = item.setdefault("state", {})
    st["timesWatched"] = int(st.get("timesWatched") or 0) + 1
    st["lastWatched"] = _now_iso()
    if ctype == "series":
        # Record the finished episode in the watched bitfield (the episode
        # checkmark) BEFORE advancing the pointer - stremio-core sets this bit
        # at the 70% mark; the later pointer-advance never touches it, so the
        # checkmark persists even though flaggedWatched resets to 0.
        _mark_video_watched(st, content_id, meta)
        st["overallTimeWatched"] = (int(st.get("overallTimeWatched") or 0)
                                    + int(st.get("timeWatched") or 0))
        st["timeWatched"] = 0
        st["flaggedWatched"] = 0
        nxt = _next_episode_id(content_id, meta)
        if nxt:
            st["video_id"] = nxt
            st["timeOffset"] = 1   # keeps the next episode in Continue Watching
        else:
            st["video_id"] = content_id
            st["timeOffset"] = 0   # series finished - drop from Continue Watching
    else:
        st["flaggedWatched"] = 1
        st["timeOffset"] = 0       # movie finished - drop from Continue Watching
    item["_mtime"] = _now_iso()
    done = _put_item(item)
    _invalidate_library()
    log("watched %s (next=%s) -> %s" % (content_id,
                                        st.get("video_id"),
                                        "ok" if done else "fail"))
    return done


# ---------------------------------------------------------------------------
# Air-date / released guard
# ---------------------------------------------------------------------------

def _video_by_id(meta, video_id):
    for v in (meta or {}).get("videos") or []:
        if isinstance(v, dict) and v.get("id") == video_id:
            return v
    return None


def is_released(meta, video_id):
    """True if the episode is out: ``available`` not explicitly False AND its
    ``released`` air date is not in the future. Unknown air date -> released."""
    v = _video_by_id(meta, video_id)
    if v is None:
        return True
    if v.get("available") is False:
        return False
    ts = _parse_iso(v.get("released"))
    if ts is None:
        return True
    return ts <= datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# New Episodes (aired + unwatched, from the account library)
# ---------------------------------------------------------------------------

_NEW_EP_CACHE_KEY = "stremio_new_episodes"


def _num(v, k):
    x = (v or {}).get(k, 0)
    return int(x) if str(x).isdigit() else 0


def new_episodes(limit=40, force=False):
    """Library series with an aired, unwatched episode waiting, newest activity
    first. Each: ``{_id, name, poster, episode_id, season, episode}``. Disk-
    cached 6h. [] when signed out."""
    if not authorized():
        return []
    if not force:
        hit, val = client.disk_get(_NEW_EP_CACHE_KEY)
        if hit and val is not None:
            return val
    from . import router  # lazy: router has no top-level stremio_api import
    series = []
    for it in library_items():
        if not isinstance(it, dict) or it.get("type") != "series":
            continue
        if it.get("removed") and not it.get("temp"):
            continue
        st = it.get("state") or {}
        if not (st.get("timesWatched") or st.get("timeOffset") or st.get("watched")):
            continue
        series.append(it)
    series.sort(key=lambda i: str((i.get("state") or {}).get("lastWatched") or ""),
                reverse=True)
    out = []
    for it in series:
        base = it.get("_id") or ""
        try:
            meta = router.get_meta("series", base)
        except Exception:  # noqa
            meta = None
        if not meta:
            continue
        vids = [v for v in (meta.get("videos") or [])
                if isinstance(v, dict) and _num(v, "season") > 0 and v.get("id")]
        vids.sort(key=lambda v: (_num(v, "season"), _num(v, "episode")))
        ordered = [v["id"] for v in vids]
        seen = watched_video_ids(it, ordered)
        nxt = None
        for v in vids:
            if v["id"] in seen:
                continue
            if not is_released(meta, v["id"]):
                break
            nxt = v
            break
        if not nxt:
            continue
        out.append({"_id": base, "name": meta.get("name") or it.get("name") or base,
                    "poster": meta.get("poster") or it.get("poster"),
                    "episode_id": nxt["id"], "season": _num(nxt, "season"),
                    "episode": _num(nxt, "episode")})
        if len(out) >= limit:
            break
    client.disk_set(_NEW_EP_CACHE_KEY, out, 6 * 3600)
    return out


# ---------------------------------------------------------------------------
# Account add-on collection write-back (explicit "Apply to account")
# ---------------------------------------------------------------------------

def account_collection_raw():
    """The account's FULL add-on collection objects (AddonCollectionGet). []."""
    if not authorized():
        return []
    resp = _post("addonCollectionGet",
                 {"type": "AddonCollectionGet", "authKey": auth_key(),
                  "update": True})
    result = resp.get("result")
    if not isinstance(result, dict):
        return []
    return [a for a in (result.get("addons") or []) if isinstance(a, dict)]


def set_account_addons(addons):
    """Replace the account's installed add-on collection (AddonCollectionSet)."""
    if not authorized():
        return False
    resp = _post("addonCollectionSet",
                 {"type": "AddonCollectionSet", "authKey": auth_key(),
                  "addons": addons})
    ok = bool(resp.get("result") is not None and not resp.get("error"))
    if ok:
        client.disk_set(_ADDONS_CACHE_KEY, None, 0)
    log("addonCollectionSet (%d add-ons) -> %s" % (len(addons),
                                                   "ok" if ok else "fail"))
    return ok
