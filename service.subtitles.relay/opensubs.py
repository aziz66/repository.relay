"""Direct OpenSubtitles REST client for the no-id case.

External players (the Stremio app launching Kodi) hand over only a stream URL,
and the filename is sometimes useless (e.g. ``n.mkv``). When we can't derive an
IMDB id, we match subtitles to the *exact file* by OpenSubtitles moviehash -
which needs no id or filename. Credentials are reused from the configured
subtitle add-on (no extra setup).
"""

from __future__ import annotations

import json
import struct
from urllib.parse import unquote
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from relay import client, store

API = "https://api.opensubtitles.com/api/v1"
UA = "Kodi-Relay/1.0"          # OS rejects generic user-agents
CHUNK = 65536
_TOKEN_KEY = "os_token"


def _creds():
    """(api_key, username, password) parsed from a subtitle add-on's manifest
    URL config blob - local entries first, then Stremio-account add-ons."""
    urls = [e.get("manifestUrl", "") for e in store.load_entries()]
    try:
        from relay import stremio_api
        urls += [a.get("transportUrl", "")
                 for a in stremio_api.get_account_addons()]
    except Exception:  # noqa - signed out / module unavailable
        pass
    for raw in urls:
        u = unquote(raw)
        if "opensubtitles" not in u.lower():
            continue
        for seg in u.split("/"):
            seg = seg.strip()
            if seg.startswith("{") and "opensubtitles" in seg.lower():
                try:
                    c = json.loads(seg)
                    return (c.get("opensubtitlesApiKey"),
                            c.get("opensubtitlesUsername"),
                            c.get("opensubtitlesPassword"))
                except ValueError:
                    pass
    return None, None, None


def configured():
    return bool(_creds()[0])


def _req(method, path, key, body=None, token=None):
    headers = {"Api-Key": key, "User-Agent": UA, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(API + path, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=client.DEFAULT_TIMEOUT) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
    except HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8", "replace") or "{}")
        except Exception:  # noqa
            return exc.code, {}
    except (URLError, OSError, ValueError):
        return 0, {}


def _range(url, start, end):
    req = Request(url, headers={"User-Agent": UA,
                               "Range": "bytes=%d-%d" % (start, end)})
    with urlopen(req, timeout=client.DEFAULT_TIMEOUT) as r:
        data = r.read()
        cr = r.headers.get("Content-Range", "")  # "bytes 0-65535/<total>"
        total = int(cr.split("/")[-1]) if "/" in cr else 0
        return data, total


def moviehash(url):
    """OpenSubtitles hash (size + first/last 64KB) of a remote file, via HTTP
    range requests. Returns ("016x", size) or (None, 0)."""
    try:
        head, total = _range(url, 0, CHUNK - 1)
        if not total or total < CHUNK * 2 or len(head) < CHUNK:
            return None, 0
        tail, _ = _range(url, total - CHUNK, total - 1)
        if len(tail) < CHUNK:
            return None, 0
        h = total & 0xFFFFFFFFFFFFFFFF
        for d in (head, tail):
            for i in range(0, CHUNK, 8):
                h = (h + struct.unpack_from("<Q", d, i)[0]) & 0xFFFFFFFFFFFFFFFF
        return "%016x" % h, total
    except Exception:  # noqa
        return None, 0


def search_by_hash(url, langs2):
    """Return [{file_id, lang, release, hash_match}] for the file's moviehash.

    Result cached per (url, langs) so the dialog and the autodownload service
    don't each re-download the 128KB hash window + re-hit the OS search API."""
    key = _creds()[0]
    if not key:
        return []
    ck = "oshash::%s::%s" % (url, ",".join(sorted(set(langs2))))
    hit, val = client.disk_get(ck)
    if hit and val is not None:
        return val
    h, _size = moviehash(url)
    if not h:
        client.disk_set(ck, [], 300)  # un-hashable (no Range support) - don't retry
        return []
    return _search_hash(key, h, langs2, ck)


def _search_hash(key, h, langs2, ck):
    path = "/subtitles?moviehash=%s" % h
    if langs2:
        path += "&languages=" + ",".join(sorted(set(langs2)))
    st, body = _req("GET", path, key)
    if st != 200:
        return []
    out = []
    for it in body.get("data", []) or []:
        at = it.get("attributes", {}) or {}
        files = at.get("files") or []
        if not files or not files[0].get("file_id"):
            continue
        out.append({"file_id": files[0]["file_id"],
                    "lang": at.get("language", ""),
                    "release": at.get("release") or files[0].get("file_name", ""),
                    "hash_match": bool(at.get("moviehash_match"))})
    out.sort(key=lambda s: 0 if s["hash_match"] else 1)  # exact-file matches first
    client.disk_set(ck, out, 600)  # cache (incl. empty - 4K rarely has hash subs)
    return out


def _token(key, user, pw):
    hit, val = client.disk_get(_TOKEN_KEY)
    if hit and val:
        return val
    if not (user and pw):
        return None
    st, body = _req("POST", "/login", key, {"username": user, "password": pw})
    tok = body.get("token") if st == 200 else None
    if tok:
        client.disk_set(_TOKEN_KEY, tok, 36000)  # ~10h
    return tok


def download_link(file_id):
    """Resolve a one-time download URL for a file_id (needs a login token)."""
    key, user, pw = _creds()
    if not key:
        return None
    tok = _token(key, user, pw)
    st, body = _req("POST", "/download", key, {"file_id": file_id}, token=tok)
    if st in (401, 403):                      # token stale -> re-login once
        client.disk_set(_TOKEN_KEY, None, 1)
        tok = _token(key, user, pw)
        st, body = _req("POST", "/download", key, {"file_id": file_id}, token=tok)
    return body.get("link") if st == 200 else None
