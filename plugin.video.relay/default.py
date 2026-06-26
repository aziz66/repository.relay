"""Relay - Kodi video plugin entry point.

Browses catalogs/meta from configured Stremio addons and resolves streams.
All cross-addon logic lives in script.module.relay; this file is the
Kodi presentation layer (routing, ListItems, dialogs).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import sys
import time
from urllib.parse import urlencode, parse_qsl, quote, unquote, urlsplit

import xbmc
import xbmcgui
import xbmcplugin
import xbmcaddon
import xbmcvfs

from relay import router, ids, store, client

ADDON = xbmcaddon.Addon()
HANDLE = int(sys.argv[1])
BASE_URL = sys.argv[0]

# Window used to stash the Stremio id of what we're playing, so the subtitle
# service can recover it for non-IMDB (kitsu/tmdb) content.
HOME = xbmcgui.Window(10000)

STREMIO_PAGE_SIZE = 100  # protocol default when a catalog declares no pageSize


def setting_bool(key, default=False):
    val = ADDON.getSetting(key)
    return (val == "true") if val else default


VERIFY_SSL = setting_bool("verify_ssl", True)
AUTOPLAY = setting_bool("autoplay", False)


def build_url(**kwargs):
    return BASE_URL + "?" + urlencode({k: v for k, v in kwargs.items()
                                       if v is not None})


def notify(msg, heading="Relay"):
    xbmcgui.Dialog().notification(heading, msg, xbmcgui.NOTIFICATION_INFO, 4000)


def _http_ok(url):
    return urlsplit(url).scheme in ("http", "https")


# ---------------------------------------------------------------------------
# Catalog extra helpers (handles both modern extra[] and legacy arrays)
# ---------------------------------------------------------------------------

def catalog_extras(cat):
    """Return ``(supported, required)`` sets of extra names for a catalog."""
    supported, required = set(), set()
    extra = cat.get("extra")
    if isinstance(extra, list):
        for e in extra:
            if isinstance(e, dict) and e.get("name"):
                supported.add(e["name"])
                if e.get("isRequired"):
                    required.add(e["name"])
    for n in cat.get("extraSupported", []) or []:
        supported.add(n)
    for n in cat.get("extraRequired", []) or []:
        required.add(n)
    return supported, required


def genre_options(cat):
    for e in cat.get("extra") or []:
        if isinstance(e, dict) and e.get("name") == "genre":
            return e.get("options") or []
    return []


def find_catalog(addon, ctype, catalog_id):
    for c in addon["catalogs"]:
        if c.get("id") == catalog_id and c.get("type") == ctype:
            return c
    return None


# Catalogs are grouped into folders by a leading "[Tag]" in their name (e.g.
# AIOMetadata's "[Discover] Latest Movies") or, failing that, by the add-on name.
GROUP_PRIORITY = ["continue", "watchlist", "trending", "popular", "latest",
                  "upcoming", "airing", "discover", "recommended", "new"]


def catalog_group(addon, cat):
    name = cat.get("name", "") or cat.get("id", "")
    m = re.match(r"\s*\[([^\]]+)\]", name)
    return m.group(1).strip() if m else addon["name"]


def _group_sort_key(group):
    low = group.lower()
    for i, tok in enumerate(GROUP_PRIORITY):
        if tok in low:
            return (0, i, low)
    return (1, 0, low)


def cat_kind(cat):
    """Bucket a catalog into 'series' or 'movie' for the top-level Movies/TV
    Shows folders (the catalog's own type string is still used for requests)."""
    t = (cat.get("type") or "").lower()
    return "series" if ("series" in t or "show" in t or "tv" == t) else "movie"


def clean_catalog_label(cat):
    """Catalog name without the leading [Tag] AND without the Movies/Shows type
    suffix - that's implied by the parent folder. 'Action (Shows)' -> 'Action',
    'Apple TV Movies' -> 'Apple TV' (bare 'TV' is kept)."""
    name = cat.get("name", "") or cat.get("id", "")
    name = re.sub(r"^\s*\[[^\]]+\]\s*", "", name)              # drop [Tag]
    name = re.sub(r"\s*\((?:movies?|shows?|series|tv\s*shows?)\)\s*$", "",
                  name, flags=re.I)                             # drop "(Shows)"
    name = re.sub(r"\s+(?:movies?|shows?|series)\s*$", "", name, flags=re.I)  # trailing word
    return name.strip() or (cat.get("name") or cat.get("id") or "")


# ---------------------------------------------------------------------------
# ListItem construction
# ---------------------------------------------------------------------------

def _parse_runtime(value):
    """Stremio runtime is often a string like '128 min' -> seconds."""
    if not value:
        return 0
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return int(digits) * 60 if digits else 0


def _trailer_ytid(meta):
    for t in (meta.get("trailers") or []):
        yt = t.get("ytId") or t.get("source")
        if yt:
            return yt
    for t in (meta.get("trailerStreams") or []):
        if t.get("ytId"):
            return t["ytId"]
    return None


def _play_context(ctype, content_id, title=None):
    """Both playback actions for the long-press menu, default action first:
    'Play best source' (auto-play) and 'Choose source…' (manual list). Lets the
    user pick either per item regardless of the global autoplay setting."""
    play_best = ("Play best source", "PlayMedia(%s)" % build_url(
        action="play", type=ctype, id=content_id, title=title))
    choose = ("Choose source…", "Container.Update(%s)" % build_url(
        action="streams", type=ctype, id=content_id, title=title))
    return [play_best, choose] if AUTOPLAY else [choose, play_best]


def _video_context(meta):
    items = [("Show info", "Action(Info)")]
    yt = _trailer_ytid(meta)
    if yt:
        items.append(("Play trailer",
                      "RunPlugin(%s)" % build_url(action="trailer", yt=yt)))
    return items


def make_video_item(meta, playable, extra_ctx=None):
    """Build a ListItem from a Stremio meta/preview dict."""
    li = xbmcgui.ListItem(label=meta.get("name", "Unknown"), offscreen=True)
    art = {}
    for key, dest in (("poster", "poster"), ("background", "fanart"),
                      ("logo", "clearlogo"), ("thumbnail", "thumb")):
        if meta.get(key):
            art[dest] = meta[key]
    if "poster" in art and "thumb" not in art:
        art["thumb"] = art["poster"]
    if meta.get("background"):  # wide art for widget/landscape skins
        art.setdefault("landscape", meta["background"])
        art.setdefault("banner", meta["background"])
    li.setArt(art)

    tag = li.getVideoInfoTag()
    tag.setTitle(meta.get("name", ""))
    if meta.get("description"):
        tag.setPlot(meta["description"])
    genres = meta.get("genres") or meta.get("genre")
    if genres:
        tag.setGenres(genres if isinstance(genres, list) else [genres])
    year = meta.get("year") or meta.get("releaseInfo")
    if year:
        digits = "".join(ch for ch in str(year)[:4] if ch.isdigit())
        if digits:
            tag.setYear(int(digits))
    if meta.get("imdbRating"):
        try:
            tag.setRating(float(meta["imdbRating"]))
        except (ValueError, TypeError):
            pass
    if meta.get("runtime"):
        tag.setDuration(_parse_runtime(meta["runtime"]))
    if meta.get("released"):
        tag.setPremiered(str(meta["released"])[:10])

    # Cast / director / writer from legacy fields or the meta `links` array.
    def _aslist(v):
        return [v] if isinstance(v, str) else list(v or [])
    directors, writers, cast = (_aslist(meta.get("director")),
                                _aslist(meta.get("writer")),
                                _aslist(meta.get("cast")))
    for ln in (meta.get("links") or []):
        cat, nm = (ln.get("category") or "").lower(), ln.get("name")
        if not nm:
            continue
        if "director" in cat:
            directors.append(nm)
        elif "writer" in cat:
            writers.append(nm)
        elif "actor" in cat or cat == "cast":
            cast.append(nm)
    try:
        if directors:
            tag.setDirectors([str(d) for d in dict.fromkeys(directors)])
        if writers:
            tag.setWriters([str(w) for w in dict.fromkeys(writers)])
        if cast:
            tag.setCast([xbmc.Actor(str(c)) for c in dict.fromkeys(cast)])
    except Exception:  # noqa - older API signature / odd data
        pass

    cid = meta.get("id", "")
    uids = {}
    if meta.get("imdb_id") or ids.is_imdb(cid):
        imdb = meta.get("imdb_id") or ids.base_id(cid)
        uids["imdb"] = imdb
        tag.setIMDBNumber(imdb)
    if meta.get("_tmdbId"):
        uids["tmdb"] = str(meta["_tmdbId"])
    if uids:
        default = "imdb" if "imdb" in uids else "tmdb"
        tag.setUniqueIDs(uids, default)

    is_series = meta.get("type") == "series"
    tag.setMediaType("tvshow" if is_series else "movie")
    if playable:
        li.setProperty("IsPlayable", "true")
    li.addContextMenuItems(list(extra_ctx or []) + _video_context(meta))
    return li


def make_episode_item(video, show_meta):
    """ListItem for a single episode entry from meta['videos']."""
    label = video.get("title") or video.get("name") or "Episode"
    season = video.get("season")
    episode = video.get("episode")
    if str(season).isdigit() and str(episode).isdigit():
        label = "%sx%02d. %s" % (season, int(episode), label)
    li = xbmcgui.ListItem(label=label, offscreen=True)
    art = {}
    if video.get("thumbnail"):
        art["thumb"] = video["thumbnail"]
    if show_meta.get("poster"):
        art["poster"] = show_meta["poster"]
    if show_meta.get("background"):
        art["fanart"] = show_meta["background"]
    li.setArt(art)

    tag = li.getVideoInfoTag()
    tag.setMediaType("episode")
    tag.setTitle(video.get("title") or video.get("name") or "")
    tag.setTvShowTitle(show_meta.get("name", ""))
    if str(season).isdigit():
        tag.setSeason(int(season))
    if str(episode).isdigit():
        tag.setEpisode(int(episode))
    if video.get("overview") or video.get("description"):
        tag.setPlot(video.get("overview") or video.get("description"))
    if video.get("released"):
        tag.setFirstAired(str(video["released"])[:10])
    imdb = ids.base_id(show_meta.get("imdb_id") or "")
    if imdb:
        tag.setIMDBNumber(imdb)
    return li


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def _browsable_catalogs():
    """(addon, cat) for catalogs that belong in the browse tree (not search-only)."""
    out = []
    for addon, cat in router.list_catalogs(VERIFY_SSL):
        _supported, required = catalog_extras(cat)
        if "search" in required:
            continue  # search-only catalogs live under the Search menu
        out.append((addon, cat))
    return out


def view_root():
    """Top level: Movies / TV Shows folders + Search + Manage.

    cacheToDisc=False so add/remove/toggle is reflected immediately.
    """
    cats = router.list_catalogs(VERIFY_SSL)
    if not cats:
        item = xbmcgui.ListItem(label="[No add-ons configured - open Manage add-ons]")
        xbmcplugin.addDirectoryItem(HANDLE, build_url(action="manage"), item, True)
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        return

    # Continue Watching from the Stremio account (also widget-friendly:
    # plugin://plugin.video.relay/?action=cw)
    try:
        from relay import stremio_api
        if stremio_api.authorized():
            li = xbmcgui.ListItem(label="[B]Continue Watching[/B]")
            xbmcplugin.addDirectoryItem(HANDLE, build_url(action="cw"), li, True)
            ne = xbmcgui.ListItem(label="[B]New Episodes[/B]")
            xbmcplugin.addDirectoryItem(HANDLE, build_url(action="new"), ne, True)
    except Exception:  # noqa
        pass

    kinds = {cat_kind(cat) for _addon, cat in _browsable_catalogs()}
    if "movie" in kinds:
        li = xbmcgui.ListItem(label="Movies")
        xbmcplugin.addDirectoryItem(HANDLE, build_url(action="type", t="movie"),
                                    li, True)
    if "series" in kinds:
        li = xbmcgui.ListItem(label="TV Shows")
        xbmcplugin.addDirectoryItem(HANDLE, build_url(action="type", t="series"),
                                    li, True)

    search = xbmcgui.ListItem(label="[B]Search[/B]")
    xbmcplugin.addDirectoryItem(HANDLE, build_url(action="search_menu"), search, True)
    manage = xbmcgui.ListItem(label="[B]Manage add-ons[/B]")
    xbmcplugin.addDirectoryItem(HANDLE, build_url(action="manage"), manage, True)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def view_type(ctype):
    """The catalog groups for one content type (Movies or TV Shows)."""
    groups = {}
    for addon, cat in _browsable_catalogs():
        if cat_kind(cat) != ctype:
            continue
        g = catalog_group(addon, cat)
        if g not in groups:
            groups[g] = addon.get("logo")  # representative folder icon
    for group in sorted(groups, key=_group_sort_key):
        li = xbmcgui.ListItem(label=group)
        if groups[group]:
            li.setArt({"icon": groups[group], "thumb": groups[group]})
        xbmcplugin.addDirectoryItem(
            HANDLE, build_url(action="group", name=group, t=ctype), li, True)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def view_group(name, ctype):
    """Catalogs in one group of one type, labels cleaned of the type suffix."""
    items = [(a, c) for a, c in _browsable_catalogs()
             if cat_kind(c) == ctype and catalog_group(a, c) == name]
    items.sort(key=lambda ac: clean_catalog_label(ac[1]).lower())
    xbmcplugin.setContent(HANDLE, "tvshows" if ctype == "series" else "movies")
    for addon, cat in items:
        li = xbmcgui.ListItem(label=clean_catalog_label(cat))
        if addon.get("logo"):
            li.setArt({"icon": addon["logo"]})
        url = build_url(action="catalog", addon=addon["entryId"],
                        type=cat.get("type", "movie"), id=cat.get("id"), skip=0)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, True)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def view_catalog(addon_id, ctype, catalog_id, skip=0, search=None, genre=None):
    addon = router.addon_by_id(addon_id, VERIFY_SSL)
    if not addon:
        notify("Add-on unavailable")
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return

    cat_def = find_catalog(addon, ctype, catalog_id)

    # A required genre with no value yet -> list genres as browsable sub-folders
    # (back-button friendly, unlike a modal prompt).
    if cat_def and not genre:
        _supported, required = catalog_extras(cat_def)
        if "genre" in required:
            opts = genre_options(cat_def)
            if opts:
                for opt in opts:
                    li = xbmcgui.ListItem(label=opt)
                    url = build_url(action="catalog", addon=addon_id, type=ctype,
                                    id=catalog_id, skip=0, genre=opt)
                    xbmcplugin.addDirectoryItem(HANDLE, url, li, True)
                xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
                return

    # Only send extras the catalog actually advertises (spec-conformant).
    supported = catalog_extras(cat_def)[0] if cat_def else {"skip", "search", "genre"}
    extra = {}
    if skip and "skip" in supported:
        extra["skip"] = skip
    if search:
        extra["search"] = search
    if genre:
        extra["genre"] = genre
    metas = router.get_catalog(addon, ctype, catalog_id, extra or None, VERIFY_SSL)

    # Watched indicators from the Stremio account library (cached 120s).
    lib = {}
    try:
        from relay import stremio_api
        if stremio_api.authorized():
            lib = stremio_api.library_map()
    except Exception:  # noqa
        pass

    def _mark_watched(li_, meta_):
        st = (lib.get(ids.base_id(meta_.get("id") or "")) or {}).get("state") or {}
        if st.get("timesWatched") or st.get("flaggedWatched"):
            try:
                li_.getVideoInfoTag().setPlaycount(1)  # Kodi's ✓ overlay
            except Exception:  # noqa
                pass

    xbmcplugin.setContent(HANDLE, "tvshows" if ctype == "series" else "movies")
    for meta in metas:
        is_series = meta.get("type") == "series" or ctype == "series"
        meta = dict(meta, type="series" if is_series else "movie")  # don't mutate cache
        if is_series:
            li = make_video_item(meta, playable=False)
            url = build_url(action="meta", type="series", id=meta.get("id"))
            xbmcplugin.addDirectoryItem(HANDLE, url, li, True)
        else:
            ctx = _play_context("movie", meta.get("id"), meta.get("name"))
            if AUTOPLAY:  # click plays best; long-press offers both
                li = make_video_item(meta, playable=True, extra_ctx=ctx)
                _mark_watched(li, meta)
                url = build_url(action="play", type="movie", id=meta.get("id"),
                                title=meta.get("name"))
                xbmcplugin.addDirectoryItem(HANDLE, url, li, False)
            else:         # click opens the list; long-press offers both
                li = make_video_item(meta, playable=False, extra_ctx=ctx)
                _mark_watched(li, meta)
                url = build_url(action="streams", type="movie", id=meta.get("id"),
                                title=meta.get("name"))
                xbmcplugin.addDirectoryItem(HANDLE, url, li, True)

    # Paging: Stremio has no total-count signal. A full page (>= the catalog's
    # own pageSize, default 100) means there may be more; advance skip by the
    # number actually returned.
    page = (cat_def.get("pageSize") if cat_def else None) or STREMIO_PAGE_SIZE
    if metas and len(metas) >= page and "skip" in supported:
        nxt = xbmcgui.ListItem(label="[B]Next page >[/B]")
        url = build_url(action="catalog", addon=addon_id, type=ctype,
                        id=catalog_id, skip=skip + len(metas),
                        search=search, genre=genre)
        xbmcplugin.addDirectoryItem(HANDLE, url, nxt, True)

    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.endOfDirectory(HANDLE)


