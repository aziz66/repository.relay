# Relay — your streaming companion

A seamless bridge between **Stremio** and **Kodi**. Relay browses and plays your
Stremio add-ons natively in Kodi, and — uniquely — gives **external** Stremio/Nuvio
→ Kodi playback the things stock external players lack: correct metadata, subtitles,
resume, skip-intro, Up Next, and watch-progress sync back to Trakt and your Stremio
account.

The suite is three add-ons:

| Add-on | Role |
|---|---|
| `plugin.video.relay` | Browse / play Stremio add-ons; the playback scrobbler & UI |
| `service.subtitles.relay` | Subtitles + external-playback identification |
| `script.module.relay` | Shared library (addon protocol + Stremio/Trakt clients) |

---

## Install (Kodi)

1. **Settings → System → Add-ons →** enable **Unknown sources**.
2. **Settings → File manager → Add source →** `https://aziz66.github.io/repository.relay/` → name it `relay`.
3. **Add-ons → Install from zip file → `relay` →** `zips/repository.relay/` → the `repository.relay-*.zip`.
4. **Add-ons → Install from repository → Relay Repository →** install the Relay add-ons. They auto-update from here afterwards.

## Building the repo (maintainers)

The installable Kodi repo under `docs/` is generated from the add-on sources:

```
python3 build_pages.py     # writes docs/ (addons.xml, .md5, zips/, index.html)
```

GitHub Pages serves `docs/` at the URL above. Bump an add-on's `addon.xml` version, re-run, commit `docs/`, and push.

---

## Features

Requirement legend: **—** none (works out of the box) · **Add-on** at least one
Stremio add-on configured (or account add-ons enabled) · **Stremio login**
Settings → Stremio account → Sign in · **Trakt auth** Settings → Trakt → Authorize
(needs your own free Trakt app — see **Trakt setup** below).

| Feature | Description | Requirement |
|---|---|---|
| Browse catalogs | Movies / TV Shows folders from every configured add-on's catalogs, native in Kodi | Add-on |
| Search + history | Full-text search across search-capable catalogs; remembers the last 15 queries | Add-on (searchable catalog) |
| Stream selection / auto-play | Pick a source, or auto-play the best one and skip the chooser | Add-on (stream) + your debrid keys |
| Subtitles from your add-ons | During playback, searches every add-on that offers subtitles and auto-downloads the preferred language | Add-on (subtitles) |
| OpenSubtitles moviehash fallback | Finds subtitles by file hash when there's no id at all (junk filenames) | OpenSubtitles add-on configured |
| Arabic / RTL subtitle fix | Corrects punctuation placement (`. , - ? !`) for right-to-left subtitles | — |
| External-playback identification | Identifies Stremio/Nuvio → Kodi launches: exact id from the stream URL → release-name via Cinemeta → Stremio account | — (account improves coverage) |
| Metadata / OSD enrich | Pushes the real title, art and plot to the OSD for external playback | — |
| Continue Watching row | Your Stremio Continue-Watching list on the home menu; resumes the right episode | Stremio login |
| New Episodes row | Library shows that have an aired, unwatched episode waiting | Stremio login |
| Watched indicators | ✓ overlays on titles/episodes you've finished | Stremio login |
| Resume prompt (per stream) | "Resume from N%? / Start over" with a countdown default; works for local *and* external playback | Trakt auth **or** Stremio login (for saved position) |
| Skip intro / recap | Skip prompt (or auto-skip) using IntroDB community timestamps | — (public IntroDB) |
| Up Next (TV) | Next-episode card at the credits (thumbnail + overview), auto-binge | — |
| Movie stop-at-credits | Offers to stop a movie at the credits | — |
| Binge source continuity | Keeps the same quality / release group across episodes (`bingeGroup`) | Add-on that sets `bingeGroup` |
| Trakt scrobbling | Reports watch history + progress to Trakt (start/pause/stop) | Trakt auth |
| Stremio progress sync | Writes Continue-Watching position + watched/advance back to your Stremio account | Stremio login |
| Account add-ons | Use the add-ons from your Stremio account; enable/disable/reorder them in Kodi | Stremio login |
| Apply add-on changes to account | Push your local enable/disable/reorder back to the Stremio account | Stremio login |
| Return to app after playback | Jumps back to Stremio (or any app, e.g. Nuvio) when external playback stops | Android; target app + its package id |
| Pop-up style | Minimal bottom-right overlay or classic Kodi dialogs; configurable resume-timer default | — |
| Backup / Restore config | Export/import add-ons, tokens and history to a single file | — |
| Clear cache · Health check | Maintenance: drop caches; probe each add-on's manifest | — |
| TMDbHelper player | Play Relay sources from TMDbHelper ("Auto-play" / "Choose source") | TMDbHelper add-on |
| Optimize buffering | Writes a tuned `advancedsettings.xml` for smoother streaming | — |
| Capture add-on (optional) | Power-user side-channel that records the exact id at click time, for the rare opaque-URL case | Self-hosted capture service (`capture_url`) |

