#!/usr/bin/env python3
"""Build a GitHub-Pages-hostable Kodi repository for Relay.

Output layout (served at https://<user>.github.io/repository.relay/):

    docs/
      addons.xml          - every add-on's <addon> block
      addons.xml.md5      - checksum of addons.xml
      index.html          - simple landing page
      zips/<id>/<id>-<ver>.zip   - installable zips (incl. repository.relay)

Install flow: users sideload zips/repository.relay/repository.relay-<ver>.zip
once, then install/auto-update the Relay add-ons from the repo.

Run:  python3 build_pages.py
"""

import hashlib
import os
import re
import xml.etree.ElementTree as ET
import zipfile

SRC = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(SRC, "docs")
ZIPS = os.path.join(OUT, "zips")

ADDONS = [
    "repository.relay",
    "script.module.relay",
    "plugin.video.relay",
    "service.subtitles.relay",
]

EXCLUDE_DIRS = {"__pycache__", ".git", ".github", ".vscode", "docs"}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".swp")
EXCLUDE_NAMES = {".DS_Store"}


def addon_version(addon_dir):
    return ET.parse(os.path.join(addon_dir, "addon.xml")).getroot().get("version")


def zip_addon(addon_id):
    addon_dir = os.path.join(SRC, addon_id)
    version = addon_version(addon_dir)
    out_dir = os.path.join(ZIPS, addon_id)
    os.makedirs(out_dir, exist_ok=True)
    zip_path = os.path.join(out_dir, "%s-%s.zip" % (addon_id, version))
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(addon_dir):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
            for fn in filenames:
                if fn in EXCLUDE_NAMES or fn.endswith(EXCLUDE_SUFFIX):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, SRC)  # arcname starts with <id>/
                zf.write(full, rel)
    # drop stale version zips
    keep = os.path.basename(zip_path)
    for fn in os.listdir(out_dir):
        if fn.endswith(".zip") and fn != keep:
            os.remove(os.path.join(out_dir, fn))
    # icon next to the zip, for the repo browser
    for cand in (os.path.join(addon_dir, "icon.png"),
                 os.path.join(addon_dir, "resources", "icon.png")):
        if os.path.exists(cand):
            with open(cand, "rb") as s, open(os.path.join(out_dir, "icon.png"), "wb") as d:
                d.write(s.read())
            break
    print("  packaged %s-%s.zip" % (addon_id, version))
    return addon_dir


def build_addons_xml(addon_dirs):
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', "<addons>"]
    for addon_dir in addon_dirs:
        with open(os.path.join(addon_dir, "addon.xml"), encoding="utf-8") as fh:
            xml = fh.read()
        parts.append(re.sub(r"<\?xml[^>]*\?>\s*", "", xml).strip())
    parts.append("</addons>\n")
    return "\n".join(parts)


def write_dir_index(path, header=""):
    """Write an index.html that Kodi's HTTP file browser can navigate.

    Kodi's CHTTPDirectory parses <a href="child"> links and only follows
    SINGLE-SEGMENT children (sub-dirs end with '/'), so each directory needs
    its own listing of its immediate children - GitHub Pages won't autogenerate
    one. Without this, "Install from zip file" shows an empty folder. The
    optional ``header`` HTML (used on the root) is shown to humans; the link
    list below it is what Kodi reads."""
    names = sorted(n for n in os.listdir(path) if n != "index.html")
    links = []
    for n in names:
        slash = "/" if os.path.isdir(os.path.join(path, n)) else ""
        links.append("<a href=\"%s%s\">%s%s</a><br>" % (n, slash, n, slash))
    html = ("<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
            "<title>Relay Repository</title></head><body>%s\n%s\n</body></html>\n"
            % (header, "\n".join(links)))
    with open(os.path.join(path, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(html)


def main():
    os.makedirs(ZIPS, exist_ok=True)
    print("Building Pages repo into %s" % OUT)
    addon_dirs = [zip_addon(a) for a in ADDONS]
    addons_xml = build_addons_xml(addon_dirs)
    with open(os.path.join(OUT, "addons.xml"), "w", encoding="utf-8") as fh:
        fh.write(addons_xml)
    md5 = hashlib.md5(addons_xml.encode("utf-8")).hexdigest()
    with open(os.path.join(OUT, "addons.xml.md5"), "w", encoding="utf-8") as fh:
        fh.write(md5)
    # GitHub Pages serves a real site only with an index; also stops Jekyll
    # from trying to process the tree.
    open(os.path.join(OUT, ".nojekyll"), "w").close()
    repo_ver = addon_version(os.path.join(SRC, "repository.relay"))
    # Per-directory listings so Kodi can browse root -> zips -> <id> -> zip.
    for addon_id in ADDONS:
        write_dir_index(os.path.join(ZIPS, addon_id))
    write_dir_index(ZIPS)
    root_header = (
        "<h1>Relay Repository</h1>"
        "<p>Your streaming companion - a Stremio&harr;Kodi bridge.</p>"
        "<p><b>Install:</b> in Kodi add this URL as a file source, then "
        "<i>Install from zip file</i> &rarr; this source &rarr; "
        "<code>zips/</code> &rarr; <code>repository.relay/</code> &rarr; "
        "<code>repository.relay-%s.zip</code>, then install Relay from the "
        "repository.</p>"
        "<hr><p style=\"font-size:0.85em;color:#666;max-width:48em\">"
        "Relay is an independent project and is <b>not affiliated with, "
        "endorsed by, or associated with</b> the Kodi/XBMC Foundation or "
        "Stremio. It ships <b>no media, content or stream sources</b> - it "
        "only presents add-ons that you choose to install. Do not use it for "
        "piracy or to access content you are not authorised to. "
        "Licensed under GPL-3.0-or-later.</p><hr>" % repo_ver)
    write_dir_index(OUT, header=root_header)
    print("addons.xml md5: %s" % md5)
    print("Done. Serve docs/ via GitHub Pages.")


if __name__ == "__main__":
    main()