def view_meta(ctype, content_id):
    """Series detail: list episodes."""
    meta = router.get_meta(ctype, content_id, VERIFY_SSL)
    if not meta:
        notify("No metadata found")
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return

    show = meta.get("name", "")
    videos = meta.get("videos") or []
    if not videos:
        ctx = _play_context(ctype, content_id, show)
        if AUTOPLAY:
            li = make_video_item(meta, playable=True, extra_ctx=ctx)
            url = build_url(action="play", type=ctype, id=content_id, title=show)
            xbmcplugin.addDirectoryItem(HANDLE, url, li, False)
        else:
            li = make_video_item(meta, playable=False, extra_ctx=ctx)
            url = build_url(action="streams", type=ctype, id=content_id, title=show)
            xbmcplugin.addDirectoryItem(HANDLE, url, li, True)
        xbmcplugin.endOfDirectory(HANDLE)
        return

    base = meta.get("imdb_id") or ids.base_id(content_id)
    xbmcplugin.setContent(HANDLE, "episodes")

    def _epnum(v, k):
        x = v.get(k, 0)
        return int(x) if str(x).isdigit() else 0

    # Per-episode watched checkmarks + the in-progress episode's resume bar,
    # decoded from the Stremio account's watched bitfield.
    libitem, watched_ids = None, set()
    try:
        from relay import stremio_api
        if stremio_api.authorized():
            libitem = stremio_api.library_map().get(ids.base_id(content_id))
            if libitem:
                ordered = sorted(videos, key=lambda v: (_epnum(v, "season"),
                                                        _epnum(v, "episode")))
                ordered_ids = [
                    v.get("id") or (ids.episode_id(base, v["season"], v["episode"])
                                    if v.get("season") is not None
                                    and v.get("episode") is not None else "")
                    for v in ordered]
                watched_ids = stremio_api.watched_video_ids(libitem, ordered_ids)
    except Exception:  # noqa
        pass

    def _vid_sort(v):
        # Specials (season 0) go LAST (Kodi convention) so the real S01E01 is
        # first - they're often extras with no debrid sources.
        s = _epnum(v, "season")
        return (s if s > 0 else 10 ** 6, _epnum(v, "episode"))

    for video in sorted(videos, key=_vid_sort):
        li = make_episode_item(video, meta)
        # Build the episode id deterministically when the addon omits it.
        ep_id = video.get("id")
        if not ep_id and video.get("season") is not None \
                and video.get("episode") is not None:
            ep_id = ids.episode_id(base, video["season"], video["episode"])
        if not ep_id:
            continue
        if ep_id in watched_ids:
            try:
                li.getVideoInfoTag().setPlaycount(1)  # ✓ watched overlay
            except Exception:  # noqa
                pass
        elif libitem:
            lst = libitem.get("state") or {}
            if lst.get("video_id") == ep_id and lst.get("duration") \
                    and int(lst.get("timeOffset") or 0) > 1000:
                try:  # progress bar on the episode being watched
                    li.getVideoInfoTag().setResumePoint(
                        lst["timeOffset"] / 1000.0, lst["duration"] / 1000.0)
                except Exception:  # noqa
                    pass
        s, e = _epnum(video, "season"), _epnum(video, "episode")
        ep_title = video.get("title") or video.get("name") or ""
        disp = "%s S%02dE%02d" % (show, s, e) if (s or e) else show
        if ep_title and ep_title != show:
            disp = "%s - %s" % (disp, ep_title)
        li.addContextMenuItems(_play_context("series", ep_id, disp))
        if AUTOPLAY:
            li.setProperty("IsPlayable", "true")
            url = build_url(action="play", type="series", id=ep_id, title=disp)
            xbmcplugin.addDirectoryItem(HANDLE, url, li, False)
        else:
            url = build_url(action="streams", type="series", id=ep_id, title=disp)
            xbmcplugin.addDirectoryItem(HANDLE, url, li, True)
    xbmcplugin.endOfDirectory(HANDLE)


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------

