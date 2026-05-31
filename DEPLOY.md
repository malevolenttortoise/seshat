# Deploying Seshat

First-time setup guide and production reference. Covers Docker,
Unraid, and the first-boot configuration walkthrough.

## Prerequisites

- A Linux host with Docker (Unraid, a Pi, a VPS — anything)
- Network access from that host to:
  - `irc.myanonamouse.net` on TCP/6697 (TLS) — for IRC announces
  - Your torrent client WebUI (typically LAN)
  - `www.myanonamouse.net` — for `.torrent` downloads and metadata
- A MAM account with:
  - A NickServ-registered IRC nick + SASL password
  - A valid `mam_id` session cookie (MAM → Preferences → Security)
- Torrent client credentials (qBittorrent, Transmission, Deluge, or rTorrent)

## Option A: Docker Compose

```bash
# Pull the image
docker pull ghcr.io/malevolenttortoise/seshat:latest

# Get the example compose file
curl -O https://raw.githubusercontent.com/malevolenttortoise/seshat/main/docker-compose.example.yml
mv docker-compose.example.yml docker-compose.yml
```

Edit `docker-compose.yml` and set the volume mount paths for your system:

| Container Path | Purpose | Example Host Path |
|---|---|---|
| `/app/data` | Databases, settings, encrypted credentials | `./data` |
| `/downloads` | Shared with your torrent client | `/mnt/downloads` |
| `/cwa-ingest` | CWA auto-import folder (if using CWA sink) | `/path/to/cwa-import` |
| `/calibre` | Calibre library (if using Calibre sink) | `/path/to/calibre/books` |
| `/audiobooks` | Audiobookshelf library path (if using ABS) — same mount ABS sees, so Seshat can drop audiobook files where ABS will scan them | `/path/to/audiobooks` |
| `/review-staging` | Books awaiting your review approval | `./review-staging` |
| `/staging` | Temp workspace for metadata patching | `./staging` |

```bash
docker compose up -d
```

Open `http://your-server:8789` in a browser.

## Option B: Unraid

1. In the Unraid web UI, go to **Docker** → **Add Container**
2. Set **Repository** to `ghcr.io/malevolenttortoise/seshat:latest`
3. Set **Name** to `Seshat`
4. Set **Network Type** to `Bridge`
5. Add a **Port** mapping: Host `8789` → Container `8789` (TCP)
6. Add **Path** mappings for each volume (see table above)
7. Optionally set:
   - **Web UI**: `http://[IP]:[PORT:8789]`
   - **Icon URL**: `https://raw.githubusercontent.com/malevolenttortoise/seshat/main/icon.png`
8. Click **Apply**

The image pulls from GHCR (public, no authentication needed).

## First-Boot Setup

On first visit to the web UI, Seshat shows a setup wizard:

### 1. Create Admin Account

Pick a username and password (minimum 8 characters). This is the only
user account — Seshat is single-admin by design. The password is
bcrypt-hashed and stored in `seshat_auth.db`.

### 2. Configure MAM

Go to **Settings** → **MAM** section:

- **IRC Nick**: Your MAM IRC nick (e.g. `YourName_seshat`). Use a
  unique suffix if running alongside Autobrr — both can share the same
  NickServ account but need different nicks.
- **IRC Account**: Your NickServ/SASL account name
- **IRC Password**: Your NickServ/SASL password
- **MAM Session Cookie** (`mam_id`): From MAM → Preferences → Security.
  Seshat auto-rotates this on every API call, so you should never
  need to update it manually.

### 3. Configure Download Client

Go to **Settings** → **Download Client** section:

- **Client Type**: qBittorrent, Transmission, Deluge, or rTorrent
- **URL**: Your client's WebUI URL (e.g. `http://10.0.10.20:8080`)
- **Username / Password**: WebUI credentials
- **Category**: The category Seshat uses for its torrents
  (default `[mam-reseed]` — must exist in your client)

**qBittorrent v5 note**: Seshat handles the v5 API renames
(pause→stop, resume→start, setLocation→setSavePath) automatically.

### 4. Configure Audiobookshelf (optional)

If you want audiobook support alongside ebooks, mount your ABS
library at `/audiobooks` in the compose file (see volume table
above) and configure the API connection:

