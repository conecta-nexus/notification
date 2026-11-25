from uuid import UUID

from sqlalchemy import func, literal, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.notification import Notification
from src.schemas.notifications import (
    MarkAllReadResult,
    MarkReadResult,
    NotificationItem,
    NotificationList,
)


def unread_count(user_id: UUID):
    return (
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user_id, Notification.is_read.is_(False))
    )


def list_statement(user_id: UUID, limit: int, offset: int):
    page = (
        select(
            Notification.id,
            Notification.event_type,
            Notification.title,
            Notification.message,
            Notification.link_url,
            Notification.is_read,
            Notification.created_at,
        )
        .where(Notification.user_id == user_id)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(limit)
        .offset(offset)
        .subquery()
    )
    # A one-row anchor preserves the count even if the requested page is empty.
    anchor = select(literal(1)).subquery()
    return (
        select(page, unread_count(user_id).scalar_subquery().label("unread_count"))
        .select_from(anchor.outerjoin(page, true()))
        .order_by(page.c.created_at.desc(), page.c.id.desc())
    )


async def list_notifications(
    session: AsyncSession, user_id: UUID, limit: int, offset: int
) -> NotificationList:
    rows = (
        (await session.execute(list_statement(user_id, limit, offset))).mappings().all()
    )
    return NotificationList(
        items=[
            NotificationItem.model_validate(row)
            for row in rows
            if row["id"] is not None
        ],
        unread_count=rows[0]["unread_count"],
    )


def mark_all_statement(user_id: UUID):
    return (
        update(Notification)
        .where(Notification.user_id == user_id, Notification.is_read.is_(False))
        .values(is_read=True)
    )


def mark_one_statement(user_id: UUID, notification_id: UUID):
    return (
        mark_all_statement(user_id)
        .where(Notification.id == notification_id)
        .returning(Notification.id)
    )


async def mark_notification_read(
    session: AsyncSession, user_id: UUID, notification_id: UUID
) -> MarkReadResult | None:
    async with session.begin():
        updated = await session.scalar(mark_one_statement(user_id, notification_id))
        if updated is None:
            owned = await session.scalar(
                select(Notification.id).where(
                    Notification.id == notification_id, Notification.user_id == user_id
                )
            )
            if owned is None:
                return None
        count = await session.scalar(unread_count(user_id))
    return MarkReadResult(id=notification_id, unread_count=count)


async def mark_all_read(session: AsyncSession, user_id: UUID) -> MarkAllReadResult:
    async with session.begin():
        updated = await session.execute(mark_all_statement(user_id))
        count = await session.scalar(unread_count(user_id))
    return MarkAllReadResult(updated_count=updated.rowcount, unread_count=count)
