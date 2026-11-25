import logging
from collections.abc import Callable, Coroutine
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.security import Principal, authenticated_user
from src.schemas.notifications import (
    MarkAllReadResult,
    MarkReadRequest,
    MarkReadResult,
    NotificationError,
    NotificationList,
)
from src.services.notification_queries import (
    list_notifications,
    mark_all_read,
    mark_notification_read,
)

PRIVATE = {"Cache-Control": "no-store"}
logger = logging.getLogger(__name__)


class PrivateNotificationRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                response = await handler(request)
            except HTTPException as error:
                error.headers = {**(error.headers or {}), **PRIVATE}
                raise
            except RequestValidationError:
                raise HTTPException(
                    422, "Invalid request payload.", headers=PRIVATE
                ) from None
            except (OSError, PoolTimeout):
                raise HTTPException(
                    503,
                    "Notification storage temporarily unavailable.",
                    headers=PRIVATE,
                ) from None
            except DBAPIError as error:
                code = getattr(error.orig, "sqlstate", None) or ""
                if (
                    error.connection_invalidated
                    or code.startswith("08")
                    or code in {"57P01", "57P02", "57P03", "53300"}
                ):
                    raise HTTPException(
                        503,
                        "Notification storage temporarily unavailable.",
                        headers=PRIVATE,
                    ) from None
                logger.error("Unexpected notification database error.")
                raise HTTPException(
                    500, "Could not complete notification operation.", headers=PRIVATE
                ) from None
            response.headers.update(PRIVATE)
            return response

        return handle


router = APIRouter(
    prefix="/api/v1/notifications",
    tags=["User notifications"],
    route_class=PrivateNotificationRoute,
    responses={
        401: {
            "model": NotificationError,
            "description": "Missing or invalid Bearer token",
        },
        422: {"model": NotificationError, "description": "Invalid input"},
        500: {"model": NotificationError, "description": "Unexpected storage error"},
        503: {
            "model": NotificationError,
            "description": "Notification storage temporarily unavailable",
        },
    },
)
User = Annotated[Principal, Depends(authenticated_user)]
Session = Annotated[AsyncSession, Depends(get_db)]
ReadBody = Annotated[MarkReadRequest | None, Body()]


@router.get("/me")
async def my_notifications(
    user: User,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=2147483647)] = 0,
) -> NotificationList:
    return await list_notifications(session, user.id, limit, offset)


@router.patch("/read-all")
async def read_all_notifications(
    user: User, session: Session, body: ReadBody = None
) -> MarkAllReadResult:
    return await mark_all_read(session, user.id)


@router.patch("/{notification_id}/read", responses={404: {"model": NotificationError}})
async def read_notification(
    notification_id: UUID,
    user: User,
    session: Session,
    body: ReadBody = None,
) -> MarkReadResult:
    result = await mark_notification_read(session, user.id, notification_id)
    if result is None:
        raise HTTPException(404, "Notification not found.")
    return result
