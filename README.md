# Activity Tracker

Lightweight local-first activity tracking: a small FastAPI app plus an optional Windows collector. **You do not need a cloud server or VPS** — everything can run on one PC. Hosting on a small server is optional if you want 24/7 uptime or to collect from multiple machines.

## Pick a setup

| Situation | What to do |
|-----------|------------|
| **Friend / single Windows PC** | Prefer a **pre-built zip** from this repo’s **Releases** page (no Python). Otherwise **Path A** (Python + one-time setup). |
| **Linux or Mac, server only** (no Windows collector on that box) | **Path B** — install `requirements.txt` and run `python serve.py`. |
| **Docker on a VPS** | **Path C** — `docker compose up` after creating `.env`. |
| **Collector on PC, API on another machine** | Run the API on the host with `ACTIVITY_HOST=0.0.0.0` (and firewall + strong API key). Point `collector/config.json` `server_url` at `http://YOUR_SERVER:8000/ingest/events`. |

**Dependencies:** `requirements.txt` is the **cross-platform** API stack (no Windows-only packages). On Windows, if you run the collector, install **`requirements-windows.txt`** (includes `psutil` and `pywin32`). The collector is **Windows-only**; the API runs on Windows, Linux, or macOS.

**Run command:** Prefer **`python serve.py`** from the project root. It reads **`ACTIVITY_HOST`** and **`ACTIVITY_PORT`** from `.env` (defaults: `127.0.0.1` and `8000`). Use `uvicorn ... --host ...` only if you prefer the CLI over `.env`.

## Friend install: how complicated is it?

### Easiest — pre-built Windows zip (no Python, no scripts)

**What your friend does (about 2 minutes):**

1. Download **`ActivityTracker-Windows-Friends.zip`** from the repo’s **Releases** page (you need to upload it once — see *Maintainer: friend zip* below).
2. Unzip anywhere; keep **`ActivityTrackerServer.exe`**, **`ActivityTrackerCollector.exe`**, **`config.json`**, and **`START-HERE.txt`** in the **same folder**.
3. Double-click **`ActivityTrackerServer.exe`**. First run creates **`.env`** and **`data\`** here and may open the browser to the dashboard. **Leave the window open.**
4. Double-click **`ActivityTrackerCollector.exe`**. It picks up **`ACTIVITY_API_KEY`** from **`.env`** (`config.json` uses `"api_key": "from-dotenv"`). **Leave that window open too.**

**Caveats:** Windows only for this bundle; large download (~tens of MB); first launch can take a few seconds (one-file exe unpacks). If **SmartScreen** appears: **More info → Run anyway** (exes are not code-signed unless you add signing later).

### More involved — Python on the same PC (Path A below)

Requires installing Python, running setup once, and two starter scripts or batch files. Fine for people who already use dev tools.

### Without a pre-built zip

There is **no** supported way to run this on Windows **without either** Python **or** a bundled `.exe`: the stack is Python-based. macOS/Linux friends can run the **API only** with Python (**Path B**); the **collector stays Windows-only**.

### Maintainer: friend zip (you, before sharing)

From the project root (with `.venv` already created, e.g. after `scripts\setup-local.ps1`):

```powershell
powershell -ExecutionPolicy Bypass -File packaging\friends\build-windows.ps1
```

This produces **`dist\ActivityTracker-Windows-Friends.zip`**. Attach it to a [GitHub Release](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository). Friends only download the zip — they do not clone the repo.

### Path A — One Windows PC (Python + scripts)

1. Install [Python 3.11+](https://www.python.org/downloads/) and enable **Add to PATH**.
2. Clone or unzip this repo and open a terminal in the project folder.
3. Run **`scripts\setup-local.bat`** (double-click) or in PowerShell: **`.\scripts\setup-local.ps1`**  
   If PowerShell blocks scripts, run once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`  
   - Creates `.venv`, installs **`requirements-windows.txt`**, copies **`.env.example` → `.env`** and **`collector\config.example.json` → `collector\config.json`** if missing.
4. Edit **`.env`**: set **`ACTIVITY_API_KEY`** to something long and random (not `change-me` if anyone else can reach your network).
5. Edit **`collector\config.json`**: set **`api_key`** to the **same** value; keep **`server_url`** as `http://127.0.0.1:8000/ingest/events` for local-only.
6. Start the API: **`scripts\run-server.bat`** or **`.\scripts\run-server.ps1`** → open **http://127.0.0.1:8000**
7. Start tracking: **`scripts\run-collector.bat`** or **`.\scripts\run-collector.ps1`** in a second window.