def _headers_of(stream):
    """HTTP request headers a stream asks for (behaviorHints.proxyHeaders).

    Header *names* are restricted to a safe charset so a malicious server can't
    inject extra header pairs or CRLF; values are percent-encoded.
    """
    hints = stream.get("behaviorHints") or {}
    raw = (hints.get("proxyHeaders") or {}).get("request") or {}
    safe = {}
    for k, v in raw.items():
        if isinstance(k, str) and k and all(
                c.isalnum() or c == "-" for c in k):
            safe[k] = v
    return safe


def _encode_headers(headers):
    return "&".join("%s=%s" % (k, quote(str(v), safe="")) for k, v in headers.items())


def _manifest_kind(url):
    """Return 'hls' / 'mpd' if the URL is an adaptive manifest, else None."""
    path = url.split("?")[0].lower()
    if path.endswith(".m3u8"):
        return "hls"
    if path.endswith(".mpd"):
        return "mpd"
    return None


def _stash_id(content_id, ctype, filename="", videosize=""):
    """Record what we're about to play for the subtitle + scrobbler services."""
    HOME.setProperty("relay.playing_id", content_id)
    HOME.setProperty("relay.playing_type", ctype)
    HOME.setProperty("relay.playing_filename", filename or "")
    HOME.setProperty("relay.playing_videosize", str(videosize or ""))
    HOME.setProperty("relay.playing_file", "")  # bound below once the path is known
    HOME.setProperty("relay.playing_ts", str(time.time()))  # freshness gate


def _has_addon(addon_id):
    try:
        xbmcaddon.Addon(addon_id)
        return True
    except Exception:  # noqa
        return False


def _tag_play_item(li, ctype, content_id, title=None):
    tag = li.getVideoInfoTag()
    tag.setMediaType("episode" if ctype == "series" else "movie")
    if title:  # so the OSD shows the title, not the source-format label
        li.setLabel(title)
        tag.setTitle(title)
    imdb = ids.base_id(content_id)
    if imdb.startswith("tt"):
        tag.setIMDBNumber(imdb)


