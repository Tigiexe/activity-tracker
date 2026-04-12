"""
Run the API using ACTIVITY_HOST / ACTIVITY_PORT from .env (see .env.example).

From the project root:
  python serve.py

Default bind is 127.0.0.1 (local-only). For a VPS or LAN-wide access, set ACTIVITY_HOST=0.0.0.0
and use a strong ACTIVITY_API_KEY (HTTPS via a reverse proxy is recommended on the public internet).
"""
from __future__ import annotations

import os

import uvicorn

from backend.app.config import settings

if __name__ == "__main__":
    reload = os.environ.get("ACTIVITY_DEV_RELOAD", "").lower() in ("1", "true", "yes")
    uvicorn.run(
        "backend.app.main:app",
        host=settings.host,
        port=settings.port,
        reload=reload,
    )
