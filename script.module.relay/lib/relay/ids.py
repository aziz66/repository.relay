"""ID helpers for the Stremio addon protocol.

Stremio content IDs are prefixed strings, e.g.::

    tt0111161            IMDB movie
    tt0386676:1:1        IMDB series episode (id:season:episode)
    tmdb:603             TMDB
    kitsu:1              Kitsu anime

An addon declares which prefixes it accepts via ``idPrefixes`` (per-resource or
manifest-wide). Matching is a simple ``startswith`` test.
"""

from __future__ import annotations


def id_matches_prefixes(content_id, prefixes):
    """True if ``content_id`` is served by an addon declaring ``prefixes``.

    An empty/None prefix list means "no restriction" -> always matches.
    """
    if not prefixes:
        return True
    return any(content_id.startswith(p) for p in prefixes)


def split_series_id(content_id):
    """Return ``(base, season, episode)`` for ``tt123:1:2`` style ids.

    For non-episode ids returns ``(content_id, None, None)``.
    """
    parts = content_id.split(":")
    if len(parts) == 3 and parts[-2].isdigit() and parts[-1].isdigit():
        return ":".join(parts[:-2]), int(parts[-2]), int(parts[-1])
    return content_id, None, None


def base_id(content_id):
    """The content id without any ``:season:episode`` suffix."""
    return split_series_id(content_id)[0]


def episode_id(base, season, episode):
    """Compose a Stremio episode id ``<base>:<season>:<episode>``."""
    return "%s:%s:%s" % (base, season, episode)


def is_imdb(content_id):
    return content_id.startswith("tt")