def _build_play_item(stream, ctype, content_id, title=None):
    """Return ``(path, ListItem)`` for a chosen stream.

    Handles YouTube (ytId), progressive files (mp4/mkv/... with pipe-headers),
    and HLS/DASH manifests (via inputstream.adaptive, Omega 21 mimetype-detected).
    """
    hints = stream.get("behaviorHints") or {}
    _stash_id(content_id, ctype, hints.get("filename") or "", hints.get("videoSize") or "")

    yt = stream.get("ytId")
    if yt:
        if not _has_addon("plugin.video.youtube"):
            notify("Install plugin.video.youtube to play YouTube streams")
        path = "plugin://plugin.video.youtube/play/?video_id=%s" % quote(yt, safe="")
        li = xbmcgui.ListItem(path=path, offscreen=True)
        li.setProperty("IsPlayable", "true")
        _tag_play_item(li, ctype, content_id, title)
        return path, li

    url = stream["url"]
    headers = _headers_of(stream)
    kind = _manifest_kind(url)

    if kind:  # HLS/DASH -> inputstream.adaptive (best DASH engine)
        if not _has_addon("inputstream.adaptive"):
            notify("Install inputstream.adaptive to play HLS/DASH")
        path = url
        li = xbmcgui.ListItem(path=path, offscreen=True)
        li.setProperty("inputstream", "inputstream.adaptive")
        li.setMimeType("application/x-mpegURL" if kind == "hls"
                       else "application/dash+xml")
        if headers:
            enc = _encode_headers(headers)
            li.setProperty("inputstream.adaptive.manifest_headers", enc)
            li.setProperty("inputstream.adaptive.stream_headers", enc)
    else:  # progressive HTTP/debrid
        path = url + "|" + _encode_headers(headers) if headers else url
        li = xbmcgui.ListItem(path=path, offscreen=True)
        # inputstream.ffmpegdirect (cURL backend) gives faster start + steadier
        # seek on progressive/notWebReady streams; headers ride the pipe URL.
        if setting_bool("use_ffmpegdirect", True) and \
                _has_addon("inputstream.ffmpegdirect"):
            li.setProperty("inputstream", "inputstream.ffmpegdirect")
            li.setProperty("inputstream.ffmpegdirect.open_mode", "curl")

    li.setProperty("IsPlayable", "true")
    li.setContentLookup(False)  # skip Kodi's HEAD probe (breaks some servers)

    if hints.get("filename"):
        li.setProperty("StremioFilename", hints["filename"])
    # Subtitles attached to this exact stream are pre-matched -> best accuracy.
    embedded = [s.get("url") for s in (stream.get("subtitles") or [])
                if s.get("url") and _http_ok(s["url"])]
    if embedded:
        li.setSubtitles(embedded)
    _tag_play_item(li, ctype, content_id, title)
    # Bind the stash to this exact path: a stash written for file A must never
    # identify file B (file-switch race: OpenFile -> onAVStarted window).
    HOME.setProperty("relay.playing_file", path)
    # Remember this stream's bingeGroup so the scrobbler can keep the same
    # source/quality/release-group for the next episode (Up Next / binge).
    HOME.setProperty("relay.playing_bingegroup",
                     (hints.get("bingeGroup") or ""))
    return path, li


_RES_MAP = [("2160", "4K"), ("4k", "4K"), ("1440", "2K"),
            ("1080", "1080p"), ("720", "720p"), ("480", "480p")]


def _human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return ("%.1f %s" % (n, unit)).replace(".0 ", " ")
        n /= 1024
    return "%.1f PB" % n


def _stream_label(stream):
    """Flatten an AIOStreams stream into one clean chooser line.

    AIOStreams already embeds quality glyphs in name/description; we pull out the
    salient bits (cache, resolution, source, HDR, codec, audio, size) into a
    compact line, falling back to the raw name.
    """
    name = (stream.get("name") or "").replace("\n", " ").strip()
    desc = (stream.get("description") or stream.get("title") or "").strip()
    hints = stream.get("behaviorHints") or {}
    blob = " ".join([name, desc, hints.get("filename", "")])

    # Cached/uncached marker. AIOStreams flags cache with emoji (⚡/⏳) in its
    # text; we DETECT those but must not RENDER emoji - Kodi's skin font shows
    # them as tofu boxes. Use a geometric dot (always in Noto Sans) + colour.
    parts = []
    if any(g in name + desc for g in ("⚡", "⌁", "♛", "⭑", "cached", "Cached")):
        parts.append("[COLOR lime]●[/COLOR]")   # ● cached
    elif any(g in name + desc for g in ("⏳", "∅")):
        parts.append("[COLOR grey]○[/COLOR]")   # ○ uncached

    for needle, lbl in _RES_MAP:
        if re.search(needle, blob, re.I):
            parts.append(lbl)
            break
    m = re.search(r"REMUX|BluRay|Blu-?ray|WEB-?DL|WEBRip|HDTV|BDRip|DVDRip", blob, re.I)
    if m:
        parts.append(m.group(0).upper().replace("BLU-RAY", "BluRay"))
    for tag in ("HDR10+", "HDR10", "HDR", "Dolby Vision", "DV"):
        if re.search(re.escape(tag), blob, re.I):
            parts.append("DV" if tag in ("Dolby Vision", "DV") else tag)
            break
    m = re.search(r"x ?26[45]|HEVC|AV1|AVC", blob, re.I)
    if m:
        parts.append(m.group(0).upper().replace("HEVC", "x265").replace(" ", ""))
    m = re.search(r"Atmos|TrueHD|DTS-?HD|DTS|DDP\+?|EAC3|AC3|AAC|FLAC", blob, re.I)
    if m:
        parts.append(m.group(0))
    if hints.get("videoSize"):
        parts.append(_human_size(hints["videoSize"]))
    else:
        m = re.search(r"\d+(?:\.\d+)?\s?[GM]B", blob)
        if m:
            parts.append(m.group(0))

    src = stream.get("_addon", "")
    body = "  ·  ".join(p for p in parts if p) or name or "Stream"
    return "%s   [%s]" % (body, src) if src else body


def _playable_streams(streams):
    """Streams Kodi can resolve now: http(s) urls + YouTube ids."""
    out = []
    for s in streams:
        url = s.get("url")
        if (url and _http_ok(url)) or s.get("ytId"):
            out.append(s)
    return out


def _stream_sort_key(stream):
    """Rank best-first: cached, then resolution, HDR, larger size."""
    name = stream.get("name") or ""
    desc = stream.get("description") or stream.get("title") or ""
    blob = "%s %s %s" % (name, desc, (stream.get("behaviorHints") or {}).get("filename", ""))
    cached = 0 if any(g in name + desc for g in ("⚡", "⌁", "♛", "⭑")) else 1
    res = len(_RES_MAP)
    for i, (needle, _lbl) in enumerate(_RES_MAP):
        if re.search(needle, blob, re.I):
            res = i
            break
    hdr = 0 if re.search(r"HDR|Dolby Vision|\bDV\b", blob, re.I) else 1
    try:
        size = int((stream.get("behaviorHints") or {}).get("videoSize") or 0)
    except (TypeError, ValueError):
        size = 0
    return (cached, res, hdr, -size)


def _sorted_playable(streams):
    """Playable streams, best-first."""
    return sorted(_playable_streams(streams), key=_stream_sort_key)


def _stream_sid(stream):
    """Stable id for a stream (its URL/ytId). Used to resolve the exact stream
    the user picked even if the cached list changed - safer than a list index."""
    raw = stream.get("url") or stream.get("ytId") or stream.get("name") or ""
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def _enrich_from_meta(li, ctype, content_id, want_title=False):
    """Add poster/fanart + plot/genre/rating to the resolved item by looking the
    id up via our meta add-on (cached). Kodi won't fetch art from an id for
    plugin playback, so we do - this makes the OSD and Info dialog rich.

    ``want_title`` also sets the OSD title from meta - used when no title was
    threaded (e.g. the manual TMDbHelper player path)."""
    try:
        meta = router.get_meta("series" if ctype == "series" else "movie",
                               ids.base_id(content_id), VERIFY_SSL)
    except Exception:  # noqa
        meta = None
    if not meta:
        return
    if want_title and meta.get("name"):
        title = meta["name"]
        _b, s, e = ids.split_series_id(content_id)
        if ctype == "series" and s is not None and e is not None:
            title = "%s S%02dE%02d" % (title, s, e)
        li.setLabel(title)
        li.getVideoInfoTag().setTitle(title)
    art = {}
    if meta.get("poster"):
        art["poster"] = art["thumb"] = meta["poster"]
    if meta.get("background"):
        art["fanart"] = art["landscape"] = meta["background"]
    if meta.get("logo"):
        art["clearlogo"] = meta["logo"]
    if art:
        li.setArt(art)
    tag = li.getVideoInfoTag()
    if meta.get("description"):
        tag.setPlot(meta["description"])
    genres = meta.get("genres") or meta.get("genre")
    if genres:
        tag.setGenres(genres if isinstance(genres, list) else [genres])
    if meta.get("imdbRating"):
        try:
            tag.setRating(float(meta["imdbRating"]))
        except (ValueError, TypeError):
            pass


