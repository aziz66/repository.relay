"""Persistence for the user's list of Stremio addon manifest URLs.

Stored as JSON under the *video plugin's* profile dir so both the plugin and the
subtitle service read one canonical list:

    special://profile/addon_data/plugin.video.relay/addons.json

Schema::

    [ {"manifestUrl": "...", "enabled": true}, ... ]

The manifest URL embeds per-user secrets (API keys / passwords), so the file is
written ``0600`` and is never passed around in plugin:// URLs -- callers refer to
add-ons by the opaque, secret-free :func:`entry_id` instead.
"""

from __future__ import annotations

import hashlib
import json
import os

try:
    import xbmcvfs
    def _store_dir():
        d = xbmcvfs.translatePath(
            "special://profile/addon_data/plugin.video.relay/")
        if not xbmcvfs.exists(d):
            xbmcvfs.mkdirs(d)
        return d
except ImportError:  # outside Kodi (tests)
    def _store_dir():
        return os.getcwd()


def _store_path():
    return os.path.join(_store_dir(), "addons.json")


def entry_id(manifest_url):
    """A stable, secret-free id for a manifest URL (for use in plugin:// URLs)."""
    return hashlib.sha1(manifest_url.encode("utf-8")).hexdigest()[:12]


_ENTRIES = None  # per-process memo (Kodi runs each click in a fresh process)


def load_entries():
    """Return persisted ``{manifestUrl, enabled, id}`` dicts (memoized).

    Entries missing a usable ``manifestUrl`` are dropped; the ``id`` is derived
    deterministically and backfilled in-memory (no rewrite needed).
    """
    global _ENTRIES
    if _ENTRIES is not None:
        return _ENTRIES
    path = _store_path()
    if not os.path.exists(path):
        _ENTRIES = []
        return _ENTRIES
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except ValueError:
        # Corrupt JSON: preserve it so a transient glitch can't wipe the list on
        # the next add/save; start empty this run.
        try:
            os.replace(path, path + ".bak")
        except OSError:
            pass
        _ENTRIES = []
        return _ENTRIES
    except OSError:
        return []  # transient read error - don't memoize, don't touch the file
    if not isinstance(data, list):
        _ENTRIES = []
        return _ENTRIES
    out = []
    for e in data:
        url = isinstance(e, dict) and e.get("manifestUrl")
        if not url:
            continue
        e.setdefault("enabled", True)
        e["id"] = entry_id(e["manifestUrl"])
        out.append(e)
    _ENTRIES = out
    return out


def save_entries(entries):
    global _ENTRIES
    path = _store_path()
    tmp = path + ".tmp"
    # 0600 from creation - the file holds per-user manifest secrets, so never let
    # it exist world-readable even briefly before the chmod.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=2)
    os.replace(tmp, path)
    _ENTRIES = None        # invalidate memo
    bump_generation()      # invalidate the result caches (catalogs/streams/...)


def invalidate():
    """Drop the per-process entry memo (for the long-lived autodownload service
    to pick up add/remove/toggle made during the same Kodi session)."""
    global _ENTRIES
    _ENTRIES = None


def generation():
    """Monotonic counter bumped on every add/remove/toggle, folded into result
    cache keys so stale catalogs/streams of a changed add-on are never served."""
    try:
        with open(os.path.join(_store_dir(), "cachegen"), encoding="utf-8") as fh:
            return int(fh.read().strip() or "0")
    except (OSError, ValueError):
        return 0


def bump_generation():
    try:
        nxt = generation() + 1  # MUST read before open("w") truncates the file
        with open(os.path.join(_store_dir(), "cachegen"), "w", encoding="utf-8") as fh:
            fh.write(str(nxt))
    except OSError:
        pass


def _acct_overrides_path():
    return os.path.join(_store_dir(), "account_addons.json")


def load_account_overrides():
    """Local per-addon overrides for Stremio-ACCOUNT add-ons (which are managed
    in Stremio itself): ``{entry_id: {"enabled": bool, "order": int}}``."""
    try:
        with open(_acct_overrides_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_account_overrides(data):
    p = _acct_overrides_path()
    tmp = p + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, p)
    except OSError:
        return
    bump_generation()  # provider set changed - invalidate result caches/memos


def find(addon_id):
    """Return the entry matching an :func:`entry_id`, or None."""
    for e in load_entries():
        if e["id"] == addon_id:
            return e
    return None


def add_url(manifest_url):
    """Add a manifest URL if not already present. Returns True if added."""
    manifest_url = manifest_url.strip()
    if not manifest_url:
        return False
    if not manifest_url.endswith("manifest.json"):
        manifest_url = manifest_url.rstrip("/") + "/manifest.json"
    entries = load_entries()
    if any(e.get("manifestUrl") == manifest_url for e in entries):
        return False
    entries.append({"manifestUrl": manifest_url, "enabled": True})
    save_entries(entries)
    return True


def remove_url(manifest_url):
    entries = [e for e in load_entries() if e.get("manifestUrl") != manifest_url]
    save_entries(entries)


def set_enabled(manifest_url, enabled):
    entries = load_entries()
    for e in entries:
        if e.get("manifestUrl") == manifest_url:
            e["enabled"] = bool(enabled)
    save_entries(entries)