Leave **`ACTIVITY_HOST=127.0.0.1`** so the dashboard is not exposed to your LAN unless you intend that.

### Path B — Linux / macOS (API only)

```bash
cd activitytracker
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# edit .env: ACTIVITY_API_KEY; use ACTIVITY_HOST=127.0.0.1 for local-only or 0.0.0.0 for LAN/VPS
python serve.py
```

### Path C — Docker (optional VPS)

```bash
cp .env.example .env
# edit .env — set ACTIVITY_API_KEY (compose sets ACTIVITY_HOST=0.0.0.0 inside the container)
docker compose up --build -d
```

Open **http://SERVER_IP:8000**. Put a reverse proxy with HTTPS in front for anything exposed to the internet.

### Hosted / LAN API (optional)

- Set **`ACTIVITY_HOST=0.0.0.0`** in `.env` so the app listens on all interfaces.
- Use a **strong** **`ACTIVITY_API_KEY`**; prefer **HTTPS** (Caddy, nginx, Traefik, etc.) on a public server.
- Windows **Firewall**: allow port **8000** only on trusted networks, or only from specific IPs.

### Dev reload

Set **`ACTIVITY_DEV_RELOAD=true`** in the environment when running **`python serve.py`** to enable uvicorn auto-reload (development only).

## Implemented features (summary)
- FastAPI backend
- SQLite storage (WAL mode)
- API key-protected ingest endpoint
- Health endpoint
- Windows collector (see `collector/README.md`)

## Git & GitHub

**Concept in one sentence:** Git keeps snapshots of your project on your PC; GitHub stores a copy online so you have backups and can share or collaborate.

**What stays private:** `.gitignore` excludes `.env`, `data/` (SQLite), `collector/config.json`, and `collector/spool/`. Never commit API keys or your real activity database.

### One-time: install Git (Windows)

