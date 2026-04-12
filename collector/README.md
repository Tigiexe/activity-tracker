# Windows Collector

Low-overhead background collector for active app/window activity.

## Setup
1. Ensure main project dependencies are installed:
   - `pip install -r requirements.txt`
2. Copy config template:
   - `copy collector\\config.example.json collector\\config.json`
3. Edit `collector\\config.json`:
   - Set `server_url` to your server endpoint.
   - Set `api_key` to your backend API key.
   - Set `device_id` to your PC name.
   - Optional: copy `games_list.example.txt` to **`games_list.txt`** (same folder as `config.json`). Put one game substring per line (or comma-separated); `#` starts a comment. The collector **merges** this file with the **server** list from **`/admin/games`** whenever `sync_settings_from_server` is true. Game detection is not stored in `config.json`.

## Run
- `python collector\\collector.py`

## Notes
- Collector stores unsent events in `collector\\spool\\events.jsonl`.
- It retries upload with exponential backoff if server is unavailable.
- It is designed to skip expensive work and keep loop overhead low.
- Collector sends periodic heartbeats so server can detect `pc_off` periods when data stops.
- Collector can auto-sync selected settings from server (`/admin/settings`) when `sync_settings_from_server` is true.
- Media-aware idle is enabled by default:
  - If no input is detected but active browser tab looks like video/media playback, it is tracked as `watching` instead of `idle`.
  - Desktop players (Jellyfin app, etc.) use `media_player_processes` in `config.json` and/or server `collector_media_player_processes` when syncing.
  - Tune this with `media_aware_idle_enabled`, `media_domains`, and `media_title_keywords` in `collector\\config.json`.
- Background timeline lanes: browsers are **not** in the default `parallel_presence_processes` list (so Chrome is not shown while gaming). Add them back if you want; `parallel_browser_recent_sec` + server `collector_parallel_browser_recent_sec` control a short grace window after real browser use.
