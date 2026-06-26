"""Resource routing across all configured Stremio addons.

This is the heart of the proxy: given a resource (catalog/meta/stream/subtitles),
a content type and an id, it asks every configured addon that *declares* support
for that combination (resource name + type + idPrefix) and aggregates results.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from . import client, ids, store

MAX_WORKERS = 8


def _parallel(func, items):
    """Map ``func`` over ``items`` concurrently, preserving input order."""
    if not items:
        return []
    workers = min(MAX_WORKERS, len(items))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(func, items))


_ADDONS_MEMO = {}  # per-process: avoid re-resolving every manifest per aggregate call
_GEN = None


def _gen():
    """Cache generation (bumped on add/remove/toggle); fresh per process."""
    global _GEN
    if _GEN is None:
        _GEN = store.generation()
    return _GEN


def reset_memo():
    """Clear per-process memos so a long-lived process (the autodownload
    service) re-reads the add-on list/generation made during the session."""
    global _GEN
    _ADDONS_MEMO.clear()
    _GEN = None
    store.invalidate()


def _account_addons_enabled():
    """The 'Use add-ons from my Stremio account' toggle (video plugin setting)."""
    try:
        import xbmcaddon
        return (xbmcaddon.Addon("plugin.video.relay")
                .getSetting("stremio_account_addons") == "true")
    except Exception:  # noqa - outside Kodi / plugin missing
        return False


def _account_entries(include_disabled=False):
    """Store-style entries for the Stremio account's installed add-ons, with the
    user's LOCAL overrides applied (enable/disable + priority order - the
    account itself is never modified from Kodi)."""
    if not _account_addons_enabled():
        return []
    try:
        from . import stremio_api
        overrides = store.load_account_overrides()
        out = []
        for idx, a in enumerate(stremio_api.get_account_addons()):
            url = a.get("transportUrl")
            if not url:
                continue
            aid = store.entry_id(url)
            ov = overrides.get(aid) or {}
            enabled = bool(ov.get("enabled", True))
            if not enabled and not include_disabled:
                continue
            out.append({"manifestUrl": url, "enabled": enabled, "id": aid,
                        "name": a.get("name") or a.get("id") or "?",
                        "order": ov.get("order", idx), "_account": True})
        out.sort(key=lambda e: e["order"])
        return out
    except Exception:  # noqa - never let account import break local browsing
        return []


def _addons(verify_ssl=True, timeout=client.MANIFEST_TIMEOUT):
    """Resolve enabled entries into full descriptors (parallel, memoized).

    With the account toggle on, the user's Stremio-account add-ons are merged in
    (deduped against local entries by manifest URL, then by manifest id, so e.g.
    a locally-seeded AIOStreams that's also in the account appears once)."""
    if verify_ssl in _ADDONS_MEMO:
        return _ADDONS_MEMO[verify_ssl]
    entries = [e for e in store.load_entries()
               if e.get("enabled", True) and e.get("manifestUrl")]
    local_urls = {e["manifestUrl"] for e in entries}
    entries += [e for e in _account_entries()
                if e["manifestUrl"] not in local_urls]

    def resolve(entry):
        desc = client.index_manifest(entry["manifestUrl"], timeout, verify_ssl)
        if desc:
            desc["entryId"] = entry["id"]
            desc["account"] = bool(entry.get("_account"))
        return desc

    result = [d for d in _parallel(resolve, entries) if d]
    local_ids = {d["id"] for d in result if not d.get("account")}
    result = [d for d in result
              if not (d.get("account") and d["id"] in local_ids)]
    _ADDONS_MEMO[verify_ssl] = result
    return result


def addon_by_id(addon_id, verify_ssl=True):
    """Resolve a single addon descriptor by its store entry id, or None.

    Falls back to the Stremio-account add-ons (their entry ids aren't in the
    local store) so account catalogs can be browsed/paged."""
    entry = store.find(addon_id)
    if entry and not entry.get("enabled", True):
        return None
    if not entry:
        entry = next((e for e in _account_entries() if e["id"] == addon_id), None)
        if not entry:
            return None
    desc = client.index_manifest(entry["manifestUrl"], client.MANIFEST_TIMEOUT,
                                 verify_ssl)
    if desc:
        desc["entryId"] = entry["id"]
    return desc


def supports(addon, resource, ctype, content_id=None):
    """True if ``addon`` declares ``resource`` for this type (and id prefix)."""
    for res in addon["resources"]:
        if res["name"] != resource:
            continue
        if res["types"] and ctype not in res["types"]:
            continue
        if content_id is not None and not ids.id_matches_prefixes(
                content_id, res["idPrefixes"]):
            continue
        return True
    return False


def providers(resource, ctype, content_id=None, verify_ssl=True,
              timeout=client.MANIFEST_TIMEOUT):
    """Return the addons that can serve ``(resource, type, id)``."""
    return [a for a in _addons(verify_ssl, timeout)
            if supports(a, resource, ctype, content_id)]


# ---------------------------------------------------------------------------
# Aggregate helpers used by the consumer addons
# ---------------------------------------------------------------------------

# Cache the *aggregated* results across plugin invocations. The critical one is
# streams: the stream list is built once when browsing sources, then re-used
# (not re-scraped) when the user picks one to play - eliminating a second
# 7-10s scrape on playback.
STREAM_TTL = 180     # short: debrid links expire; just bridges browse -> play
META_TTL = 86400     # title metadata is static -> cache a day (instant re-open)
CATALOG_TTL = 3600   # discover lists change slowly -> 1h (instant re-browse)
SUBTITLE_TTL = 300


def _cached(key, ttl, producer):
    hit, val = client.disk_get(key)
    if hit and val is not None:
        return val
    val = producer()
    if val:  # don't cache empty/failed results (allow a quick retry)
        client.disk_set(key, val, ttl)
    return val


def list_catalogs(verify_ssl=True):
    """Return ``(addon, catalog)`` for every catalog across enabled addons."""
    result = []
    for addon in _addons(verify_ssl):
        for cat in addon["catalogs"]:
            result.append((addon, cat))
    return result


def get_catalog(addon, ctype, catalog_id, extra=None, verify_ssl=True):
    """Return the ``metas`` list for one catalog (empty list on failure)."""
    key = "cat::%d::%s::%s::%s::%s" % (
        _gen(), addon.get("entryId") or addon["base"], ctype, catalog_id,
        sorted((extra or {}).items()))

    def produce():
        data = client.get_resource(addon, "catalog", ctype, catalog_id, extra,
                                   verify_ssl=verify_ssl)
        return (data or {}).get("metas", []) or []

    return _cached(key, CATALOG_TTL, produce)


def get_meta(ctype, content_id, verify_ssl=True):
    """Fetch detailed meta from the first addon that can provide it."""
    def produce():
        for addon in providers("meta", ctype, content_id, verify_ssl):
            data = client.get_resource(addon, "meta", ctype, content_id,
                                       verify_ssl=verify_ssl)
            meta = (data or {}).get("meta")
            if meta:
                return meta
        return None

    return _cached("meta::%d::%s::%s" % (_gen(), ctype, content_id), META_TTL,
                   produce)


def get_streams(ctype, content_id, verify_ssl=True):
    """Aggregate streams from every serving addon (parallel), cached so picking
    a source to play does NOT re-scrape. Each stream gets an ``_addon`` key.

    Stream URLs are debrid/secret-bearing, so this uses an on-disk 0600 cache
    (NOT the addon-readable Window property used for catalogs/meta)."""
    key = "str::%d::%s::%s" % (_gen(), ctype, content_id)
    hit, val = client.disk_get(key)
    if hit and val is not None:
        return val

    provs = providers("stream", ctype, content_id, verify_ssl)

    def fetch(addon):
        data = client.get_resource(addon, "stream", ctype, content_id,
                                   verify_ssl=verify_ssl)
        out = []
        for s in (data or {}).get("streams", []) or []:
            s = dict(s)
            s["_addon"] = addon["name"]
            out.append(s)
        return out

    streams = []
    for chunk in _parallel(fetch, provs):
        streams.extend(chunk)
    if streams:
        client.disk_set(key, streams, STREAM_TTL)
    return streams


def get_subtitles(ctype, content_id, extra=None, verify_ssl=True):
    """Aggregate subtitles from every addon that serves them (parallel, cached)."""
    def produce():
        provs = providers("subtitles", ctype, content_id, verify_ssl)

        def fetch(addon):
            data = client.get_resource(addon, "subtitles", ctype, content_id,
                                       extra, verify_ssl=verify_ssl)
            out = []
            for sub in (data or {}).get("subtitles", []) or []:
                sub = dict(sub)
                sub["_addon"] = addon["name"]
                out.append(sub)
            return out

        subs = []
        for chunk in _parallel(fetch, provs):
            subs.extend(chunk)
        return subs

    key = "sub::%d::%s::%s::%s" % (_gen(), ctype, content_id,
                                   (extra or {}).get("filename", ""))
    return _cached(key, SUBTITLE_TTL, produce)
