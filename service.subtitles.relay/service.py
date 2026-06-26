"""Relay Subtitles - Kodi subtitle service.

Kodi drives subtitle addons with two actions on the plugin URL:

    ?action=search&languages=<names>&preferredlanguage=<name>
    ?action=download&url=<subtitle-url>&...

We reconstruct the playing item's Stremio id from VideoPlayer info labels (or a
window property the video plugin stashes for non-IMDB content), ask every addon
that declares a 'subtitles' resource (via the shared library and the same addon
list as plugin.video.relay), and hand results back.
"""

from __future__ import annotations

import gzip
import io
import os
import re
import shutil
import ssl
import sys
import tempfile
import time
import zipfile
from urllib.parse import urlencode, parse_qsl, urlsplit
from urllib.request import Request, urlopen

import xbmc
import xbmcgui
import xbmcplugin
import xbmcvfs
import xbmcaddon

from relay import router, client, ids

ADDON = xbmcaddon.Addon()
# argv[1] is the handle for the subtitle-module entry point, but this file is
# also imported by the background autodownload service (no handle) - guard it.
HANDLE = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].lstrip("-").isdigit() else -1
BASE_URL = sys.argv[0] if sys.argv else ""
PROFILE = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
HOME = xbmcgui.Window(10000)

SIZE_LIMIT = client.MAX_DECOMPRESSED  # cap extracted subtitle size


def verify_ssl():
    """Share the SSL preference with the video plugin if it's installed."""
    try:
        other = xbmcaddon.Addon("plugin.video.relay")
        return other.getSetting("verify_ssl") != "false"
    except Exception:
        return True


def log(msg):
    xbmc.log("[service.subtitles.relay] " + msg, xbmc.LOGINFO)


# ---------------------------------------------------------------------------
# Identify the playing item
# ---------------------------------------------------------------------------

def current_video():
    """Return ``(ctype, stremio_id)`` for what's playing, or ``(None, None)``.

    PREFERS the id the video plugin stashed at play time - it is the EXACT
    ``tt:S:E`` (+ type) we resolved. This matters for series: Kodi's
    VideoPlayer.TVShowTitle/Season/Episode info labels are usually EMPTY for
    plugin-resolved playback, so the old "trust the live IMDB number" path saw
    only the show id and treated every episode as a movie -> the provider then
    returned subtitles for the whole show (all seasons mixed). We only ignore the
    stash when the live IMDB id clearly points at *different* content (non-proxy
    playback through another add-on).
    """
    imdb = xbmc.getInfoLabel("VideoPlayer.IMDBNumber").strip()
    pid = HOME.getProperty("relay.playing_id").strip()
    if pid and not _stash_file_mismatch() \
            and (not imdb.startswith("tt") or imdb == pid.split(":")[0]):
        ptype = (HOME.getProperty("relay.playing_type").strip()
                 or ("series" if pid.count(":") >= 2 else "movie"))
        return ptype, pid

    if imdb.startswith("tt"):
        season = xbmc.getInfoLabel("VideoPlayer.Season").strip()
        episode = xbmc.getInfoLabel("VideoPlayer.Episode").strip()
        if season.isdigit() and episode.isdigit():
            return "series", "%s:%s:%s" % (imdb, int(season), int(episode))
        tvshow = xbmc.getInfoLabel("VideoPlayer.TVShowTitle").strip()
        return ("series" if tvshow else "movie"), imdb

    # External players hand over only the stream URL/filename. Trust order:
    #   1. the EXACT stremio id the stream add-on embeds in the URL
    #      (.../strem/tt..:S:E/ or media_id=tt..+SxxEyy) - structured addon
    #      data, immune to the capture preload-ordering problem.
    #   2. capture helper (show), episode pinned from the release-name SxxEyy
    #      when capture's episode disagrees (corrects preload-poisoned capture).
    #   3. filename title-search, then 4. Stremio account.
    conf = "exact"
    ctype, cid = _id_from_url()
    if not cid:
        ctype, cid = _id_from_capture()
        if cid and ctype == "series" and cid.count(":") >= 2:
            fs, fe = _episode_from_url()
            if fs is not None:
                fixed = "%s:%d:%d" % (cid.split(":")[0], fs, fe)
                if fixed != cid:
                    log("capture %s -> %s (episode from release name)"
                        % (cid, fixed))
                    cid = fixed
    if not cid:
        # Order the remaining fallbacks by what the filename looks like:
        #  - a clean SERIES name (has SxxEyy) -> filename FIRST: it yields the
        #    exact episode, where the account only knows the show + a guess.
        #  - otherwise (movie / no SxxEyy) -> account FIRST: recent_click is the
        #    user's actual click (exact), more reliable than a fuzzy title search.
        if _episode_from_url()[0] is not None:
            ctype, cid = _id_from_filename()
            if not cid:
                ctype, cid, conf = _id_from_account()
        else:
            ctype, cid, conf = _id_from_account()
            if not cid:
                ctype, cid = _id_from_filename()
    # If the episode was only GUESSED (account knew the show but not the
    # episode) yet the release filename carries an exact SxxEyy, trust the file:
    # pin the episode and treat it as exact - no confirm prompt needed. The
    # prompt then only appears when there's truly no episode signal anywhere.
    if cid and ctype == "series" and conf == "guess":
        fs, fe = _episode_from_url()
        if fs is not None:
            cid = "%s:%d:%d" % (cid.split(":")[0], fs, fe)
            conf = "exact"
            log("pinned episode from release name -> %s (exact)" % cid)
    if cid:
        _share_external(ctype, cid, conf)
    return ctype, cid


