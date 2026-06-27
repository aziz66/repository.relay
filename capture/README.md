# Relay Capture (optional, advanced)

A tiny self-hosted helper add-on for **Relay**. It's a last-resort way to
identify external playback (Stremio/Nuvio → Kodi) when the stream URL embeds no
id, the release name doesn't resolve, and you're not signed into Stremio.

**You almost certainly don't need this.** Relay already identifies playback from
the stream URL, the release name (via Cinemeta), and your Stremio account. Only
set this up if you hit a source none of those can identify.

## What it does

`capture.py` is a minimal Stremio **stream** add-on that returns **no streams**.
It just records the `type` + `id` of each stream request and exposes the most
recent one at `/last.json`. Relay polls that and uses the id when it's fresh
(within 120 s of the click).

It serves three routes:

| Route | Returns |
|---|---|
| `GET /manifest.json` | the add-on manifest (`stream` resource, `tt`/`kitsu` ids) |
| `GET /stream/<type>/<id>.json` | `{"streams": []}` — and records `{type, id, ts}` |
| `GET /last.json` | `{"type": "...", "id": "...", "ts": <unix-seconds>}` |

It keeps the **first** request of a play session as the anchor (`SESSION_GAP=60`
seconds) so Stremio's next-episode prefetch — which fires within seconds of a
click — doesn't overwrite the title you actually pressed.

## Run it

Requirements: Python 3 (standard library only — no dependencies).

```sh
python3 capture.py                      # listens on 0.0.0.0:7700
RELAY_CAPTURE_PORT=9000 python3 capture.py   # custom port
```

Keep it running on a host reachable from your media box (LAN is fine; it should
respond well under Relay's 4 s timeout). Optionally run it under systemd:

```ini
# /etc/systemd/system/relay-capture.service
[Unit]
Description=Relay Capture
After=network.target

[Service]
ExecStart=/usr/bin/python3 /path/to/capture/capture.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Hook it up

1. **Register the add-on** so it receives your stream requests — either directly
   in your Stremio client's add-ons, or in a stream aggregator such as
   **AIOStreams** as a custom add-on — using:
   ```
   http://<this-host>:7700/manifest.json
   ```
2. **Point Relay at it.** In Kodi: set the settings level to **Expert**
   (bottom-left of the settings screen), then **Relay Subtitles → Settings →
   Capture URL**:
   ```
   http://<this-host>:7700
   ```

Leave the Capture URL blank to disable it — Relay falls back to URL / Cinemeta /
account identification.

## Notes

- Single recent-id slot — designed for a single-user setup.
- Returns no streams, so it never affects your results; it only observes.
- This is a **server tool**, not a Kodi add-on; it is not part of the Relay
  add-on zips.
