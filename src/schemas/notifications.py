from datetime import UTC
from typing import Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    NonNegativeInt,
    field_validator,
)


class NotificationItem(BaseModel):
    id: UUID
    event_type: str
    title: str
    message: str
    link_url: str | None
    is_read: bool
    created_at: AwareDatetime

    @field_validator("link_url")
    @classmethod
    def safe_navigation(cls, value: str | None) -> str | None:
        if (
            value is None
            or not value.startswith("/")
            or "//" in value
            or any(ch in "%\\?#:" or not 33 <= ord(ch) <= 126 for ch in value)
            or any(segment in {".", ".."} for segment in value.split("/"))
        ):
            return None
        return value

    @field_validator("created_at")
    @classmethod
    def utc_timestamp(cls, value: AwareDatetime) -> AwareDatetime:
        return value.astimezone(UTC)


class NotificationList(BaseModel):
    items: list[NotificationItem]
    unread_count: NonNegativeInt


class MarkReadRequest(BaseModel):
    """An optional empty object. The route determines the requested operation."""

    model_config = ConfigDict(extra="forbid")


class MarkReadResult(BaseModel):
    id: UUID
    is_read: Literal[True] = True
    unread_count: NonNegativeInt


class MarkAllReadResult(BaseModel):
    updated_count: NonNegativeInt
    unread_count: NonNegativeInt


class NotificationError(BaseModel):
    detail: str