def _id_from_url():
    """(ctype, stremio_id) from the EXACT id the stream add-on embeds in the
    playback URL (.../strem/tt..:S:E/ or media_id=tt..+SxxEyy). Structured
    addon data, not a title search. (None, None) when the URL carries no id."""
    u = _playing_url()
    if not u:
        return None, None
    # Full series id embedded ANYWHERE in the URL. Colons never occur in
    # base64/hex config or infohash segments, so a global match is safe and
    # covers most addons that keep the id in the playback URL (StremThru's
    # /strem/, Comet, MediaFusion, Jackettio, ...).
    m = re.search(r"(tt\d+:\d+:\d+)", u)
    if m:
        return "series", m.group(1)
    m = re.search(r"(kitsu:\d+:\d+)", u)  # anime
    if m:
        return "series", m.group(1)
    # media_id=tt.. (+ SxxEyy from the release name) - Comet / AIOStreams
    mid = re.search(r"media_id=(tt\d+)", u)
    if mid:
        se = re.search(r"[Ss](\d{1,2})[ ._-]?[Ee](\d{1,2})", u)
        if se:
            return "series", "%s:%d:%d" % (mid.group(1), int(se.group(1)),
                                           int(se.group(2)))
        return "movie", mid.group(1)
    # Movie id only where explicitly labelled - never a bare 'tt' that could
    # appear by chance inside an opaque base64 blob.
    m = re.search(r"/strem/(tt\d+)(?:[/.?]|$)", u)
    if m:
        return "movie", m.group(1)
    return None, None


def _episode_from_url():
    """(season, episode) from the playing URL/file's SxxEyy, or (None, None).
    Pins the episode NUMBER of an already-identified show - never identifies it."""
    fn = _playing_filename() or _playing_url()
    m = re.search(r"[Ss](\d{1,2})[ ._-]?[Ee](\d{1,2})", fn or "")
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def _stash_file_mismatch():
    """True when the playing_* stash was written for a DIFFERENT file than the
    one playing now. Launching title B externally while A plays leaves A's
    stash live until the scrobbler's onAVStarted clears it - in that window
    the stash must not identify B (it made Black Bird show as Big Mistakes)."""
    pf = HOME.getProperty("relay.playing_file")
    if not pf:
        return False  # legacy/unknown binding - trust the stash as before
    cur = _playing_url().split("|")[0]
    return bool(cur) and pf.split("|")[0] != cur


def _share_external(ctype, cid, conf="exact"):
    """Publish the externally-resolved id so the scrobbler service can adopt it
    (giving external Stremio playback Trakt + Stremio-account sync too).
    ``conf`` is 'exact' or 'guess' - the scrobbler confirms guessed episodes
    with the user before any account write."""
    HOME.setProperty("relay.external_id", cid)
    HOME.setProperty("relay.external_type", ctype or "")
    HOME.setProperty("relay.external_conf", conf)
    HOME.setProperty("relay.external_ts", str(time.time()))


def _is_prefetch_echo(data):
    """True when the capture record is our OWN next-episode prefetch rather
    than a user click: the autodownload service's prefetch queries the stream
    addons, which can include the capture helper - so last.json would name
    the NEXT episode and mis-identify the current playback."""
    pid = HOME.getProperty("relay.prefetch_id")
    if not pid or (data or {}).get("id") != pid:
        return False
    try:
        return abs(float(data.get("ts") or 0)
                   - float(HOME.getProperty("relay.prefetch_ts") or 0)) <= 600
    except (TypeError, ValueError):
        return False


_CAP_CACHE = {"ts": 0.0, "file": "", "result": (None, None)}  # dedupe the poll burst


def _id_from_capture():
    """Recent id recorded by a self-hosted capture add-on (optional power-user
    helper: a tiny Stremio addon that records the exact id at stream-click
    time). Disabled unless capture_url is set - regular installs identify
    external playback via the Stremio account instead (_id_from_account).

    Cached ~5s so the autodownload poll loop (current_video x5) and a dialog
    open don't each hit the network - keyed on the playing file, so a quick
    stop-and-play-another never reuses the previous file's id."""
    base = ADDON.getSetting("capture_url").strip()
    if not base:
        return None, None
    now = time.time()
    cur = _playing_url()
    if now - _CAP_CACHE["ts"] < 5 and _CAP_CACHE["file"] == cur:
        return _CAP_CACHE["result"]
    data = client.fetch_json(base.rstrip("/") + "/last.json", timeout=4)
    result = (None, None)
    # 120s freshness = "just launched" (matches the scrobbler stash window),
    # avoids mis-using a title browsed minutes ago.
    try:
        fresh = bool(data and data.get("id")
                     and now - float(data.get("ts") or 0) <= 120)
    except (TypeError, ValueError):
        fresh = False
    if fresh and _is_prefetch_echo(data):
        log("capture record is our own prefetch (%s) - ignored" % data.get("id"))
        fresh = False
    if fresh:
        cid = data["id"]
        ctype = data.get("type") or ("series" if cid.count(":") >= 2 else "movie")
        log("recovered id from capture add-on -> %s %s" % (ctype, cid))
        result = (ctype, cid)
    _CAP_CACHE.update(ts=now, file=cur, result=result)
    return result