def view_streams(ctype, content_id, title=None):
    """List a title's streams as a directory of playable items.

    Selection happens in a normal folder, NOT via a modal dialog during URL
    resolution. The modal-during-resolve pattern intermittently fails on the
    2nd+ playback (the dialog silently returns -1), which is why the previous
    chooser worked once then did nothing. ``title`` is the movie/show title,
    threaded through to the resolved item so the OSD shows it (not the source).
    """
    streams = router.get_streams(ctype, content_id, VERIFY_SSL)
    playable = _sorted_playable(streams)

    if not playable:
        others = [s for s in streams if s.get("infoHash") or s.get("externalUrl")]
        if others:
            kinds = sorted({"torrent" if s.get("infoHash") else "external"
                            for s in others})
            notify("Only unsupported streams (%s)" % ", ".join(kinds))
        else:
            notify("No streams found")
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return

    xbmcplugin.setContent(HANDLE, "videos")
    for stream in playable:
        li = xbmcgui.ListItem(label=_stream_label(stream))  # source label in the list
        li.setLabel2(stream.get("_addon", ""))
        li.setProperty("IsPlayable", "true")
        tag = li.getVideoInfoTag()
        tag.setMediaType("episode" if ctype == "series" else "movie")
        if title:
            tag.setTitle(title)  # OSD title even if Kodi reads the outer item
        url = build_url(action="play", type=ctype, id=content_id,
                        sid=_stream_sid(stream), title=title)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, False)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


# The resume prompt now lives in the scrobbler service (service.py): it shows
# per stream DURING playback for both our own and external Stremio playback,
# with a 10s auto-close that defaults to Resume - so playback start is never
# delayed and the behaviour is identical in every flow.


def play(ctype, content_id, sid=None, title=None, bg=None):
    """Resolve a stream via setResolvedUrl (no modal dialog here).

    The exact stream is matched by ``sid`` (stable, survives a changed/expired
    cache); with no sid (auto-play leaf / TMDbHelper) the best stream is used.
    ``title`` (when supplied) becomes the OSD title.
    """
    playable = _sorted_playable(router.get_streams(ctype, content_id, VERIFY_SSL))
    if not playable:
        notify("No streams found")
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return
    chosen = None
    if sid:
        chosen = next((s for s in playable if _stream_sid(s) == sid), None)
    if chosen is None and bg:  # binge continuity: same release group/quality
        chosen = next((s for s in playable
                       if (s.get("behaviorHints") or {}).get("bingeGroup") == bg),
                      None)
    if chosen is None:
        chosen = playable[0]  # best: auto-play, TMDbHelper, or expired-cache fallback
    _path, li = _build_play_item(chosen, ctype, content_id, title)
    # poster/fanart/plot by id (cached); also fill the title when none was
    # threaded (manual TMDbHelper path passes only an id).
    _enrich_from_meta(li, ctype, content_id, want_title=not title)
    xbmcplugin.setResolvedUrl(HANDLE, True, li)


def play_select(ctype, content_id, title=None):
    """Manual source picker for the TMDbHelper 'Choose source' player.

    TMDbHelper runs this as a *resolvable* player (is_resolvable:true), so it must
    end in setResolvedUrl - a directory (action=streams) does NOT work there
    (TMDbHelper falls through to Player().play() on the folder URL and nothing
    plays). We force-close the busy dialog first so the modal select() doesn't
    silently return -1 mid-resolve (the jacktook-proven pattern). The in-Kodi
    long-press 'Choose source...' still uses the action=streams directory.
    """
    xbmc.executebuiltin("Dialog.Close(busydialog, force)")
    playable = _sorted_playable(router.get_streams(ctype, content_id, VERIFY_SSL))
    if not playable:
        notify("No streams found")
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return
    idx = xbmcgui.Dialog().select("Choose source",
                                  [_stream_label(s) for s in playable])
    if idx < 0:  # user cancelled
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return
    _path, li = _build_play_item(playable[idx], ctype, content_id, title)
    _enrich_from_meta(li, ctype, content_id, want_title=not title)
    xbmcplugin.setResolvedUrl(HANDLE, True, li)


# ---------------------------------------------------------------------------
# Continue Watching (Stremio account)
# ---------------------------------------------------------------------------

def view_cw():
    """The account's Continue Watching row: click resumes (best source); the
    in-playback resume prompt then offers the exact saved position."""
    from relay import stremio_api
    items = stremio_api.continue_watching()
    if not items:
        notify("Nothing in Continue Watching")
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        return
    xbmcplugin.setContent(HANDLE, "videos")
    for it in items:
        st = it.get("state") or {}
        base = it.get("_id") or ""
        ctype = "series" if it.get("type") == "series" else "movie"
        play_id = (st.get("video_id") or base) if ctype == "series" else base
        pct = 0
        if st.get("duration"):
            pct = int(round(100.0 * (st.get("timeOffset") or 0) / st["duration"]))
        label = it.get("name") or base
        _b, s, e = ids.split_series_id(play_id)
        if ctype == "series" and s is not None:
            label += " · S%02dE%02d" % (s, e)
        if 0 < pct < 100:
            label += "  [COLOR gray]%d%%[/COLOR]" % pct
        li = xbmcgui.ListItem(label=label, offscreen=True)
        if it.get("poster"):
            li.setArt({"poster": it["poster"], "thumb": it["poster"]})
        li.setProperty("IsPlayable", "true")
        tag = li.getVideoInfoTag()
        tag.setMediaType("episode" if ctype == "series" else "movie")
        tag.setTitle(it.get("name") or "")
        try:  # progress bar in skins that render resume points
            if st.get("duration") and st.get("timeOffset"):
                tag.setResumePoint(st["timeOffset"] / 1000.0,
                                   st["duration"] / 1000.0)
        except Exception:  # noqa - older API
            pass
        li.addContextMenuItems([
            ("Choose source…", "Container.Update(%s)" % build_url(
                action="streams", type=ctype, id=play_id, title=it.get("name"))),
            ("Remove from Continue Watching", "RunPlugin(%s)" % build_url(
                action="cw_dismiss", id=base)),
        ])
        url = build_url(action="play", type=ctype, id=play_id,
                        title=it.get("name"))
        xbmcplugin.addDirectoryItem(HANDLE, url, li, False)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def action_cw_dismiss(lib_id):
    from relay import stremio_api
    if stremio_api.dismiss_cw(lib_id):
        notify("Removed from Continue Watching")
    xbmc.executebuiltin("Container.Refresh")


def view_new():
    """Library shows with an aired, unwatched episode waiting (account-driven)."""
    from relay import stremio_api
    items = stremio_api.new_episodes()
    if not items:
        notify("No new episodes")
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        return
    xbmcplugin.setContent(HANDLE, "videos")
    for it in items:
        eid = it["episode_id"]
        label = "%s · S%02dE%02d" % (it["name"], it["season"], it["episode"])
        li = xbmcgui.ListItem(label=label, offscreen=True)
        if it.get("poster"):
            li.setArt({"poster": it["poster"], "thumb": it["poster"]})
        li.setProperty("IsPlayable", "true")
        tag = li.getVideoInfoTag()
        tag.setMediaType("episode")
        tag.setTitle(it["name"])
        li.addContextMenuItems([
            ("Choose source…", "Container.Update(%s)" % build_url(
                action="streams", type="series", id=eid, title=it["name"])),
        ])
        url = build_url(action="play", type="series", id=eid, title=it["name"])
        xbmcplugin.addDirectoryItem(HANDLE, url, li, False)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


# ---------------------------------------------------------------------------
# Search (with history) + add-on management
# ---------------------------------------------------------------------------

def _hist_path():
    return os.path.join(
        xbmcvfs.translatePath(ADDON.getAddonInfo("profile")),
        "search_history.json")

