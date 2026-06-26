"""Minimal Trakt.tv client: device-code auth + scrobble (start/pause/stop).

Records in-progress playback ("currently watching") and watch history on Trakt
for anything played through the proxy. Content ids are IMDB (``tt...``), which
Trakt accepts directly, so no TMDB lookup is needed.

Tokens are stored 0600 in the add-on profile; client id/secret come from the
video plugin's settings.
"""

from __future__ import annotations

import json
import os
import time
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from . import store, ids, client

try:
    import xbmc
    def log(msg, level=1):
        xbmc.log("[relay.trakt] " + msg, level)
except ImportError:  # outside Kodi (tests)
    def log(msg, level=1):
        print("[trakt] " + msg)

API = "https://api.trakt.tv"
TIMEOUT = 15
DEFAULT_EXPIRY = 7776000  # 90 days


# ---------------------------------------------------------------------------
# HTTP (POST/GET with JSON) - requests if available, urllib fallback
# ---------------------------------------------------------------------------
try:
    import requests

    def _http(method, url, headers, body=None):
        try:
            r = requests.request(method, url, headers=headers, timeout=TIMEOUT,
                                 data=json.dumps(body) if body is not None else None)
            return r.status_code, (r.text or "")
        except Exception:  # noqa - network blip must not raise inside a player
            return 0, ""   # callback (matches the urllib fallback's behaviour)
except ImportError:
    def _http(method, url, headers, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=TIMEOUT) as resp:
                return resp.status, resp.read().decode("utf-8")
        except HTTPError as exc:
            try:
                return exc.code, exc.read().decode("utf-8", "replace")
            except Exception:  # noqa
                return exc.code, ""
        except (URLError, OSError):
            return 0, ""


# ---------------------------------------------------------------------------
# Credentials + token storage
# ---------------------------------------------------------------------------

def _setting(key):
    try:
        import xbmcaddon
        return xbmcaddon.Addon("plugin.video.relay").getSetting(key).strip()
    except Exception:  # noqa
        return ""


def _client_id():
    return _setting("trakt_client_id")


def _client_secret():
    return _setting("trakt_secret")


def _token_path():
    return os.path.join(store._store_dir(), "trakt.json")


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
        # 0600 from creation - holds the Trakt access/refresh tokens.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(tok, fh)
        os.replace(tmp, p)
    except OSError:
        pass


def is_authorized():
    return bool(_load().get("access_token"))


def logout():
    try:
        os.remove(_token_path())
    except OSError:
        pass


def _access_token():
    """Return a valid access token, refreshing if expired; or None."""
    tok = _load()
    at = tok.get("access_token")
    if not at:
        return None
    if tok.get("expires_at", 0) > time.time() + 60:
        return at
    rt = tok.get("refresh_token")
    if not rt:
        return at
    st, body = _http("POST", API + "/oauth/token",
                     {"Content-Type": "application/json"},
                     {"refresh_token": rt, "client_id": _client_id(),
                      "client_secret": _client_secret(),
                      "grant_type": "refresh_token",
                      "redirect_uri": "urn:ietf:wg:oauth:2.0:oob"})
    if st == 200:
        try:
            new = json.loads(body)
        except ValueError:
            return at
        new["expires_at"] = time.time() + new.get("expires_in", DEFAULT_EXPIRY)
        _save(new)
        return new.get("access_token")
    if st in (401, 403) or "invalid_grant" in (body or ""):
        # refresh token revoked/expired -> drop it so the user is prompted to
        # re-authorize instead of silently failing every scrobble forever.
        log("Trakt refresh rejected (%s); clearing tokens" % st, 3)
        logout()
        return None
    return at  # transient (network/5xx) - keep token, retry later


def _auth_headers():
    h = {"Content-Type": "application/json", "trakt-api-version": "2",
         "trakt-api-key": _client_id()}
    at = _access_token()
    if at:
        h["Authorization"] = "Bearer " + at
    return h


# ---------------------------------------------------------------------------
# Device-code OAuth
# ---------------------------------------------------------------------------