_ACC_CACHE = {"until": 0.0, "file": "", "result": (None, None, "exact")}


def _id_from_account():
    """Identify external playback from the Stremio account: the app syncs a
    library item the instant a stream is clicked (verified live on Android),
    so the freshest item IS the title being launched - no capture service or
    parseable filename needed.

    Movies are exact. Series items carry no episode (the app leaves
    state.video_id empty for external launches), so it is resolved via
    stremio_api.likely_episode: continue-watching pointer -> exact;
    first-unwatched / episode 1 -> guess (the scrobbler confirms guesses with
    the user before any account write). Returns (ctype, cid, conf); keyed on the
    playing file so a quick stop-and-play-another never reuses the previous
    title. A HIT is cached 15s; a MISS only 5s, so the autodownload retry loop
    re-queries the account once it (or the click) is ready (boot-race)."""
    now = time.time()
    cur = _playing_url()
    if now < _ACC_CACHE["until"] and _ACC_CACHE["file"] == cur:
        return _ACC_CACHE["result"]
    from relay import stremio_api
    result = (None, None, "exact")
    item = stremio_api.recent_click(120) if stremio_api.authorized() else None
    if item:
        base = item.get("_id") or ""
        if item.get("type") != "series":
            result = ("movie", base, "exact")
        else:
            try:
                meta = router.get_meta("series", base, verify_ssl()) or {}
            except Exception:  # noqa
                meta = {}
            try:
                vid, exact = stremio_api.likely_episode(item, meta)
            except Exception:  # noqa - junk meta must never kill the poll loop
                vid, exact = None, False
            if vid:
                result = ("series", vid, "exact" if exact else "guess")
            else:
                result = ("series", base, "guess")
        log("recovered id from Stremio account -> %s %s (%s)"
            % (result[0], result[1], result[2]))
    ttl = 15 if result[1] else 5  # hit cached 15s; miss only 5s (allow retry)
    _ACC_CACHE.update(until=now + ttl, file=cur, result=result)
    return result


def _parse_release_name(fn):
    """(title, year, season, episode) from a release filename, best-effort."""
    name = fn.rsplit("/", 1)[-1]
    name = re.sub(r"\.(mkv|mp4|avi|m2ts|mov|ts|wmv|webm)$", "", name, flags=re.I)
    name = name.replace(".", " ").replace("_", " ")
    # Strip leading scene-site tags like 'www.UIndex.org   -   ' that otherwise
    # poison the title search (they become bogus leading tokens).
    name = re.sub(r"^\s*www\s+\S+\s+(?:com|org|net|info|tv|me|to|cc|xyz|club|app)\b[\s_-]*",
                  "", name, flags=re.I)
    m = re.search(r"[Ss](\d{1,2})[ ._-]?[Ee](\d{1,2})|\b(\d{1,2})x(\d{1,2})\b", name)
    if m:
        s, e = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        title = name[:m.start()].strip(" -")
        # Strip a trailing release year: 'FROM 2026 S04E04' is the show 'FROM'
        # (2026 = air year) - leaving it in poisons the catalog title search.
        title = re.sub(r"\s+(19|20)\d{2}$", "", title).strip()
        return title, None, int(s), int(e)
    y = re.search(r"\b(19|20)\d{2}\b", name)
    if y:
        return name[:y.start()].strip(" -"), int(y.group(0)), None, None
    return name.strip(" -"), None, None, None


def _title_tokens(s):
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _meta_matches(mt, norm, norm_tokens, year):
    """Confident match test for a catalog/Cinemeta result:
      - exact title (case-insensitive), OR
      - the parsed YEAR matches the result's year AND every token of the
        result's name appears in the filename (its name is fully contained).
    The year+contained-name rule catches messy release names (site tags, "A
    Marvel Television Special Presentation -- ...") without the loose "first
    result" guessing that once matched 'FROM 2026' to 'From Scratch' - series
    carry no parsed year here, so they stay exact-only."""
    name = str(mt.get("name") or "").strip().lower()
    if not name:
        return False
    if name == norm:
        return True
    if year:
        ry = str(mt.get("releaseInfo") or mt.get("year") or "")
        nt = _title_tokens(name)
        if str(year) in ry and len(nt) >= 2 and nt <= norm_tokens:
            return True
    return False


