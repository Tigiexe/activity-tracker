from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import Depends, FastAPI, Form, Header, HTTPException, Query, status
from fastapi.responses import HTMLResponse, RedirectResponse

from .config import settings
from .database import get_connection, init_db
from .models import HeartbeatRequest, HeartbeatResponse, IngestRequest, IngestResponse

app = FastAPI(title="Activity Tracker API", version="0.1.0")


def get_setting_map(conn) -> dict:
    rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


def ingest_merge_adjacent_events(conn) -> bool:
    """When false, each ingest row is stored separately (more granular raw_events). Missing key = legacy merge-on."""
    v = get_setting_map(conn).get("ingest_merge_adjacent")
    if v is None:
        return True
    return str(v).lower() == "true"


def parse_game_match_list_input(raw: str) -> str:
    """Normalize multiline/comma game substring list to comma-separated lowercase for storage."""
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    seen: set[str] = set()
    order: list[str] = []
    for line in text.split("\n"):
        line = line.split("#", 1)[0]
        for seg in line.split(","):
            s = seg.strip().lower()
            if not s:
                continue
            if s not in seen:
                seen.add(s)
                order.append(s)
    return ",".join(order)


def format_game_match_list_for_editor(stored_csv: str) -> str:
    parts = [x.strip() for x in (stored_csv or "").split(",") if x.strip()]
    return "\n".join(parts)


def get_dashboard_timezone(conn) -> ZoneInfo:
    settings_map = get_setting_map(conn)
    tz_name = settings_map.get("dashboard_timezone", "Europe/Zagreb")
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        # Fallback for environments missing IANA tzdata (common on Windows).
        local_tz = datetime.now().astimezone().tzinfo
        if local_tz is not None:
            return local_tz
        return timezone.utc


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _ingest_row_duration_sec(row: tuple) -> int | None:
    s = parse_iso(row[0])
    e = parse_iso(row[1])
    if not s or not e:
        return None
    return max(0, int((e - s).total_seconds()))


def _ingest_gap_seconds(prev_end_ts: str, next_start_ts: str) -> int | None:
    a = parse_iso(prev_end_ts)
    b = parse_iso(next_start_ts)
    if not a or not b:
        return None
    return int((b - a).total_seconds())


def _ingest_same_bucket_row(prev: tuple, row: tuple) -> bool:
    return (
        prev[2] == row[2]
        and prev[3] == row[3]
        and prev[4] == row[4]
        and prev[5] == row[5]
        and prev[6] == row[6]
        and prev[7] == row[7]
        and prev[8] == row[8]
        and prev[9] == row[9]
        and prev[10] == row[10]
        and prev[11] == row[11]
    )


def _ingest_outer_match_rows(a: tuple, c: tuple) -> bool:
    return a[2:12] == c[2:12]


def apply_ingest_short_split_bridge(rows: list[tuple], max_gap_sec: int, bridge_sec: int) -> list[tuple]:
    """Merge A + short B + A (same outer bucket) e.g. brief tab away from same page."""
    if bridge_sec <= 0 or len(rows) < 3:
        return rows
    changed = True
    while changed:
        changed = False
        new_rows: list[tuple] = []
        i = 0
        while i < len(rows):
            if i + 2 < len(rows):
                a, b, c = rows[i], rows[i + 1], rows[i + 2]
                dur_b = _ingest_row_duration_sec(b)
                gab = _ingest_gap_seconds(a[1], b[0])
                gbc = _ingest_gap_seconds(b[1], c[0])
                if (
                    dur_b is not None
                    and dur_b <= bridge_sec
                    and _ingest_outer_match_rows(a, c)
                    and gab is not None
                    and gbc is not None
                    and 0 <= gab <= max_gap_sec
                    and 0 <= gbc <= max_gap_sec
                ):
                    new_rows.append((a[0], c[1], *tuple(a[2:])))
                    i += 3
                    changed = True
                    continue
            new_rows.append(rows[i])
            i += 1
        rows = new_rows
    return rows


def get_ingest_merge_tuning(conn) -> tuple[int, int]:
    m = get_setting_map(conn)
    try:
        max_gap = max(0, int(m.get("ingest_merge_max_gap_sec", "8")))
    except ValueError:
        max_gap = 8
    try:
        bridge = max(0, int(m.get("ingest_short_split_bridge_sec", "30")))
    except ValueError:
        bridge = 30
    return max_gap, bridge


def get_merge_gaps(conn) -> dict:
    settings_map = get_setting_map(conn)
    defaults = {
        "game": 600,
        "watching": 900,
        "coding": 300,
        "browser": 240,
        "other": 180,
        "idle": 60,
        "pc_off": 60,
    }
    out = {}
    for key, default_val in defaults.items():
        raw = settings_map.get(f"timeline_merge_gap_{key}_sec")
        try:
            out[key] = max(0, int(raw)) if raw is not None else default_val
        except ValueError:
            out[key] = default_val
    return out


def merge_requires_same_app(conn) -> bool:
    settings_map = get_setting_map(conn)
    return settings_map.get("timeline_merge_require_same_app", "false").lower() == "true"


def get_timeline_algo_settings(conn) -> dict:
    settings_map = get_setting_map(conn)

    def _safe_int(key: str, default: int) -> int:
        raw = settings_map.get(key)
        try:
            return max(0, int(raw)) if raw is not None else default
        except ValueError:
            return default

    includes = [x.strip().lower() for x in settings_map.get("timeline_background_include_apps", "").split(",") if x.strip()]
    excludes = [x.strip().lower() for x in settings_map.get("timeline_background_exclude_apps", "").split(",") if x.strip()]
    return {
        "min_fragment_sec": _safe_int("timeline_min_fragment_sec", 600),
        "bridge_interrupt_sec": _safe_int("timeline_bridge_interrupt_sec", 300),
        "bg_include": set(includes),
        "bg_exclude": set(excludes),
        "dominance_window_sec": _safe_int("timeline_dominance_window_sec", 3600),
        "dominance_threshold_pct": _safe_int("timeline_dominance_threshold_pct", 70),
        "dominance_min_block_sec": _safe_int("timeline_dominance_min_block_sec", 900),
    }


def get_merge_gap_sec(activity: str, gaps: dict) -> int:
    return gaps.get(activity, gaps.get("other", 180))