def device_code():
    """Start device auth -> dict with user_code/verification_url/interval, or None."""
    cid = _client_id()
    if not cid:
        return None
    st, body = _http("POST", API + "/oauth/device/code",
                     {"Content-Type": "application/json"}, {"client_id": cid})
    if st == 200:
        try:
            return json.loads(body)
        except ValueError:
            return None
    return None


def poll_token(device, should_cancel=None):
    """Poll oauth/device/token until authorized. Returns True on success."""
    cid, cs = _client_id(), _client_secret()
    code = device.get("device_code")
    interval = max(int(device.get("interval", 5)), 1)
    deadline = time.time() + int(device.get("expires_in", 600))
    while time.time() < deadline:
        if should_cancel and should_cancel():
            return False
        time.sleep(interval)
        st, body = _http("POST", API + "/oauth/device/token",
                         {"Content-Type": "application/json"},
                         {"code": code, "client_id": cid, "client_secret": cs})
        if st == 200:
            try:
                tok = json.loads(body)
            except ValueError:
                return False
            tok["expires_at"] = time.time() + tok.get("expires_in", DEFAULT_EXPIRY)
            _save(tok)
            return True
        if st == 429:        # slow down -> back off
            interval += 1
            continue
        if st == 400:        # pending -> keep polling
            continue
        return False         # 404/409/410/418 -> give up
    return False


# ---------------------------------------------------------------------------
# Scrobble
# ---------------------------------------------------------------------------

def _payload(ctype, content_id, progress):
    base = ids.base_id(content_id)
    if not base.startswith("tt"):
        return None  # Trakt-by-imdb only; skip kitsu/tmdb-only ids
    try:
        pct = max(0.0, min(100.0, float(progress)))
    except (TypeError, ValueError):
        pct = 0.0
    payload = {"progress": pct}
    if ctype == "series":
        _b, season, episode = ids.split_series_id(content_id)
        if season is None or episode is None:
            return None
        payload["show"] = {"ids": {"imdb": base}}
        payload["episode"] = {"season": season, "number": episode}
    else:
        payload["movie"] = {"ids": {"imdb": base}}
    return payload


def scrobble(action, ctype, content_id, progress):
    """POST scrobble/{start,pause,stop}. Trakt marks watched when stop >= 80%."""
    if action not in ("start", "pause", "stop"):
        return
    payload = _payload(ctype, content_id, progress)
    if payload is None:
        return
    if action in ("pause", "stop") and payload["progress"] < 1.0:
        return  # don't record a barely-started item
    headers = _auth_headers()
    if "Authorization" not in headers:
        return  # not authorized
    st, _body = _http("POST", "%s/scrobble/%s" % (API, action), headers, payload)
    log("scrobble/%s %s @%.1f%% -> %s" % (action, content_id, payload["progress"], st))
    if action == "stop":
        client.disk_set("trakt_playback", None, 0)  # progress changed - drop cache


def playback_progress(ctype, content_id):
    """Trakt's saved progress percent (float) for this item, or None.

    Reads /sync/playback so a freshly-opened item can offer 'resume vs restart'.
    The list is disk-cached 60s (and invalidated on scrobble stop) so binge
    playback doesn't re-fetch it on every episode start.
    """
    base = ids.base_id(content_id)
    if not base.startswith("tt"):
        return None
    hit, items = client.disk_get("trakt_playback")
    if not hit or items is None:
        headers = _auth_headers()
        if "Authorization" not in headers:
            return None
        st, body = _http("GET", API + "/sync/playback?limit=100", headers)
        if st != 200:
            return None
        try:
            items = json.loads(body)
        except ValueError:
            return None
        if isinstance(items, list):
            client.disk_set("trakt_playback", items, 60)
    if not isinstance(items, list):
        return None
    if ctype == "series":
        _b, season, episode = ids.split_series_id(content_id)
        for it in items:
            if it.get("type") != "episode":
                continue
            sh = (it.get("show") or {}).get("ids") or {}
            ep = it.get("episode") or {}
            if sh.get("imdb") == base and ep.get("season") == season \
                    and ep.get("number") == episode:
                return it.get("progress")
    else:
        for it in items:
            if it.get("type") != "movie":
                continue
            if ((it.get("movie") or {}).get("ids") or {}).get("imdb") == base:
                return it.get("progress")
    return None