If `git --version` fails in a new terminal, install [Git for Windows](https://git-scm.com/download/win) (defaults are fine), or run `winget install Git.Git -e`, then **restart Cursor** so it picks up `git` on `PATH`.

### Create the empty repo on GitHub

1. Log in at [github.com](https://github.com).
2. **New repository** (green button or plus menu).
3. Name it (e.g. `activitytracker`), choose **Public** or **Private**.
4. **Do not** add a README, `.gitignore`, or license (this project already has them).
5. Create the repository and copy the **HTTPS** URL (looks like `https://github.com/YOUR_USER/activitytracker.git`).

### First push from this folder (PowerShell)

Run in `Documents\activitytracker` (use your real GitHub URL on the last lines):

```powershell
git init
git add .
git status
```

Check `git status`: you should **not** see `.env`, `data\`, `collector\config.json`, or `.venv\`. If anything sensitive appears, stop and fix `.gitignore` before committing.

```powershell
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/YOUR_USER/YOUR_REPO.git
git push -u origin main
```

GitHub may ask you to sign in (browser or token). After that, **Source Control** in Cursor will show changes; use **Sync** / **Push** for future updates.

### Daily workflow (short)

- **`git status`** — what changed.
- **`git add -A`** then **`git commit -m "Describe the change"`** — save a snapshot locally.
- **`git push`** — upload commits to GitHub.

### Optional: GitHub CLI

If you use `gh` and are logged in (`gh auth login`), you can create and push in one step from the project folder:

```powershell
gh repo create activitytracker --private --source=. --remote=origin --push
```

Adjust `--public` / name as you like.

## Endpoints
- `GET /health`
- `POST /ingest/events` (requires `x-api-key` header)
- `POST /ingest/heartbeat` (device heartbeat for online/offline status)
- `GET /collector/settings` (collector remote settings sync)
- `GET /admin/rules` (manage categories/rules)
- `GET /admin/settings` (global customization settings)
- `GET /admin/games` (editable game substring list for collectors + timeline inference)
- Settings also cover desktop media players (Jellyfin exe list), parallel-browser grace, and ingest tuning (`ingest_merge_adjacent`, `ingest_merge_max_gap_sec`, `ingest_short_split_bridge_sec`).
- `GET /api/timeline/segment-events?start=&end=&activity=` — raw events overlapping the bar time range with that `activity_type` only.
- `GET /api/summary/day` (daily materialized summary)
- `GET /api/summary/range` (range of daily summaries)

### Example ingest request
```bash
curl -X POST "http://127.0.0.1:8000/ingest/events" \
  -H "Content-Type: application/json" \
  -H "x-api-key: change-me" \
  -d '{
    "events": [
      {
        "ts_start": "2026-04-10T19:00:00Z",
        "ts_end": "2026-04-10T19:00:04Z",
        "device_id": "main-pc",
        "app_name": "Chrome",
        "process_name": "chrome.exe",
        "window_title": "YouTube - Video",
        "url_full": "https://www.youtube.com/watch?v=abc123",
        "url_domain": "youtube.com",
        "activity_type": "browser",
        "idle_flag": false,
        "source": "windows_collector"
      }
    ]
  }'
```

## Windows collector

- Files: `collector/collector.py`, `collector/config.example.json` (see **`collector/README.md`**).
- **Same PC as the API:** use `server_url` `http://127.0.0.1:8000/ingest/events` and the same `api_key` as in `.env`.
- **Remote API:** set `server_url` to `http://YOUR_HOST:8000/ingest/events` (HTTPS if you terminate TLS in front of the app).
- Install on Windows with **`pip install -r requirements-windows.txt`** (or run **`scripts\setup-local.ps1`**).
- Run: `python collector\collector.py` or **`scripts\run-collector.bat`**

## Daily rollup job
- Computes materialized `daily_metrics` rows used by summary APIs and future notifications.
- Run manually:
  - `python -m backend.jobs.daily_rollup`

## Discord daily summary
- Configure in `/admin/settings`:
  - `discord_notifications_enabled`
  - `discord_webhook_url`
- Manual send from UI:
  - button: `Send today's summary to Discord`
- CLI send (yesterday by default):
  - `python -m backend.jobs.discord_summary`

## Automation (nightly + startup-safe)
- New orchestrator:
  - `python -m backend.jobs.automation --mode nightly`
- What it does in nightly mode:
  - recomputes rollups for a small window (`--days-back`, default `2`)
  - sends yesterday's Discord summary (unless `--no-discord`)
  - runs maintenance (retention + backup rotation) unless `--no-maintenance`
- Startup-safe mode:
  - `python -m backend.jobs.automation --mode startup --days-back 7`
  - recomputes recent days after boot so outages/restarts do not leave metric gaps.

### Ubuntu systemd setup (recommended)
Create service units (replace `/opt/activitytracker` and Python path with yours):

`/etc/systemd/system/activitytracker-nightly.service`
```ini
[Unit]
Description=Activity Tracker nightly rollup + Discord summary
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/activitytracker
ExecStart=/opt/activitytracker/.venv/bin/python -m backend.jobs.automation --mode nightly --days-back 2
```

`/etc/systemd/system/activitytracker-nightly.timer`
```ini
[Unit]
Description=Run Activity Tracker nightly job at 00:10

[Timer]
OnCalendar=*-*-* 00:10:00
Persistent=true
Unit=activitytracker-nightly.service

[Install]
WantedBy=timers.target
```

`/etc/systemd/system/activitytracker-startup-safe.service`
```ini
[Unit]
Description=Activity Tracker startup-safe catch-up rollups
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/activitytracker
ExecStart=/opt/activitytracker/.venv/bin/python -m backend.jobs.automation --mode startup --days-back 7
```

Enable:
- `sudo systemctl daemon-reload`
- `sudo systemctl enable --now activitytracker-nightly.timer`
- `sudo systemctl enable activitytracker-startup-safe.service`

Run startup-safe once manually (or wire it to boot target):
- `sudo systemctl start activitytracker-startup-safe.service`

### Ubuntu cron alternative (if you prefer cron)
- Nightly at 00:10:
  - `10 0 * * * cd /opt/activitytracker && /opt/activitytracker/.venv/bin/python -m backend.jobs.automation --mode nightly --days-back 2 >> /var/log/activitytracker-nightly.log 2>&1`
- Startup-safe on reboot:
  - `@reboot sleep 30 && cd /opt/activitytracker && /opt/activitytracker/.venv/bin/python -m backend.jobs.automation --mode startup --days-back 7 >> /var/log/activitytracker-startup.log 2>&1`

## Retention + backups
- Configure in `/admin/settings`:
  - `retention_policy_preset`:
    - `forever` (no retention deletes)
    - `hybrid` (raw 365 days, metrics forever)
    - `space_saver` (raw 120 days, metrics 3 years)
    - `custom` (use manual values below)
  - `history_keep_forever` (when `true`, retention deletes are disabled)
  - `retention_raw_events_days`
  - `retention_daily_metrics_days`
  - `maintenance_backup_enabled`
  - `maintenance_backup_dir`
  - `maintenance_backup_keep_count`
  - `maintenance_vacuum_enabled`
- Run standalone maintenance manually:
  - `python -m backend.jobs.maintenance`
