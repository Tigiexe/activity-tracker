"""
Run the API using ACTIVITY_HOST / ACTIVITY_PORT from .env.

From the project root (developer):
  python serve.py

Bundled as a Windows .exe (portable build): working directory is set to the folder
containing the executable; a .env is created on first run with a random API key.
"""
from __future__ import annotations

import os
import secrets
import sys
import threading
import time
import webbrowser
from pathlib import Path


def _app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def _prepare_app_directory() -> None:
    base = _app_base_dir()
    os.chdir(base)
    (base / "data").mkdir(parents=True, exist_ok=True)
    env_path = base / ".env"
    if not env_path.exists():
        key = secrets.token_urlsafe(32)
        env_path.write_text(
            "ACTIVITY_HOST=127.0.0.1\n"
            "ACTIVITY_PORT=8000\n"
            f"ACTIVITY_API_KEY={key}\n"
            "ACTIVITY_DB_PATH=./data/activity.db\n",
            encoding="utf-8",
        )


def _maybe_open_browser_later(host: str, port: int) -> None:
    """Portable build: open dashboard once the server is listening."""
    if host not in ("127.0.0.1", "localhost"):
        return

    def _open() -> None:
        time.sleep(1.2)
        webbrowser.open(f"http://127.0.0.1:{port}/")

    threading.Thread(target=_open, daemon=True).start()


_prepare_app_directory()

import uvicorn  # noqa: E402

from backend.app.config import settings  # noqa: E402
from backend.app.main import app as asgi_application  # noqa: E402

if __name__ == "__main__":
    reload = os.environ.get("ACTIVITY_DEV_RELOAD", "").lower() in ("1", "true", "yes")
    if reload and getattr(sys, "frozen", False):
        reload = False
    if getattr(sys, "frozen", False) or os.environ.get("ACTIVITY_OPEN_BROWSER", "").lower() in (
        "1",
        "true",
        "yes",
    ):
        _maybe_open_browser_later(settings.host, settings.port)
    # Pass the app object when not using reload so PyInstaller bundles backend.app.main.
    # String import alone is not traced by PyInstaller and breaks the frozen .exe.
    app_target: str | object = "backend.app.main:app" if reload else asgi_application
    uvicorn.run(
        app_target,
        host=settings.host,
        port=settings.port,
        reload=reload,
    )