def _hist_load():
    try:
        with open(_hist_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _hist_save(items):
    try:
        os.makedirs(os.path.dirname(_hist_path()), exist_ok=True)
        with open(_hist_path(), "w", encoding="utf-8") as fh:
            json.dump(items[:15], fh)
    except OSError:
        pass


def _hist_add(query):
    items = [q for q in _hist_load() if q.lower() != query.lower()]
    _hist_save([query] + items)


def view_search_menu():
    """Search hub: new search + recent queries (long-press to remove)."""
    li = xbmcgui.ListItem(label="[B]New search…[/B]")
    xbmcplugin.addDirectoryItem(HANDLE, build_url(action="search_new"), li, True)
    history = _hist_load()
    for q in history:
        li = xbmcgui.ListItem(label=q)
        li.addContextMenuItems([
            ("Remove from history", "RunPlugin(%s)" % build_url(
                action="hist_del", q=q)),
            ("Clear history", "RunPlugin(%s)" % build_url(action="hist_clear")),
        ])
        xbmcplugin.addDirectoryItem(HANDLE, build_url(action="search_run", q=q),
                                    li, True)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def view_search_new():
    query = xbmcgui.Dialog().input("Search")
    if not query:
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return
    _hist_add(query)
    _search_results(query)


def view_search_run(query):
    _hist_add(query)  # bump to most-recent
    _search_results(query)


def _search_results(query):
    """List the search-capable catalogs carrying this query."""
    targets = []
    for addon, cat in router.list_catalogs(VERIFY_SSL):
        supported, _required = catalog_extras(cat)
        if "search" in supported:
            targets.append((addon, cat))
    if not targets:
        notify("No searchable catalogs")
        xbmcplugin.endOfDirectory(HANDLE)
        return
    for addon, cat in targets:
        label = "%s  ·  %s" % (cat.get("name", cat.get("id")), addon["name"])
        li = xbmcgui.ListItem(label=label)
        url = build_url(action="catalog", addon=addon["entryId"],
                        type=cat.get("type", "movie"), id=cat.get("id"),
                        skip=0, search=query)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, True)
    xbmcplugin.endOfDirectory(HANDLE)


def action_hist_del(query):
    _hist_save([q for q in _hist_load() if q != query])
    xbmc.executebuiltin("Container.Refresh")


def action_hist_clear():
    _hist_save([])
    xbmc.executebuiltin("Container.Refresh")


def view_manage():
    add = xbmcgui.ListItem(label="[B]+ Add add-on (manifest URL)[/B]")
    xbmcplugin.addDirectoryItem(HANDLE, build_url(action="add"), add, True)
    hc = xbmcgui.ListItem(label="[B]Check add-ons health[/B]")
    xbmcplugin.addDirectoryItem(HANDLE, build_url(action="health"), hc, False)
    for entry in store.load_entries():
        addon = router.addon_by_id(entry["id"], VERIFY_SSL)
        name = addon["name"] if addon else ("Add-on " + entry["id"])  # never show secret URL
        state = "" if entry.get("enabled", True) else "  [COLOR gray](disabled)[/COLOR]"
        res = ", ".join(sorted({r["name"] for r in addon["resources"]})) if addon else "?"
        li = xbmcgui.ListItem(label="%s%s" % (name, state))
        li.setLabel2(res)
        li.addContextMenuItems([
            ("Toggle enabled", "RunPlugin(%s)" % build_url(
                action="toggle", addon=entry["id"])),
            ("Remove", "RunPlugin(%s)" % build_url(
                action="remove", addon=entry["id"])),
        ])
        xbmcplugin.addDirectoryItem(
            HANDLE, build_url(action="toggle", addon=entry["id"]), li, False)
    # Stremio-account add-ons: enable/disable + priority are LOCAL overrides
    # (click toggles; long-press also offers Move up / Move down). The Stremio
    # account itself is never modified from here.
    if setting_bool("stremio_account_addons", False):
        try:
            acct = router._account_entries(include_disabled=True)
        except Exception:  # noqa
            acct = []
        for e in acct:
            state = "" if e.get("enabled", True) else "  [COLOR gray](disabled)[/COLOR]"
            li = xbmcgui.ListItem(label="%s  [COLOR cyan](account)[/COLOR]%s"
                                  % (e["name"], state))
            li.addContextMenuItems([
                ("Toggle enabled", "RunPlugin(%s)" % build_url(
                    action="acct_toggle", addon=e["id"])),
                ("Move up", "RunPlugin(%s)" % build_url(
                    action="acct_move", addon=e["id"], dir="up")),
                ("Move down", "RunPlugin(%s)" % build_url(
                    action="acct_move", addon=e["id"], dir="down")),
            ])
            xbmcplugin.addDirectoryItem(
                HANDLE, build_url(action="acct_toggle", addon=e["id"]), li, False)
        # Push the local enable/disable/reorder up to the Stremio account
        # (explicit, never automatic) - shown only when there ARE local changes.
        if store.load_account_overrides():
            ap = xbmcgui.ListItem(
                label="[B]Apply add-on changes to Stremio account[/B]")
            xbmcplugin.addDirectoryItem(
                HANDLE, build_url(action="apply_account_addons"), ap, False)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def action_add():
    """Add a manifest. Works both as a folder item and as a Settings RunPlugin."""
    url = xbmcgui.Dialog().input("Stremio manifest URL (or addon base URL)")
    if url:
        notify("Add-on added" if store.add_url(url) else "Already present or invalid")
    if HANDLE >= 0:  # entered as a folder - stay on parent
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)
    xbmc.executebuiltin("Container.Refresh")


_ADVANCED_SETTINGS = """<advancedsettings>
  <cache>
    <buffermode>1</buffermode>
    <memorysize>157286400</memorysize>
    <readfactor>30</readfactor>
  </cache>
  <network>
    <curlclienttimeout>10</curlclienttimeout>
    <curllowspeedtime>20</curllowspeedtime>
  </network>
</advancedsettings>
"""


def action_trakt_auth():
    """Authorize Trakt via the device-code flow (records watch history/progress)."""
    from relay import trakt
    if not ADDON.getSetting("trakt_client_id") or not ADDON.getSetting("trakt_secret"):
        xbmcgui.Dialog().ok(
            "Trakt setup needed",
            "Create a free Trakt app and paste its Client ID and Client Secret "
            "under Settings > Trakt first.\n\nSee the README (\"Trakt setup\") "
            "for step-by-step instructions.")
        return
    if trakt.is_authorized():
        if not xbmcgui.Dialog().yesno("Trakt", "Already authorized. Re-authorize?"):
            return
    dev = trakt.device_code()
    if not dev:
        notify("Trakt: couldn't start auth (check client id/secret)")
        return
    dlg = xbmcgui.DialogProgress()
    dlg.create("Authorize Trakt",
               "On any device go to:\n[B]%s[/B]\nand enter code:  [B]%s[/B]"
               % (dev.get("verification_url", "trakt.tv/activate"),
                  dev.get("user_code", "")))
    ok = trakt.poll_token(dev, should_cancel=dlg.iscanceled)
    dlg.close()
    notify("Trakt authorized" if ok else "Trakt authorization failed/cancelled")


def action_trakt_logout():
    from relay import trakt
    trakt.logout()
    notify("Trakt signed out")


def action_stremio_login():
    """Prompt for email + password (masked), exchange them for an authKey, and
    keep only the authKey (0600). Prompt-only by design: Kodi doesn't persist
    settings-dialog fields until the dialog closes, so a button can't read
    freshly-typed fields - dialogs are the one reliable TV flow."""
    from relay import stremio_api
    prefill = stremio_api.account_email()
    email = xbmcgui.Dialog().input("Stremio email", defaultt=prefill).strip()
    if not email:
        return
    password = xbmcgui.Dialog().input(
        "Stremio password", option=xbmcgui.ALPHANUM_HIDE_INPUT)
    if not password:
        return
    ok, err = stremio_api.login(email, password)
    if ok:
        store.bump_generation()  # account add-ons may change
        notify("Stremio: signed in as %s" % stremio_api.account_email())
    else:
        notify("Stremio sign-in failed: %s" % err)


def action_stremio_logout():
    from relay import stremio_api
    stremio_api.logout()
    store.bump_generation()
    notify("Stremio signed out")


def action_refresh_account_addons():
    from relay import stremio_api
    if not stremio_api.authorized():
        notify("Sign in to Stremio first")
        return
    addons = stremio_api.get_account_addons(force=True)
    store.bump_generation()  # resident services drop their memoized add-on list
    notify("Stremio account add-ons: %d imported" % len(addons))


def action_trailer(yt):
    if not _has_addon("plugin.video.youtube"):
        notify("Install plugin.video.youtube to play trailers")
        return
    xbmc.executebuiltin(
        "PlayMedia(plugin://plugin.video.youtube/play/?video_id=%s)" % quote(yt, safe=""))


