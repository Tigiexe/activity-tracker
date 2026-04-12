import argparse
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.app.config import settings as app_config
from backend.app.database import get_connection, init_db


def _bool(v: str, default: bool) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() == "true"


def _int(v: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        parsed = int(str(v).strip())
    except (TypeError, ValueError):
        return default
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _resolve_backup_dir(raw_dir: str) -> Path:
    p = Path(raw_dir.strip() or "./data/backups")
    if p.is_absolute():
        return p
    db_parent = Path(app_config.db_path).resolve().parent
    return (db_parent / p).resolve()


def _rotate_backups(backup_dir: Path, keep_count: int) -> int:
    backups = sorted(backup_dir.glob("activity_*.sqlite3"), key=lambda x: x.stat().st_mtime, reverse=True)
    removed = 0
    for old in backups[keep_count:]:
        old.unlink(missing_ok=True)
        removed += 1
    return removed


def run_maintenance(backup_allowed: bool = True) -> dict:
    init_db()
    conn = get_connection()
    try:
        setting_map = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM app_settings").fetchall()}
        keep_forever = _bool(setting_map.get("history_keep_forever"), False)
        raw_days = _int(setting_map.get("retention_raw_events_days"), 180, minimum=7, maximum=3650)
        metric_days = _int(setting_map.get("retention_daily_metrics_days"), 3650, minimum=30, maximum=36500)
        backup_enabled = _bool(setting_map.get("maintenance_backup_enabled"), True) and backup_allowed
        backup_dir = _resolve_backup_dir(setting_map.get("maintenance_backup_dir", "./data/backups"))
        keep_count = _int(setting_map.get("maintenance_backup_keep_count"), 21, minimum=1, maximum=365)
        vacuum_enabled = _bool(setting_map.get("maintenance_vacuum_enabled"), False)

        raw_cutoff = (datetime.now(timezone.utc) - timedelta(days=raw_days)).isoformat().replace("+00:00", "Z")
        metric_cutoff = (datetime.now().date() - timedelta(days=metric_days)).isoformat()

        deleted_raw = 0
        deleted_metrics = 0
        if not keep_forever:
            cur1 = conn.execute("DELETE FROM raw_events WHERE ts_start < ?", (raw_cutoff,))
            cur2 = conn.execute("DELETE FROM daily_metrics WHERE day < ?", (metric_cutoff,))
            deleted_raw = int(cur1.rowcount or 0)
            deleted_metrics = int(cur2.rowcount or 0)
        conn.commit()

        backup_file = None
        pruned_backups = 0
        if backup_enabled:
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_file = backup_dir / f"activity_{stamp}.sqlite3"
            with sqlite3.connect(str(backup_file)) as backup_conn:
                conn.backup(backup_conn)
            pruned_backups = _rotate_backups(backup_dir, keep_count=keep_count)

        if vacuum_enabled and (deleted_raw > 0 or deleted_metrics > 0):
            conn.execute("VACUUM")

        return {
            "deleted_raw_events": deleted_raw,
            "deleted_daily_metrics": deleted_metrics,
            "raw_cutoff_utc": raw_cutoff,
            "metric_cutoff_day": metric_cutoff,
            "backup_path": str(backup_file) if backup_file else "",
            "backup_pruned_files": pruned_backups,
            "vacuum_ran": vacuum_enabled and (deleted_raw > 0 or deleted_metrics > 0),
            "keep_forever": keep_forever,
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Activity Tracker retention and backup maintenance")
    parser.add_argument("--no-backup", action="store_true", help="Skip backup creation for this run")
    args = parser.parse_args()
    out = run_maintenance(backup_allowed=not args.no_backup)
    print(
        "maintenance: "
        f"raw_deleted={out['deleted_raw_events']}, "
        f"metrics_deleted={out['deleted_daily_metrics']}, "
        f"backup={out['backup_path'] or 'skipped'}, "
        f"pruned_backups={out['backup_pruned_files']}, "
        f"vacuum={out['vacuum_ran']}"
    )


if __name__ == "__main__":
    main()

