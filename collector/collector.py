import ctypes
import json
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import psutil
import requests
import win32gui
import win32process

USER32 = ctypes.windll.user32
KERNEL32 = ctypes.windll.kernel32

GAMES_LIST_FILENAME = "games_list.txt"

BROWSER_EXES = frozenset({"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe"})


@dataclass
class CollectorConfig:
    server_url: str
    api_key: str
    device_id: str
    sampling_interval_default_sec: int
    sampling_interval_stable_sec: int
    stable_after_sec: int
    sampling_interval_game_sec: int
    upload_interval_sec: int
    max_batch_size: int
    idle_threshold_sec: int
    gaming_mode_enabled: bool
    media_aware_idle_enabled: bool
    process_exclusions: List[str]
    domain_blocklist: List[str]
    media_domains: List[str]
    media_title_keywords: List[str]
    sync_settings_from_server: bool
    settings_refresh_sec: int
    heartbeat_interval_sec: int
    parallel_presence_processes: List[str]
    parallel_presence_max: int
    parallel_browser_recent_sec: int
    media_player_processes: List[str]
    game_match_strings: List[str]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def get_idle_seconds() -> int:
    last_input = LASTINPUTINFO()
    last_input.cbSize = ctypes.sizeof(LASTINPUTINFO)
    ok = USER32.GetLastInputInfo(ctypes.byref(last_input))
    if not ok:
        return 0

    # GetTickCount/GetTickCount64 are exported by kernel32, not user32.
    if hasattr(KERNEL32, "GetTickCount64"):
        now_ticks = KERNEL32.GetTickCount64()
    else:
        now_ticks = KERNEL32.GetTickCount()

    millis = int(now_ticks) - int(last_input.dwTime)
    if millis < 0:
        return 0
    return int(millis / 1000)


def get_active_window_info() -> Tuple[Optional[str], Optional[str], Optional[str], Optional[int]]:
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return None, None, None, None

    title = win32gui.GetWindowText(hwnd) or None
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    if not pid:
        return title, None, None, None

    try:
        proc = psutil.Process(pid)
        process_name = proc.name()
        exe_name = Path(proc.exe()).name if proc.exe() else process_name
        return title, process_name, exe_name, pid
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return title, None, None, pid


