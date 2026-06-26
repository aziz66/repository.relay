"""IntroDB (introdb.app) client - crowdsourced intro/recap/outro timestamps.

GET https://api.introdb.app/segments?imdb_id=tt..&season=N&episode=N
  -> {intro|recap|outro: {start_sec, end_sec, confidence, submission_count} | null}

Used by the scrobbler service to offer "Skip intro/recap" and to time the
Up Next popup at the outro start. Results are disk-cached (segments don't
change often); 404 ("no data for this episode") is cached too so we don't
re-ask on every playback. Never raises.
"""

from __future__ import annotations

import json
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from . import client, ids

API = "https://api.introdb.app"
TIMEOUT = 8
HIT_TTL = 7 * 86400   # found segments: re-check weekly (community data refines)
MISS_TTL = 86400      # no data yet: re-check daily

try:
    import xbmc
    def log(msg, level=1):
        xbmc.log("[relay.introdb] " + msg, level)
except ImportError:  # outside Kodi (tests)
    def log(msg, level=1):
        print("[introdb] " + msg)


def _get(url):
    try:
        req = Request(url, headers={"User-Agent": "Kodi-Relay/1.0",
                                    "Accept": "application/json"})
        with urlopen(req, timeout=TIMEOUT) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
    except HTTPError as exc:
        return exc.code, {}
    except (URLError, OSError, ValueError):
        return 0, {}


def segments(content_id):
    """``{"intro": {"start": s, "end": s}, "recap": ..., "outro": ...}`` for a
    series episode id (``tt...:S:E``). Missing kinds are absent. ``{}`` = no
    data; ``None`` = transient API failure (not cached)."""
    base, season, episode = ids.split_series_id(content_id)
    if not base.startswith("tt") or season is None or episode is None:
        return {}
    key = "introdb::%s:%d:%d" % (base, season, episode)
    hit, val = client.disk_get(key)
    if hit and val is not None:
        return val
    st, body = _get("%s/segments?imdb_id=%s&season=%d&episode=%d"
                    % (API, base, season, episode))
    if st == 200 and isinstance(body, dict):
        out = {}
        for kind in ("intro", "recap", "outro"):
            seg = body.get(kind)
            if isinstance(seg, dict) and seg.get("end_sec") is not None:
                try:
                    start = float(seg.get("start_sec") or 0)
                    end = float(seg["end_sec"])
                except (TypeError, ValueError):
                    continue
                if end > start >= 0:
                    out[kind] = {"start": start, "end": end,
                                 "confidence": seg.get("confidence")}
        client.disk_set(key, out, HIT_TTL if out else MISS_TTL)
        if out:
            log("segments %s -> %s" % (content_id, sorted(out)))
        return out
    if st == 404:
        client.disk_set(key, {}, MISS_TTL)
        return {}
    return None  # transient - retry next playback


def submit(api_key, content_id, segment_type, start_sec, end_sec):
    """POST a community segment (X-API-Key auth). Returns (ok, message)."""
    base, season, episode = ids.split_series_id(content_id)
    if not api_key or not base.startswith("tt") or season is None:
        return False, "missing key or not an episode id"
    body = json.dumps({"segment_type": segment_type, "imdb_id": base,
                       "season": season, "episode": episode,
                       "start_sec": float(start_sec),
                       "end_sec": float(end_sec)}).encode("utf-8")
    try:
        req = Request(API + "/submit", data=body, method="POST",
                      headers={"X-API-Key": api_key,
                               "Content-Type": "application/json",
                               "User-Agent": "Kodi-Relay/1.0"})
        with urlopen(req, timeout=TIMEOUT) as r:
            return True, ""
    except HTTPError as exc:
        try:
            err = json.loads(exc.read().decode("utf-8", "replace")).get("error", "")
        except Exception:  # noqa
            err = "HTTP %s" % exc.code
        return False, err
    except (URLError, OSError) as exc:
        return False, type(exc).__name__
