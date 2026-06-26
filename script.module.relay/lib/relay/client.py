"""HTTP transport + Stremio manifest/resource access.

Pure-ish module: it imports ``xbmc`` only for logging and falls back to ``print``
when run outside Kodi (handy for testing the routing logic).
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import ssl
import time
import zlib
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

try:
    import xbmc  # noqa
    def log(msg, level=1):
        xbmc.log("[relay] " + msg, level)
except ImportError:  # running outside Kodi (tests)
    def log(msg, level=1):
        print("[relay] " + msg)

try:
    import xbmcvfs
    _KODI = True
except ImportError:  # running outside Kodi (tests)
    _KODI = False

DEFAULT_TIMEOUT = 20
MANIFEST_TIMEOUT = 5  # manifests are tiny; don't let one dead add-on stall a page
_USER_AGENT = "Kodi-Relay/1.0"

# Hard limits to defend against decompression bombs / runaway responses from a
# malicious or compromised configured addon.
MAX_COMPRESSED = 16 * 1024 * 1024    # 16 MB on the wire
MAX_DECOMPRESSED = 64 * 1024 * 1024  # 64 MB after gunzip

ALLOWED_SCHEMES = ("http", "https")
NEG_TTL = 60  # short cache for failures so a dead add-on doesn't block every page


# ---------------------------------------------------------------------------
# Cross-session cache: a single on-disk store (0600). Each Kodi click is a fresh
# process so an in-memory cache never helps; the disk cache makes browsing
# instant across navigations AND across Kodi restarts, keeps secret-bearing
# stream URLs out of the addon-readable Window properties, and is thread-safe
# (each key is its own file written atomically - no lock needed).
# ---------------------------------------------------------------------------

def _disk_dir():
    if _KODI:
        d = xbmcvfs.translatePath(
            "special://profile/addon_data/plugin.video.relay/cache/")
    else:
        d = os.path.join(os.getcwd(), "spcache")
    try:
        os.makedirs(d, exist_ok=True)
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _disk_path(key):
    return os.path.join(_disk_dir(),
                        hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".json")


def disk_get(key):
    """Return ``(hit, value)``; ``value`` may be None (a cached failure)."""
    try:
        with open(_disk_path(key), encoding="utf-8") as fh:
            exp, val = json.load(fh)
    except (OSError, ValueError):
        return False, None
    if exp < time.time():
        return False, None
    return True, val


def disk_set(key, val, ttl):
    path = _disk_path(key)
    tmp = "%s.%d.tmp" % (path, os.getpid())  # per-process tmp avoids cross-writes
    try:
        # 0600 from creation (not chmod-after) so a stream URL's debrid token is
        # never momentarily world-readable on a shared host.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump([time.time() + ttl, val], fh)
        os.replace(tmp, path)
    except OSError:
        return
    if random.random() < 0.05:  # amortise the listdir+sort (cheap on Android/SD)
        _disk_prune()


def _disk_prune(limit=400):
    """Keep the cache dir bounded; drop oldest files past the cap."""
    try:
        d = _disk_dir()
        files = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".json")]
        if len(files) <= limit:
            return
        files.sort(key=lambda p: os.path.getmtime(p))
        for p in files[:len(files) - limit]:
            try:
                os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


def redact_url(url):
    """Mask secret-bearing parts of a URL for logging.

    Stremio embeds per-user config (API keys, even passwords) in the path/query.
    We keep scheme+host and the short resource tail, masking long segments
    (uuids, config blobs) and any query string.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<url>"
    segs = ["***" if len(seg) > 24 else seg for seg in parts.path.split("/")]
    tail = "?***" if parts.query else ""
    return "%s://%s%s%s" % (parts.scheme, parts.netloc, "/".join(segs), tail)


def _ssl_context(verify=True):
    if verify:
        return None  # default verified context
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _gunzip_limited(data, limit):
    """Decompress a gzip stream, refusing to exceed ``limit`` bytes."""
    dec = zlib.decompressobj(31)  # 31 = gzip header autodetect
    out = dec.decompress(data, limit)
    if dec.unconsumed_tail:
        raise ValueError("gzip payload exceeds size limit")
    return out


# ---------------------------------------------------------------------------
# HTTP backend: a pooled requests.Session (keep-alive connection reuse, a real
# win on the parallel same-host fan-out to AIOStreams) with a urllib fallback so
# the lib still works without script.module.requests (and in tests). Both paths
# read the *compressed* body with a hard size cap to preserve bomb defence.
# ---------------------------------------------------------------------------

_HEADERS = {"User-Agent": _USER_AGENT, "Accept": "application/json",
            "Accept-Encoding": "gzip"}


def _read_capped(reader, cap):
    chunks, total = [], 0
    while True:
        b = reader(65536)
        if not b:
            break
        total += len(b)
        if total > cap:
            raise ValueError("response exceeds size limit")
        chunks.append(b)
    return b"".join(chunks)


try:
    import requests
    from requests.adapters import HTTPAdapter

    _SESSION = requests.Session()
    _ADAPTER = HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=0)
    _SESSION.mount("http://", _ADAPTER)
    _SESSION.mount("https://", _ADAPTER)
    FETCH_ERRORS = (ValueError, OSError, requests.exceptions.RequestException)

    def _fetch_raw(url, timeout, verify_ssl):
        resp = _SESSION.get(url, headers=_HEADERS, timeout=timeout,
                            verify=verify_ssl, stream=True)
        try:
            resp.raw.decode_content = False  # keep gzip so our cap applies
            raw = _read_capped(resp.raw.read, MAX_COMPRESSED)
            gz = resp.headers.get("Content-Encoding", "") == "gzip"
        finally:
            resp.close()
        return raw, gz