def action_optimize_buffer():
    """Write a tuned advancedsettings.xml so HTTP/debrid streams start fast.

    Non-destructive: if one already exists we show the block to merge by hand.
    """
    path = xbmcvfs.translatePath("special://userdata/advancedsettings.xml")
    if os.path.exists(path):
        xbmcgui.Dialog().textviewer(
            "advancedsettings.xml exists - add this <cache> block, then restart Kodi",
            _ADVANCED_SETTINGS)
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_ADVANCED_SETTINGS)
    except OSError:
        notify("Could not write advancedsettings.xml")
        return
    xbmcgui.Dialog().ok("Relay",
                        "Optimized playback buffering written.\n"
                        "Restart Kodi to apply.")


def action_health():
    """Probe every enabled add-on's manifest (fresh, parallel) and report
    status + latency - pinpoints the broken layer without log digging."""
    entries = [dict(e, _src="local") for e in store.load_entries()
               if e.get("enabled", True)]
    try:
        entries += [dict(e, _src="account") for e in router._account_entries()]
    except Exception:  # noqa
        pass
    if not entries:
        notify("No add-ons configured")
        if HANDLE >= 0:
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return

    def probe(e):
        t0 = time.time()
        m = client.fetch_json(e["manifestUrl"], timeout=8,
                              verify_ssl=VERIFY_SSL, cache_ttl=0)
        dt = time.time() - t0
        name = (m or {}).get("name") or e.get("name") or ("Add-on " + e.get("id", "?"))
        res = ""
        if m:
            res = ", ".join(sorted({r["name"] if isinstance(r, dict) else r
                                    for r in m.get("resources") or []}))
        return bool(m), name, dt, e.get("_src", "?"), res

    results = router._parallel(probe, entries)
    lines = []
    for ok, name, dt, src, res in sorted(results, key=lambda r: (r[0] is False, r[2])):
        mark = "[COLOR lime]OK  [/COLOR]" if ok else "[COLOR red]FAIL[/COLOR]"
        extra = ("  ·  " + res) if res else ""
        lines.append("%s %5.2fs  %s  [%s]%s" % (mark, dt, name, src, extra))
    xbmcgui.Dialog().textviewer("Add-on health", "\n".join(lines))
    if HANDLE >= 0:
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())


_BACKUP_FILES = ("addons.json", "account_addons.json", "trakt.json",
                 "stremio.json", "settings.xml", "search_history.json",
                 "cachegen")


def action_backup_config():
    """Bundle the add-on's config + tokens into one 0600 file on the device."""
    prof = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
    bundle = {"_created": time.strftime("%Y-%m-%d %H:%M:%S"), "files": {}}
    for fn in _BACKUP_FILES:
        p = os.path.join(prof, fn)
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    bundle["files"][fn] = fh.read()
            except OSError:
                pass
    dest = xbmcvfs.translatePath("special://home/relay-backup.json")
    try:
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(bundle, fh, indent=1)
    except OSError:
        notify("Backup failed")
        return
    xbmcgui.Dialog().ok("Relay",
                        "Backed up %d file(s) to:\n%s\n\n"
                        "It contains account tokens - keep it private."
                        % (len(bundle["files"]), dest))


