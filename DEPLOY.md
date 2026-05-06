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

### 4. Configure Paths

Go to **Settings** → **Pipeline** section:

- **Download Path** (qBit namespace): Where your torrent client saves
  files (e.g. `/data/[mam-complete]`)
- **Path Prefix Translation**: If Seshat and your torrent client run
  in different containers with different mount paths, set the
  translation pair (e.g. qBit sees `/data`, Seshat sees `/downloads`)
- **Folder Structure**: Monthly `[YYYY-MM]`, yearly `[YYYY]`, or flat

### 5. Verify

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
| Amazon scraper returns "—" | Cloudflare blocking (503) | Expected intermittently; other scrapers compensate |
| Review queue shows wrong metadata | File scoped to wrong directory | Was fixed in v1.0.0; ensure you're on latest |
