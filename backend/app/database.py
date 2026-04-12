import sqlite3
from pathlib import Path

from .config import settings


def _ensure_parent_dir() -> None:
    db_path = Path(settings.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)


def get_connection() -> sqlite3.Connection:
    _ensure_parent_dir()
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS raw_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_start TEXT NOT NULL,
                ts_end TEXT NOT NULL,
                device_id TEXT NOT NULL,
                app_name TEXT,
                process_name TEXT,
                window_title TEXT,
                url_full TEXT,
                url_domain TEXT,
                activity_type TEXT,
                idle_flag INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'windows_collector',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(raw_events)").fetchall()
        }
        if "parallel_apps_json" not in cols:
            conn.execute("ALTER TABLE raw_events ADD COLUMN parallel_apps_json TEXT;")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_raw_events_ts_start ON raw_events(ts_start);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_raw_events_device_id ON raw_events(device_id);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_raw_events_app_name ON raw_events(app_name);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_raw_events_url_domain ON raw_events(url_domain);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_raw_events_activity_type ON raw_events(activity_type);"
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                color TEXT NOT NULL DEFAULT '#6aa9ff',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS classification_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_type TEXT NOT NULL,
                match_value TEXT NOT NULL,
                activity_name TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 100,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rules_lookup ON classification_rules(enabled, rule_type, priority);"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                platform TEXT NOT NULL DEFAULT 'unknown',
                source TEXT NOT NULL DEFAULT 'collector',
                last_seen TEXT NOT NULL,
                last_event_ts TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_metrics (
                day TEXT PRIMARY KEY,
                timezone TEXT NOT NULL,
                total_tracked_sec INTEGER NOT NULL DEFAULT 0,
                total_idle_sec INTEGER NOT NULL DEFAULT 0,
                total_pc_off_sec INTEGER NOT NULL DEFAULT 0,
                top_activity TEXT,
                top_app TEXT,
                summary_json TEXT NOT NULL DEFAULT '{}',
                computed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        default_categories = [
            ("coding", "#7dd3fc"),
            ("browser", "#f9a8d4"),
            ("game", "#86efac"),
            ("discord", "#8b5cf6"),
            ("idle", "#cbd5e1"),
            ("other", "#fdba74"),
            ("youtube", "#ef4444"),
            ("watching", "#f97316"),
            ("pc_off", "#94a3b8"),
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO activity_categories(name, color) VALUES(?, ?);",
            default_categories,
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO classification_rules(id, rule_type, match_value, activity_name, priority, enabled)
            VALUES (1, 'domain_contains', 'youtube.com', 'youtube', 10, 1);
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO classification_rules(id, rule_type, match_value, activity_name, priority, enabled)
            VALUES (2, 'process_contains', 'discord.exe', 'discord', 15, 1);
            """
        )
        default_settings = [
            ("collector_media_aware_idle_enabled", "true"),
            ("collector_idle_threshold_sec", "120"),
            ("collector_media_domains", "youtube.com,netflix.com,twitch.tv"),
            ("collector_media_title_keywords", "youtube,netflix,twitch,watching,player,video"),
            (
                "collector_media_player_processes",
                "jellyfinmediaplayer.exe,jellyfin media player.exe,jellyfin.exe",
            ),
            ("collector_parallel_browser_recent_sec", "120"),
            ("collector_game_match_strings", ""),
            ("ingest_merge_adjacent", "true"),
            ("ingest_merge_max_gap_sec", "8"),
            ("ingest_short_split_bridge_sec", "30"),
            ("dashboard_timezone", "Europe/Zagreb"),
            ("timeline_merge_gap_game_sec", "600"),
            ("timeline_merge_gap_watching_sec", "900"),
            ("timeline_merge_gap_coding_sec", "300"),
            ("timeline_merge_gap_browser_sec", "240"),
            ("timeline_merge_gap_other_sec", "180"),
            ("timeline_merge_gap_idle_sec", "60"),
            ("timeline_merge_gap_pc_off_sec", "60"),
            ("timeline_merge_require_same_app", "false"),
            ("timeline_min_fragment_sec", "300"),
            ("timeline_bridge_interrupt_sec", "600"),
            ("timeline_background_include_apps", "chrome.exe,msedge.exe,firefox.exe,spotify.exe"),
            ("timeline_background_exclude_apps", "discord.exe,steam.exe,explorer.exe"),
            ("timeline_dominance_window_sec", "3600"),
            ("timeline_dominance_threshold_pct", "70"),
            ("timeline_dominance_min_block_sec", "900"),
            ("discord_notifications_enabled", "false"),
            ("discord_webhook_url", ""),
            ("retention_policy_preset", "custom"),
            ("history_keep_forever", "false"),
            ("retention_raw_events_days", "180"),
            ("retention_daily_metrics_days", "3650"),
            ("maintenance_backup_enabled", "true"),
            ("maintenance_backup_dir", "./data/backups"),
            ("maintenance_backup_keep_count", "21"),
            ("maintenance_vacuum_enabled", "false"),
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO app_settings(key, value) VALUES(?, ?);",
            default_settings,
        )

        conn.commit()
    finally:
        conn.close()
