"""In-app notifications, shown under the bell on the dashboard's Home screen."""
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth import current_user, same_origin
from database import get_db
from models import Notification, User

router = APIRouter(prefix="/api/v1/notifications", tags=["Notifications"])


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


async def _unread(db: AsyncSession, user_id: uuid.UUID) -> int:
    return await db.scalar(select(func.count()).select_from(Notification).where(
        Notification.user_id == user_id, Notification.read_at.is_(None)
    ))


class NotificationOut(BaseModel):
    id: uuid.UUID
    kind: str
    title: str
    body: str
    link: Optional[str]
    created_at: datetime
    read: bool


class Inbox(BaseModel):
    unread: int
    items: list[NotificationOut]


class MarkRead(BaseModel):
    ids: Optional[list[uuid.UUID]] = Field(default=None, max_length=200, description="Omit to mark everything read")


@router.get("", response_model=Inbox)
async def list_notifications(limit: int = Query(30, ge=1, le=100), user: User = Depends(current_user),
                             db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(
        select(Notification).where(Notification.user_id == user.id)
        .order_by(Notification.created_at.desc()).limit(limit)
    )).all()
    return Inbox(unread=await _unread(db, user.id), items=[
        NotificationOut(id=n.id, kind=n.kind, title=n.title, body=n.body, link=n.link,
                        created_at=_utc(n.created_at), read=n.read_at is not None)
        for n in rows
    ])


@router.post("/read", dependencies=[Depends(same_origin)])
async def mark_read(body: MarkRead, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    statement = update(Notification).where(Notification.user_id == user.id, Notification.read_at.is_(None))
    if body.ids is not None:
        statement = statement.where(Notification.id.in_(body.ids))
    await db.execute(statement.values(read_at=datetime.now(timezone.utc)))
    await db.commit()
    return {"unread": await _unread(db, user.id)}
