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

### 6. Enable the Amazon metadata cache (optional, recommended)

As of v2.21.0, Amazon scans run in a paced background worker that
writes to `metadata_cache_amazon.db` alongside your other Seshat
state. Synchronous scans read from this cache instead of hitting
Akamai live — the rest of the discovery flow no longer pauses when
Amazon throws a 429 or 202 challenge.

The worker is **disabled by default** on a fresh deploy. To enable:

1. Go to **Settings** → **Sources** → **Amazon**
2. Toggle **"Enable Amazon Cache Worker"** on
3. (Optional) Adjust **Format** (kindle / paperback / hardcover /
   mass_market) and **Language** (default English)

What you'll see on the panel:

- **Status pill** — green when actively scanning, yellow on cooldown,
  red on error
- **Queue depth** — authors currently waiting to be scanned
- **Today's scan count** — resets daily at
  `metadata_cache_daily_summary_hour` (default 9am local)
- **Last block** — most recent soft-block timestamp + reason
- **Reset Cooldown** — emergency override that clears the cooldown
  state (use sparingly; the cooldown exists for a reason)

For a 600-author library, the worker typically completes a full
refresh cycle in 2–4 days at ~200–400 successful scans/day. High-
priority authors (recent activity) refresh roughly daily.

Status surfaces in three places besides the cache panel itself:

- **Navbar status icon** (color-coded, hover for brief state)
- **Author detail pages** — per-author cache badge ("scanned 3d ago,
  12 books" / "in queue" / "cooldown")
- **Dashboard** — Amazon Cache rail at the bottom of the Seshat Stats
  widget with the most-recent discoveries

Optional notifications (require `ntfy_url` + `ntfy_topic` set):

- `notify_on_metadata_cache_error` (default ON) — worker stall,
  cache-write failure, tick crash
- `notify_on_metadata_cache_warning` (default ON) — top-tier
  cooldown escalation, author flipped to `failed_permanent`
- `notify_on_metadata_cache_daily_summary` (default OFF) — once-per-day
  digest of today's scans + blocks
- `notify_on_metadata_cache_new_book` (default OFF) — fires when the
  worker discovers a new ASIN for an existing author

Optional rotated log file: set `metadata_cache_log_file_enabled` to
True in `settings.json` to attach a `RotatingFileHandler` writing to
`/app/data/logs/metadata_cache_worker.log` (1 MB × 3 rotations).

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
| qBit login fails with 403 | IP banned (too many bad attempts) | Restart your qBit container to clear the ban |
| Books queue instead of downloading | Snatch budget full | Check the budget widget — wait for releases or increase cap |
| Pipeline finds wrong file | Single-file torrent name mismatch | Usually resolves on retry; check logs for file matching |
| Amazon column blank for an author after a scan | Cache miss — worker hasn't reached this author yet | Synchronous scans never touch Amazon as of v2.21.0; they read `metadata_cache_amazon.db`. The miss enqueues the author at priority 1000. Check the Amazon Cache panel for queue depth + worker status. |
| Amazon Cache Status pill is red | Worker stall or repeated soft-blocks | Check container logs for the `[scan]` summary lines under `seshat.discovery.metadata_cache_worker.amazon`. Use "Reset Cooldown" sparingly; the cooldown is usually justified. |
| Review queue shows wrong metadata | File scoped to wrong directory | Was fixed in v1.0.0; ensure you're on latest |