- **ABS URL**: Your Audiobookshelf base URL (e.g.
  `http://10.0.10.20:13378`). Settable via the `ABS_URL` env var
  on first boot or under **Settings** → **Library** → **Audiobookshelf**.
- **ABS API key**: Generated in ABS under **Settings** →
  **Users** → *your-user* → **API Tokens**. Pasted into Seshat's
  Settings UI; stored encrypted at rest.

Once configured, ABS appears as a discovered library alongside
Calibre on the Dashboard, and audiobook MAM grabs route through a
dedicated sink that drops files into `/audiobooks` and triggers an
ABS rescan.

### 5. Configure Paths

Go to **Settings** → **Pipeline** section:

- **Download Path** (qBit namespace): Where your torrent client saves
  files (e.g. `/data/[mam-complete]`)
- **Path Prefix Translation**: If Seshat and your torrent client run
  in different containers with different mount paths, set the
  translation pair (e.g. qBit sees `/data`, Seshat sees `/downloads`)
- **Folder Structure**: Monthly `[YYYY-MM]`, yearly `[YYYY]`, or flat

### 6. Metadata cache (Amazon + Goodreads)

Amazon and Goodreads both run behind paced background workers that
write per-source cache DBs (`metadata_cache_amazon.db`,
`metadata_cache_goodreads.db`) alongside your other Seshat state.
Synchronous scans read from these caches instead of hitting Akamai or
Cloudflare live, so a soft-block on either provider no longer pauses
discovery for the rest of the library. **Both workers are disabled by
default on a fresh deploy.** Enable them per source from
**Settings → Sources → Amazon** and **Settings → Sources → Goodreads**.

**What to expect on first scan after enable.** The worker starts an
opportunistic warmup: it pulls authors from the discovery queue at its
configured pace and gradually fills the cache. Synchronous scans
return partial data with a "warming" badge until the worker catches
up. For a 600-author library the Amazon cache typically completes a
full refresh cycle in 2–4 days at ~200–400 successful scans/day;
Goodreads is throughput-bound rather than cap-bound and paces itself
to source-side rate limits. See
[`docs/guide/metadata-cache.md`](docs/guide/metadata-cache.md) for the
full first-fill UX, the per-source cooldown model, and the escalation
curve.

What you'll see on each source's cache panel:

- **Status pill** — green when actively scanning, yellow on cooldown,
  red on error.
- **Queue depth** — authors currently waiting to be scanned.
- **Today's scan count** — resets daily at the configured summary hour.
- **Reset Cooldown** — emergency override; use sparingly. The
  cooldown is usually doing real work.

Status also surfaces in the navbar status icon (hover for state), on
author detail pages (per-author badge: "scanned 3d ago, 12 books" /
"in queue" / "cooldown"), and on the Dashboard's per-source cache
rail. The full status model, posture differences between the two
workers, and operator interventions live in
[`docs/guide/metadata-cache.md`](docs/guide/metadata-cache.md).

Notification routing is configurable per event — see
[`docs/guide/notifications.md`](docs/guide/notifications.md). The
legacy `notify_on_metadata_cache_*` keys still work as a fallback if
you'd rather not migrate.

### 7. Verify

After saving, check the **Dashboard**:

- **Dispatcher**: Online (green)
- **IRC Listener**: Online (green) — should connect within seconds
- **MAM Cookie**: Online (green) — validates on first API call
- **Budget Watcher**: Online (green) — starts ticking every 60s

The snatch budget widget shows your current MAM active-snatches count.
Recent announces should start appearing in the logs within minutes.

## Smoke Test

Pick a small free-leech ebook from MAM's Recent Activity page. Note
the torrent ID (the number in the URL). Go to **Settings** → scroll
to the bottom → use the manual inject field to submit the torrent ID.

The book should:
1. Appear in qBit under your configured category
2. Download and trigger the pipeline
3. Show up in the **Review Queue** with enriched metadata and cover
4. After your approval, land in CWA/Calibre

## Coexistence with Autobrr

If running Autobrr alongside Seshat, give them different IRC nicks
sharing the same NickServ account. MAM SASL authenticates against the
account, not the nick:

