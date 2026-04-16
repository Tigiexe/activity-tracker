import json
import os
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from PIL import Image, ImageDraw, ImageFont

from backend.app.database import get_connection, init_db
from backend.app.main import build_day_sessions


def _safe_tz(name: str):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return datetime.now().astimezone().tzinfo or timezone.utc


def _fmt_secs(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _hex_to_rgb(value: str, fallback=(100, 116, 139)):
    if not value:
        return fallback
    value = value.strip().lstrip("#")
    if len(value) != 6:
        return fallback
    try:
        return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return fallback


def _text_color_for_bg(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Pick black/white text based on relative luminance for contrast."""
    r, g, b = rgb
    # Perceived brightness approximation; higher means lighter background.
    luminance = (0.299 * r) + (0.587 * g) + (0.114 * b)
    return (15, 23, 42) if luminance >= 150 else (248, 250, 252)


def _render_timeline_png(day_iso: str, tz, layered_segments: list, activity_colors: dict) -> str:
    lane_count = 5
    lanes = [[] for _ in range(lane_count)]
    activity_totals_for_lanes = defaultdict(int)
    for seg in layered_segments:
        activity_totals_for_lanes[seg["activity"]] += int(seg["duration"])
    activity_order = [x[0] for x in sorted(activity_totals_for_lanes.items(), key=lambda x: x[1], reverse=True)]
    activity_to_lane = {activity: idx for idx, activity in enumerate(activity_order[:lane_count])}
    for seg in sorted(layered_segments, key=lambda x: x["start_dt"]):
        lane_idx = activity_to_lane.get(seg["activity"], lane_count - 1)
        lanes[lane_idx].append(seg)
    used_lane_count = max(1, sum(1 for lane in lanes if lane))

    width = 1600
    height = 170 + (used_lane_count * 34) + 90
    img = Image.new("RGB", (width, height), (11, 18, 32))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()

    left = 96
    right = width - 36
    top = 86
    lane_h = 22
    lane_gap = 12

    day_local = datetime.fromisoformat(day_iso).date()
    day_start_local = datetime.combine(day_local, datetime.min.time(), tzinfo=tz)

    # Zoom to actual activity extent for better readability in Discord.
    if layered_segments:
        all_local_starts = [s["start_dt"].astimezone(tz) for s in layered_segments]
        all_local_ends = [s["end_dt"].astimezone(tz) for s in layered_segments]
        range_start_sec = max(0, int((min(all_local_starts) - day_start_local).total_seconds()))
        range_end_sec = min(86400, int((max(all_local_ends) - day_start_local).total_seconds()))
        if range_end_sec <= range_start_sec:
            range_start_sec, range_end_sec = 0, 86400
    else:
        range_start_sec, range_end_sec = 0, 86400

    range_span = max(1, range_end_sec - range_start_sec)

    def sec_to_x(sec: int) -> int:
        rel = (sec - range_start_sec) / range_span
        rel = max(0.0, min(1.0, rel))
        return left + int(rel * (right - left))

    range_label = (
        f"{(day_start_local + timedelta(seconds=range_start_sec)).strftime('%H:%M')} - "
        f"{(day_start_local + timedelta(seconds=range_end_sec)).strftime('%H:%M')}"
    )
    draw.text((left, 24), f"Activity Timeline - {day_iso}", fill=(226, 232, 240), font=font)
    draw.text((left, 44), f"Timezone: {tz} | Range: {range_label}", fill=(148, 163, 184), font=font)

    for idx in range(used_lane_count):
        y = top + idx * (lane_h + lane_gap)
        draw.rounded_rectangle((left, y, right, y + lane_h), radius=6, fill=(15, 23, 42), outline=(51, 65, 85), width=1)
        draw.text((24, y + 5), f"L{idx+1}", fill=(148, 163, 184), font=font)

    # Adaptive guides based on displayed range (roughly 7 ticks).
    tick_count = 6
    for i in range(tick_count + 1):
        sec = range_start_sec + int((i / tick_count) * range_span)
        x = sec_to_x(sec)
        draw.line((x, top - 8, x, top + (used_lane_count * (lane_h + lane_gap))), fill=(45, 55, 72), width=1)
        draw.text(
            (x - 14, top - 22),
            (day_start_local + timedelta(seconds=sec)).strftime("%H:%M"),
            fill=(148, 163, 184),
            font=font,
        )

    for lane_idx, lane in enumerate(lanes):
        if lane_idx >= used_lane_count:
            break
        y = top + lane_idx * (lane_h + lane_gap)
        for seg in lane:
            local_start = seg["start_dt"].astimezone(tz)
            local_end = seg["end_dt"].astimezone(tz)
            start_sec = int((local_start - day_start_local).total_seconds())
            end_sec = int((local_end - day_start_local).total_seconds())
            start_sec = max(0, min(86400, start_sec))
            end_sec = max(0, min(86400, end_sec))
            if end_sec <= start_sec:
                continue
            x1 = sec_to_x(start_sec)
            x2 = sec_to_x(end_sec)
            rgb = _hex_to_rgb(activity_colors.get(seg["activity"], "#64748b"))
            draw.rounded_rectangle((x1, y + 2, max(x1 + 2, x2), y + lane_h - 2), radius=5, fill=rgb)
            # Show app labels on longer blocks for readability.
            if (x2 - x1) > 90:
                app = (seg.get("app") or "").strip() or seg["activity"]
                draw.text((x1 + 4, y + 6), app[:24], fill=_text_color_for_bg(rgb), font=font)

    legend_y = top + used_lane_count * (lane_h + lane_gap) + 20
    draw.text((left, legend_y), "Top activities:", fill=(148, 163, 184), font=font)
    legend_x = left + 110
    for activity, _dur in sorted(activity_totals_for_lanes.items(), key=lambda x: x[1], reverse=True)[:6]:
        rgb = _hex_to_rgb(activity_colors.get(activity, "#64748b"))
        draw.rounded_rectangle((legend_x, legend_y - 1, legend_x + 16, legend_y + 14), radius=3, fill=rgb)
        draw.text((legend_x + 22, legend_y), activity, fill=(226, 232, 240), font=font)
        legend_x += 150

    fd, path = tempfile.mkstemp(prefix=f"timeline_{day_iso}_", suffix=".png")
    os.close(fd)
    img.save(path, "PNG")
    return path


def _build_timeline_with_aggressive_preset(conn, day_local, tz) -> dict:
    # Use aggressive values only for Discord image render without persisting changes.
    aggressive = {
        "timeline_merge_require_same_app": "false",
        "timeline_merge_gap_game_sec": "1200",
        "timeline_merge_gap_watching_sec": "1800",
        "timeline_merge_gap_coding_sec": "600",
        "timeline_merge_gap_browser_sec": "480",
        "timeline_merge_gap_other_sec": "360",
        "timeline_merge_gap_idle_sec": "120",
        "timeline_bridge_interrupt_sec": "600",
        "timeline_min_fragment_sec": "900",
        "timeline_dominance_window_sec": "5400",
        "timeline_dominance_threshold_pct": "60",
        "timeline_dominance_min_block_sec": "1200",
    }
    conn.execute("SAVEPOINT discord_aggressive_preview")
    try:
        for key, value in aggressive.items():
            conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                  value=excluded.value,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (key, value),
            )
        data = build_day_sessions(conn=conn, day_local=day_local, tz=tz, include_pc_off=True)
    finally:
        conn.execute("ROLLBACK TO SAVEPOINT discord_aggressive_preview")
        conn.execute("RELEASE SAVEPOINT discord_aggressive_preview")
    return data


def send_day_summary(day_iso: str) -> tuple[bool, str]:
    conn = get_connection()
    try:
        settings = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM app_settings").fetchall()}
        enabled = settings.get("discord_notifications_enabled", "false").lower() == "true"
        webhook = settings.get("discord_webhook_url", "").strip()
        tz_name = settings.get("dashboard_timezone", "Europe/Zagreb")
        if not enabled:
            return False, "discord_notifications_enabled is false"
        if not webhook:
            return False, "discord_webhook_url is empty"

        row = conn.execute(
            """
            SELECT day, total_tracked_sec, total_idle_sec, total_pc_off_sec, top_activity, top_app, summary_json
            FROM daily_metrics
            WHERE day = ?
            """,
            (day_iso,),
        ).fetchone()
        color_rows = conn.execute("SELECT name, color FROM activity_categories").fetchall()
        day_local = datetime.fromisoformat(day_iso).date()
        tz = _safe_tz(tz_name)
        timeline_data = _build_timeline_with_aggressive_preset(conn=conn, day_local=day_local, tz=tz)
    finally:
        conn.close()

    if not row:
        return False, f"no daily_metrics row for {day_iso}"

    details = json.loads(row["summary_json"] or "{}")
    app_totals = details.get("app_totals_sec", {})
    top_apps = sorted(app_totals.items(), key=lambda x: x[1], reverse=True)[:3]
    top_apps_text = "\n".join([f"{k}: {_fmt_secs(int(v))}" for k, v in top_apps]) or "No app data"

    tz = _safe_tz(tz_name)
    day_local = datetime.fromisoformat(day_iso).date()
    day_label = day_local.strftime("%Y-%m-%d")
    color_map = {r["name"]: r["color"] for r in color_rows}
    timeline_path = None
    timeline_path = _render_timeline_png(day_iso, tz, timeline_data.get("layers", []), color_map)
    embed = {
        "title": f"Daily Activity Summary - {day_label}",
        "description": "Aggressive timeline preset image is posted below.",
        "color": 0x22D3EE,
        "fields": [
            {"name": "Total tracked", "value": _fmt_secs(int(row["total_tracked_sec"] or 0)), "inline": True},
            {"name": "Idle", "value": _fmt_secs(int(row["total_idle_sec"] or 0)), "inline": True},
            {"name": "PC off", "value": _fmt_secs(int(row["total_pc_off_sec"] or 0)), "inline": True},
            {"name": "Top activity", "value": row["top_activity"] or "n/a", "inline": True},
            {"name": "Top app", "value": row["top_app"] or "n/a", "inline": True},
            {"name": "Timezone", "value": str(tz), "inline": True},
            {"name": "Top apps", "value": top_apps_text, "inline": False},
        ],
    }
    summary_payload = {"embeds": [embed]}

    try:
        summary_resp = requests.post(webhook, json=summary_payload, timeout=(3, 12))
        if not (200 <= summary_resp.status_code < 300):
            return False, f"discord webhook status {summary_resp.status_code} on summary embed"

        with open(timeline_path, "rb") as f:
            r = requests.post(
                webhook,
                data={"content": f"Timeline ({day_label}) - aggressive preset"},
                files={"file": ("timeline.png", f, "image/png")},
                timeout=(3, 12),
            )
        if 200 <= r.status_code < 300:
            return True, "sent"
        return False, f"discord webhook status {r.status_code}"
    except requests.RequestException as exc:
        return False, f"request error: {exc}"
    finally:
        try:
            if timeline_path:
                os.remove(timeline_path)
        except OSError:
            pass


def main() -> None:
    init_db()
    conn = get_connection()
    try:
        settings = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM app_settings").fetchall()}
    finally:
        conn.close()
    tz = _safe_tz(settings.get("dashboard_timezone", "Europe/Zagreb"))
    target_day = (datetime.now(tz).date() - timedelta(days=1)).isoformat()
    ok, msg = send_day_summary(target_day)
    print(f"discord summary ({target_day}): {ok} - {msg}")


if __name__ == "__main__":
    main()
