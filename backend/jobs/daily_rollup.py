import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.app.database import get_connection, init_db


def _get_timezone(conn) -> str:
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'dashboard_timezone'").fetchone()
    return row["value"] if row and row["value"] else "Europe/Zagreb"


def _safe_zoneinfo(tz_name: str):
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return datetime.now().astimezone().tzinfo or timezone.utc


def compute_day(day_iso: str) -> None:
    conn = get_connection()
    try:
        tz_name = _get_timezone(conn)
        tz = _safe_zoneinfo(tz_name)
        day = datetime.fromisoformat(day_iso).date()

        start_local = datetime.combine(day, datetime.min.time(), tzinfo=tz)
        end_local = start_local + timedelta(days=1)
        start_utc = start_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        end_utc = end_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

        rows = conn.execute(
            """
            SELECT ts_start, ts_end, activity_type, app_name, process_name
            FROM raw_events
            WHERE ts_start >= ? AND ts_start < ?
            ORDER BY ts_start ASC
            """,
            (start_utc, end_utc),
        ).fetchall()

        total = 0
        idle_total = 0
        pc_off_total = 0
        activity_totals = defaultdict(int)
        app_totals = defaultdict(int)

        for row in rows:
            try:
                start = datetime.fromisoformat(row["ts_start"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(row["ts_end"].replace("Z", "+00:00"))
            except ValueError:
                continue
            dur = int((end - start).total_seconds())
            if dur <= 0:
                continue
            total += dur
            activity = row["activity_type"] or "other"
            app = row["app_name"] or row["process_name"] or "unknown"
            activity_totals[activity] += dur
            app_totals[app] += dur
            if activity == "idle":
                idle_total += dur
            if activity == "pc_off":
                pc_off_total += dur

        top_activity = max(activity_totals.items(), key=lambda x: x[1])[0] if activity_totals else None
        top_app = max(app_totals.items(), key=lambda x: x[1])[0] if app_totals else None
        summary_json = json.dumps(
            {
                "activity_totals_sec": dict(activity_totals),
                "app_totals_sec": dict(sorted(app_totals.items(), key=lambda x: x[1], reverse=True)[:20]),
            },
            separators=(",", ":"),
        )

        conn.execute(
            """
            INSERT INTO daily_metrics(
                day, timezone, total_tracked_sec, total_idle_sec, total_pc_off_sec,
                top_activity, top_app, summary_json, computed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(day) DO UPDATE SET
                timezone = excluded.timezone,
                total_tracked_sec = excluded.total_tracked_sec,
                total_idle_sec = excluded.total_idle_sec,
                total_pc_off_sec = excluded.total_pc_off_sec,
                top_activity = excluded.top_activity,
                top_app = excluded.top_app,
                summary_json = excluded.summary_json,
                computed_at = CURRENT_TIMESTAMP
            """,
            (day_iso, tz_name, total, idle_total, pc_off_total, top_activity, top_app, summary_json),
        )
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    init_db()
    today = datetime.now().date().isoformat()
    compute_day(today)
    print(f"computed daily metrics for {today}")


if __name__ == "__main__":
    main()