- Autobrr: `YourName_arrbot`
- Seshat: `YourName_seshat`

Both connect simultaneously without conflict.

## Updating

### Docker Compose
```bash
docker compose pull
docker compose down
docker compose up -d
```

### Unraid
Click the Seshat container icon → **Update**. Unraid pulls the
latest image automatically.

Data volumes persist across updates — your databases, settings, and
encrypted credentials are safe.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Dashboard shows "Dispatcher: Offline" | Startup error | Check container logs |
| IRC never connects | Missing/wrong IRC credentials | Verify in Settings → MAM |
| qBit login fails with 403 | Host header validation rejecting Seshat (qBit 5.1+ default) or IP-banned from prior bad attempts | Disable Host header validation in qBit → Options → Web UI → Security, or set Server domains to `*`. If the 403 persists, restart qBit to clear the ban list. |
| qBit login fails with `HTTP 204 body=''` | qBit 5.2+ IP-whitelist bypass returning 204 instead of 200 | Update Seshat to the latest image — the 204 response is now handled as success. |
| Books queue instead of downloading | Snatch budget full | Check the budget widget — wait for releases or increase cap |
| Pipeline finds wrong file | Single-file torrent name mismatch | Usually resolves on retry; check logs for file matching |
| Amazon column blank for an author after a scan | Cache miss — worker hasn't reached this author yet | Synchronous scans read `metadata_cache_amazon.db` rather than calling Akamai live. The miss enqueues the author at priority 1000. Check the Amazon Cache panel for queue depth + worker status. See [`docs/guide/metadata-cache.md`](docs/guide/metadata-cache.md). |
| Amazon Cache Status pill is red | Worker stall or repeated soft-blocks | Check container logs for the `[scan]` summary lines under `seshat.discovery.metadata_cache_worker.amazon`. Use "Reset Cooldown" sparingly; the cooldown is usually justified. See [`docs/guide/metadata-cache.md` → Operator interventions](docs/guide/metadata-cache.md#operator-interventions). |
| Series shows as "Shared" instead of "Per-author" after upgrade | Pre-3.0 thin rows in the series only carry a single contributor each, so the cross-book intersection collapses to empty | The next discovery scan that touches one of those rows heals the contributor set (additive union) and the series flips automatically. See [`docs/guide/multi-author-and-series.md` → Heal-on-convergence](docs/guide/multi-author-and-series.md#heal-on-convergence--pre-30-thin-rows-self-correct). |
| Persons & IDs page shows a duplicate person (same author, two rows) | Pre-v3.2 name-only matching split one person across two rows even though the underlying author rows share a Goodreads/Amazon ID | Run Hygiene Job 9 (Consolidate persons by shared source ID). **Back the metadata DB up first** — person merges are hard to reverse. See [`docs/guide/hygiene-jobs.md` → Job 9](docs/guide/hygiene-jobs.md#job-9--consolidate-persons-by-shared-source-id). |
| Approved Metadata Manager change didn't push back to Calibre/CWA | Sink-specific push path failed silently, or the sink itself is unreachable. CWA and calibredb diverge on the authors field. | Check the row's status badge and the sink's last reachability check. See [`docs/guide/metadata-manager.md` → Push-back routing](docs/guide/metadata-manager.md#push-back-routing) and [`docs/guide/metadata-manager.md` → Failure modes](docs/guide/metadata-manager.md#failure-modes). |
| Audiobookshelf library doesn't pull into Seshat | Folder layout is irrelevant — Seshat reads ABS's API, not the filesystem. The library is either typed Podcast (Seshat filters `mediaType=book` only) or the API token isn't saved correctly. | Confirm the ABS library type is **Book**. Click the **Test Connection** button under Settings → Audiobookshelf — it returns a real error message if the token is wrong. |
| Review queue shows wrong metadata | File scoped to wrong directory | Was fixed in v1.0.0; ensure you're on latest |

## Further reading

- [Operator + power-user guide](docs/guide/README.md) — the chapters that cover multi-author/series, per-source behavior, the metadata cache, the Metadata Manager, hygiene jobs, notification routing, and active replacement.
- [Architecture Decision Records](docs/adr/README.md) — the durable "why" behind the decisions the guide chapters describe.
