"""In-app notifications, shown under the bell on the dashboard's Home screen.

Swiping one away or clearing them all hides them (`notification_dismissals`) rather than deleting
them: each notification is also the record that its alert went out, which stops repeats.

Some notifications carry buttons; see register_actions.
"""
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Awaitable, Callable, NamedTuple, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth import current_user, same_origin
from database import get_db
from models import Notification, NotificationDismissal, User

router = APIRouter(prefix="/api/v1/notifications", tags=["Notifications"])
logger = logging.getLogger(__name__)


class NotificationAction(BaseModel):
    label: str
    path: str


class Resolution(NamedTuple):
    actions: list[NotificationAction]
    resolved: Optional[str]


ActionResolver = Callable[[AsyncSession, list[Notification]], Awaitable[dict[uuid.UUID, Resolution]]]
_RESOLVERS: dict[str, ActionResolver] = {}


def register_actions(kind: str, resolver: ActionResolver) -> None:
    """For notifications with buttons. A feature registers its kind once, at import. The inbox calls the
    resolver once per request with every listed notification of that kind and gets back, per
    notification id, its buttons (each a POST path under /api/v1/) or, once decided, the text shown in
    their place. A notification it leaves out gets neither."""
    _RESOLVERS[kind] = resolver


def _utc(value: datetime) -> datetime:
    # SQLite drops the offset; values are always stored in UTC.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


async def create_once(db: AsyncSession, user_id: uuid.UUID, *, kind: str, title: str, body: str,
                      link: Optional[str], dedupe_key: str) -> bool:
    """Adds a notification unless one with the same key exists. Returns True if created."""
    exists = await db.scalar(select(Notification.id).where(
        Notification.user_id == user_id, Notification.dedupe_key == dedupe_key
    ))
    if exists:
        return False
    db.add(Notification(user_id=user_id, kind=kind, title=title, body=body, link=link, dedupe_key=dedupe_key,
                        created_at=datetime.now(timezone.utc)))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()  # a simultaneous SMS created it first
        return False
    return True


def _visible(user_id: uuid.UUID):
    """Yours and not swiped away or cleared."""
    return (
        Notification.user_id == user_id,
        ~exists().where(NotificationDismissal.notification_id == Notification.id),
    )


async def _unread(db: AsyncSession, user_id: uuid.UUID) -> int:
    return await db.scalar(select(func.count()).select_from(Notification).where(
        *_visible(user_id), Notification.read_at.is_(None)
    ))


class NotificationOut(BaseModel):
    id: uuid.UUID
    kind: str
    title: str
    body: str
    link: Optional[str]
    created_at: datetime
    read: bool
    actions: list[NotificationAction] = []
    resolved: Optional[str] = None


class Inbox(BaseModel):
    unread: int
    items: list[NotificationOut]


class MarkRead(BaseModel):
    ids: Optional[list[uuid.UUID]] = Field(default=None, max_length=200, description="Omit to mark everything read")


@router.get("", response_model=Inbox)
async def list_notifications(limit: int = Query(30, ge=1, le=100), user: User = Depends(current_user),
                             db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(
        select(Notification).where(*_visible(user.id))
        .order_by(Notification.created_at.desc()).limit(limit)
    )).all()
    unread = await _unread(db, user.id)
    # Built before the resolvers run: a resolver that fails rolls back, which expires these rows.
    items = [
        NotificationOut(id=n.id, kind=n.kind, title=n.title, body=n.body, link=n.link,
                        created_at=_utc(n.created_at), read=n.read_at is not None)
        for n in rows
    ]
    resolutions = await _resolve(db, rows)
    for item in items:
        resolution = resolutions.get(item.id)
        if resolution is not None:
            item.actions = list(resolution.actions)
            item.resolved = resolution.resolved
    return Inbox(unread=unread, items=items)


async def _resolve(db: AsyncSession, rows: list[Notification]) -> dict[uuid.UUID, Resolution]:
    """Buttons or resolved text per notification, one resolver call per kind. A resolver that fails
    is logged and its notifications show neither, so a broken feature never breaks the bell."""
    by_kind: dict[str, list[Notification]] = defaultdict(list)
    for row in rows:
        if row.kind in _RESOLVERS:
            by_kind[row.kind].append(row)
    resolutions: dict[uuid.UUID, Resolution] = {}
    rolled_back = False
    for kind, kind_rows in by_kind.items():
        try:
            if rolled_back:  # the rollback expired these rows; reload them for the next resolver
                for row in kind_rows:
                    await db.refresh(row)
            resolutions.update(await _RESOLVERS[kind](db, kind_rows))
        except Exception:
            logger.exception("Notification actions for %s failed", kind)
            await db.rollback()
            rolled_back = True
    return resolutions


@router.post("/read", dependencies=[Depends(same_origin)])
async def mark_read(body: MarkRead, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    statement = update(Notification).where(Notification.user_id == user.id, Notification.read_at.is_(None))
    if body.ids is not None:
        statement = statement.where(Notification.id.in_(body.ids))
    await db.execute(statement.values(read_at=datetime.now(timezone.utc)))
    await db.commit()
    return {"unread": await _unread(db, user.id)}


async def _dismiss(db: AsyncSession, ids: list[uuid.UUID]) -> None:
    if ids:
        db.add_all(NotificationDismissal(notification_id=i) for i in ids)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()  # another tap already hid it


@router.delete("/{notification_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(same_origin)])
async def dismiss_notification(notification_id: uuid.UUID, user: User = Depends(current_user),
                               db: AsyncSession = Depends(get_db)):
    """Swipe to delete: hides one notification."""
    if not await db.scalar(select(Notification.id).where(Notification.id == notification_id, Notification.user_id == user.id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found.")
    if not await db.get(NotificationDismissal, notification_id):
        await _dismiss(db, [notification_id])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(same_origin)])
async def clear_notifications(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Clear: hides every notification you can see."""
    await _dismiss(db, list((await db.scalars(select(Notification.id).where(*_visible(user.id)))).all()))
    return Response(status_code=status.HTTP_204_NO_CONTENT)

