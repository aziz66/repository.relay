#!/usr/bin/env python3
"""Relay Capture - an optional self-hosted helper add-on for Relay.

This is a tiny Stremio "stream" add-on that returns NO streams. Its only job is
to record the exact ``type`` + ``id`` of whatever you just opened, so Relay's
subtitle service can recover it when an external player (Stremio/Nuvio -> Kodi)
hands Kodi a useless filename and the playback URL embeds no id.

You almost certainly DON'T need this - Relay identifies external playback from
the stream URL, the release name (via Cinemeta), and your Stremio account. The
capture add-on is a last-resort hook for the rare case where none of those work.

Usage:
  1. Run it:           python3 capture.py        (listens on 0.0.0.0:7700)
                       RELAY_CAPTURE_PORT=9000 python3 capture.py  (custom port)
  2. Register it as a stream add-on wherever your stream requests flow - either
     directly in your Stremio client's add-ons, or in a stream aggregator such
     as AIOStreams as a custom add-on - using its manifest URL:
         http://<this-host>:7700/manifest.json
  3. In Kodi: settings level -> Expert, then Relay Subtitles -> Settings ->
     Capture URL = http://<this-host>:7700

Relay then polls ``<Capture URL>/last.json`` and uses the recorded id when it
is fresh (within 120 s of the click). Zero dependencies (stdlib only); single
recent-id slot (single-user setup).

ANCHOR (first-of-session) semantics: when you click a title, Stremio (and
aggregators like AIOStreams) fire EXTRA stream requests right after - they
pre-cache the NEXT episode(s) for instant binge. Those arrive within seconds, so
a naive "last id wins" slot ends up holding E+1 instead of the episode you
pressed. The FIRST request of a play burst is the one you clicked; later ones are
preload. So we keep the first id of each session (a session = requests with no
>SESSION_GAP idle gap between them) and only its freshness ts is refreshed by
later in-session requests, so a slow-loading stream still reads fresh.
"""

import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote

PORT = int(os.environ.get("RELAY_CAPTURE_PORT", "7700"))
SESSION_GAP = 60  # s of idle that starts a new play session (new anchor)

_last = {"type": None, "id": None, "ts": 0}  # anchor = first id of the session
_prev_ts = 0.0                               # last request time (session tracking)

MANIFEST = {
    "id": "com.relay.capture",
    "version": "1.0.0",
    "name": "Relay Capture",
    "description": "Records the playing id for Relay's external-playback "
                   "identification. Returns no streams - add it to your Stremio "
                   "client's add-ons or to a stream aggregator (e.g. AIOStreams).",
    "resources": ["stream"],
    "types": ["movie", "series", "anime"],
    "idPrefixes": ["tt", "kitsu"],
    "catalogs": [],
    "behaviorHints": {"configurable": False, "p2p": False},
}

_STREAM_RE = re.compile(r"^/stream/([^/]+)/(.+)\.json$")


class Handler(BaseHTTPRequestHandler):
    timeout = 10  # single-threaded server: drop stalled connections so one
                  # broken/idle LAN client can't block everyone else

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        global _prev_ts
        path = self.path.split("?")[0]
        if path == "/manifest.json":
            return self._send(MANIFEST)
        if path == "/last.json":
            return self._send(_last)
        m = _STREAM_RE.match(path)
        if m:
            now = time.time()
            cid = unquote(m.group(2))
            if now - _prev_ts > SESSION_GAP:
                # New play session: THIS is the title the user clicked - keep it
                # as the anchor. (Stremio/aggregators fire follow-up requests
                # within seconds to pre-cache the NEXT episode for binge; those
                # must NOT overwrite what you actually pressed.)
                _last.update(type=m.group(1), id=cid, ts=now)
            else:
                # Same session (a preload): keep the anchor id, only refresh its
                # freshness ts so a slow-loading stream still reads as recent.
                _last["ts"] = now
            _prev_ts = now
            return self._send({"streams": []})  # contribute nothing to results
        return self._send({"err": "not found"}, 404)

    def log_message(self, *a):  # quiet
        return


if __name__ == "__main__":
    # Single-threaded: each request is a trivial dict read/write, so serialised
    # handling is plenty and avoids an unbounded thread-per-connection surface.
    print("Relay Capture listening on 0.0.0.0:%d "
          "(manifest: http://<host>:%d/manifest.json)" % (PORT, PORT))
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
