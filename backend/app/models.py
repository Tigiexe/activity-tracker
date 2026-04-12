from typing import List, Optional

from pydantic import BaseModel, Field


class RawEventIn(BaseModel):
    ts_start: str
    ts_end: str
    device_id: str
    app_name: Optional[str] = None
    process_name: Optional[str] = None
    window_title: Optional[str] = None
    url_full: Optional[str] = None
    url_domain: Optional[str] = None
    activity_type: Optional[str] = None
    idle_flag: bool = False
    source: str = "windows_collector"
    parallel_apps: List[str] = Field(default_factory=list)


class IngestRequest(BaseModel):
    events: List[RawEventIn] = Field(default_factory=list)


class IngestResponse(BaseModel):
    inserted: int


class HeartbeatRequest(BaseModel):
    device_id: str
    platform: str = "windows"
    source: str = "windows_collector"


class HeartbeatResponse(BaseModel):
    status: str
