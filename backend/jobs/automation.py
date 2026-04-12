import argparse
import os
import tempfile
from datetime import datetime, timedelta

from backend.app.database import init_db
from backend.jobs.daily_rollup import compute_day
from backend.jobs.discord_summary import send_day_summary
from backend.jobs.maintenance import run_maintenance


class _FileLock:
    def __init__(self, name: str):
        self.path = os.path.join(tempfile.gettempdir(), name)
        self.fd = None

    def acquire(self) -> bool:
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.fd, str(os.getpid()).encode("ascii", errors="ignore"))
            return True
        except FileExistsError:
            return False

    def release(self) -> None:
        try:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            if os.path.exists(self.path):
                os.remove(self.path)
        except OSError:
            pass


def _run_rollup(days_back: int) -> list[str]:
    today = datetime.now().date()
    computed = []
    for i in range(days_back, -1, -1):
        day_iso = (today - timedelta(days=i)).isoformat()
        compute_day(day_iso)
        computed.append(day_iso)
    return computed


def run_nightly(days_back: int, send_discord: bool, run_maint: bool) -> int:
    lock = _FileLock("activitytracker-nightly.lock")
    if not lock.acquire():
        print("nightly automation skipped: another run is already in progress")
        return 0
    try:
        init_db()
        computed = _run_rollup(days_back=days_back)
        print(f"nightly rollup complete: {', '.join(computed)}")
        if send_discord:
            yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
            ok, msg = send_day_summary(yesterday)
            print(f"discord summary ({yesterday}): {ok} - {msg}")
        if run_maint:
            maint = run_maintenance(backup_allowed=True)
            print(
                "nightly maintenance: "
                f"raw_deleted={maint['deleted_raw_events']}, "
                f"metrics_deleted={maint['deleted_daily_metrics']}, "
                f"backup={maint['backup_path'] or 'skipped'}"
            )
        return 0
    finally:
        lock.release()


def run_startup_safe(days_back: int) -> int:
    lock = _FileLock("activitytracker-startup.lock")
    if not lock.acquire():
        print("startup-safe automation skipped: another run is already in progress")
        return 0
    try:
        init_db()
        computed = _run_rollup(days_back=days_back)
        print(f"startup-safe catch-up complete: {', '.join(computed)}")
        return 0
    finally:
        lock.release()


def main() -> None:
    parser = argparse.ArgumentParser(description="Activity Tracker automation jobs")
    parser.add_argument("--mode", choices=["nightly", "startup"], default="nightly")
    parser.add_argument(
        "--days-back",
        type=int,
        default=2,
        help="Recompute from N days ago through today",
    )
    parser.add_argument(
        "--no-discord",
        action="store_true",
        help="Skip Discord send (nightly mode only)",
    )
    parser.add_argument(
        "--no-maintenance",
        action="store_true",
        help="Skip retention+backup maintenance (nightly mode only)",
    )
    args = parser.parse_args()

    days_back = max(0, args.days_back)
    if args.mode == "startup":
        raise SystemExit(run_startup_safe(days_back=days_back))
    raise SystemExit(
        run_nightly(days_back=days_back, send_discord=not args.no_discord, run_maint=not args.no_maintenance)
    )


if __name__ == "__main__":
    main()

