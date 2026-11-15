"""Request models for the Backup Manager API (kept out of the router for size)."""

import logging
from typing import Optional

from pydantic import BaseModel

logger = logging.getLogger("ethos-nas.services.backup_models")


class ScheduleCreateRequest(BaseModel):
    name: str
    source: str = ""
    destination: str
    cron: str = "0 2 * * *"
    type: str = "folder"
    enabled: bool = True
    options: dict = {}
    retention: Optional[dict] = None
    verify_after_run: Optional[str] = None
    skip_if_missing: bool = True
    remote: dict = {}
    model_config = {"extra": "forbid"}


class ScheduleUpdateRequest(BaseModel):
    name: Optional[str] = None
    source: Optional[str] = None
    destination: Optional[str] = None
    cron: Optional[str] = None
    type: Optional[str] = None
    enabled: Optional[bool] = None
    options: Optional[dict] = None
    retention: Optional[dict] = None
    verify_after_run: Optional[str] = None
    skip_if_missing: Optional[bool] = None
    remote: Optional[dict] = None
    model_config = {"extra": "forbid"}


class RestoreRequest(BaseModel):
    generation: Optional[str] = None
    mode: str = "in_place"
    target: Optional[str] = None
    delete_extra: bool = False
    model_config = {"extra": "forbid"}


class VerifyRequest(BaseModel):
    deep: bool = False
    model_config = {"extra": "forbid"}


class CancelRequest(BaseModel):
    op_id: Optional[str] = None
    model_config = {"extra": "forbid"}


class RemoteTestRequest(BaseModel):
    remote: str
    model_config = {"extra": "forbid"}


class SettingsRequest(BaseModel):
    default_retention: Optional[dict] = None
    verify_after_run: Optional[str] = None
    digest_enabled: Optional[bool] = None
    digest_weekday: Optional[int] = None
    digest_hour: Optional[int] = None
    model_config = {"extra": "forbid"}