def _resolve_imdb(title, year, ctype):
    """Look up an IMDB id by searching a configured catalog (AIOMetadata etc.).

    CONFIDENT-ONLY (see :func:`_meta_matches`): exact title, or year + the
    result's name fully contained in the filename. The fuzzy "first search
    result" fallback was removed - it could confidently return the WRONG show.
    A non-confident filename yields None so the chain falls through to the
    Stremio account (exact) and then moviehash subtitles."""
    if not title:
        return None
    norm = title.strip().lower()
    norm_tokens = _title_tokens(norm)

    def _confident(metas):
        for mt in (metas or [])[:8]:
            cid = mt.get("id", "")
            imdb = mt.get("imdb_id") or (cid if cid.startswith("tt") else "")
            if imdb and _meta_matches(mt, norm, norm_tokens, year):
                return imdb
        return None

    # 1. Cinemeta - the canonical IMDB-id catalog. It's deduped out of the
    #    user's browse catalogs (official add-on), but it's the most reliable
    #    title->id source, so query it directly here. No config needed.
    imdb = _cinemeta_id(title, ctype, year)
    if imdb:
        return imdb
    # 2. the user's configured search-capable catalogs (AIOMetadata etc.)
    for addon, cat in router.list_catalogs(verify_ssl()):
        ex = {e.get("name") for e in (cat.get("extra") or []) if isinstance(e, dict)}
        if "search" not in ex or cat.get("type") != ctype:
            continue
        imdb = _confident(router.get_catalog(addon, ctype, cat["id"],
                                             {"search": title}, verify_ssl()))
        if imdb:
            return imdb
    return None


_CINEMETA = "https://v3-cinemeta.strem.io"


def _cinemeta_id(title, ctype, year=None):
    """Confident id from Cinemeta's search (canonical IMDB ids): exact title,
    or year + name-contained-in-filename (see :func:`_meta_matches`). None
    otherwise. Cached per (query, year); never raises."""
    from urllib.parse import quote
    key = "cinemeta2::%s::%s::%s" % (ctype, title.lower(), year or "")
    hit, val = client.disk_get(key)
    if hit:
        return val or None
    url = "%s/catalog/%s/top/search=%s.json" % (_CINEMETA, ctype, quote(title))
    data = client.fetch_json(url, timeout=6)
    norm = title.strip().lower()
    norm_tokens = _title_tokens(norm)
    out = None
    for mt in ((data or {}).get("metas") or [])[:8]:
        cid = mt.get("id", "")
        if cid.startswith("tt") and _meta_matches(mt, norm, norm_tokens, year):
            out = cid
            break
    client.disk_set(key, out or "", 86400)
    return out


def _id_from_filename():
    """Resolve (ctype, stremio_id) from the playing filename (external players).
    Cached per filename so we don't re-search on every poll/dialog open."""
    fn = _playing_filename()
    if not fn:
        return None, None
    key = "fnid4::" + fn  # v4: bumped when resolver logic changes, to orphan stale (negative) caches
    hit, val = client.disk_get(key)
    if hit and val:
        return (val[0] or None), (val[1] or None)
    title, year, season, episode = _parse_release_name(fn)
    if not title or len(title.strip()) < 2:
        return None, None  # junk name (e.g. 'n.mkv') -> OpenSubtitles moviehash path
    ctype = "series" if season is not None else "movie"
    imdb = _resolve_imdb(title, year, ctype)
    if not imdb:
        # Negative-cache the miss: without this the catalog-search fan-out
        # re-fires on every autodownload poll tick AND every dialog open.
        client.disk_set(key, ["", ""], 600)
        return None, None
    if ctype == "series" and season is not None and episode is not None:
        result = ("series", "%s:%d:%d" % (imdb, season, episode))
    else:
        result = ("movie", imdb)
    client.disk_set(key, list(result), 86400)
    log("resolved external filename %r -> %s %s" % (fn[:60], result[0], result[1]))
    return result


def _playing_filename():
    fn = HOME.getProperty("relay.playing_filename").strip()
    if fn:
        return fn
    try:
        return os.path.basename(xbmc.Player().getPlayingFile() or "")
    except Exception:
        return ""


def _playing_url():
    """Full URL/path of what's playing (for OpenSubtitles moviehash)."""
    try:
        return xbmc.Player().getPlayingFile() or ""
    except Exception:
        return ""