except ImportError:  # no requests (tests / minimal Kodi) -> urllib
    FETCH_ERRORS = (HTTPError, URLError, ValueError, OSError)

    def _fetch_raw(url, timeout, verify_ssl):
        req = Request(url, headers=_HEADERS)
        with urlopen(req, timeout=timeout, context=_ssl_context(verify_ssl)) as resp:
            raw = resp.read(MAX_COMPRESSED + 1)
            if len(raw) > MAX_COMPRESSED:
                raise ValueError("response exceeds size limit")
            gz = resp.headers.get("Content-Encoding") == "gzip"
        return raw, gz


def fetch_json(url, timeout=DEFAULT_TIMEOUT, verify_ssl=True, cache_ttl=0):
    """GET ``url`` and parse JSON. Returns a dict, or ``None`` on failure.

    Rejects non-http(s) schemes (no ``file://`` local reads), caps response and
    decompressed sizes, and never logs the raw (secret-bearing) URL.
    """
    if urlsplit(url).scheme not in ALLOWED_SCHEMES:
        log("blocked non-http url %s" % redact_url(url), 3)
        return None

    if cache_ttl:
        hit, val = disk_get(url)
        if hit:
            return val  # may be None (a cached failure)

    try:
        raw, is_gzip = _fetch_raw(url, timeout, verify_ssl)
        if is_gzip or raw[:2] == b"\x1f\x8b":  # honour gzip even if header is absent
            raw = _gunzip_limited(raw, MAX_DECOMPRESSED)
        data = json.loads(raw.decode("utf-8"))
    except FETCH_ERRORS as exc:
        log("fetch failed %s -> %s" % (redact_url(url), type(exc).__name__), 3)
        if cache_ttl:
            disk_set(url, None, NEG_TTL)  # negative cache
        return None

    if cache_ttl:
        disk_set(url, data, cache_ttl)
    return data


# ---------------------------------------------------------------------------
# Manifest handling
# ---------------------------------------------------------------------------

def manifest_base(manifest_url):
    """Strip the trailing ``/manifest.json`` to get the resource base URL.

    The per-user config segment in the path is left untouched and opaque.
    """
    if manifest_url.endswith("/manifest.json"):
        return manifest_url[: -len("/manifest.json")]
    return manifest_url.rstrip("/")


def normalize_resources(manifest):
    """Return a list of ``{name, types, idPrefixes}`` dicts.

    The protocol allows each entry in ``resources`` to be either a plain string
    (which inherits the manifest-level ``types`` / ``idPrefixes``) or a full
    object. For objects we distinguish "key absent" (inherit manifest defaults)
    from "key present but empty" (an explicit *no restriction*), so an addon can
    deliberately broaden one resource to all types/ids.
    """
    m_types = manifest.get("types", []) or []
    m_prefixes = manifest.get("idPrefixes", []) or []
    out = []
    for res in manifest.get("resources", []) or []:
        if isinstance(res, str):
            out.append({"name": res, "types": m_types, "idPrefixes": m_prefixes})
        elif isinstance(res, dict) and res.get("name"):
            out.append({
                "name": res["name"],
                "types": res["types"] if "types" in res else m_types,
                "idPrefixes": (res["idPrefixes"] if "idPrefixes" in res
                               else m_prefixes),
            })
    return out


def index_manifest(manifest_url, timeout=DEFAULT_TIMEOUT, verify_ssl=True):
    """Fetch a manifest and return a normalized addon descriptor, or None."""
    manifest = fetch_json(manifest_url, timeout, verify_ssl, cache_ttl=3600)
    if not manifest:
        return None
    return {
        "manifestUrl": manifest_url,
        "base": manifest_base(manifest_url),
        "id": manifest.get("id", manifest_url),
        "name": manifest.get("name", manifest.get("id", "Unknown addon")),
        "version": manifest.get("version", "?"),
        "logo": manifest.get("logo"),
        "types": manifest.get("types", []) or [],
        "catalogs": manifest.get("catalogs", []) or [],
        "resources": normalize_resources(manifest),
        "configurationRequired": bool(
            (manifest.get("behaviorHints") or {}).get("configurationRequired")),
        "enabled": True,
    }


# ---------------------------------------------------------------------------
# Resource URL building + requests
# ---------------------------------------------------------------------------

def _encode_extra(extra):
    """Encode an ``{k: v}`` dict into the Stremio extra path segment.

    e.g. ``{"skip": 100, "genre": "Action"}`` -> ``genre=Action&skip=100``.
    Values are fully percent-encoded (``safe=""``) so a ``/`` in a genre/search
    value cannot inject an extra path component.
    """
    if not extra:
        return None
    parts = []
    for key in sorted(extra):
        val = extra[key]
        if val is None or val == "":
            continue
        parts.append("%s=%s" % (quote(str(key), safe=""),
                                quote(str(val), safe="")))
    return "&".join(parts) if parts else None


def resource_url(base, resource, ctype, content_id, extra=None):
    """Build ``{base}/{resource}/{type}/{id}[/{extra}].json``."""
    path = "%s/%s/%s/%s" % (base, resource, quote(ctype, safe=""),
                            quote(content_id, safe=":"))
    enc = _encode_extra(extra)
    if enc:
        path += "/" + enc
    return path + ".json"


def get_resource(addon, resource, ctype, content_id, extra=None,
                 timeout=DEFAULT_TIMEOUT, verify_ssl=True, cache_ttl=0):
    """Call a single addon for one resource. Returns the parsed dict or None."""
    url = resource_url(addon["base"], resource, ctype, content_id, extra)
    log("GET %s" % redact_url(url), 0)  # LOGDEBUG - per-request noise; errors still log
    return fetch_json(url, timeout, verify_ssl, cache_ttl)
