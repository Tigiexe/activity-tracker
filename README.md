# Activity Tracker

Lightweight local-first activity tracking system.

## Phase 1 (implemented)
- FastAPI backend
- SQLite storage (WAL mode)
- API key-protected ingest endpoint
- Health endpoint

## Local setup (Windows or Ubuntu)
1. Create and activate a virtual environment.
2. Install dependencies:
   - `pip install -r requirements.txt`
3. Copy env file:
   - `copy .env.example .env` (Windows PowerShell)
   - `cp .env.example .env` (Linux)
4. Set `ACTIVITY_API_KEY` in `.env`.
5. Run server:
   - `uvicorn backend.app.main:app --host 0.0.0.0 --port 8000`

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

## Next phase
- Windows collector script with low-overhead sampling and batching.

## Windows collector (phase 2)
- Files:
  - `collector/collector.py`
  - `collector/config.example.json`
- Setup:
  - `copy collector\\config.example.json collector\\config.json`
  - edit `collector\\config.json` with your server URL and API key
- Run:
  - `python collector\\collector.py`

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