def enrich_osd(player, ctype, cid):
    """For external playback (Stremio app -> Kodi, where Kodi only knows the junk
    filename), push the real title + art to the OSD from the resolved id's meta.
    No-op when Kodi already has an IMDB id (library / our own plugin playback)."""
    if xbmc.getInfoLabel("VideoPlayer.IMDBNumber").strip():
        return  # Kodi already has proper info
    try:
        meta = router.get_meta("series" if ctype == "series" else "movie",
                               ids.base_id(cid), verify_ssl())
    except Exception:  # noqa
        meta = None
    if not meta or not meta.get("name"):
        return
    title = meta["name"]
    _b, season, episode = ids.split_series_id(cid)
    if ctype == "series" and season is not None and episode is not None:
        title = "%s S%02dE%02d" % (title, season, episode)
    li = xbmcgui.ListItem(label=title, path=_playing_url(), offscreen=True)
    tag = li.getVideoInfoTag()
    tag.setMediaType("episode" if ctype == "series" else "movie")
    tag.setTitle(title)
    if ctype == "series":
        tag.setTvShowTitle(meta.get("name", ""))
        if season is not None:
            tag.setSeason(season)
        if episode is not None:
            tag.setEpisode(episode)
    if meta.get("description"):
        tag.setPlot(meta["description"])
    g = meta.get("genres") or meta.get("genre")
    if g:
        tag.setGenres(g if isinstance(g, list) else [g])
    if meta.get("imdbRating"):
        try:
            tag.setRating(float(meta["imdbRating"]))
        except (ValueError, TypeError):
            pass
    art = {}
    if meta.get("poster"):
        art["poster"] = art["thumb"] = meta["poster"]
    if meta.get("background"):
        art["fanart"] = art["landscape"] = meta["background"]
    if meta.get("logo"):
        art["clearlogo"] = meta["logo"]
    if art:
        li.setArt(art)
    try:
        player.updateInfoTag(li)  # Kodi 20+ - update the now-playing OSD info
        log("OSD enriched -> %s" % title)
    except Exception as exc:  # noqa
        log("OSD enrich failed: %r" % exc)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _lang_names(raw):
    """Kodi passes selected languages as a comma list of full names."""
    return [n for n in (raw or "").split(",") if n]


# Provider codes Kodi's convertLanguage() doesn't know (mostly OpenSubtitles).
# Mapping them to a standard code lets the filter recognise them as a real
# (non-preferred) language instead of leaking them through as "unknown".
_LANG_ALIASES = {
    "pob": "pt", "pb": "pt", "ptbr": "pt", "ptpt": "pt",
    "ze": "zh", "zht": "zh", "zhe": "zh", "zhtw": "zh", "zhcn": "zh", "chi": "zh",
    "spl": "es", "esla": "es", "esmx": "es", "spa": "es",
    "scc": "sr", "scr": "hr", "mne": "sr",
    "per": "fa", "fas": "fa", "pes": "fa",
    "gre": "el", "ell": "el", "may": "ms", "msa": "ms", "fil": "tl",
    "ger": "de", "fre": "fr", "dut": "nl", "cze": "cs", "rum": "ro",
    "ara": "ar", "arz": "ar", "ary": "ar", "acm": "ar",  # Arabic variants -> ar
}


def _english_name(lang):
    """Best-effort English language name for a provider lang code/name.

    Tries the raw value, an alias map for non-standard provider codes, and a
    region-stripped base (e.g. 'pt-BR' -> 'pt'). Returns '' only when nothing
    resolves (kept by the filter as a possible false-positive of the wanted lang).
    """
    if not lang:
        return ""
    raw = lang.strip().lower().replace("_", "-")
    for cand in (raw, raw.replace("-", ""), _LANG_ALIASES.get(raw.replace("-", "")),
                 raw.split("-")[0], _LANG_ALIASES.get(raw.split("-")[0])):
        if not cand:
            continue
        name = xbmc.convertLanguage(cand, xbmc.ENGLISH_NAME)
        if name:
            return name
    return xbmc.convertLanguage(lang, xbmc.ENGLISH_NAME) or ""


def sub_extra():
    """Stremio subtitle `extra` (filename + videoSize) for better matching."""
    extra = {}
    fn = _playing_filename()
    if fn:
        extra["filename"] = fn
    vs = HOME.getProperty("relay.playing_videosize").strip()
    if vs.isdigit():
        extra["videoSize"] = vs
    return extra or None


def _list_oshash(wanted):
    """List OpenSubtitles moviehash results when there's no id (external player /
    junk filename). Matches the exact file by content hash."""
    url = _playing_url()
    if not url:
        log("no usable id and no playing url - cannot search")
        return
    import opensubs
    if not opensubs.configured():
        log("no id and OpenSubtitles not configured - cannot search")
        return
    langs2 = sorted({c for c in (xbmc.convertLanguage(n, xbmc.ISO_639_1)
                                 for n in wanted) if c})
    items = opensubs.search_by_hash(url, langs2)
    log("OpenSubtitles moviehash search: %d result(s)" % len(items))
    for s in items:
        eng = _english_name(s["lang"]) or s["lang"] or "Unknown"
        if wanted and eng and eng not in wanted:
            continue
        li = xbmcgui.ListItem(label=eng, label2=(s.get("release") or "")[:70])
        if s["lang"]:
            li.setArt({"thumb": s["lang"]})
        li.setProperty("sync", "true" if s.get("hash_match") else "false")
        li.setProperty("hearing_imp", "false")
        dl = BASE_URL + "?" + urlencode({"action": "download",
                                         "url": "osfileid:%s" % s["file_id"],
                                         "lang": s["lang"]})
        xbmcplugin.addDirectoryItem(HANDLE, dl, li, False)