---

## Notes & credits

- **Stremio account integration uses Stremio's own (unofficial) API** — the same
  endpoints the Stremio apps use, acting only on *your* account with *your*
  credentials. It may change or break if Stremio changes those endpoints.
- The optional Arabic/RTL subtitle font is **Noto Naskh Arabic** (© Google,
  [SIL Open Font License 1.1](https://openfontlicense.org)), downloaded on demand
  to your device — it is not redistributed with Relay.

## Disclaimer

Relay is an **independent, unofficial** project. It is **not affiliated with,
endorsed by, or associated with** the Kodi / XBMC Foundation or Stremio. "Kodi",
"Stremio" and other names are the property of their respective owners.

Relay **ships no media, content, or stream sources of any kind**. It is only a
frontend that presents the add-ons *you* choose to install and the account/services
*you* configure. It does **not** condone, encourage, or facilitate piracy or the
infringement of copyright. **Do not** use Relay to access or distribute content you
are not legally authorised to. You are solely responsible for the add-ons you
install and the content you access. The software is provided "as is", without
warranty of any kind. Licensed under GPL-3.0-or-later.

## Trakt setup (optional — enables watch-history / progress sync)

Trakt scrobbling needs **your own free Trakt app** (Relay ships no credentials).
One-time, ~2 minutes:

1. Sign in at **https://trakt.tv** and open **https://trakt.tv/oauth/applications**.
2. Click **New Application** and fill in:
   - **Name:** anything, e.g. `Relay`
   - **Redirect URI:** `urn:ietf:wg:oauth:2.0:oob`
   - **Permissions:** tick **/scrobble** (leave the rest at defaults)
3. **Save**, then copy the **Client ID** and **Client Secret**.
4. In Kodi: **Relay → Settings → Trakt** → paste the **Client ID** and **Client
   Secret** → tap **Authorize Trakt** → on any device go to **trakt.tv/activate**
   and enter the shown code.

That's it — Relay will then scrobble your playback (history + resume progress) to
your Trakt account. Skip this section entirely if you don't use Trakt.

## Capture add-on (optional, advanced)

**You almost certainly don't need this.** Relay identifies external playback
(Stremio/Nuvio → Kodi) from the stream URL, the release name (via Cinemeta), and
your Stremio account. The capture add-on is a last-resort hook for the rare case
where *none* of those work — e.g. a debrid source whose URL embeds no id and whose
filename is unrecognisable, and you're not signed into Stremio.

It is **off by default** and Relay ships **only the client side** — you self-host
the capture endpoint yourself. To enable it: in Kodi set the settings level to
**Expert** (bottom-left of the settings screen), then **Relay Subtitles →
Settings → Capture URL** and enter your endpoint's base URL. Leave it blank to
disable.

**What the endpoint must do.** A capture endpoint is a tiny self-hosted service
(typically a minimal Stremio add-on) that records the exact id at the moment a
stream is clicked, and exposes it at:

```
GET  <capture_url>/last.json
```

returning JSON:

```json
{ "id": "tt1234567", "type": "movie", "ts": 1782478290 }
```

- **`id`** — the Stremio id of what was just launched: `tt…` for a movie,
  `tt…:S:E` for a series episode (e.g. `tt0903747:1:4`); `kitsu:…` ids also work.
- **`type`** *(optional)* — `"movie"` or `"series"`; inferred from the id when omitted.
- **`ts`** — UNIX epoch seconds of the click. Relay ignores anything **older than
  120 s**, so the file must reflect the *most recent* click.

How you populate `last.json` is up to you. The usual approach is to install the
capture endpoint as an extra Stremio add-on that declares a `stream` resource; when
Stremio requests streams for a title (i.e. you opened it), your service writes that
request's `{id, type, ts}` to `last.json` before returning. Record the **first**
request of a click and ignore rapid follow-ups, since Stremio pre-fetches the next
episode's streams immediately after a click (otherwise `last.json` can end up
pointing at the *next* episode). Relay also filters out its own next-episode
prefetch automatically.

Because the endpoint is queried over the network on each external launch, host it
on your LAN and keep it fast; `last.json` should respond in well under the 4 s
Relay allows.

## Notes

- **Trakt** requires the one-time app setup above (Relay intentionally ships **no**
  Trakt credentials). Without it, everything else still works — only Trakt
  scrobbling is disabled.
- **Stremio login** stores only the auth token (the password is exchanged once and
  never saved); the token file is written `0600`.
- **Identification works without any account** for most add-ons that embed the id in
  the playback URL (e.g. Comet, StremThru) or whose release name resolves via
  Cinemeta. Signing into Stremio adds the account fallback for the remaining
  opaque-URL / odd-release-name cases, and unlocks the Continue Watching / New
  Episodes / progress-sync features.
- For the most reliable external identification, prefer a stream source that keeps
  the id in the URL (Comet / StremThru). After starting Kodi, give it ~20 s before
  launching a stream so the background services are up.