def infer_url_from_title(window_title: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not window_title:
        return None, None
    if "youtube" in window_title.lower():
        return None, "youtube.com"

    tokens = window_title.split()
    domain = None
    for token in tokens:
        t = token.strip("()[]{}<>|,")
        if "." in t and len(t) > 4 and "/" not in t and not t.endswith(".exe"):
            domain = t.lower()
            break

    if not domain:
        return None, None
    return None, domain


def is_media_playback(
    exe_name: Optional[str],
    window_title: Optional[str],
    url_domain: Optional[str],
    config: CollectorConfig,
) -> bool:
    exe_lower = (exe_name or "").lower()
    # Desktop players (Jellyfin app, VLC, etc.) — not browser tabs
    if exe_lower in config.media_player_processes:
        return True

    if exe_lower not in {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe"}:
        return False

    domain = (url_domain or "").lower()
    title = (window_title or "").lower()
    if any(d in domain for d in config.media_domains):
        return True
    if any(k in title for k in config.media_title_keywords):
        return True
    return False


def is_probable_game(exe_name: Optional[str], window_title: Optional[str], config: CollectorConfig) -> bool:
    """True if exe+title contains any configured substring (see game_match_strings / server sync)."""
    if not config.game_match_strings:
        return False
    if not exe_name and not window_title:
        return False
    text = f"{exe_name or ''} {window_title or ''}".lower()
    return any(sig in text for sig in config.game_match_strings if sig)


def classify_activity(
    exe_name: Optional[str],
    process_name: Optional[str],
    window_title: Optional[str],
    idle_flag: bool,
    config: CollectorConfig,
) -> str:
    if idle_flag:
        return "idle"
    name_blob = f"{exe_name or ''} {process_name or ''} {window_title or ''}".lower()
    if any(x in name_blob for x in ["code.exe", "pycharm", "idea64", "notepad++", "terminal", "powershell", "cmd.exe"]):
        return "coding"
    if any(x in name_blob for x in ["chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe"]):
        return "browser"
    if is_probable_game(exe_name, window_title, config):
        return "game"
    return "other"


def load_config(path: Path) -> CollectorConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return CollectorConfig(
        server_url=raw["server_url"],
        api_key=raw["api_key"],
        device_id=raw.get("device_id") or socket.gethostname(),
        sampling_interval_default_sec=int(raw.get("sampling_interval_default_sec", 4)),
        sampling_interval_stable_sec=int(raw.get("sampling_interval_stable_sec", 12)),
        stable_after_sec=int(raw.get("stable_after_sec", 60)),
        sampling_interval_game_sec=int(raw.get("sampling_interval_game_sec", 15)),
        upload_interval_sec=int(raw.get("upload_interval_sec", 20)),
        max_batch_size=int(raw.get("max_batch_size", 100)),
        idle_threshold_sec=int(raw.get("idle_threshold_sec", 120)),
        gaming_mode_enabled=bool(raw.get("gaming_mode_enabled", True)),
        media_aware_idle_enabled=bool(raw.get("media_aware_idle_enabled", True)),
        process_exclusions=[x.lower() for x in raw.get("process_exclusions", [])],
        domain_blocklist=[x.lower() for x in raw.get("domain_blocklist", [])],
        media_domains=[x.lower() for x in raw.get("media_domains", ["youtube.com", "netflix.com", "twitch.tv"])],
        media_title_keywords=[
            x.lower()
            for x in raw.get(
                "media_title_keywords",
                ["youtube", "netflix", "twitch", "watching", "player", "video"],
            )
        ],
        sync_settings_from_server=bool(raw.get("sync_settings_from_server", True)),
        settings_refresh_sec=int(raw.get("settings_refresh_sec", 180)),
        heartbeat_interval_sec=int(raw.get("heartbeat_interval_sec", 30)),
        parallel_presence_processes=[
            x.lower()
            for x in raw.get(
                "parallel_presence_processes",
                ["discord.exe", "spotify.exe", "steam.exe"],
            )
        ],
        parallel_presence_max=int(raw.get("parallel_presence_max", 5)),
        parallel_browser_recent_sec=int(raw.get("parallel_browser_recent_sec", 120)),
        media_player_processes=[
            x.lower().strip()
            for x in raw.get(
                "media_player_processes",
                [
                    "jellyfinmediaplayer.exe",
                    "jellyfin media player.exe",
                    "jellyfin.exe",
                ],
            )
            if str(x).strip()
        ],
        game_match_strings=[],
    )


def _split_csv(value: str) -> List[str]:
    return [x.strip().lower() for x in value.split(",") if x.strip()]


def load_games_list_file(path: Path) -> List[str]:
    """Load substring list from games_list.txt (one entry per line and/or comma-separated; # starts comment)."""
    if not path.exists():
        return []
    seen: set[str] = set()
    order: List[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.split("#", 1)[0]
        for seg in line.split(","):
            s = seg.strip().lower()
            if not s:
                continue
            if s not in seen:
                seen.add(s)
                order.append(s)
    return order


def merge_game_match_strings(file_signals: List[str], server_csv: str) -> List[str]:
    return sorted(set(file_signals) | set(_split_csv(server_csv or "")))


def apply_remote_settings(config: CollectorConfig, remote: Dict[str, Any], games_file: Path) -> None:
    config.media_aware_idle_enabled = str(remote.get("collector_media_aware_idle_enabled", "true")).lower() == "true"
    try:
        config.idle_threshold_sec = max(1, int(str(remote.get("collector_idle_threshold_sec", config.idle_threshold_sec))))
    except ValueError:
        pass
    domains = remote.get("collector_media_domains")
    if isinstance(domains, str):
        config.media_domains = _split_csv(domains)
    keywords = remote.get("collector_media_title_keywords")
    if isinstance(keywords, str):
        config.media_title_keywords = _split_csv(keywords)
    game_raw = remote.get("collector_game_match_strings")
    server_games = game_raw if isinstance(game_raw, str) else ""
    config.game_match_strings = merge_game_match_strings(load_games_list_file(games_file), server_games)
    players = remote.get("collector_media_player_processes")
    if isinstance(players, str):
        config.media_player_processes = _split_csv(players)
    try:
        config.parallel_browser_recent_sec = max(
            0, int(str(remote.get("collector_parallel_browser_recent_sec", config.parallel_browser_recent_sec)))
        )
    except ValueError:
        pass


def fetch_remote_settings(config: CollectorConfig, games_file: Path) -> None:
    endpoint = urljoin(config.server_url, "/collector/settings")
    try:
        resp = requests.get(
            endpoint,
            headers={"x-api-key": config.api_key},
            params={"device_id": config.device_id},
            timeout=(2, 2),
        )
        if resp.status_code == 200:
            apply_remote_settings(config, resp.json(), games_file)
    except requests.RequestException:
        return


def send_heartbeat(config: CollectorConfig) -> None:
    endpoint = urljoin(config.server_url, "/ingest/heartbeat")
    try:
        requests.post(
            endpoint,
            headers={"x-api-key": config.api_key, "Content-Type": "application/json"},
            json={
                "device_id": config.device_id,
                "platform": "windows",
                "source": "windows_collector",
            },
            timeout=(2, 2),
        )
    except requests.RequestException:
        return


class EventSpool:
    def __init__(self, spool_file: Path) -> None:
        spool_file.parent.mkdir(parents=True, exist_ok=True)
        self.spool_file = spool_file
        if not spool_file.exists():
            spool_file.write_text("", encoding="utf-8")
        self.lock = threading.Lock()

    def append_many(self, events: List[Dict[str, Any]]) -> None:
        if not events:
            return
        with self.lock:
            with self.spool_file.open("a", encoding="utf-8") as f:
                for event in events:
                    f.write(json.dumps(event, separators=(",", ":")))
                    f.write("\n")

    def read_batch(self, max_items: int) -> List[Dict[str, Any]]:
        with self.lock:
            lines = self.spool_file.read_text(encoding="utf-8").splitlines()
            batch_lines = lines[:max_items]
        return [json.loads(x) for x in batch_lines]

    def drop_batch(self, dropped_count: int) -> None:
        if dropped_count <= 0:
            return
        with self.lock:
            lines = self.spool_file.read_text(encoding="utf-8").splitlines()
            remaining = lines[dropped_count:]
            self.spool_file.write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")


class Uploader(threading.Thread):
    def __init__(self, config: CollectorConfig, spool: EventSpool) -> None:
        super().__init__(daemon=True)
        self.config = config
        self.spool = spool
        self.stop_event = threading.Event()
        self.backoff_sec = 0

    def send_batch(self, events: List[Dict[str, Any]]) -> bool:
        try:
            resp = requests.post(
                self.config.server_url,
                headers={"x-api-key": self.config.api_key, "Content-Type": "application/json"},
                json={"events": events},
                timeout=(2, 2),
            )
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def run(self) -> None:
        while not self.stop_event.is_set():
            if self.backoff_sec > 0:
                time.sleep(self.backoff_sec)
            else:
                time.sleep(self.config.upload_interval_sec)

            batch = self.spool.read_batch(self.config.max_batch_size)
            if not batch:
                self.backoff_sec = 0
                continue

            ok = self.send_batch(batch)
            if ok:
                self.spool.drop_batch(len(batch))
                self.backoff_sec = 0
            else:
                self.backoff_sec = 30 if self.backoff_sec == 0 else min(self.backoff_sec * 2, 300)

    def stop(self) -> None:
        self.stop_event.set()


def run_collector(config_path: Path) -> None:
    config = load_config(config_path)
    games_path = config_path.parent / GAMES_LIST_FILENAME
    config.game_match_strings = merge_game_match_strings(load_games_list_file(games_path), "")
    if config.sync_settings_from_server:
        fetch_remote_settings(config, games_path)
    spool = EventSpool(config_path.parent / "spool" / "events.jsonl")
    uploader = Uploader(config, spool)
    uploader.start()

    last_signature = None
    last_change = time.time()
    prev_ts = utc_now_iso()
    last_settings_sync = 0.0
    last_heartbeat = 0.0
    parallel_browser_deadline = 0.0

    while True:
        loop_started = time.perf_counter()
        now_unix = time.time()
        if config.sync_settings_from_server and now_unix - last_settings_sync >= config.settings_refresh_sec:
            fetch_remote_settings(config, games_path)
            last_settings_sync = now_unix
        if now_unix - last_heartbeat >= config.heartbeat_interval_sec:
            send_heartbeat(config)
            last_heartbeat = now_unix

        now_ts = utc_now_iso()
        idle_seconds = get_idle_seconds()
        idle_flag = idle_seconds >= config.idle_threshold_sec

        window_title, process_name, exe_name, _ = get_active_window_info()
        exe_lower = (exe_name or "").lower()

        if exe_lower and exe_lower in config.process_exclusions:
            time.sleep(config.sampling_interval_default_sec)
            prev_ts = now_ts
            continue

        url_full = None
        url_domain = None
        if not (config.gaming_mode_enabled and is_probable_game(exe_name, window_title, config)):
            if exe_lower in {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe"}:
                url_full, url_domain = infer_url_from_title(window_title)
                if url_full:
                    parsed = urlparse(url_full)
                    url_domain = parsed.netloc.lower() if parsed.netloc else url_domain

        if url_domain and any(block in url_domain for block in config.domain_blocklist):
            url_full = None
            url_domain = None

        media_active = is_media_playback(exe_name, window_title, url_domain, config)
        if config.media_aware_idle_enabled and idle_flag and media_active:
            # Keep passive video watching from being counted as idle.
            idle_flag = False

        activity_type = classify_activity(exe_name, process_name, window_title, idle_flag, config)
        if media_active and activity_type == "browser":
            activity_type = "watching"
        if media_active and activity_type == "other" and exe_lower in config.media_player_processes:
            activity_type = "watching"

        now_wall = time.time()
        if activity_type == "game":
            parallel_browser_deadline = 0.0
        elif exe_lower in BROWSER_EXES:
            parallel_browser_deadline = now_wall + float(config.parallel_browser_recent_sec)
        allow_parallel_browser = (
            config.parallel_browser_recent_sec > 0
            and parallel_browser_deadline > now_wall
            and activity_type != "game"
        )

        if now_ts == prev_ts:
            time.sleep(1)
            continue

        parallel_apps: List[str] = []
        if config.parallel_presence_processes:
            wanted = set(config.parallel_presence_processes)
            active_name = (exe_name or "").lower()
            seen = set()
            try:
                for proc in psutil.process_iter(["name"]):
                    name = (proc.info.get("name") or "").lower()
                    if not name or name == active_name or name not in wanted or name in seen:
                        continue
                    if name in BROWSER_EXES and not allow_parallel_browser:
                        continue
                    seen.add(name)
                    parallel_apps.append(name)
                    if len(parallel_apps) >= config.parallel_presence_max:
                        break
            except (psutil.Error, OSError):
                parallel_apps = []

        event = {
            "ts_start": prev_ts,
            "ts_end": now_ts,
            "device_id": config.device_id,
            "app_name": exe_name,
            "process_name": process_name,
            "window_title": window_title,
            "url_full": url_full,
            "url_domain": url_domain,
            "activity_type": activity_type,
            "idle_flag": bool(idle_flag),
            "source": "windows_collector",
            "parallel_apps": parallel_apps,
        }
        spool.append_many([event])
        prev_ts = now_ts

        signature = f"{exe_name}|{window_title}|{url_domain}|{activity_type}|{idle_flag}"
        if signature != last_signature:
            last_signature = signature
            last_change = time.time()

        stable_for = time.time() - last_change
        if config.gaming_mode_enabled and activity_type == "game":
            next_sleep = config.sampling_interval_game_sec
        elif stable_for >= config.stable_after_sec:
            next_sleep = config.sampling_interval_stable_sec
        else:
            next_sleep = config.sampling_interval_default_sec

        loop_ms = (time.perf_counter() - loop_started) * 1000
        if loop_ms > 50:
            next_sleep = max(next_sleep, 8)
        time.sleep(next_sleep)


if __name__ == "__main__":
    cfg = Path(__file__).parent / "config.json"
    if not cfg.exists():
        raise SystemExit("Missing config file: collector/config.json (copy from config.example.json)")
    run_collector(cfg)