def oshash_autodownload(player, pref):
    """Auto-download via OpenSubtitles moviehash for the no-id case. True if loaded."""
    url = _playing_url()
    if not url:
        return False
    import opensubs
    if not opensubs.configured():
        return False
    langs2 = sorted({c for c in (xbmc.convertLanguage(n, xbmc.ISO_639_1)
                                 for n in pref) if c})
    for s in opensubs.search_by_hash(url, langs2):
        eng = _english_name(s["lang"])
        if pref and eng and eng not in pref:
            continue
        path = download_to_temp("osfileid:%s" % s["file_id"], s["lang"])
        if path:
            player.setSubtitles(path)
            log("auto-downloaded subtitle via OS moviehash (%s)" % (eng or s["lang"]))
            return True
    return False


def do_search(params):
    ctype, content_id = current_video()
    # Normalise the user's wanted languages to canonical English names too, so
    # "Arabic"/"arabic"/"ara" all compare equal.
    wanted = {_english_name(n) or n for n in _lang_names(params.get("languages"))}

    if not content_id:
        _list_oshash(wanted)  # external player / junk filename -> moviehash
        xbmcplugin.endOfDirectory(HANDLE)
        return

    subs = router.get_subtitles(ctype, content_id, extra=sub_extra(),
                                verify_ssl=verify_ssl())
    log("found %d subtitle(s) for %s %s" % (len(subs), ctype, content_id))

    seen = set()
    for sub in subs:
        url = sub.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        lang = sub.get("lang", "")
        eng = _english_name(lang)
        # Only drop when we resolved a real language that the user didn't ask
        # for; keep unrecognised languages rather than silently hiding them.
        if wanted and eng and eng not in wanted:
            continue

        li = xbmcgui.ListItem(label=eng or lang or "Unknown",
                              label2=sub.get("id") or sub.get("_addon", ""))
        iso2 = xbmc.convertLanguage(_english_name(lang) or lang, xbmc.ISO_639_1)
        if iso2:
            li.setArt({"thumb": iso2})
        li.setProperty("sync", "false")
        li.setProperty("hearing_imp",
                       "true" if sub.get("hearing_impaired") else "false")
        dl = BASE_URL + "?" + urlencode({
            "action": "download",
            "url": url,
            "lang": lang,
        })
        xbmcplugin.addDirectoryItem(HANDLE, dl, li, False)

    xbmcplugin.endOfDirectory(HANDLE)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

SUB_EXTS = (".srt", ".vtt", ".ssa", ".ass", ".sub", ".smi", ".aqt", ".jss", ".rt")


def _ext_for(name):
    low = (name or "").lower().split("?")[0]
    for ext in SUB_EXTS:
        if low.endswith(ext):
            return ext
    return ".srt"


def _find_member(names):
    for n in names:
        if n.lower().endswith(SUB_EXTS):
            return n
    return None


def _lang_code(lang):
    """Filename language code Kodi can detect; prefer 2-letter ISO-639-1."""
    return (xbmc.convertLanguage(lang, xbmc.ISO_639_1)
            or xbmc.convertLanguage(lang, xbmc.ISO_639_2)
            or "und")


def _bounded_copy(src, dst, limit):
    """Copy a stream, aborting if it expands past ``limit`` (bomb defence)."""
    remaining = limit
    while True:
        chunk = src.read(65536)
        if not chunk:
            break
        remaining -= len(chunk)
        if remaining < 0:
            raise ValueError("subtitle payload exceeds size limit")
        dst.write(chunk)


def _http_get(url):
    if urlsplit(url).scheme not in ("http", "https"):
        raise ValueError("unsupported url scheme")  # no file:// local reads
    ctx = None
    if not verify_ssl():
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = Request(url, headers={"User-Agent": "Kodi-Relay/1.0"})
    with urlopen(req, timeout=client.DEFAULT_TIMEOUT, context=ctx) as resp:
        data = resp.read(client.MAX_COMPRESSED + 1)
        if len(data) > client.MAX_COMPRESSED:
            raise ValueError("subtitle download exceeds size limit")
        return data


def _extract(archive_path, out_dir, hint_name=""):
    """Return a path to a plain subtitle file. Kodi unpacks nothing itself, so
    we sniff the magic bytes and handle gzip / zip / raw text ourselves.

    Output paths never incorporate an archive member's name (no zip-slip), and
    extraction is size-capped (no decompression bombs).
    """
    with open(archive_path, "rb") as fh:
        magic = fh.read(4)

    # gzip: 1f 8b
    if magic[:2] == b"\x1f\x8b":
        out = os.path.join(out_dir, "subtitle.srt")
        with gzip.open(archive_path, "rb") as f_in, open(out, "wb") as f_out:
            _bounded_copy(f_in, f_out, SIZE_LIMIT)
        return out

    # zip: 50 4b 03 04 (Python zipfile only - no libarchive/native fallback)
    if magic[:4] == b"PK\x03\x04":
        try:
            with open(archive_path, "rb") as fh:
                zf = zipfile.ZipFile(io.BytesIO(fh.read()))
        except (zipfile.BadZipFile, OSError):
            return None
        member = _find_member(zf.namelist())
        if not member:
            return None
        if zf.getinfo(member).file_size > SIZE_LIMIT:
            log("zip member exceeds size limit; skipping")
            return None
        out = os.path.join(out_dir, "subtitle" + _ext_for(member))
        with zf.open(member) as src, open(out, "wb") as dst:
            _bounded_copy(src, dst, SIZE_LIMIT)
        return out

    # plain text subtitle - keep its real extension (.vtt/.ass/...)
    out = os.path.join(out_dir, "subtitle" + _ext_for(hint_name))
    os.replace(archive_path, out)
    return out