def infer_activity_from_app_name(app_name: str, game_match_strings: list[str]) -> str | None:
    """Infer background activity from process name; game only if app matches configured substrings."""
    name = (app_name or "").lower()
    if name in {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe"}:
        return "browser"
    if name in {"code.exe", "cursor.exe", "pycharm64.exe", "idea64.exe", "notepad++.exe"}:
        return "coding"
    for sig in game_match_strings:
        if sig and sig in name:
            return "game"
    return None


def build_day_sessions(
    conn,
    day_local,
    tz,
    include_pc_off: bool = True,
):
    setting_map = get_setting_map(conn)
    game_infer_strings = [
        x.strip().lower()
        for x in (setting_map.get("collector_game_match_strings") or "").split(",")
        if x.strip()
    ]
    merge_gaps = get_merge_gaps(conn)
    require_same_app = merge_requires_same_app(conn)
    algo = get_timeline_algo_settings(conn)
    start_local = datetime.combine(day_local, datetime.min.time(), tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    end_utc = end_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    rows = conn.execute(
        """
        SELECT ts_start, ts_end, app_name, process_name, window_title, activity_type, parallel_apps_json
        FROM raw_events
        WHERE ts_start >= ? AND ts_start < ?
        ORDER BY ts_start ASC
        """,
        (start_utc, end_utc),
    ).fetchall()

    now_utc = datetime.now(timezone.utc)
    is_today = day_local == datetime.now(tz).date()
    events = []
    for row in rows:
        s = parse_iso(row["ts_start"])
        e = parse_iso(row["ts_end"])
        if not s or not e:
            continue
        if is_today and s > (now_utc + timedelta(minutes=2)):
            continue
        dur = int((e - s).total_seconds())
        if dur <= 0:
            continue
        activity = row["activity_type"] or "other"
        app_name = row["app_name"] or row["process_name"] or "unknown"
        events.append(
            {
                "start_dt": s,
                "end_dt": e,
                "duration": dur,
                "activity": activity,
                "app": app_name,
                "title": row["window_title"] or "(no title)",
            }
        )

    sessions = []
    for item in events:
        if not sessions:
            sessions.append(item.copy())
            continue
        prev = sessions[-1]
        gap = int((item["start_dt"] - prev["end_dt"]).total_seconds())
        merge_gap = get_merge_gap_sec(item["activity"], merge_gaps)
        same_group = item["activity"] == prev["activity"] and (
            (item["app"] == prev["app"]) if require_same_app else True
        )
        if same_group and 0 <= gap <= merge_gap:
            prev["end_dt"] = item["end_dt"]
            prev["duration"] += item["duration"] + gap
            prev["title"] = item["title"]
        else:
            sessions.append(item.copy())

    # Aggressive smoothing: absorb short interruptions between same activity blocks (A-B-A).
    changed = True
    while changed and len(sessions) >= 3:
        changed = False
        i = 1
        while i < len(sessions) - 1:
            prev = sessions[i - 1]
            cur = sessions[i]
            nxt = sessions[i + 1]
            if (
                prev["activity"] == nxt["activity"]
                and cur["duration"] <= algo["bridge_interrupt_sec"]
            ):
                prev["end_dt"] = nxt["end_dt"]
                prev["duration"] = int((prev["end_dt"] - prev["start_dt"]).total_seconds())
                prev["title"] = nxt["title"]
                del sessions[i : i + 2]
                changed = True
                i = max(1, i - 1)
            else:
                i += 1

    # Merge tiny fragments into neighbors for cleaner visualization.
    i = 0
    while len(sessions) > 1 and i < len(sessions):
        cur = sessions[i]
        if cur["duration"] >= algo["min_fragment_sec"]:
            i += 1
            continue
        if i == 0:
            nxt = sessions[1]
            nxt["start_dt"] = cur["start_dt"]
            nxt["duration"] = int((nxt["end_dt"] - nxt["start_dt"]).total_seconds())
            del sessions[0]
            continue
        if i == len(sessions) - 1:
            prev = sessions[i - 1]
            prev["end_dt"] = cur["end_dt"]
            prev["duration"] = int((prev["end_dt"] - prev["start_dt"]).total_seconds())
            del sessions[i]
            continue
        prev = sessions[i - 1]
        nxt = sessions[i + 1]
        # Prefer merging into same-activity neighbor, otherwise longer neighbor.
        if prev["activity"] == cur["activity"] or (prev["activity"] == nxt["activity"]):
            prev["end_dt"] = cur["end_dt"]
            prev["duration"] = int((prev["end_dt"] - prev["start_dt"]).total_seconds())
            del sessions[i]
        elif prev["duration"] >= nxt["duration"]:
            prev["end_dt"] = cur["end_dt"]
            prev["duration"] = int((prev["end_dt"] - prev["start_dt"]).total_seconds())
            del sessions[i]
        else:
            nxt["start_dt"] = cur["start_dt"]
            nxt["duration"] = int((nxt["end_dt"] - nxt["start_dt"]).total_seconds())
            del sessions[i]

    # Dominance compression (Pass 3):
    # in a local window, if one activity dominates, fold short non-dominant fragments into it.
    dom_window = algo["dominance_window_sec"]
    dom_threshold = algo["dominance_threshold_pct"] / 100.0
    dom_min_block = algo["dominance_min_block_sec"]
    changed = True
    pass_count = 0
    max_passes = 8
    while changed and len(sessions) >= 2 and pass_count < max_passes:
        pass_count += 1
        changed = False
        i = 0
        while i < len(sessions):
            cluster = [sessions[i]]
            j = i + 1
            while j < len(sessions):
                gap = int((sessions[j]["start_dt"] - cluster[-1]["end_dt"]).total_seconds())
                cluster_span = int((sessions[j]["end_dt"] - cluster[0]["start_dt"]).total_seconds())
                if gap <= algo["bridge_interrupt_sec"] and cluster_span <= dom_window:
                    cluster.append(sessions[j])
                    j += 1
                else:
                    break
            if len(cluster) >= 2:
                totals = defaultdict(int)
                for c in cluster:
                    totals[c["activity"]] += c["duration"]
                dominant_activity, dominant_dur = max(totals.items(), key=lambda x: x[1])
                total_dur = sum(totals.values())
                if total_dur >= dom_min_block and dominant_dur / max(1, total_dur) >= dom_threshold:
                    # rewrite short interruptions in cluster to dominant activity
                    rewritten = False
                    for k in range(i, j):
                        frag = sessions[k]
                        if (
                            frag["activity"] != dominant_activity
                            and frag["duration"] <= algo["bridge_interrupt_sec"]
                            and frag["activity"] not in {"idle", "pc_off"}
                        ):
                            frag["activity"] = dominant_activity
                            rewritten = True
                    if not rewritten:
                        i += 1
                        continue
                    # rematerialize cluster merges after rewrite
                    merged_cluster = [sessions[i].copy()]
                    for k in range(i + 1, j):
                        cur = sessions[k]
                        prev = merged_cluster[-1]
                        gap = int((cur["start_dt"] - prev["end_dt"]).total_seconds())
                        mg = get_merge_gap_sec(cur["activity"], merge_gaps)
                        if cur["activity"] == prev["activity"] and 0 <= gap <= mg:
                            prev["end_dt"] = cur["end_dt"]
                            prev["duration"] = int((prev["end_dt"] - prev["start_dt"]).total_seconds())
                        else:
                            merged_cluster.append(cur.copy())
                    sessions[i:j] = merged_cluster
                    changed = True
                    i = max(0, i - 1)
                    continue
            i += 1


    if include_pc_off:
        with_off = []
        pc_off_threshold = 300
        for idx, item in enumerate(sessions):
            with_off.append(item)
            if idx + 1 < len(sessions):
                nxt = sessions[idx + 1]
                gap = int((nxt["start_dt"] - item["end_dt"]).total_seconds())
                if gap > pc_off_threshold:
                    with_off.append(
                        {
                            "start_dt": item["end_dt"],
                            "end_dt": nxt["start_dt"],
                            "duration": gap,
                            "activity": "pc_off",
                            "app": "(offline)",
                            "title": "No collector data",
                        }
                    )
        if sessions and is_today:
            tail_gap = int((now_utc - sessions[-1]["end_dt"]).total_seconds())
            if tail_gap > pc_off_threshold:
                with_off.append(
                    {
                        "start_dt": sessions[-1]["end_dt"],
                        "end_dt": now_utc,
                        "duration": tail_gap,
                        "activity": "pc_off",
                        "app": "(offline)",
                        "title": "No collector data",
                    }
                )
        sessions = with_off

    # Build activity windows for layered timeline view.
    # Foreground events always contribute; selected background apps only extend activity windows.
    activity_events = []
    for ev in events:
        activity_events.append(
            {
                "start_dt": ev["start_dt"],
                "end_dt": ev["end_dt"],
                "activity": ev["activity"],
                "app": ev["app"],
                "source": "fg",
            }
        )

    bg_include = algo["bg_include"]
    bg_exclude = algo["bg_exclude"]
    if bg_include:
        for row in rows:
            s = parse_iso(row["ts_start"])
            e = parse_iso(row["ts_end"])
            if not s or not e:
                continue
            if int((e - s).total_seconds()) <= 0:
                continue
            try:
                apps = json.loads(row["parallel_apps_json"] or "[]")
            except json.JSONDecodeError:
                apps = []
            for app in apps:
                app_l = str(app).lower()
                if app_l in bg_exclude or app_l not in bg_include:
                    continue
                inferred = infer_activity_from_app_name(app_l, game_infer_strings)
                if not inferred:
                    continue
                activity_events.append(
                    {
                        "start_dt": s,
                        "end_dt": e,
                        "activity": inferred,
                        "app": app_l,
                        "source": "bg_support",
                    }
                )

    # Merge independently per activity so larger "outer" blocks can wrap inner blocks.
    buckets = {}
    for ev in activity_events:
        buckets.setdefault(ev["activity"], []).append(ev)

    layer_candidates = []
    for activity, arr in buckets.items():
        arr.sort(key=lambda x: x["start_dt"])
        merged = []
        for ev in arr:
            if not merged:
                merged.append(ev.copy())
                continue
            prev = merged[-1]
            gap = int((ev["start_dt"] - prev["end_dt"]).total_seconds())
            mg = get_merge_gap_sec(activity, merge_gaps)
            if 0 <= gap <= mg:
                prev["end_dt"] = max(prev["end_dt"], ev["end_dt"])
                prev["source"] = "mixed" if prev["source"] != ev["source"] else prev["source"]
            else:
                merged.append(ev.copy())
        for m in merged:
            m["duration"] = int((m["end_dt"] - m["start_dt"]).total_seconds())
            if m["duration"] <= 0:
                continue
            if m["duration"] < algo["min_fragment_sec"]:
                continue
            layer_candidates.append(
                {
                    "start_dt": m["start_dt"],
                    "end_dt": m["end_dt"],
                    "duration": m["duration"],
                    "activity": activity,
                    "app": m["app"],
                    "segment_source": m.get("source") or "fg",
                    "title": f"{activity} window ({m['source']})",
                }
            )

    # longest windows first gives "outer then inner" lane behavior
    layer_candidates.sort(key=lambda x: x["duration"], reverse=True)

    return {"primary": sessions, "layers": layer_candidates}


def upsert_device_seen(
    conn,
    device_id: str,
    platform: str,
    source: str,
    last_event_ts: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO devices(device_id, platform, source, last_seen, last_event_ts, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(device_id) DO UPDATE SET
            platform = excluded.platform,
            source = excluded.source,
            last_seen = CURRENT_TIMESTAMP,
            last_event_ts = COALESCE(excluded.last_event_ts, devices.last_event_ts),
            updated_at = CURRENT_TIMESTAMP
        """,
        (device_id, platform, source, last_event_ts),
    )


def load_rules(conn) -> list:
    return conn.execute(
        """
        SELECT rule_type, match_value, activity_name
        FROM classification_rules
        WHERE enabled = 1
        ORDER BY priority ASC, id ASC
        """
    ).fetchall()


def classify_with_rules(
    rules,
    process_name: str | None,
    app_name: str | None,
    window_title: str | None,
    url_domain: str | None,
    idle_flag: bool,
    fallback_activity: str | None = None,
) -> str:
    if idle_flag:
        return "idle"
    proc = (process_name or app_name or "").lower()
    title = (window_title or "").lower()
    domain = (url_domain or "").lower()

    for rule in rules:
        rule_type = (rule["rule_type"] or "").lower()
        value = (rule["match_value"] or "").lower()
        target = (rule["activity_name"] or "").strip() or "other"
        if not value:
            continue
        if rule_type == "process_equals" and proc == value:
            return target
        if rule_type == "process_contains" and value in proc:
            return target
        if rule_type == "title_contains" and value in title:
            return target
        if rule_type == "domain_contains" and value in domain:
            return target

    return fallback_activity or "other"


def require_api_key(x_api_key: str = Header(default="")) -> None:
    if x_api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def dashboard_today(date: str | None = Query(default=None)) -> str:
    conn = get_connection()
    try:
        tz = get_dashboard_timezone(conn)
        selected_day_local = None
        if date:
            try:
                selected_day_local = datetime.fromisoformat(date).date()
            except ValueError:
                selected_day_local = None
        if selected_day_local is None:
            selected_day_local = datetime.now(tz).date()

        start_local = datetime.combine(selected_day_local, datetime.min.time(), tzinfo=tz)
        end_local = start_local + timedelta(days=1)
        start_utc = start_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        end_utc = end_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

        rows = conn.execute(
            """
            SELECT
                id, ts_start, ts_end, app_name, process_name, window_title,
                activity_type, idle_flag
            FROM raw_events
            WHERE ts_start >= ? AND ts_start < ?
            ORDER BY ts_start ASC
            """,
            (start_utc, end_utc),
        ).fetchall()
        category_rows = conn.execute("SELECT name, color FROM activity_categories").fetchall()
        device_rows = conn.execute(
            "SELECT device_id, platform, source, last_seen, last_event_ts FROM devices ORDER BY device_id ASC"
        ).fetchall()
        trend_rows = conn.execute(
            """
            SELECT ts_start, ts_end
            FROM raw_events
            WHERE ts_start >= ?
            ORDER BY ts_start ASC
            """,
            ((datetime.now(timezone.utc) - timedelta(days=90)).isoformat().replace("+00:00", "Z"),),
        ).fetchall()
    finally:
        conn.close()

    category_colors = {cat["name"]: cat["color"] for cat in category_rows}
    conn_settings = get_connection()
    try:
        setting_map = get_setting_map(conn_settings)
    finally:
        conn_settings.close()
    current_preset = setting_map.get("timeline_preset_name", "custom")
    preview_merge_same_app = setting_map.get("timeline_merge_require_same_app", "false")
    preview_gap_browser = setting_map.get("timeline_merge_gap_browser_sec", "240")
    preview_bridge = setting_map.get("timeline_bridge_interrupt_sec", "300")
    preview_min_fragment = setting_map.get("timeline_min_fragment_sec", "600")
    preview_dom_threshold = setting_map.get("timeline_dominance_threshold_pct", "70")

    timeline = []
    activity_totals = defaultdict(int)
    app_totals = defaultdict(int)
    hourly_totals = defaultdict(int)
    total_seconds = 0

    timeline_segments = []
    now_utc = datetime.now(timezone.utc)
    is_selected_today = selected_day_local == datetime.now(tz).date()

    for row in rows:
        try:
            start = datetime.fromisoformat(row["ts_start"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(row["ts_end"].replace("Z", "+00:00"))
        except ValueError:
            continue

        duration = int((end - start).total_seconds())
        if duration <= 0:
            continue
        # Ignore future-dated events on today's view (often caused by test payloads).
        if is_selected_today and start > (now_utc + timedelta(minutes=2)):
            continue

        total_seconds += duration
        activity = row["activity_type"] or "other"
        app_name = row["app_name"] or row["process_name"] or "unknown"
        title = row["window_title"] or "(no title)"

        activity_totals[activity] += duration
        app_totals[app_name] += duration
        hourly_totals[start.astimezone(tz).hour] += duration
        timeline.append(
            {
                "start_dt": start,
                "end_dt": end,
                "duration": duration,
                "activity": activity,
                "app": app_name,
                "title": title,
            }
        )

    def fmt_secs(seconds: int) -> str:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        if h:
            return f"{h}h {m}m"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"

    activity_rows = sorted(activity_totals.items(), key=lambda x: x[1], reverse=True)
    app_rows = sorted(app_totals.items(), key=lambda x: x[1], reverse=True)[:12]

    activity_html = "".join(
        f"<tr><td>{name}</td><td>{fmt_secs(sec)}</td></tr>" for name, sec in activity_rows
    ) or "<tr><td colspan='2'>No data yet</td></tr>"

    app_html = "".join(f"<tr><td>{name}</td><td>{fmt_secs(sec)}</td></tr>" for name, sec in app_rows) or (
        "<tr><td colspan='2'>No data yet</td></tr>"
    )

    # Pass 1 timeline engine: merged sessions with activity-specific gap smoothing.
    conn_tl = get_connection()
    try:
        timeline_data = build_day_sessions(
            conn=conn_tl,
            day_local=selected_day_local,
            tz=tz,
            include_pc_off=True,
        )
    finally:
        conn_tl.close()
    timeline_segments = timeline_data["primary"]
    layered_segments = timeline_data.get("layers", [])

    timeline_html = "".join(
        f"""
        <tr>
          <td>{item["start_dt"].astimezone(tz).strftime("%H:%M:%S")}</td>
          <td>{item["end_dt"].astimezone(tz).strftime("%H:%M:%S")}</td>
          <td>{fmt_secs(item["duration"])}</td>
          <td><span class="pill" style="background:{category_colors.get(item["activity"], "#64748b")}22;border-color:{category_colors.get(item["activity"], "#64748b")}66;color:{category_colors.get(item["activity"], "#cbd5e1")}">{escape(item["activity"])}</span></td>
          <td>{item["app"]}</td>
          <td>{escape(item["title"])}</td>
        </tr>
        """
        for item in timeline_segments[-160:]
    ) or "<tr><td colspan='6'>No timeline data yet</td></tr>"

    day_start_local = datetime.combine(selected_day_local, datetime.min.time(), tzinfo=tz)
    lane_count = 5
    lanes = [[] for _ in range(lane_count)]
    activity_totals_for_lanes = defaultdict(int)
    for seg in layered_segments:
        activity_totals_for_lanes[seg["activity"]] += seg["duration"]
    activity_order = [x[0] for x in sorted(activity_totals_for_lanes.items(), key=lambda x: x[1], reverse=True)]
    activity_to_lane = {activity: idx for idx, activity in enumerate(activity_order[:lane_count])}

    # Keep same activity on same lane for readability.
    for seg in sorted(layered_segments, key=lambda x: x["start_dt"]):
        lane_idx = activity_to_lane.get(seg["activity"], lane_count - 1)
        lanes[lane_idx].append({**seg, "lane_type": "activity"})

    timeline_graph_bars = []
    now_local = datetime.now(tz)
    now_line_pct = None
    if selected_day_local == now_local.date():
        day_elapsed_sec = int((now_local - day_start_local).total_seconds())
        day_elapsed_sec = max(0, min(86400, day_elapsed_sec))
        now_line_pct = (day_elapsed_sec / 86400) * 100
    for lane_idx, lane in enumerate(lanes):
        for seg in lane:
            local_start = seg["start_dt"].astimezone(tz)
            local_end = seg["end_dt"].astimezone(tz)
            start_sec = int((local_start - day_start_local).total_seconds())
            end_sec = int((local_end - day_start_local).total_seconds())
            start_sec = max(0, min(86400, start_sec))
            end_sec = max(0, min(86400, end_sec))
            if end_sec <= start_sec:
                continue
            left_pct = (start_sec / 86400) * 100
            width_pct = ((end_sec - start_sec) / 86400) * 100
            color = category_colors.get(seg["activity"], "#64748b")
            tooltip = f"{seg['activity']} | {seg['app']} | {local_start.strftime('%H:%M')} - {local_end.strftime('%H:%M')}"
            dur_txt = fmt_secs(seg["duration"])
            top_px = 6 + (lane_idx * 26)
            ts_s = seg["start_dt"].astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            ts_e = seg["end_dt"].astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            act_esc = escape(str(seg.get("activity") or ""))
            timeline_graph_bars.append(
                f"<div class='tl-seg' data-ts-start=\"{escape(ts_s)}\" data-ts-end=\"{escape(ts_e)}\" data-activity=\"{act_esc}\" data-info='{escape(tooltip)} | {dur_txt}' title='{escape(tooltip)} | {dur_txt}' style='top:{top_px}px; left:{left_pct:.4f}%; width:{max(0.25, width_pct):.4f}%; background:{color};'></div>"
            )
    timeline_graph_html = "".join(timeline_graph_bars)
    used_lane_count = max(1, sum(1 for lane in lanes if lane))
    timeline_height_px = 12 + (used_lane_count * 26)

    activity_html = "".join(
        f"<tr><td><span class='pill' style='background:{category_colors.get(name, '#64748b')}22;border-color:{category_colors.get(name, '#64748b')}66;color:{category_colors.get(name, '#cbd5e1')}'>{escape(name)}</span></td><td>{fmt_secs(sec)}</td></tr>"
        for name, sec in sorted(activity_totals.items(), key=lambda x: x[1], reverse=True)
    ) or "<tr><td colspan='2'>No data yet</td></tr>"

    sessions = []
    for item in timeline:
        if not sessions:
            sessions.append(
                {
                    "start_dt": item["start_dt"],
                    "end_dt": item["end_dt"],
                    "duration": item["duration"],
                    "activity": item["activity"],
                    "app": item["app"],
                }
            )
            continue
        prev = sessions[-1]
        gap = int((item["start_dt"] - prev["end_dt"]).total_seconds())
        if item["activity"] == prev["activity"] and item["app"] == prev["app"] and gap <= 90:
            prev["end_dt"] = item["end_dt"]
            prev["duration"] += item["duration"] + max(0, gap)
        else:
            sessions.append(
                {
                    "start_dt": item["start_dt"],
                    "end_dt": item["end_dt"],
                    "duration": item["duration"],
                    "activity": item["activity"],
                    "app": item["app"],
                }
            )

    top_sessions = sorted(sessions, key=lambda x: x["duration"], reverse=True)[:8]
    sessions_html = "".join(
        f"<tr><td>{s['start_dt'].astimezone(tz).strftime('%H:%M')}</td><td>{s['end_dt'].astimezone(tz).strftime('%H:%M')}</td><td>{fmt_secs(s['duration'])}</td><td><span class='pill' style='background:{category_colors.get(s['activity'], '#64748b')}22;border-color:{category_colors.get(s['activity'], '#64748b')}66;color:{category_colors.get(s['activity'], '#cbd5e1')}'>{escape(s['activity'])}</span></td><td>{escape(s['app'])}</td></tr>"
        for s in top_sessions
    ) or "<tr><td colspan='5'>No sessions yet</td></tr>"

    peak_hour = max(hourly_totals.items(), key=lambda x: x[1])[0] if hourly_totals else None
    max_hour_secs = max(hourly_totals.values()) if hourly_totals else 1
    heatmap_html = "".join(
        f"<tr><td>{hour:02d}:00</td><td><div class='bar-wrap'><div class='bar' style='width:{max(2, int((hourly_totals.get(hour, 0) / max_hour_secs) * 100))}%;'></div></div></td><td>{fmt_secs(hourly_totals.get(hour, 0))}</td></tr>"
        for hour in range(24)
    )

    streak = 0
    conn = get_connection()
    try:
        active_day_rows = conn.execute(
            """
            SELECT date(ts_start) AS day, SUM(
                CAST((julianday(ts_end) - julianday(ts_start)) * 86400 AS INTEGER)
            ) AS total_sec
            FROM raw_events
            GROUP BY date(ts_start)
            ORDER BY day DESC
            LIMIT 60
            """
        ).fetchall()
    finally:
        conn.close()

    active_days = {row["day"] for row in active_day_rows if (row["total_sec"] or 0) > 600}
    day_cursor = datetime.now(tz).date()
    while day_cursor.isoformat() in active_days:
        streak += 1
        day_cursor = day_cursor.fromordinal(day_cursor.toordinal() - 1)

    day_totals = defaultdict(int)
    for row in trend_rows:
        s = parse_iso(row["ts_start"])
        e = parse_iso(row["ts_end"])
        if not s or not e:
            continue
        dur = int((e - s).total_seconds())
        if dur > 0:
            day_totals[s.astimezone(tz).date().isoformat()] += dur

    selected_iso = selected_day_local.isoformat()
    week_start = selected_day_local - timedelta(days=selected_day_local.weekday())
    week_days = [(week_start + timedelta(days=i)).isoformat() for i in range(7)]
    week_total = sum(day_totals.get(d, 0) for d in week_days)
    month_prefix = f"{selected_day_local.year:04d}-{selected_day_local.month:02d}-"
    month_total = sum(v for k, v in day_totals.items() if k.startswith(month_prefix))
    last14 = [selected_day_local - timedelta(days=i) for i in range(13, -1, -1)]
    max_day = max([day_totals.get(d.isoformat(), 0) for d in last14] + [1])
    trend14_html = "".join(
        f"<tr><td>{d.strftime('%m-%d')}</td><td><div class='bar-wrap'><div class='bar' style='width:{max(2, int((day_totals.get(d.isoformat(), 0) / max_day) * 100))}%;'></div></div></td><td>{fmt_secs(day_totals.get(d.isoformat(), 0))}</td></tr>"
        for d in last14
    )

    device_status_rows = []
    for device in device_rows:
        last_seen = parse_iso(device["last_seen"])
        seconds_since = int((now_utc - last_seen).total_seconds()) if last_seen else 999999
        is_online = seconds_since <= 90
        status_text = "online" if is_online else "pc_off"
        device_status_rows.append(
            f"<tr><td>{escape(device['device_id'])}</td><td>{escape(device['platform'])}</td><td><span class='pill' style='background:{category_colors.get(status_text, '#64748b')}22;border-color:{category_colors.get(status_text, '#64748b')}66;color:{category_colors.get(status_text, '#cbd5e1')}'>{status_text}</span></td><td>{fmt_secs(seconds_since)} ago</td></tr>"
        )
    device_status_html = "".join(device_status_rows) or "<tr><td colspan='4'>No devices seen yet</td></tr>"

    return f"""
    <html>
      <head>
        <title>Activity Tracker Dashboard</title>
        <style>
          body {{ font-family: Inter, Arial, sans-serif; margin: 24px; background: linear-gradient(180deg, #0b1020 0%, #0f172a 100%); color: #e5e7eb; }}
          .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }}
          .card {{ background: rgba(15, 23, 42, 0.75); border: 1px solid #334155; border-radius: 12px; padding: 14px; backdrop-filter: blur(6px); }}
          h1, h2 {{ margin-top: 0; }}
          table {{ width: 100%; border-collapse: collapse; }}
          th, td {{ text-align: left; padding: 8px; border-bottom: 1px solid #334155; vertical-align: top; }}
          .muted {{ color: #94a3b8; }}
          .pill {{ display: inline-block; padding: 2px 10px; border-radius: 999px; border: 1px solid; font-size: 12px; font-weight: 600; }}
          a {{ color: #93c5fd; text-decoration: none; }}
          a:hover {{ text-decoration: underline; }}
          .metrics {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 16px; }}
          .metric {{ background: rgba(15, 23, 42, 0.75); border: 1px solid #334155; border-radius: 12px; padding: 12px; }}
          .metric .label {{ color: #94a3b8; font-size: 12px; }}
          .metric .value {{ font-size: 22px; font-weight: 700; margin-top: 6px; }}
          .bar-wrap {{ background: #1e293b; border-radius: 999px; height: 10px; }}
          .bar {{ background: linear-gradient(90deg, #38bdf8, #60a5fa); height: 10px; border-radius: 999px; }}
          .timeline-wrap {{ position: relative; background: #0b1220; border: 1px solid #334155; border-radius: 10px; overflow: hidden; }}
          .timeline-grid {{ position:absolute; inset:0; background-image: linear-gradient(to right, rgba(148,163,184,0.16) 1px, transparent 1px); background-size: calc(100% / 24) 100%; pointer-events:none; }}
          .tl-seg {{ position: absolute; height: 22px; opacity: 0.95; border-radius: 6px; cursor: pointer; }}
          .timeline-now {{ position:absolute; top:0; bottom:0; width:2px; background:#f43f5e; box-shadow:0 0 8px rgba(244,63,94,0.8); pointer-events:none; }}
          .axis {{ display: flex; justify-content: space-between; color: #94a3b8; font-size: 12px; margin-top: 6px; }}
          .hoverbox {{ margin-top: 8px; padding: 8px; border: 1px solid #334155; border-radius: 8px; color: #cbd5e1; font-size: 12px; line-height: 1.45; background: #0b1220; max-height: 240px; overflow-y: auto; white-space: normal; }}
          .timeline-tooltip {{ position: fixed; z-index: 9999; max-width: 320px; display:none; pointer-events:none; padding:10px 12px; border-radius:10px; border:1px solid #334155; background: rgba(2,6,23,0.95); color:#e2e8f0; font-size:12px; box-shadow:0 8px 26px rgba(0,0,0,0.35); }}
          .timeline-tooltip .k {{ color:#93c5fd; font-weight:600; }}
        </style>
      </head>
      <body>
        <h1>Activity Tracker</h1>
        <p class="muted">Date: {selected_iso} ({escape(str(tz))}) | Tracked: {fmt_secs(total_seconds)} | Events: {len(timeline)} | <a href="/explorer">Session explorer</a> | <a href="/stats">Stats</a> | <a href="/admin/rules">Manage categories & rules</a> | <a href="/admin/settings">Settings</a></p>
        <form method="get" action="/" style="margin-bottom: 14px;">
          <label class="muted">View date:</label>
          <input type="date" name="date" value="{selected_iso}" style="margin:0 8px; padding:6px; border-radius:8px; border:1px solid #334155; background:#0b1220; color:#e2e8f0;" />
          <button type="submit" style="padding:6px 10px; border-radius:8px; border:1px solid #1d4ed8; background:#1d4ed8; color:#fff;">Go</button>
        </form>
        <div class="metrics">
          <div class="metric"><div class="label">Longest session</div><div class="value">{fmt_secs(top_sessions[0]["duration"]) if top_sessions else "0s"}</div></div>
          <div class="metric"><div class="label">Peak hour</div><div class="value">{f"{peak_hour:02d}:00" if peak_hour is not None else "--:--"}</div></div>
          <div class="metric"><div class="label">Session count</div><div class="value">{len(sessions)}</div></div>
          <div class="metric"><div class="label">Active-day streak</div><div class="value">{streak}d</div></div>
        </div>
        <div class="metrics">
          <div class="metric"><div class="label">Selected day</div><div class="value">{fmt_secs(day_totals.get(selected_iso, 0))}</div></div>
          <div class="metric"><div class="label">Week total</div><div class="value">{fmt_secs(week_total)}</div></div>
          <div class="metric"><div class="label">Month total</div><div class="value">{fmt_secs(month_total)}</div></div>
          <div class="metric"><div class="label">Daily average (14d)</div><div class="value">{fmt_secs(int(sum(day_totals.get(d.isoformat(), 0) for d in last14) / 14))}</div></div>
        </div>
        <div class="grid">
          <div class="card">
            <h2>Activity Breakdown</h2>
            <table>
              <thead><tr><th>Type</th><th>Duration</th></tr></thead>
              <tbody>{activity_html}</tbody>
            </table>
          </div>
          <div class="card">
            <h2>Top Apps</h2>
            <table>
              <thead><tr><th>App</th><th>Duration</th></tr></thead>
              <tbody>{app_html}</tbody>
            </table>
          </div>
        </div>
        <div class="card" style="margin-bottom: 16px;">
          <h2>Top Sessions</h2>
          <table>
            <thead><tr><th>Start</th><th>End</th><th>Duration</th><th>Type</th><th>App</th></tr></thead>
            <tbody>{sessions_html}</tbody>
          </table>
        </div>
        <div class="card" style="margin-bottom: 16px;">
          <h2>Day Timeline (00:00-24:00)</h2>
          <form method="post" action="/admin/timeline/apply-preset" style="margin-bottom:8px; display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
            <input type="hidden" name="day" value="{selected_iso}" />
            <label class="muted">Preset:</label>
            <select name="preset_name" style="padding:6px 8px; border-radius:8px; border:1px solid #334155; background:#0b1220; color:#e2e8f0;">
              <option value="legacy" {'selected' if current_preset == 'legacy' else ''}>Legacy</option>
              <option value="conservative" {'selected' if current_preset == 'conservative' else ''}>Conservative</option>
              <option value="balanced" {'selected' if current_preset == 'balanced' else ''}>Balanced</option>
              <option value="aggressive" {'selected' if current_preset == 'aggressive' else ''}>Aggressive</option>
            </select>
            <button type="submit">Apply preset</button>
            <span class="muted">Current: same_app={preview_merge_same_app}, browser_gap={preview_gap_browser}s, bridge={preview_bridge}s, min_frag={preview_min_fragment}s, dom={preview_dom_threshold}%</span>
          </form>
          <form method="post" action="/admin/timeline/compact-day" style="margin-bottom:8px;">
            <input type="hidden" name="day" value="{selected_iso}" />
            <button type="submit">Rebuild/Compact this day</button>
          </form>
          <div class="timeline-wrap" id="timelineWrap" style="height:{timeline_height_px}px;">
            <div class="timeline-grid"></div>
            {timeline_graph_html}
            {"<div class='timeline-now' style='left:" + format(now_line_pct, ".4f") + "%;'></div>" if now_line_pct is not None else ""}
          </div>
          <div class="axis"><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span></div>
          <div class="hoverbox" id="timelineHover">Click a timeline bar to list every raw event in that range. Hover still shows the tooltip.</div>
        </div>
        <div class="card" style="margin-bottom: 16px;">
          <h2>Device Status</h2>
          <table>
            <thead><tr><th>Device</th><th>Platform</th><th>Status</th><th>Last Seen</th></tr></thead>
            <tbody>{device_status_html}</tbody>
          </table>
        </div>
        <div class="card">
          <h2>Timeline (latest segments incl. PC Off)</h2>
          <table>
            <thead>
              <tr>
                <th>Start</th><th>End</th><th>Duration</th><th>Type</th><th>App</th><th>Window</th>
              </tr>
            </thead>
            <tbody>{timeline_html}</tbody>
          </table>
        </div>
        <div class="timeline-tooltip" id="timelineTooltip"></div>
        <script>
          const hoverBox = document.getElementById("timelineHover");
          const tooltip = document.getElementById("timelineTooltip");
          const segs = document.querySelectorAll(".tl-seg");
          function placeTooltip(evt) {{
            const x = evt.clientX + 14;
            const y = evt.clientY + 14;
            tooltip.style.left = x + "px";
            tooltip.style.top = y + "px";
          }}
          segs.forEach((seg) => {{
            seg.addEventListener("mouseenter", (evt) => {{
              const info = seg.getAttribute("data-info") || "No details";
              tooltip.innerHTML = "<span class='k'>Segment:</span> " + info;
              tooltip.style.display = "block";
              placeTooltip(evt);
            }});
            seg.addEventListener("mousemove", (evt) => placeTooltip(evt));
            seg.addEventListener("mouseleave", () => {{
              tooltip.style.display = "none";
            }});
            seg.addEventListener("click", (evt) => {{
              evt.preventDefault();
              const s = seg.getAttribute("data-ts-start");
              const e = seg.getAttribute("data-ts-end");
              const act = seg.getAttribute("data-activity") || "";
              if (!s || !e || !act) return;
              hoverBox.innerHTML = "Loading raw events…";
              const url = "/api/timeline/segment-events?start=" + encodeURIComponent(s)
                + "&end=" + encodeURIComponent(e)
                + "&activity=" + encodeURIComponent(act);
              fetch(url)
                .then((r) => r.json())
                .then((data) => {{
                  const rows = data.events || [];
                  if (!rows.length) {{
                    hoverBox.textContent = "No raw events overlap this segment.";
                    return;
                  }}
                  const esc = (t) => (t || "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
                  const lines = rows.map((ev) => {{
                    const st = (ev.ts_start || "").replace("T"," ").slice(11, 19);
                    const en = (ev.ts_end || "").replace("T"," ").slice(11, 19);
                    const app = esc(ev.app_name || ev.process_name || "");
                    const tit = esc((ev.window_title || "").slice(0, 120));
                    const act = esc(ev.activity_type || "");
                    return "<div style='margin-bottom:6px;border-bottom:1px solid #1e293b;padding-bottom:4px;'><strong>" + st + "–" + en + "</strong> <span style='color:#93c5fd'>" + act + "</span> · " + app + "<br/><span class='muted' style='color:#94a3b8'>" + tit + "</span></div>";
                  }});
                  hoverBox.innerHTML = "<div style='font-weight:600;margin-bottom:8px;color:#e2e8f0'>Raw events in this bar (" + rows.length + ")</div>" + lines.join("");
                }})
                .catch(() => {{ hoverBox.textContent = "Could not load events."; }});
            }});
          }});
        </script>
      </body>
    </html>
    """


@app.post("/ingest/events", response_model=IngestResponse, dependencies=[Depends(require_api_key)])
def ingest_events(payload: IngestRequest) -> IngestResponse:
    if not payload.events:
        return IngestResponse(inserted=0)

    conn = get_connection()
    try:
        rules = load_rules(conn)
        merge_adj = ingest_merge_adjacent_events(conn)
        max_gap_sec, bridge_sec = get_ingest_merge_tuning(conn)

        rows_to_insert = []
        for event in payload.events:
            row = (
                event.ts_start,
                event.ts_end,
                event.device_id,
                event.app_name,
                event.process_name,
                event.window_title,
                event.url_full,
                event.url_domain,
                classify_with_rules(
                    rules,
                    process_name=event.process_name,
                    app_name=event.app_name,
                    window_title=event.window_title,
                    url_domain=event.url_domain,
                    idle_flag=bool(event.idle_flag),
                    fallback_activity=event.activity_type,
                ),
                1 if event.idle_flag else 0,
                event.source,
                json.dumps(event.parallel_apps or [], separators=(",", ":")),
            )
            if not merge_adj:
                rows_to_insert.append(row)
                continue

            if not rows_to_insert:
                rows_to_insert.append(row)
                continue

            prev = rows_to_insert[-1]
            gap_batch = _ingest_gap_seconds(prev[1], row[0])
            if _ingest_same_bucket_row(prev, row) and gap_batch is not None and 0 <= gap_batch <= max_gap_sec:
                rows_to_insert[-1] = (
                    prev[0],
                    row[1],
                    prev[2],
                    prev[3],
                    prev[4],
                    prev[5],
                    prev[6],
                    prev[7],
                    prev[8],
                    prev[9],
                    prev[10],
                    prev[11],
                )
            else:
                rows_to_insert.append(row)

        if merge_adj:
            rows_to_insert = apply_ingest_short_split_bridge(rows_to_insert, max_gap_sec, bridge_sec)

        def same_bucket(prev_row, new_row) -> bool:
            return (
                prev_row["device_id"] == new_row[2]
                and prev_row["app_name"] == new_row[3]
                and prev_row["process_name"] == new_row[4]
                and prev_row["window_title"] == new_row[5]
                and prev_row["url_full"] == new_row[6]
                and prev_row["url_domain"] == new_row[7]
                and prev_row["activity_type"] == new_row[8]
                and int(prev_row["idle_flag"]) == int(new_row[9])
                and prev_row["source"] == new_row[10]
                and (prev_row["parallel_apps_json"] or "[]") == (new_row[11] or "[]")
            )

        inserted_count = 0
        for row in rows_to_insert:
            if merge_adj:
                last = conn.execute(
                    """
                    SELECT id, ts_start, ts_end, device_id, app_name, process_name, window_title,
                           url_full, url_domain, activity_type, idle_flag, source, parallel_apps_json
                    FROM raw_events
                    WHERE device_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (row[2],),
                ).fetchone()

                gap_db = _ingest_gap_seconds(last["ts_end"], row[0]) if last else None
                if (
                    last
                    and same_bucket(last, row)
                    and gap_db is not None
                    and 0 <= gap_db <= max_gap_sec
                ):
                    conn.execute(
                        "UPDATE raw_events SET ts_end = ? WHERE id = ?",
                        (row[1], last["id"]),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO raw_events (
                            ts_start, ts_end, device_id, app_name, process_name, window_title,
                            url_full, url_domain, activity_type, idle_flag, source, parallel_apps_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        row,
                    )
                    inserted_count += 1
            else:
                conn.execute(
                    """
                    INSERT INTO raw_events (
                        ts_start, ts_end, device_id, app_name, process_name, window_title,
                        url_full, url_domain, activity_type, idle_flag, source, parallel_apps_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    row,
                )
                inserted_count += 1
        if payload.events:
            last_event = payload.events[-1]
            platform = "windows"
            if "android" in (last_event.source or "").lower():
                platform = "android"
            elif "ios" in (last_event.source or "").lower():
                platform = "ios"
            upsert_device_seen(
                conn,
                device_id=last_event.device_id,
                platform=platform,
                source=last_event.source or "collector",
                last_event_ts=last_event.ts_end,
            )
        conn.commit()
        return IngestResponse(inserted=inserted_count)
    finally:
        conn.close()


@app.get("/api/timeline/segment-events")
def api_timeline_segment_events(
    start: str = Query(..., description="Range start (ISO-8601, UTC)"),
    end: str = Query(..., description="Range end (ISO-8601, UTC)"),
    activity: str = Query(..., description="Must match raw_events.activity_type (case-insensitive)"),
) -> dict:
    """Raw rows overlapping the bar time range with the same activity_type only."""
    sdt = parse_iso(start)
    edt = parse_iso(end)
    if not sdt or not edt or edt <= sdt:
        raise HTTPException(status_code=400, detail="invalid start/end range")

    act = (activity or "").strip().lower()
    if not act:
        raise HTTPException(status_code=400, detail="activity is required")

    start_i = sdt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    end_i = edt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, ts_start, ts_end, device_id, app_name, process_name, window_title,
                   activity_type, idle_flag, url_domain
            FROM raw_events
            WHERE ts_start < ? AND ts_end > ?
              AND lower(coalesce(activity_type, '')) = ?
            ORDER BY ts_start ASC, id ASC
            """,
            (end_i, start_i, act),
        ).fetchall()
    finally:
        conn.close()

    return {
        "events": [
            {
                "ts_start": r["ts_start"],
                "ts_end": r["ts_end"],
                "device_id": r["device_id"],
                "app_name": r["app_name"],
                "process_name": r["process_name"],
                "window_title": r["window_title"],
                "activity_type": r["activity_type"],
                "idle_flag": int(r["idle_flag"] or 0),
                "url_domain": r["url_domain"],
            }
            for r in rows
        ]
    }


@app.get("/debug/recent-events")
def recent_events(limit: int = 50) -> dict:
    safe_limit = max(1, min(limit, 200))
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT
                id, ts_start, ts_end, device_id, app_name, process_name, window_title,
                url_full, url_domain, activity_type, idle_flag, source, created_at
            FROM raw_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
        return {
            "count": len(rows),
            "events": [dict(row) for row in rows],
        }
    finally:
        conn.close()


@app.post("/admin/rollups/recompute-today")
def recompute_today_rollup():
    from backend.jobs.daily_rollup import compute_day

    conn = get_connection()
    try:
        tz = get_dashboard_timezone(conn)
    finally:
        conn.close()
    today_local = datetime.now(tz).date().isoformat()
    compute_day(today_local)
    return RedirectResponse(url="/stats", status_code=303)


@app.post("/admin/timeline/compact-day")
def compact_timeline_day(day: str = Form(...)):
    conn = get_connection()
    try:
        tz = get_dashboard_timezone(conn)
        try:
            day_local = datetime.fromisoformat(day).date()
        except ValueError:
            day_local = datetime.now(tz).date()

        start_local = datetime.combine(day_local, datetime.min.time(), tzinfo=tz)
        end_local = start_local + timedelta(days=1)
        start_utc = start_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        end_utc = end_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

        rows = conn.execute(
            """
            SELECT id, ts_start, ts_end, device_id, app_name, process_name, window_title,
                   url_full, url_domain, activity_type, idle_flag, source, parallel_apps_json
            FROM raw_events
            WHERE ts_start >= ? AND ts_start < ?
            ORDER BY device_id ASC, ts_start ASC, id ASC
            """,
            (start_utc, end_utc),
        ).fetchall()

        to_update = []
        to_delete = []

        def _same_bucket(a, b) -> bool:
            return (
                a["device_id"] == b["device_id"]
                and a["app_name"] == b["app_name"]
                and a["process_name"] == b["process_name"]
                and a["window_title"] == b["window_title"]
                and a["url_full"] == b["url_full"]
                and a["url_domain"] == b["url_domain"]
                and a["activity_type"] == b["activity_type"]
                and int(a["idle_flag"]) == int(b["idle_flag"])
                and a["source"] == b["source"]
                and (a["parallel_apps_json"] or "[]") == (b["parallel_apps_json"] or "[]")
            )

        current = None
        for row in rows:
            if current is None:
                current = dict(row)
                continue
            contiguous = current["ts_end"] == row["ts_start"]
            if _same_bucket(current, row) and contiguous:
                current["ts_end"] = row["ts_end"]
                to_delete.append(row["id"])
            else:
                to_update.append((current["ts_end"], current["id"]))
                current = dict(row)

        if current is not None:
            to_update.append((current["ts_end"], current["id"]))

        conn.executemany("UPDATE raw_events SET ts_end = ? WHERE id = ?", to_update)
        if to_delete:
            conn.executemany("DELETE FROM raw_events WHERE id = ?", [(x,) for x in to_delete])
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url=f"/?date={day}", status_code=303)


@app.post("/admin/timeline/apply-preset")
def apply_timeline_preset(preset_name: str = Form(...), day: str = Form("")):
    presets = {
        "legacy": {
            # Mimics older behavior before stronger dominance compression.
            "timeline_merge_require_same_app": "false",
            "timeline_merge_gap_game_sec": "600",
            "timeline_merge_gap_watching_sec": "900",
            "timeline_merge_gap_coding_sec": "300",
            "timeline_merge_gap_browser_sec": "300",
            "timeline_merge_gap_other_sec": "180",
            "timeline_merge_gap_idle_sec": "60",
            "timeline_bridge_interrupt_sec": "300",
            "timeline_min_fragment_sec": "300",
            "timeline_dominance_window_sec": "3600",
            "timeline_dominance_threshold_pct": "100",
            "timeline_dominance_min_block_sec": "999999",
        },
        "conservative": {
            "timeline_merge_require_same_app": "false",
            "timeline_merge_gap_game_sec": "420",
            "timeline_merge_gap_watching_sec": "600",
            "timeline_merge_gap_coding_sec": "240",
            "timeline_merge_gap_browser_sec": "300",
            "timeline_merge_gap_other_sec": "150",
            "timeline_merge_gap_idle_sec": "30",
            "timeline_bridge_interrupt_sec": "180",
            "timeline_min_fragment_sec": "300",
            "timeline_dominance_window_sec": "2400",
            "timeline_dominance_threshold_pct": "80",
            "timeline_dominance_min_block_sec": "1200",
        },
        "balanced": {
            "timeline_merge_require_same_app": "false",
            "timeline_merge_gap_game_sec": "600",
            "timeline_merge_gap_watching_sec": "900",
            "timeline_merge_gap_coding_sec": "300",
            "timeline_merge_gap_browser_sec": "240",
            "timeline_merge_gap_other_sec": "180",
            "timeline_merge_gap_idle_sec": "60",
            "timeline_bridge_interrupt_sec": "300",
            "timeline_min_fragment_sec": "600",
            "timeline_dominance_window_sec": "3600",
            "timeline_dominance_threshold_pct": "70",
            "timeline_dominance_min_block_sec": "900",
        },
        "aggressive": {
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
        },
    }
    chosen = presets.get(preset_name.lower())
    if not chosen:
        return RedirectResponse(url=f"/?date={day}" if day else "/", status_code=303)

    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO app_settings(key, value, updated_at)
            VALUES ('timeline_preset_name', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (preset_name.lower(),),
        )
        for k, v in chosen.items():
            conn.execute(
                """
                INSERT INTO app_settings(key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """,
                (k, v),
            )
        conn.commit()
    finally:
        conn.close()

    target = f"/?date={day}" if day else "/"
    return RedirectResponse(url=target, status_code=303)


@app.get("/api/summary/day")
def summary_day(day: str | None = None) -> dict:
    conn = get_connection()
    try:
        tz = get_dashboard_timezone(conn)
        target = day or datetime.now(tz).date().isoformat()
        row = conn.execute(
            """
            SELECT day, timezone, total_tracked_sec, total_idle_sec, total_pc_off_sec,
                   top_activity, top_app, summary_json, computed_at
            FROM daily_metrics
            WHERE day = ?
            """,
            (target,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return {"day": target, "status": "missing_rollup"}

    parsed = json.loads(row["summary_json"] or "{}")
    return {
        "day": row["day"],
        "timezone": row["timezone"],
        "total_tracked_sec": row["total_tracked_sec"],
        "total_idle_sec": row["total_idle_sec"],
        "total_pc_off_sec": row["total_pc_off_sec"],
        "top_activity": row["top_activity"],
        "top_app": row["top_app"],
        "details": parsed,
        "computed_at": row["computed_at"],
    }


@app.get("/api/timeline/day")
def timeline_day(day: str | None = None) -> dict:
    conn = get_connection()
    try:
        tz = get_dashboard_timezone(conn)
        try:
            target_day = datetime.fromisoformat(day).date() if day else datetime.now(tz).date()
        except ValueError:
            target_day = datetime.now(tz).date()
        timeline_data = build_day_sessions(conn=conn, day_local=target_day, tz=tz, include_pc_off=True)
    finally:
        conn.close()
    sessions = timeline_data["primary"]

    rows = [
        {
            "start": s["start_dt"].isoformat().replace("+00:00", "Z"),
            "end": s["end_dt"].isoformat().replace("+00:00", "Z"),
            "duration_sec": s["duration"],
            "activity": s["activity"],
            "app": s["app"],
            "title": s["title"],
        }
        for s in sessions
    ]
    return {"day": target_day.isoformat(), "timezone": str(tz), "count": len(rows), "sessions": rows}


@app.get("/api/summary/range")
def summary_range(start_day: str, end_day: str) -> dict:
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT day, total_tracked_sec, total_idle_sec, total_pc_off_sec, top_activity, top_app
            FROM daily_metrics
            WHERE day >= ? AND day <= ?
            ORDER BY day ASC
            """,
            (start_day, end_day),
        ).fetchall()
    finally:
        conn.close()
    return {"count": len(rows), "rows": [dict(r) for r in rows]}


@app.post("/ingest/heartbeat", response_model=HeartbeatResponse, dependencies=[Depends(require_api_key)])
def heartbeat(payload: HeartbeatRequest) -> HeartbeatResponse:
    conn = get_connection()
    try:
        upsert_device_seen(
            conn,
            device_id=payload.device_id,
            platform=payload.platform.lower(),
            source=payload.source or "collector",
        )
        conn.commit()
    finally:
        conn.close()
    return HeartbeatResponse(status="ok")


@app.get("/collector/settings", dependencies=[Depends(require_api_key)])
def collector_settings(device_id: str | None = None) -> dict:
    conn = get_connection()
    try:
        settings_map = get_setting_map(conn)
    finally:
        conn.close()
    return {
        "device_id": device_id,
        "collector_media_aware_idle_enabled": settings_map.get("collector_media_aware_idle_enabled", "true"),
        "collector_idle_threshold_sec": settings_map.get("collector_idle_threshold_sec", "120"),
        "collector_media_domains": settings_map.get("collector_media_domains", "youtube.com,netflix.com,twitch.tv"),
        "collector_media_title_keywords": settings_map.get(
            "collector_media_title_keywords", "youtube,netflix,twitch,watching,player,video"
        ),
        "collector_game_match_strings": settings_map.get("collector_game_match_strings", ""),
        "collector_media_player_processes": settings_map.get(
            "collector_media_player_processes",
            "jellyfinmediaplayer.exe,jellyfin media player.exe,jellyfin.exe",
        ),
        "collector_parallel_browser_recent_sec": settings_map.get("collector_parallel_browser_recent_sec", "120"),
    }


@app.get("/admin/rules", response_class=HTMLResponse)
def admin_rules() -> str:
    conn = get_connection()
    try:
        categories = conn.execute(
            "SELECT id, name, color FROM activity_categories ORDER BY name ASC"
        ).fetchall()
        rules = conn.execute(
            """
            SELECT id, rule_type, match_value, activity_name, priority, enabled
            FROM classification_rules
            ORDER BY priority ASC, id ASC
            """
        ).fetchall()
    finally:
        conn.close()

    cat_rows = "".join(
        f"""
        <tr>
          <td>{escape(c['name'])}</td>
          <td>
            <div class="color-cell">
              <span class="swatch" style="background:{escape(c['color'])};"></span>
              <code>{escape(c['color'])}</code>
            </div>
          </td>
          <td>
            <form method="post" action="/admin/categories/{c['id']}/update">
              <div class="row">
                <input name="name" value="{escape(c['name'])}" required />
                <input name="color" type="color" value="{escape(c['color'])}" />
                <button type="submit">Save</button>
              </div>
            </form>
          </td>
          <td>
            <form method="post" action="/admin/categories/{c['id']}/delete">
              <button type="submit" class="danger">Delete</button>
            </form>
          </td>
        </tr>
        """
        for c in categories
    ) or "<tr><td colspan='4'>No categories yet</td></tr>"

    category_names = [c["name"] for c in categories]

    def render_activity_options(selected: str | None = None, include_placeholder: bool = False) -> str:
        parts = []
        if include_placeholder:
            parts.append("<option value=''>--select--</option>")
        for name in category_names:
            selected_attr = " selected" if selected == name else ""
            parts.append(f"<option value='{escape(name)}'{selected_attr}>{escape(name)}</option>")
        return "".join(parts)

    rule_rows = "".join(
        f"""
        <tr>
          <td>{r['id']}</td>
          <td colspan="4">
            <form method="post" action="/admin/rules/{r['id']}/update">
              <div class="row">
                <select name="rule_type">
                  <option value="process_equals" {'selected' if r['rule_type'] == 'process_equals' else ''}>process_equals</option>
                  <option value="process_contains" {'selected' if r['rule_type'] == 'process_contains' else ''}>process_contains</option>
                  <option value="title_contains" {'selected' if r['rule_type'] == 'title_contains' else ''}>title_contains</option>
                  <option value="domain_contains" {'selected' if r['rule_type'] == 'domain_contains' else ''}>domain_contains</option>
                </select>
                <input name="match_value" value="{escape(r['match_value'])}" required />
                <select name="activity_name">{render_activity_options(r['activity_name'], include_placeholder=True)}</select>
                <input name="priority" value="{r['priority']}" style="width: 80px;" />
                <button type="submit">Save</button>
              </div>
            </form>
          </td>
          <td>
            <form method="post" action="/admin/rules/{r['id']}/toggle" style="display:inline">
              <button type="submit">{'Disable' if r['enabled'] else 'Enable'}</button>
            </form>
            <form method="post" action="/admin/rules/{r['id']}/delete" style="display:inline">
              <button type="submit" class="danger">Delete</button>
            </form>
          </td>
        </tr>
        """
        for r in rules
    ) or "<tr><td colspan='6'>No rules yet</td></tr>"

    category_options = render_activity_options()

    return f"""
    <html>
      <head>
        <title>Activity Rules Admin</title>
        <style>
          body {{ font-family: Inter, Arial, sans-serif; margin: 24px; background: #0f172a; color: #e2e8f0; }}
          .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
          .card {{ background: #111827; border: 1px solid #334155; border-radius: 12px; padding: 14px; }}
          table {{ width: 100%; border-collapse: collapse; margin-top: 8px; }}
          th, td {{ text-align: left; padding: 8px; border-bottom: 1px solid #334155; }}
          input, select, button {{ padding: 8px; border-radius: 8px; border: 1px solid #334155; background: #0b1220; color: #e2e8f0; }}
          button {{ background: #1d4ed8; border-color: #1d4ed8; cursor: pointer; }}
          .danger {{ background: #b91c1c; border-color: #b91c1c; }}
          .row {{ display: flex; gap: 8px; flex-wrap: wrap; }}
          a {{ color: #93c5fd; }}
          code {{ color: #fda4af; }}
          .color-cell {{ display: inline-flex; align-items: center; gap: 8px; }}
          .swatch {{ width: 18px; height: 18px; border-radius: 4px; border: 1px solid #64748b; display: inline-block; }}
        </style>
      </head>
      <body>
        <h1>Activity Management</h1>
        <p><a href="/">Back to dashboard</a> | <a href="/admin/settings">Settings</a></p>
        <form method="post" action="/admin/reclassify-today" style="margin-bottom: 16px;">
          <button type="submit">Reclassify today's existing events</button>
        </form>
        <div class="grid">
          <div class="card">
            <h2>Add category</h2>
            <form method="post" action="/admin/categories">
              <div class="row">
                <input name="name" placeholder="category name (e.g. youtube)" required />
                <input name="color" value="#6aa9ff" />
                <button type="submit">Add</button>
              </div>
            </form>
            <table><thead><tr><th>Category</th><th>Color</th><th>Edit</th><th>Delete</th></tr></thead><tbody>{cat_rows}</tbody></table>
          </div>
          <div class="card">
            <h2>Add rule</h2>
            <form method="post" action="/admin/rules">
              <div class="row">
                <select name="rule_type">
                  <option value="process_equals">process_equals</option>
                  <option value="process_contains">process_contains</option>
                  <option value="title_contains">title_contains</option>
                  <option value="domain_contains">domain_contains</option>
                </select>
                <input name="match_value" placeholder="match value" required />
                <select name="activity_name">{category_options}</select>
                <input name="priority" value="50" />
                <button type="submit">Add rule</button>
              </div>
            </form>
            <table><thead><tr><th>ID</th><th colspan="4">Rule</th><th>Actions</th></tr></thead><tbody>{rule_rows}</tbody></table>
          </div>
        </div>
      </body>
    </html>
    """


@app.get("/explorer", response_class=HTMLResponse)
def session_explorer(
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    activity: str | None = Query(default=None),
    app: str | None = Query(default=None),
    preset: str | None = Query(default=None),
) -> str:
    conn = get_connection()
    try:
        tz = get_dashboard_timezone(conn)
        today = datetime.now(tz).date()
        if preset == "today":
            start_day = today
            end_day = today
        elif preset == "7d":
            start_day = today - timedelta(days=6)
            end_day = today
        elif preset == "30d":
            start_day = today - timedelta(days=29)
            end_day = today
        else:
            try:
                start_day = datetime.fromisoformat(start_date).date() if start_date else (today - timedelta(days=6))
            except ValueError:
                start_day = today - timedelta(days=6)
            try:
                end_day = datetime.fromisoformat(end_date).date() if end_date else today
            except ValueError:
                end_day = today

        if end_day < start_day:
            end_day = start_day

        start_local = datetime.combine(start_day, datetime.min.time(), tzinfo=tz)
        end_local = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tzinfo=tz)
        start_utc = start_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        end_utc = end_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

        rows = conn.execute(
            """
            SELECT ts_start, ts_end, app_name, process_name, window_title, activity_type
            FROM raw_events
            WHERE ts_start >= ? AND ts_start < ?
            ORDER BY ts_start ASC
            """,
            (start_utc, end_utc),
        ).fetchall()
        category_rows = conn.execute("SELECT name, color FROM activity_categories").fetchall()
    finally:
        conn.close()

    category_colors = {cat["name"]: cat["color"] for cat in category_rows}
    sessions = []
    for row in rows:
        s = parse_iso(row["ts_start"])
        e = parse_iso(row["ts_end"])
        if not s or not e:
            continue
        duration = int((e - s).total_seconds())
        if duration <= 0:
            continue
        activity_type = row["activity_type"] or "other"
        app_name = row["app_name"] or row["process_name"] or "unknown"
        title = row["window_title"] or "(no title)"

        if activity and activity_type != activity:
            continue
        if app and app.lower() not in app_name.lower():
            continue

        if not sessions:
            sessions.append(
                {
                    "start_dt": s,
                    "end_dt": e,
                    "duration": duration,
                    "activity": activity_type,
                    "app": app_name,
                    "title": title,
                }
            )
            continue
        prev = sessions[-1]
        gap = int((s - prev["end_dt"]).total_seconds())
        if prev["activity"] == activity_type and prev["app"] == app_name and gap <= 120:
            prev["end_dt"] = e
            prev["duration"] += duration + max(0, gap)
            prev["title"] = title
        else:
            sessions.append(
                {
                    "start_dt": s,
                    "end_dt": e,
                    "duration": duration,
                    "activity": activity_type,
                    "app": app_name,
                    "title": title,
                }
            )

    def fmt_secs(seconds: int) -> str:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        if h:
            return f"{h}h {m}m"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"

    total = sum(x["duration"] for x in sessions)
    table_html = "".join(
        f"<tr><td>{x['start_dt'].astimezone(tz).strftime('%Y-%m-%d %H:%M')}</td><td>{x['end_dt'].astimezone(tz).strftime('%Y-%m-%d %H:%M')}</td><td>{fmt_secs(x['duration'])}</td><td><span class='pill' style='background:{category_colors.get(x['activity'], '#64748b')}22;border-color:{category_colors.get(x['activity'], '#64748b')}66;color:{category_colors.get(x['activity'], '#cbd5e1')}'>{escape(x['activity'])}</span></td><td>{escape(x['app'])}</td><td>{escape(x['title'])}</td></tr>"
        for x in sessions[-500:]
    ) or "<tr><td colspan='6'>No sessions in range</td></tr>"

    activity_options = "".join(
        f"<option value='{escape(name)}' {'selected' if activity == name else ''}>{escape(name)}</option>"
        for name in sorted(category_colors.keys())
    )

    return f"""
    <html>
      <head>
        <title>Session Explorer</title>
        <style>
          body {{ font-family: Inter, Arial, sans-serif; margin: 24px; background: #0f172a; color: #e2e8f0; }}
          .card {{ background: #111827; border: 1px solid #334155; border-radius: 12px; padding: 14px; margin-bottom: 14px; }}
          table {{ width: 100%; border-collapse: collapse; }}
          th, td {{ text-align: left; padding: 8px; border-bottom: 1px solid #334155; vertical-align: top; }}
          .row {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
          input, select, button {{ padding: 8px; border-radius: 8px; border: 1px solid #334155; background: #0b1220; color: #e2e8f0; }}
          button {{ background: #1d4ed8; border-color: #1d4ed8; cursor: pointer; }}
          .muted {{ color: #94a3b8; }}
          .pill {{ display: inline-block; padding: 2px 10px; border-radius: 999px; border: 1px solid; font-size: 12px; font-weight: 600; }}
          a {{ color: #93c5fd; }}
        </style>
      </head>
      <body>
        <h1>Session Explorer</h1>
        <p><a href="/">Back to dashboard</a> | <a href="/stats">Stats</a></p>
        <div class="card">
          <div class="row" style="margin-bottom:10px;">
            <a href="/explorer?preset=today"><button type="button">Today</button></a>
            <a href="/explorer?preset=7d"><button type="button">Last 7 days</button></a>
            <a href="/explorer?preset=30d"><button type="button">Last 30 days</button></a>
          </div>
          <form method="get" action="/explorer">
            <div class="row">
              <label>From</label><input type="date" name="start_date" value="{start_day.isoformat()}" />
              <label>To</label><input type="date" name="end_date" value="{end_day.isoformat()}" />
              <label>Activity</label>
              <select name="activity">
                <option value="">(all)</option>
                {activity_options}
              </select>
              <label>App contains</label><input name="app" value="{escape(app or '')}" placeholder="chrome / code / osu" />
              <button type="submit">Apply</button>
            </div>
          </form>
          <p class="muted">Timezone: {escape(str(tz))} | Sessions: {len(sessions)} | Total: {fmt_secs(total)}</p>
        </div>
        <div class="card">
          <table>
            <thead><tr><th>Start</th><th>End</th><th>Duration</th><th>Type</th><th>App</th><th>Last Window</th></tr></thead>
            <tbody>{table_html}</tbody>
          </table>
        </div>
      </body>
    </html>
    """


@app.get("/stats", response_class=HTMLResponse)
def stats_page(
    preset: str | None = Query(default="14d"),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
) -> str:
    conn = get_connection()
    try:
        tz = get_dashboard_timezone(conn)
        today = datetime.now(tz).date()

        if preset == "today":
            start_day = today
            end_day = today
        elif preset == "7d":
            start_day = today - timedelta(days=6)
            end_day = today
        elif preset == "30d":
            start_day = today - timedelta(days=29)
            end_day = today
        elif preset == "custom":
            try:
                start_day = datetime.fromisoformat(start_date).date() if start_date else (today - timedelta(days=13))
            except ValueError:
                start_day = today - timedelta(days=13)
            try:
                end_day = datetime.fromisoformat(end_date).date() if end_date else today
            except ValueError:
                end_day = today
        else:
            start_day = today - timedelta(days=13)
            end_day = today

        if end_day < start_day:
            end_day = start_day

        start_local = datetime.combine(start_day, datetime.min.time(), tzinfo=tz)
        end_local = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tzinfo=tz)
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
        category_rows = conn.execute("SELECT name, color FROM activity_categories").fetchall()
    finally:
        conn.close()

    colors = {x["name"]: x["color"] for x in category_rows}
    day_totals = defaultdict(int)
    activity_totals = defaultdict(int)
    app_totals = defaultdict(int)
    hourly_totals = defaultdict(int)

    for row in rows:
        s = parse_iso(row["ts_start"])
        e = parse_iso(row["ts_end"])
        if not s or not e:
            continue
        duration = int((e - s).total_seconds())
        if duration <= 0:
            continue
        local_dt = s.astimezone(tz)
        day_totals[local_dt.date().isoformat()] += duration
        hourly_totals[local_dt.hour] += duration
        activity = row["activity_type"] or "other"
        app_name = row["app_name"] or row["process_name"] or "unknown"
        activity_totals[activity] += duration
        app_totals[app_name] += duration

    day_count = (end_day - start_day).days + 1
    days = [start_day + timedelta(days=i) for i in range(day_count)]
    day_labels = [d.strftime("%m-%d") for d in days]
    day_values = [round(day_totals.get(d.isoformat(), 0) / 3600, 2) for d in days]

    top_activities = sorted(activity_totals.items(), key=lambda x: x[1], reverse=True)[:8]
    act_labels = [x[0] for x in top_activities]
    act_values = [round(x[1] / 3600, 2) for x in top_activities]
    act_colors = [colors.get(x[0], "#64748b") for x in top_activities]

    top_apps = sorted(app_totals.items(), key=lambda x: x[1], reverse=True)[:10]
    app_labels = [x[0] for x in top_apps]
    app_values = [round(x[1] / 3600, 2) for x in top_apps]

    hour_labels = [f"{h:02d}:00" for h in range(24)]
    hour_values = [round(hourly_totals.get(h, 0) / 3600, 2) for h in range(24)]

    total_hours = round(sum(day_values), 2)
    avg_hours = round((sum(day_values) / max(1, day_count)), 2)

    return f"""
    <html>
      <head>
        <title>Stats</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
          body {{ font-family: Inter, Arial, sans-serif; margin: 24px; background: linear-gradient(180deg,#0b1020 0%, #0f172a 100%); color: #e5e7eb; }}
          .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
          .card {{ background: rgba(15,23,42,0.75); border: 1px solid #334155; border-radius: 12px; padding: 14px; }}
          .metrics {{ display:grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 12px; margin-bottom: 14px; }}
          .metric {{ background: rgba(15,23,42,0.75); border: 1px solid #334155; border-radius: 12px; padding: 12px; }}
          .label {{ color:#94a3b8; font-size: 12px; }}
          .value {{ font-size: 22px; font-weight: 700; margin-top: 6px; }}
          .row {{ display:flex; gap:8px; align-items:center; margin-bottom: 12px; }}
          select, button, input {{ padding: 8px; border-radius: 8px; border: 1px solid #334155; background:#0b1220; color:#e2e8f0; }}
          button {{ background:#1d4ed8; border-color:#1d4ed8; cursor:pointer; }}
          a {{ color:#93c5fd; }}
        </style>
      </head>
      <body>
        <h1>Stats</h1>
        <p><a href="/">Dashboard</a> | <a href="/explorer">Session Explorer</a></p>
        <form method="post" action="/admin/rollups/recompute-today" style="margin-bottom:10px;">
          <button type="submit">Recompute today's rollup</button>
        </form>
        <form method="get" action="/stats" class="row">
          <label>Preset:</label>
          <select name="preset">
            <option value="today" {'selected' if preset == 'today' else ''}>Today</option>
            <option value="7d" {'selected' if preset == '7d' else ''}>Last 7 days</option>
            <option value="14d" {'selected' if preset == '14d' else ''}>Last 14 days</option>
            <option value="30d" {'selected' if preset == '30d' else ''}>Last 30 days</option>
            <option value="custom" {'selected' if preset == 'custom' else ''}>Custom</option>
          </select>
          <label>From</label><input type="date" name="start_date" value="{start_day.isoformat()}" />
          <label>To</label><input type="date" name="end_date" value="{end_day.isoformat()}" />
          <button type="submit">Apply</button>
        </form>

        <div class="metrics">
          <div class="metric"><div class="label">Total tracked</div><div class="value">{total_hours}h</div></div>
          <div class="metric"><div class="label">Daily avg</div><div class="value">{avg_hours}h</div></div>
          <div class="metric"><div class="label">Range</div><div class="value">{day_count}d</div></div>
        </div>

        <div class="grid">
          <div class="card"><h2>Tracked Hours Trend</h2><canvas id="trendChart"></canvas></div>
          <div class="card"><h2>Activity Split</h2><canvas id="activityChart"></canvas></div>
        </div>
        <div class="grid" style="margin-top:16px;">
          <div class="card"><h2>Top Apps (hours)</h2><canvas id="appsChart"></canvas></div>
          <div class="card"><h2>Peak Hours (range)</h2><canvas id="hoursChart"></canvas></div>
        </div>

        <script>
          const dayLabels = {json.dumps(day_labels)};
          const dayValues = {json.dumps(day_values)};
          const actLabels = {json.dumps(act_labels)};
          const actValues = {json.dumps(act_values)};
          const actColors = {json.dumps(act_colors)};
          const appLabels = {json.dumps(app_labels)};
          const appValues = {json.dumps(app_values)};
          const hourLabels = {json.dumps(hour_labels)};
          const hourValues = {json.dumps(hour_values)};

          const textColor = "#cbd5e1";
          const gridColor = "#334155";

          new Chart(document.getElementById("trendChart"), {{
            type: "line",
            data: {{ labels: dayLabels, datasets: [{{ label: "Hours", data: dayValues, borderColor: "#60a5fa", backgroundColor: "rgba(96,165,250,0.25)", fill: true, tension: 0.25 }}] }},
            options: {{ plugins: {{ legend: {{ labels: {{ color: textColor }} }} }}, scales: {{ x: {{ ticks: {{ color: textColor }}, grid: {{ color: gridColor }} }}, y: {{ ticks: {{ color: textColor }}, grid: {{ color: gridColor }} }} }} }}
          }});

          new Chart(document.getElementById("activityChart"), {{
            type: "doughnut",
            data: {{ labels: actLabels, datasets: [{{ data: actValues, backgroundColor: actColors }}] }},
            options: {{ plugins: {{ legend: {{ labels: {{ color: textColor }} }} }} }}
          }});

          new Chart(document.getElementById("appsChart"), {{
            type: "bar",
            data: {{ labels: appLabels, datasets: [{{ label: "Hours", data: appValues, backgroundColor: "#38bdf8" }}] }},
            options: {{ plugins: {{ legend: {{ labels: {{ color: textColor }} }} }}, scales: {{ x: {{ ticks: {{ color: textColor }}, grid: {{ color: gridColor }} }}, y: {{ ticks: {{ color: textColor }}, grid: {{ color: gridColor }} }} }} }}
          }});

          new Chart(document.getElementById("hoursChart"), {{
            type: "bar",
            data: {{ labels: hourLabels, datasets: [{{ label: "Hours", data: hourValues, backgroundColor: "#a78bfa" }}] }},
            options: {{ plugins: {{ legend: {{ labels: {{ color: textColor }} }} }}, scales: {{ x: {{ ticks: {{ color: textColor }}, grid: {{ color: gridColor }} }}, y: {{ ticks: {{ color: textColor }}, grid: {{ color: gridColor }} }} }} }}
          }});
        </script>
      </body>
    </html>
    """


@app.post("/admin/categories")
def create_category(name: str = Form(...), color: str = Form("#6aa9ff")):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO activity_categories(name, color) VALUES(?, ?)",
            (name.strip().lower(), color.strip() or "#6aa9ff"),
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/admin/rules", status_code=303)


@app.post("/admin/rules")
def create_rule(
    rule_type: str = Form(...),
    match_value: str = Form(...),
    activity_name: str = Form(...),
    priority: int = Form(50),
):
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO classification_rules(rule_type, match_value, activity_name, priority, enabled)
            VALUES (?, ?, ?, ?, 1)
            """,
            (
                rule_type.strip().lower(),
                match_value.strip().lower(),
                activity_name.strip().lower(),
                int(priority),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/admin/rules", status_code=303)


@app.post("/admin/reclassify-today")
def reclassify_today():
    conn = get_connection()
    try:
        rules = load_rules(conn)
        rows = conn.execute(
            """
            SELECT id, process_name, app_name, window_title, url_domain, idle_flag, activity_type
            FROM raw_events
            WHERE date(ts_start) = date('now')
            """
        ).fetchall()
        for row in rows:
            new_type = classify_with_rules(
                rules,
                process_name=row["process_name"],
                app_name=row["app_name"],
                window_title=row["window_title"],
                url_domain=row["url_domain"],
                idle_flag=bool(row["idle_flag"]),
                fallback_activity=row["activity_type"],
            )
            conn.execute(
                "UPDATE raw_events SET activity_type = ? WHERE id = ?",
                (new_type, row["id"]),
            )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/admin/rules", status_code=303)


@app.post("/admin/rules/{rule_id}/update")
def update_rule(
    rule_id: int,
    rule_type: str = Form(...),
    match_value: str = Form(...),
    activity_name: str = Form(...),
    priority: int = Form(50),
):
    conn = get_connection()
    try:
        conn.execute(
            """
            UPDATE classification_rules
            SET rule_type = ?, match_value = ?, activity_name = ?, priority = ?
            WHERE id = ?
            """,
            (
                rule_type.strip().lower(),
                match_value.strip().lower(),
                activity_name.strip().lower(),
                int(priority),
                rule_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/admin/rules", status_code=303)


@app.post("/admin/rules/{rule_id}/toggle")
def toggle_rule(rule_id: int):
    conn = get_connection()
    try:
        row = conn.execute("SELECT enabled FROM classification_rules WHERE id = ?", (rule_id,)).fetchone()
        if row:
            new_enabled = 0 if row["enabled"] else 1
            conn.execute(
                "UPDATE classification_rules SET enabled = ? WHERE id = ?",
                (new_enabled, rule_id),
            )
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/admin/rules", status_code=303)


@app.post("/admin/rules/{rule_id}/delete")
def delete_rule(rule_id: int):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM classification_rules WHERE id = ?", (rule_id,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/admin/rules", status_code=303)


@app.post("/admin/categories/{category_id}/update")
def update_category(category_id: int, name: str = Form(...), color: str = Form("#6aa9ff")):
    normalized_name = name.strip().lower()
    normalized_color = color.strip() or "#6aa9ff"
    conn = get_connection()
    try:
        old = conn.execute("SELECT name FROM activity_categories WHERE id = ?", (category_id,)).fetchone()
        if old:
            old_name = old["name"]
            conn.execute(
                "UPDATE activity_categories SET name = ?, color = ? WHERE id = ?",
                (normalized_name, normalized_color, category_id),
            )
            if old_name != normalized_name:
                conn.execute(
                    "UPDATE classification_rules SET activity_name = ? WHERE activity_name = ?",
                    (normalized_name, old_name),
                )
                conn.execute(
                    "UPDATE raw_events SET activity_type = ? WHERE activity_type = ?",
                    (normalized_name, old_name),
                )
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/admin/rules", status_code=303)


@app.post("/admin/categories/{category_id}/delete")
def delete_category(category_id: int):
    protected = {"idle", "other"}
    conn = get_connection()
    try:
        cat = conn.execute("SELECT name FROM activity_categories WHERE id = ?", (category_id,)).fetchone()
        if not cat:
            return RedirectResponse(url="/admin/rules", status_code=303)

        category_name = cat["name"]
        if category_name in protected:
            return RedirectResponse(url="/admin/rules", status_code=303)

        in_use = conn.execute(
            "SELECT COUNT(*) AS c FROM classification_rules WHERE activity_name = ?",
            (category_name,),
        ).fetchone()
        if in_use and in_use["c"] > 0:
            return RedirectResponse(url="/admin/rules", status_code=303)

        conn.execute("DELETE FROM activity_categories WHERE id = ?", (category_id,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/admin/rules", status_code=303)


@app.get("/admin/settings", response_class=HTMLResponse)
def admin_settings() -> str:
    conn = get_connection()
    try:
        setting_map = get_setting_map(conn)
    finally:
        conn.close()

    media_aware_idle = setting_map.get("collector_media_aware_idle_enabled", "true")
    idle_threshold = setting_map.get("collector_idle_threshold_sec", "120")
    media_domains = setting_map.get("collector_media_domains", "youtube.com,netflix.com,twitch.tv")
    media_keywords = setting_map.get("collector_media_title_keywords", "youtube,netflix,twitch,watching,player,video")
    media_player_processes = setting_map.get(
        "collector_media_player_processes",
        "jellyfinmediaplayer.exe,jellyfin media player.exe,jellyfin.exe",
    )
    parallel_browser_recent = setting_map.get("collector_parallel_browser_recent_sec", "120")
    _ing_raw = setting_map.get("ingest_merge_adjacent")
    if _ing_raw is None:
        ingest_merge_adjacent = "true"
    else:
        ingest_merge_adjacent = "true" if str(_ing_raw).lower() == "true" else "false"
    ingest_merge_max_gap = setting_map.get("ingest_merge_max_gap_sec", "8")
    ingest_short_split_bridge = setting_map.get("ingest_short_split_bridge_sec", "30")
    game_list_stored = setting_map.get("collector_game_match_strings", "")
    game_list_count = len([x for x in game_list_stored.split(",") if x.strip()])
    dashboard_timezone = setting_map.get("dashboard_timezone", "Europe/Zagreb")
    discord_enabled = setting_map.get("discord_notifications_enabled", "false")
    discord_webhook = setting_map.get("discord_webhook_url", "")
    gap_game = setting_map.get("timeline_merge_gap_game_sec", "600")
    gap_watching = setting_map.get("timeline_merge_gap_watching_sec", "900")
    gap_coding = setting_map.get("timeline_merge_gap_coding_sec", "300")
    gap_browser = setting_map.get("timeline_merge_gap_browser_sec", "240")
    gap_other = setting_map.get("timeline_merge_gap_other_sec", "180")
    gap_idle = setting_map.get("timeline_merge_gap_idle_sec", "60")
    merge_require_same_app = setting_map.get("timeline_merge_require_same_app", "false")
    bridge_interrupt = setting_map.get("timeline_bridge_interrupt_sec", "300")
    min_fragment = setting_map.get("timeline_min_fragment_sec", "600")
    bg_include = setting_map.get("timeline_background_include_apps", "chrome.exe,msedge.exe,firefox.exe")
    bg_exclude = setting_map.get("timeline_background_exclude_apps", "discord.exe,steam.exe,explorer.exe")
    dom_window = setting_map.get("timeline_dominance_window_sec", "3600")
    dom_threshold = setting_map.get("timeline_dominance_threshold_pct", "70")
    dom_min_block = setting_map.get("timeline_dominance_min_block_sec", "900")
    history_keep_forever = setting_map.get("history_keep_forever", "false")
    retention_policy_preset = setting_map.get("retention_policy_preset", "custom")
    retention_raw_days = setting_map.get("retention_raw_events_days", "180")
    retention_metrics_days = setting_map.get("retention_daily_metrics_days", "3650")
    maintenance_backup_enabled = setting_map.get("maintenance_backup_enabled", "true")
    maintenance_backup_dir = setting_map.get("maintenance_backup_dir", "./data/backups")
    maintenance_backup_keep_count = setting_map.get("maintenance_backup_keep_count", "21")
    maintenance_vacuum_enabled = setting_map.get("maintenance_vacuum_enabled", "false")

    return f"""
    <html>
      <head>
        <title>Settings</title>
        <style>
          body {{ font-family: Inter, Arial, sans-serif; margin: 24px; background: #0f172a; color: #e2e8f0; }}
          .page {{ max-width: 1120px; }}
          .toolbar {{ margin-bottom: 14px; }}
          .muted {{ color: #94a3b8; }}
          a {{ color: #93c5fd; }}
          .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; }}
          .section {{ background: #111827; border: 1px solid #334155; border-radius: 12px; padding: 14px; }}
          .section h2 {{ margin: 2px 0 6px; font-size: 18px; }}
          .section p {{ margin: 0 0 10px; }}
          .field {{ margin-top: 10px; }}
          label {{ display: block; margin-bottom: 6px; color: #cbd5e1; font-size: 13px; }}
          input, textarea, select, button {{ width: 100%; padding: 9px; border-radius: 8px; border: 1px solid #334155; background: #0b1220; color: #e2e8f0; }}
          textarea {{ min-height: 78px; }}
          .actions {{ margin-top: 16px; display: flex; gap: 10px; flex-wrap: wrap; }}
          .btn-primary {{ background: #1d4ed8; border-color: #1d4ed8; cursor: pointer; width: auto; }}
          .btn-secondary {{ background: #0b1220; border-color: #334155; cursor: pointer; width: auto; }}
        </style>
      </head>
      <body>
        <div class="page">
          <h1>Global Settings</h1>
          <p class="toolbar"><a href="/">Dashboard</a> | <a href="/admin/games">Games list</a> | <a href="/admin/rules">Rules & Categories</a></p>
          <p class="muted">Settings are grouped by purpose and saved in DB. Collectors can auto-sync relevant settings when `sync_settings_from_server` is enabled.</p>
          <form method="post" action="/admin/settings">
            <div class="grid">
              <section class="section">
                <h2>Collector</h2>
                <p class="muted">Idle and media-aware behavior. Game substring list is edited on <a href="/admin/games">/admin/games</a> and/or <code>collector/games_list.txt</code> on the PC (merged when the collector syncs).</p>
                <div class="field">
                  <label>Media-aware idle enabled</label>
                  <select name="collector_media_aware_idle_enabled">
                    <option value="true" {'selected' if media_aware_idle == 'true' else ''}>true</option>
                    <option value="false" {'selected' if media_aware_idle == 'false' else ''}>false</option>
                  </select>
                </div>
                <div class="field">
                  <label>Idle threshold seconds</label>
                  <input type="number" min="1" name="collector_idle_threshold_sec" value="{escape(idle_threshold)}" />
                </div>
                <div class="field">
                  <label>Media domains (comma-separated)</label>
                  <textarea name="collector_media_domains">{escape(media_domains)}</textarea>
                </div>
                <div class="field">
                  <label>Media title keywords (comma-separated)</label>
                  <textarea name="collector_media_title_keywords">{escape(media_keywords)}</textarea>
                </div>
                <div class="field">
                  <label>Media player processes (desktop apps, comma-separated exe names)</label>
                  <textarea name="collector_media_player_processes">{escape(media_player_processes)}</textarea>
                </div>
                <div class="field">
                  <label>Parallel browser grace (seconds)</label>
                  <input type="number" min="0" name="collector_parallel_browser_recent_sec" value="{escape(parallel_browser_recent)}" />
                  <p class="muted" style="margin-top:6px;">Browsers are no longer in the default parallel-presence list. If you add them back: 0 = never show Chrome/etc. in background lanes; after foreground browser use, include them for this many seconds (unless foreground is a game).</p>
                </div>
                <div class="field">
                  <label>Game substring list (server copy)</label>
                  <p class="muted">{game_list_count} entr{"ies" if game_list_count != 1 else "y"} in DB. Collectors merge this with <code>games_list.txt</code>. <a href="/admin/games">Edit list</a></p>
                </div>

                <div class="field">
                  <label>Dashboard timezone (IANA, e.g. Europe/Zagreb)</label>
                  <input name="dashboard_timezone" value="{escape(dashboard_timezone)}" />
                </div>
              </section>

              <section class="section">
                <h2>Raw data ingest</h2>
                <p class="muted">Merging only joins rows with the same app, title, URL, activity, etc. Different browser tabs stay separate. Timeline view still uses its own aggressive merge for display.</p>
                <div class="field">
                  <label>Merge adjacent identical ingest rows</label>
                  <select name="ingest_merge_adjacent">
                    <option value="true" {'selected' if ingest_merge_adjacent == 'true' else ''}>true — merge duplicates &amp; small gaps (recommended)</option>
                    <option value="false" {'selected' if ingest_merge_adjacent == 'false' else ''}>false — one DB row per collector slice</option>
                  </select>
                </div>
                <div class="field">
                  <label>Max gap to treat as same run (seconds)</label>
                  <input type="number" min="0" name="ingest_merge_max_gap_sec" value="{escape(ingest_merge_max_gap)}" />
                  <p class="muted" style="margin-top:6px;">If the gap between two identical rows is ≤ this, merge them (fixes clock/batch micro-gaps).</p>
                </div>
                <div class="field">
                  <label>Short middle bridge (seconds)</label>
                  <input type="number" min="0" name="ingest_short_split_bridge_sec" value="{escape(ingest_short_split_bridge)}" />
                  <p class="muted" style="margin-top:6px;">If A → brief B → A with identical outer row (same tab/context) and B is ≤ this long, store as one A. Set 0 to disable. Separate from timeline bridge.</p>
                </div>
              </section>

              <section class="section">
                <h2>Discord</h2>
                <p class="muted">Daily summary delivery settings and manual test send.</p>
                <div class="field">
                  <label>Discord notifications enabled</label>
                  <select name="discord_notifications_enabled">
                    <option value="false" {'selected' if discord_enabled == 'false' else ''}>false</option>
                    <option value="true" {'selected' if discord_enabled == 'true' else ''}>true</option>
                  </select>
                </div>
                <div class="field">
                  <label>Discord webhook URL</label>
                  <input name="discord_webhook_url" value="{escape(discord_webhook)}" placeholder="https://discord.com/api/webhooks/..." />
                </div>
              </section>

              <section class="section">
                <h2>Timeline Algorithm</h2>
                <p class="muted">Advanced merge/smoothing controls for timeline readability.</p>
                <div class="field"><label>Merge gap: game (seconds)</label><input type="number" min="0" name="timeline_merge_gap_game_sec" value="{escape(gap_game)}" /></div>
                <div class="field"><label>Merge gap: watching (seconds)</label><input type="number" min="0" name="timeline_merge_gap_watching_sec" value="{escape(gap_watching)}" /></div>
                <div class="field"><label>Merge gap: coding (seconds)</label><input type="number" min="0" name="timeline_merge_gap_coding_sec" value="{escape(gap_coding)}" /></div>
                <div class="field"><label>Merge gap: browser (seconds)</label><input type="number" min="0" name="timeline_merge_gap_browser_sec" value="{escape(gap_browser)}" /></div>
                <div class="field"><label>Merge gap: other (seconds)</label><input type="number" min="0" name="timeline_merge_gap_other_sec" value="{escape(gap_other)}" /></div>
                <div class="field"><label>Merge gap: idle (seconds)</label><input type="number" min="0" name="timeline_merge_gap_idle_sec" value="{escape(gap_idle)}" /></div>
                <div class="field">
                  <label>Merge requires same app</label>
                  <select name="timeline_merge_require_same_app">
                    <option value="false" {'selected' if merge_require_same_app == 'false' else ''}>false (merge by activity)</option>
                    <option value="true" {'selected' if merge_require_same_app == 'true' else ''}>true (same app only)</option>
                  </select>
                </div>
                <div class="field"><label>Bridge interruption max (seconds)</label><input type="number" min="0" name="timeline_bridge_interrupt_sec" value="{escape(bridge_interrupt)}" /></div>
                <div class="field"><label>Minimum fragment target (seconds)</label><input type="number" min="0" name="timeline_min_fragment_sec" value="{escape(min_fragment)}" /></div>
                <div class="field"><label>Background include apps (comma-separated exe names)</label><textarea name="timeline_background_include_apps">{escape(bg_include)}</textarea></div>
                <div class="field"><label>Background exclude apps (comma-separated exe names)</label><textarea name="timeline_background_exclude_apps">{escape(bg_exclude)}</textarea></div>
                <div class="field"><label>Dominance window (seconds)</label><input type="number" min="60" name="timeline_dominance_window_sec" value="{escape(dom_window)}" /></div>
                <div class="field"><label>Dominance threshold (%)</label><input type="number" min="1" max="100" name="timeline_dominance_threshold_pct" value="{escape(dom_threshold)}" /></div>
                <div class="field"><label>Dominance min block (seconds)</label><input type="number" min="60" name="timeline_dominance_min_block_sec" value="{escape(dom_min_block)}" /></div>
              </section>

              <section class="section">
                <h2>Retention & Maintenance</h2>
                <p class="muted">Choose long-term history strategy and backup behavior.</p>
                <div class="field">
                  <label>Retention policy preset</label>
                  <select name="retention_policy_preset">
                    <option value="custom" {'selected' if retention_policy_preset == 'custom' else ''}>custom</option>
                    <option value="forever" {'selected' if retention_policy_preset == 'forever' else ''}>forever</option>
                    <option value="hybrid" {'selected' if retention_policy_preset == 'hybrid' else ''}>hybrid (raw 365d, metrics forever)</option>
                    <option value="space_saver" {'selected' if retention_policy_preset == 'space_saver' else ''}>space saver (raw 120d, metrics 3y)</option>
                  </select>
                </div>
                <div class="field">
                  <label>Keep history forever (disable retention deletes)</label>
                  <select name="history_keep_forever">
                    <option value="false" {'selected' if history_keep_forever == 'false' else ''}>false</option>
                    <option value="true" {'selected' if history_keep_forever == 'true' else ''}>true</option>
                  </select>
                </div>
                <div class="field"><label>Retention: raw events days</label><input type="number" min="7" name="retention_raw_events_days" value="{escape(retention_raw_days)}" /></div>
                <div class="field"><label>Retention: daily metrics days</label><input type="number" min="30" name="retention_daily_metrics_days" value="{escape(retention_metrics_days)}" /></div>
                <div class="field">
                  <label>Nightly backup enabled</label>
                  <select name="maintenance_backup_enabled">
                    <option value="true" {'selected' if maintenance_backup_enabled == 'true' else ''}>true</option>
                    <option value="false" {'selected' if maintenance_backup_enabled == 'false' else ''}>false</option>
                  </select>
                </div>
                <div class="field"><label>Backup directory (absolute path or relative to DB folder)</label><input name="maintenance_backup_dir" value="{escape(maintenance_backup_dir)}" /></div>
                <div class="field"><label>Backup files to keep</label><input type="number" min="1" max="365" name="maintenance_backup_keep_count" value="{escape(maintenance_backup_keep_count)}" /></div>
                <div class="field">
                  <label>Vacuum DB after retention deletes</label>
                  <select name="maintenance_vacuum_enabled">
                    <option value="false" {'selected' if maintenance_vacuum_enabled == 'false' else ''}>false</option>
                    <option value="true" {'selected' if maintenance_vacuum_enabled == 'true' else ''}>true</option>
                  </select>
                </div>
              </section>
            </div>

            <div class="actions">
              <button type="submit" class="btn-primary">Save settings</button>
            </div>
          </form>
          <form method="post" action="/admin/discord/send-today" style="margin-top:12px;">
            <button type="submit" class="btn-secondary">Send today's summary to Discord</button>
          </form>
        </div>
      </body>
    </html>
    """


@app.get("/admin/games", response_class=HTMLResponse)
def admin_games() -> str:
    conn = get_connection()
    try:
        raw = get_setting_map(conn).get("collector_game_match_strings", "")
    finally:
        conn.close()

    body = escape(format_game_match_list_for_editor(raw))

    return f"""
    <html>
      <head>
        <title>Game match list</title>
        <style>
          body {{ font-family: Inter, Arial, sans-serif; margin: 24px; background: #0f172a; color: #e2e8f0; }}
          .page {{ max-width: 720px; }}
          .card {{ background: #111827; border: 1px solid #334155; border-radius: 12px; padding: 16px; }}
          label {{ display: block; margin-bottom: 8px; color: #cbd5e1; }}
          textarea {{ width: 100%; min-height: 320px; padding: 12px; border-radius: 8px; border: 1px solid #334155; background: #0b1220; color: #e2e8f0; font-family: ui-monospace, Consolas, monospace; font-size: 13px; }}
          button {{ margin-top: 14px; padding: 10px 18px; border-radius: 8px; border: 1px solid #1d4ed8; background: #1d4ed8; color: #fff; cursor: pointer; }}
          .muted {{ color: #94a3b8; font-size: 14px; line-height: 1.5; }}
          a {{ color: #93c5fd; }}
        </style>
      </head>
      <body>
        <div class="page">
          <h1>Game substring list</h1>
          <p class="toolbar"><a href="/">Dashboard</a> | <a href="/admin/settings">Settings</a> | <a href="/admin/rules">Rules</a></p>
          <p class="muted">One substring per line (or comma-separated). Lines starting with <code>#</code> are comments. Matching is case-insensitive against the active window <strong>exe name + title</strong>. Empty = no auto &quot;game&quot; from this list.<br />
          On Windows, the collector also loads <code>collector/games_list.txt</code> and <strong>merges</strong> it with this server list whenever it syncs settings.</p>
          <div class="card">
            <form method="post" action="/admin/games">
              <label for="games_list">Entries</label>
              <textarea id="games_list" name="games_list" spellcheck="false" placeholder="osu!.exe&#10;elden ring&#10;riotclientservices.exe">{body}</textarea>
              <button type="submit">Save</button>
            </form>
          </div>
        </div>
      </body>
    </html>
    """


@app.post("/admin/games")
def save_admin_games(games_list: str = Form("")):
    val = parse_game_match_list_input(games_list)
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO app_settings(key, value, updated_at)
            VALUES ('collector_game_match_strings', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (val,),
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/admin/games", status_code=303)


@app.post("/admin/settings")
def update_settings(
    collector_media_aware_idle_enabled: str = Form("true"),
    collector_idle_threshold_sec: str = Form("120"),
    collector_media_domains: str = Form(""),
    collector_media_title_keywords: str = Form(""),
    collector_media_player_processes: str = Form(""),
    collector_parallel_browser_recent_sec: str = Form("120"),
    ingest_merge_adjacent: str = Form("true"),
    ingest_merge_max_gap_sec: str = Form("8"),
    ingest_short_split_bridge_sec: str = Form("30"),
    dashboard_timezone: str = Form("Europe/Zagreb"),
    discord_notifications_enabled: str = Form("false"),
    discord_webhook_url: str = Form(""),
    timeline_merge_gap_game_sec: str = Form("600"),
    timeline_merge_gap_watching_sec: str = Form("900"),
    timeline_merge_gap_coding_sec: str = Form("300"),
    timeline_merge_gap_browser_sec: str = Form("240"),
    timeline_merge_gap_other_sec: str = Form("180"),
    timeline_merge_gap_idle_sec: str = Form("60"),
    timeline_merge_require_same_app: str = Form("false"),
    timeline_bridge_interrupt_sec: str = Form("300"),
    timeline_min_fragment_sec: str = Form("600"),
    timeline_background_include_apps: str = Form(""),
    timeline_background_exclude_apps: str = Form(""),
    timeline_dominance_window_sec: str = Form("3600"),
    timeline_dominance_threshold_pct: str = Form("70"),
    timeline_dominance_min_block_sec: str = Form("900"),
    history_keep_forever: str = Form("false"),
    retention_policy_preset: str = Form("custom"),
    retention_raw_events_days: str = Form("180"),
    retention_daily_metrics_days: str = Form("3650"),
    maintenance_backup_enabled: str = Form("true"),
    maintenance_backup_dir: str = Form("./data/backups"),
    maintenance_backup_keep_count: str = Form("21"),
    maintenance_vacuum_enabled: str = Form("false"),
):
    try:
        idle_threshold_value = max(1, int(collector_idle_threshold_sec or "120"))
    except ValueError:
        idle_threshold_value = 120

    def _safe_int(raw: str, fallback: int) -> int:
        try:
            return max(0, int(raw))
        except ValueError:
            return fallback

    normalized = {
        "collector_media_aware_idle_enabled": "true" if collector_media_aware_idle_enabled == "true" else "false",
        "collector_idle_threshold_sec": str(idle_threshold_value),
        "collector_media_domains": ",".join([x.strip().lower() for x in collector_media_domains.split(",") if x.strip()]),
        "collector_media_title_keywords": ",".join(
            [x.strip().lower() for x in collector_media_title_keywords.split(",") if x.strip()]
        ),
        "collector_media_player_processes": ",".join(
            [x.strip().lower() for x in collector_media_player_processes.split(",") if x.strip()]
        ),
        "collector_parallel_browser_recent_sec": str(max(0, _safe_int(collector_parallel_browser_recent_sec, 120))),
        "ingest_merge_adjacent": "true" if ingest_merge_adjacent == "true" else "false",
        "ingest_merge_max_gap_sec": str(max(0, _safe_int(ingest_merge_max_gap_sec, 8))),
        "ingest_short_split_bridge_sec": str(max(0, _safe_int(ingest_short_split_bridge_sec, 30))),
        "dashboard_timezone": dashboard_timezone.strip() or "Europe/Zagreb",
        "discord_notifications_enabled": "true" if discord_notifications_enabled == "true" else "false",
        "discord_webhook_url": discord_webhook_url.strip(),
        "timeline_merge_gap_game_sec": str(_safe_int(timeline_merge_gap_game_sec, 600)),
        "timeline_merge_gap_watching_sec": str(_safe_int(timeline_merge_gap_watching_sec, 900)),
        "timeline_merge_gap_coding_sec": str(_safe_int(timeline_merge_gap_coding_sec, 300)),
        "timeline_merge_gap_browser_sec": str(_safe_int(timeline_merge_gap_browser_sec, 240)),
        "timeline_merge_gap_other_sec": str(_safe_int(timeline_merge_gap_other_sec, 180)),
        "timeline_merge_gap_idle_sec": str(_safe_int(timeline_merge_gap_idle_sec, 60)),
        "timeline_merge_require_same_app": "true" if timeline_merge_require_same_app == "true" else "false",
        "timeline_bridge_interrupt_sec": str(_safe_int(timeline_bridge_interrupt_sec, 300)),
        "timeline_min_fragment_sec": str(_safe_int(timeline_min_fragment_sec, 600)),
        "timeline_background_include_apps": ",".join(
            [x.strip().lower() for x in timeline_background_include_apps.split(",") if x.strip()]
        ),
        "timeline_background_exclude_apps": ",".join(
            [x.strip().lower() for x in timeline_background_exclude_apps.split(",") if x.strip()]
        ),
        "timeline_dominance_window_sec": str(_safe_int(timeline_dominance_window_sec, 3600)),
        "timeline_dominance_threshold_pct": str(min(100, max(1, _safe_int(timeline_dominance_threshold_pct, 70)))),
        "timeline_dominance_min_block_sec": str(_safe_int(timeline_dominance_min_block_sec, 900)),
        "retention_policy_preset": retention_policy_preset if retention_policy_preset in {"custom", "forever", "hybrid", "space_saver"} else "custom",
        "history_keep_forever": "true" if history_keep_forever == "true" else "false",
        "retention_raw_events_days": str(max(7, _safe_int(retention_raw_events_days, 180))),
        "retention_daily_metrics_days": str(max(30, _safe_int(retention_daily_metrics_days, 3650))),
        "maintenance_backup_enabled": "true" if maintenance_backup_enabled == "true" else "false",
        "maintenance_backup_dir": maintenance_backup_dir.strip() or "./data/backups",
        "maintenance_backup_keep_count": str(min(365, max(1, _safe_int(maintenance_backup_keep_count, 21)))),
        "maintenance_vacuum_enabled": "true" if maintenance_vacuum_enabled == "true" else "false",
    }
    preset = normalized["retention_policy_preset"]
    if preset == "forever":
        normalized["history_keep_forever"] = "true"
        normalized["retention_raw_events_days"] = "36500"
        normalized["retention_daily_metrics_days"] = "36500"
    elif preset == "hybrid":
        normalized["history_keep_forever"] = "false"
        normalized["retention_raw_events_days"] = "365"
        normalized["retention_daily_metrics_days"] = "36500"
    elif preset == "space_saver":
        normalized["history_keep_forever"] = "false"
        normalized["retention_raw_events_days"] = "120"
        normalized["retention_daily_metrics_days"] = "1095"

    conn = get_connection()
    try:
        for key, value in normalized.items():
            conn.execute(
                """
                INSERT INTO app_settings(key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """,
                (key, value),
            )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/admin/settings", status_code=303)


@app.post("/admin/discord/send-today")
def send_discord_today():
    from backend.jobs.daily_rollup import compute_day
    from backend.jobs.discord_summary import send_day_summary

    conn = get_connection()
    try:
        tz = get_dashboard_timezone(conn)
    finally:
        conn.close()

    target_day = datetime.now(tz).date().isoformat()
    compute_day(target_day)
    ok, msg = send_day_summary(target_day)
    url = "/admin/settings"
    if not ok:
        url = f"/admin/settings?discord_error={escape(msg)}"
    return RedirectResponse(url=url, status_code=303)