def action_restore_config():
    src = xbmcvfs.translatePath("special://home/relay-backup.json")
    try:
        with open(src, encoding="utf-8") as fh:
            bundle = json.load(fh)
    except (OSError, ValueError):
        notify("No backup found at special://home/relay-backup.json")
        return
    files = bundle.get("files") or {}
    if not files or not xbmcgui.Dialog().yesno(
            "Restore config",
            "Restore %d file(s) from %s?\nExisting configuration is overwritten."
            % (len(files), bundle.get("_created", "?"))):
        return
    prof = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
    os.makedirs(prof, exist_ok=True)
    n = 0
    for fn, content in files.items():
        if fn not in _BACKUP_FILES:   # whitelist - never write arbitrary names
            continue
        try:
            fd = os.open(os.path.join(prof, fn),
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            n += 1
        except OSError:
            pass
    store.bump_generation()
    xbmcgui.Dialog().ok("Relay",
                        "Restored %d file(s). Restart Kodi to fully apply." % n)


def action_clear_cache():
    """Wipe the on-disk result cache (catalogs/meta/streams/subtitles).

    Each entry is TTL'd and the dir auto-prunes at 400 files, so this is just a
    'fetch everything fresh now' button. Also bumps the generation counter so the
    resident services drop any in-memory memo."""
    cache_dir = client._disk_dir()
    removed = 0
    try:
        for fn in os.listdir(cache_dir):
            if fn.endswith(".json"):
                try:
                    os.remove(os.path.join(cache_dir, fn))
                    removed += 1
                except OSError:
                    pass
    except OSError:
        pass
    store.bump_generation()  # invalidate any in-process/service memo too
    notify("Cleared cache (%d item%s)" % (removed, "" if removed == 1 else "s"))


def _set_kodi_setting(setting, value):
    q = {"jsonrpc": "2.0", "id": 1, "method": "Settings.SetSettingValue",
         "params": {"setting": setting, "value": value}}
    try:
        return json.loads(xbmc.executeJSONRPC(json.dumps(q))).get("result") is True
    except Exception:  # noqa
        return False


def _font_family(path):
    """Read a TTF/OTF font's family name from its 'name' table. Kodi lists subtitle
    fonts by family name, so we need this to auto-select the downloaded font."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        num = struct.unpack(">H", data[4:6])[0]
        noff = 0
        for i in range(num):
            rec = data[12 + i * 16:28 + i * 16]
            if rec[0:4] == b"name":
                noff = struct.unpack(">I", rec[8:12])[0]
                break
        if not noff:
            return None
        count = struct.unpack(">H", data[noff + 2:noff + 4])[0]
        soff = struct.unpack(">H", data[noff + 4:noff + 6])[0]
        fam = None
        for i in range(count):
            r = noff + 6 + i * 12
            pid, _eid, _lid, nid, ln, off = struct.unpack(">HHHHHH", data[r:r + 12])
            if nid not in (1, 16):
                continue
            s = data[noff + soff + off:noff + soff + off + ln]
            try:
                val = s.decode("utf-16-be" if pid in (0, 3) else "latin-1").strip()
            except Exception:  # noqa
                continue
            if not val:
                continue
            if nid == 16:        # typographic family preferred
                return val
            fam = fam or val     # nid == 1 fallback
        return fam
    except Exception:  # noqa
        return None


def action_download_font():
    """Download a font into Kodi's subtitle font folder and select it.

    Kodi scans special://home/media/Fonts/ (capital F - case matters on Android)
    for subtitle fonts and lists them by family name; we set subtitles.fontname.
    """
    url = ADDON.getSetting("font_url").strip()
    if urlsplit(url).scheme not in ("http", "https"):
        notify("Set a valid font URL in Settings")
        return
    raw = unquote(os.path.basename(urlsplit(url).path)) or "subtitle-font.ttf"
    root, ext = os.path.splitext(raw)
    if ext.lower() not in (".ttf", ".otf"):
        ext = ".ttf"
    name = "".join(c for c in root.split("[")[0] if c.isalnum() or c in "-_") \
        or "subtitle-font"
    fonts_dir = xbmcvfs.translatePath("special://home/media/Fonts/")
    dest = os.path.join(fonts_dir, name + ext)
    try:
        os.makedirs(fonts_dir, exist_ok=True)
        data, _gz = client._fetch_raw(url, 30, VERIFY_SSL)
        with open(dest, "wb") as fh:
            fh.write(data)
    except Exception as exc:  # noqa
        xbmcgui.Dialog().ok("Relay",
                            "Font download failed (%s).\nURL: %s"
                            % (type(exc).__name__, url))
        return
    family = _font_family(dest)
    set_ok = _set_kodi_setting("subtitles.fontname", family) if family else False
    if set_ok:
        tail = "Selected '%s' as the subtitle font. Restart Kodi to apply." % family
    else:
        tail = ("Now pick it: settings level Advanced -> Player -> Subtitles -> "
                "Font -> %s." % (family or name))
    xbmcgui.Dialog().ok("Relay",
                        "Saved to media/Fonts (family: %s).\n\n%s"
                        % (family or "?", tail))


def action_install_tmdbhelper():
    """Register this add-on as a TMDbHelper 'player' so its rich discover lists
    and Trakt Continue-Watching/Next-Up can play through us."""
    try:
        xbmcaddon.Addon("plugin.video.themoviedb.helper")
    except Exception:  # noqa
        notify("TMDbHelper is not installed")
        return
    ddir = xbmcvfs.translatePath(
        "special://profile/addon_data/plugin.video.themoviedb.helper/players/")
    if not xbmcvfs.exists(ddir):
        xbmcvfs.mkdirs(ddir)
    done = 0
    for fname in ("relay.json", "relay_select.json"):  # auto + manual
        src = xbmcvfs.translatePath(
            "special://home/addons/plugin.video.relay/" + fname)
        dst = os.path.join(ddir, fname)
        if xbmcvfs.exists(dst):
            xbmcvfs.delete(dst)
        if xbmcvfs.copy(src, dst):
            done += 1
    xbmcgui.Dialog().ok(
        "Relay",
        "Installed %d TMDbHelper player(s):\n"
        "  • Relay (Auto-play)\n"
        "  • Relay (Choose source)\n\n"
        "Pick one when you play in TMDbHelper (or set a default)." % done)


def action_remove_menu():
    """Pick an add-on to remove (used from Settings and from Manage)."""
    entries = store.load_entries()
    if not entries:
        notify("No add-ons to remove")
        return
    labels = []
    for e in entries:
        a = router.addon_by_id(e["id"], VERIFY_SSL)
        labels.append(a["name"] if a else ("Add-on " + e["id"]))
    idx = xbmcgui.Dialog().select("Remove add-on", labels)
    if idx >= 0:
        store.remove_url(entries[idx]["manifestUrl"])
        notify("Removed")
        xbmc.executebuiltin("Container.Refresh")


def action_remove(addon_id):
    entry = store.find(addon_id)
    if entry and xbmcgui.Dialog().yesno("Remove add-on", "Remove this add-on?"):
        store.remove_url(entry["manifestUrl"])
        xbmc.executebuiltin("Container.Refresh")
    if HANDLE >= 0:
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())


def action_toggle(addon_id):
    entry = store.find(addon_id)
    if entry:
        store.set_enabled(entry["manifestUrl"], not entry.get("enabled", True))
        xbmc.executebuiltin("Container.Refresh")
    if HANDLE >= 0:
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())


def action_acct_toggle(addon_id):
    """Enable/disable a Stremio-account add-on locally (account untouched)."""
    overrides = store.load_account_overrides()
    cur = bool((overrides.get(addon_id) or {}).get("enabled", True))
    overrides.setdefault(addon_id, {})["enabled"] = not cur
    store.save_account_overrides(overrides)
    xbmc.executebuiltin("Container.Refresh")
    if HANDLE >= 0:
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())


def action_acct_move(addon_id, direction):
    """Reorder a Stremio-account add-on (priority: affects meta-provider order
    and catalog listing order)."""
    entries = router._account_entries(include_disabled=True)
    order = [e["id"] for e in entries]
    if addon_id not in order:
        return
    i = order.index(addon_id)
    j = i - 1 if direction == "up" else i + 1
    if not 0 <= j < len(order):
        return
    order[i], order[j] = order[j], order[i]
    overrides = store.load_account_overrides()
    for pos, aid in enumerate(order):
        overrides.setdefault(aid, {})["order"] = pos
    store.save_account_overrides(overrides)
    xbmc.executebuiltin("Container.Refresh")


def action_apply_account_addons():
    """Push the LOCAL enable/disable/reorder of account add-ons up to the Stremio
    account (AddonCollectionSet). Explicit + confirmed; disabling removes the
    add-on from the account everywhere. Official add-ons are preserved."""
    from relay import stremio_api
    overrides = store.load_account_overrides()
    if not overrides:
        notify("No local add-on changes to apply")
        return
    raw = stremio_api.account_collection_raw()
    if not raw:
        notify("Could not read account add-ons")
        return
    officials, customs = [], []
    for a in raw:
        if not a.get("transportUrl"):
            continue
        m = a.get("manifest") or {}
        (officials if m.get("id") in stremio_api.OFFICIAL_IDS
         else customs).append(a)
    kept, removed = [], []
    for idx, a in enumerate(customs):
        aid = store.entry_id(a["transportUrl"])
        ov = overrides.get(aid) or {}
        if ov.get("enabled", True) is False:
            removed.append((a.get("manifest") or {}).get("name") or aid)
        else:
            kept.append((ov.get("order", idx), a))
    kept.sort(key=lambda t: t[0])
    new_customs = [a for _o, a in kept]
    new_collection = officials + new_customs
    names = [(a.get("manifest") or {}).get("name", "?") for a in new_customs]
    msg = "New order:\n" + "\n".join("%d. %s" % (i + 1, n)
                                     for i, n in enumerate(names))
    if removed:
        msg += ("\n\nRemove from account (uninstalls everywhere):\n"
                + "\n".join("- " + r for r in removed))
    if not xbmcgui.Dialog().yesno("Apply to Stremio account", msg,
                                  yeslabel="Apply", nolabel="Cancel"):
        return
    if stremio_api.set_account_addons(new_collection):
        store.save_account_overrides({})  # applied -> baseline is now the account
        notify("Add-ons synced to Stremio account")
        xbmc.executebuiltin("Container.Refresh")
    else:
        notify("Failed to update account")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _terminate(action):
    """Make sure every action closes its handle so Kodi never hangs."""
    if action in ("play", "streams_select"):
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
    elif action in (None, "type", "group", "catalog", "meta", "streams",
                    "search_menu", "search_new", "search_run", "cw", "new",
                    "manage", "add"):
        try:
            xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        except Exception:  # noqa
            pass


def router_dispatch():
    params = dict(parse_qsl(sys.argv[2][1:]))
    action = params.get("action")
    try:
        if not action:
            view_root()
        elif action == "type":
            view_type(params["t"])
        elif action == "group":
            view_group(params["name"], params["t"])
        elif action == "catalog":
            view_catalog(params["addon"], params["type"], params["id"],
                         skip=int(params.get("skip", 0)),
                         search=params.get("search"), genre=params.get("genre"))
        elif action == "meta":
            view_meta(params["type"], params["id"])
        elif action == "streams":
            view_streams(params["type"], params["id"], params.get("title"))
        elif action == "play":
            play(params["type"], params["id"], params.get("sid"),
                 params.get("title"), params.get("bg"))
        elif action == "streams_select":
            play_select(params["type"], params["id"], params.get("title"))
        elif action == "search_menu":
            view_search_menu()
        elif action == "search_new":
            view_search_new()
        elif action == "search_run":
            view_search_run(params["q"])
        elif action == "hist_del":
            action_hist_del(params["q"])
        elif action == "hist_clear":
            action_hist_clear()
        elif action == "cw":
            view_cw()
        elif action == "new":
            view_new()
        elif action == "cw_dismiss":
            action_cw_dismiss(params["id"])
        elif action == "health":
            action_health()
        elif action == "backup_config":
            action_backup_config()
        elif action == "restore_config":
            action_restore_config()
        elif action == "manage":
            view_manage()
        elif action == "add":
            action_add()
        elif action == "install_tmdbhelper":
            action_install_tmdbhelper()
        elif action == "optimize_buffer":
            action_optimize_buffer()
        elif action == "clear_cache":
            action_clear_cache()
        elif action == "download_font":
            action_download_font()
        elif action == "trakt_auth":
            action_trakt_auth()
        elif action == "trakt_logout":
            action_trakt_logout()
        elif action == "stremio_login":
            action_stremio_login()
        elif action == "stremio_logout":
            action_stremio_logout()
        elif action == "refresh_account_addons":
            action_refresh_account_addons()
        elif action == "trailer":
            action_trailer(params["yt"])
        elif action == "remove_menu":
            action_remove_menu()
        elif action == "remove":
            action_remove(params["addon"])
        elif action == "toggle":
            action_toggle(params["addon"])
        elif action == "acct_toggle":
            action_acct_toggle(params["addon"])
        elif action == "acct_move":
            action_acct_move(params["addon"], params.get("dir", "up"))
        elif action == "apply_account_addons":
            action_apply_account_addons()
        else:
            view_root()
    except Exception as exc:  # noqa - never leave Kodi spinning
        xbmc.log("[relay] action %s failed: %r" % (action, exc),
                 xbmc.LOGERROR)
        notify("Error: %s" % type(exc).__name__)
        _terminate(action)


if __name__ == "__main__":
    router_dispatch()