# Decoders tried in order. Arabic subs are usually Windows-1256; many others are
# UTF-8/Western. latin-1 is the never-fails catch-all (kept last).
_SUB_ENCODINGS = ("utf-8-sig", "utf-8", "cp1256", "cp1252", "latin-1")


def _to_utf8(path):
    """Re-save a subtitle file as UTF-8 so Kodi renders Arabic/Cyrillic correctly
    (the #1 cause of garbled Arabic is a cp1256 file Kodi reads as UTF-8)."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return
    for enc in _SUB_ENCODINGS:
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass


# Provider watermark / ad cues to drop: a cue whose entire text (tags stripped,
# entities decoded) is a release filename, a URL, an "=>" pointer, or a known
# subtitle-site credit. These display as junk on screen AND trip Kodi's SRT CSS
# parser (e.g. "<u>=&gt;Movie.2025.WEBRip.srt</u>" -> InsertCssStyleStartTag error).
_WM_SITES = ("opensubtitles", "subscene", "subdl", "sub-dl", "podnapisi",
             "addic7ed", "subsource", "yifysubtitles", "yify", "downloaded from",
             "provided by", "support us", "osdb", "www.", "http://", "https://",
             ".com", ".org", ".net")


def _is_watermark(cue_text):
    t = re.sub(r"<[^>]+>", "", cue_text)                       # drop html tags
    t = t.replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&").strip()
    if not t:
        return False                                          # empty -> leave it
    low = t.lower()
    if re.search(r"\.(srt|ass|ssa|sub|vtt|mkv|mp4|avi|m2ts|webrip)\b", low):
        return True                                           # release filename
    if t.lstrip("=").lstrip().startswith(">") or "=>" in t:
        return True                                           # "=>" pointer line
    return any(w in low for w in _WM_SITES)                   # site/URL credit


def _strip_watermark_cues(path):
    """Remove provider watermark/ad cues from an SRT/VTT, renumbering survivors."""
    try:
        with open(path, encoding="utf-8") as fh:
            norm = fh.read().replace("\r\n", "\n").replace("\r", "\n")
    except OSError:
        return
    blocks = re.split(r"\n[ \t]*\n", norm.strip("\n"))
    kept = []
    for blk in blocks:
        lines = blk.split("\n")
        ts = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ts is None:                                        # header/NOTE/non-cue
            kept.append(blk)
            continue
        if _is_watermark(" ".join(lines[ts + 1:])):
            continue                                          # drop the cue
        kept.append(blk)
    if len(kept) == len(blocks):
        return                                                # nothing removed
    out, n = [], 1
    for blk in kept:
        lines = blk.split("\n")
        ts = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ts is None:
            out.append(blk)
        else:
            out.append("%d\n%s" % (n, "\n".join(lines[ts:])))  # renumber index
            n += 1
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n\n".join(out) + "\n")
    except OSError:
        pass


_RLM = "‏"   # U+200F Right-to-Left Mark (zero-width STRONG rtl char)
_RLE = "‫"   # U+202B Right-to-Left Embedding
_PDF = "‬"   # U+202C Pop Directional Formatting
_RTL_MARKS = (_RLM, _RLE, "‪", "‭", "⁧", "⁦")  # RLM/RLE/LRE/LRO/RLI/LRI


def _has_arabic(s):
    return any("؀" <= c <= "ۿ" or "ݐ" <= c <= "ݿ"
               or "ࢠ" <= c <= "ࣿ" or "ﭐ" <= c <= "﷿"
               or "ﹰ" <= c <= "﻿" for c in s)


_TERM = ".,!?:;٫،؟…"  # sentence-terminating punctuation (incl. Arabic , ? )


def _is_visual_order(cues):
    """A visual-order (pre-reversed) file stores sentence-ending punctuation at
    the START of each line. Detect that vs normal logical order."""
    if len(cues) < 10:
        return False
    starts = sum(1 for l in cues if l.lstrip("-–—•♪ ")[:1] in _TERM)
    ends = sum(1 for l in cues if l.rstrip()[-1:] in _TERM)
    return starts > 3 * ends and starts >= 0.25 * len(cues)


def _relocate_leading_punct(line):
    """Visual-order line -> logical: move a leading run of sentence punctuation
    to the end ('.حبيبتي' -> 'حبيبتي.')."""
    m = re.match(r"^([%s]+)[ \t]*(.+)$" % re.escape(_TERM), line)
    return (m.group(2).rstrip() + m.group(1)) if m else line


def _starts_ltr(line):
    """True if the line's first alphabetic char is Latin/non-Arabic, so it needs
    an explicit RLM to flip the auto base direction to RTL. Arabic-led lines (and
    dialogue-dash-led lines, where the first letter is Arabic) DON'T need it - and
    adding it there pushes a leading '-' to the wrong side."""
    for c in line:
        if c.isalpha():
            return not _has_arabic(c)
    return False


def _rtl_fix_file(path):
    """Force an RTL base direction on Arabic cue lines so neutral punctuation
    (. , - ? !) sits at the correct (left) end.

    Wrap each Arabic line in a Right-to-Left Embedding (U+202B..U+202C) - the
    proven fix that honours the embedding code path. Lines whose first letter is
    Latin also get a leading Right-to-Left Mark (U+200F) to flip the first-strong
    / auto-direction path; Arabic- and dash-led lines do not (the RLM would
    misplace a leading dialogue dash). Visual-order (pre-reversed) files first
    have leading punctuation moved back to the end to restore logical order."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().replace("\r\n", "\n").replace("\r", "\n").split("\n")
    except OSError:
        return
    cues = [l for l in lines if l and "-->" not in l and not l.isdigit()
            and _has_arabic(l) and not any(m in l for m in _RTL_MARKS)]
    visual = _is_visual_order(cues)
    changed = False
    for i, line in enumerate(lines):
        if "-->" in line or not _has_arabic(line):
            continue
        if any(m in line for m in _RTL_MARKS):  # already direction-marked
            continue
        if visual:
            line = _relocate_leading_punct(line)
        prefix = _RLM + _RLE if _starts_ltr(line) else _RLE
        lines[i] = prefix + line + _PDF
        changed = True
    if not changed:
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
    except OSError:
        pass


def download_to_temp(url, lang):
    """Fetch + unpack a subtitle URL to PROFILE/temp/subtitle.<lang>.<ext>.

    Returns the local path or None. Shared by the manual download action and the
    background autodownload service.
    """
    url = url or ""  # already decoded by parse_qsl (manual) / raw (auto) - do NOT unquote again
    if not url:
        return None
    if url.startswith("osfileid:"):  # OpenSubtitles moviehash result -> resolve link
        import opensubs
        url = opensubs.download_link(url.split(":", 1)[1]) or ""
        if not url:
            log("OpenSubtitles download link failed (quota?)")
            return None
    # Unique dir per download so the manual subtitle action and the background
    # autodownload service never clobber each other's in-flight files.
    base = os.path.join(PROFILE, "temp")
    os.makedirs(base, exist_ok=True)
    now = time.time()
    for name in os.listdir(base):  # prune old downloads Kodi has finished with
        old = os.path.join(base, name)
        try:
            if os.path.isdir(old) and now - os.path.getmtime(old) > 3600:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass
    work = tempfile.mkdtemp(dir=base)
    archive = os.path.join(work, "download.bin")
    try:
        with open(archive, "wb") as fh:
            fh.write(_http_get(url))
    except Exception as exc:  # noqa
        log("download failed: %s" % type(exc).__name__)
        return None
    sub = _extract(archive, work, hint_name=url)
    if not sub:
        log("no subtitle file found in payload")
        return None
    # Rename to subtitle.<lang>.<ext> so Kodi detects the language.
    ext = os.path.splitext(sub)[1] or ".srt"
    final = os.path.join(work, "subtitle.%s%s" % (_lang_code(lang), ext))
    try:
        os.replace(sub, final)
    except OSError:
        final = sub
    el = ext.lower()
    if el in (".srt", ".vtt", ".ssa", ".ass", ".sub", ".smi"):
        _to_utf8(final)  # normalise cp1256/etc -> UTF-8 (fixes Arabic mojibake)
    if el in (".srt", ".vtt"):
        _strip_watermark_cues(final)  # drop provider filename/URL/ad cues
    # RTL punctuation fix - plain-text cue formats only (NOT .ass/.ssa, whose
    # lines carry override tags + libass already does bidi).
    if el in (".srt", ".vtt", ".sub", ".smi") and ADDON.getSetting("rtl_fix") != "false":
        _rtl_fix_file(final)
    return final


def do_download(params):
    final = download_to_temp(params.get("url", ""), params.get("lang", ""))
    if not final:
        xbmcgui.Dialog().notification("Relay Subtitles", "Download failed",
                                      xbmcgui.NOTIFICATION_ERROR)
        xbmcplugin.endOfDirectory(HANDLE)
        return
    li = xbmcgui.ListItem(label=final, offscreen=True)
    xbmcplugin.addDirectoryItem(HANDLE, final, li, False)
    xbmcplugin.endOfDirectory(HANDLE)


def main():
    params = dict(parse_qsl(sys.argv[2][1:]))
    action = params.get("action")
    try:
        if action in ("search", "manualsearch"):
            do_search(params)
        elif action == "download":
            do_download(params)
        else:
            xbmcplugin.endOfDirectory(HANDLE)
    except Exception as exc:  # noqa - never leave the subtitle dialog spinning
        log("action %s failed: %r" % (action, exc))
        try:
            xbmcplugin.endOfDirectory(HANDLE)
        except Exception:  # noqa
            pass


if __name__ == "__main__":
    main()
