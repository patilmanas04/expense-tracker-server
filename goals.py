"""Savings goals: something you're saving toward, with a target, an optional target month and a tag.

The server only stores goals. Progress is worked out in the app (lib/goals.ts): "Saved so far" (`manual_saved`) plus
the counted amount of every parsed INR payment whose note carries the goal's #tag, so moving money to a savings
account (category Self Transfer, not spending) and tagging it counts toward the goal.

A tag is normalised like lib/tags.ts tagsOf: trimmed, one leading "#" dropped, NFC, lower case; 1 to 30 letters
(any language, with their combining marks), digits, "_" and "-", with at least one letter. Only one active goal
can use a tag (409, checked under a lock so a double submit can't make two); archived goals keep theirs.

**Cost**: GET is one query; a write at most 4 statements (the goal, the lock and the tag check, the save).
"""
import re
import unicodedata
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import merchants
from auth import current_user, same_origin
from database import get_db
from limits import _encodable, _utc
from models import SavingsGoal, User

router = APIRouter(prefix="/api/v1/goals", tags=["Goals"])

TAG_MAX = 30  # code points after the "#", as lib/tags.ts TAG_MAX
MAX_NAME = 60  # String(60)
MAX_AMOUNT = Decimal("9999999999.99")  # Numeric(12, 2)
CENTS = Decimal("0.01")
MONTH = re.compile(r"\d{4}-(0[1-9]|1[0-2])")


def _tag_character(c: str) -> bool:
    return unicodedata.category(c)[0] in "LMN" or c in "_-"


def clean_tag(value: str) -> str:
    """The tag as stored and matched: "#Goa" → "goa". Raises ValueError with the message to show."""
    tag = value.strip()
    if tag.startswith("#"):
        tag = tag[1:]
    tag = unicodedata.normalize("NFC", tag)
    if not tag:
        raise ValueError("Enter a tag.")
    if not all(_tag_character(c) for c in tag):
        raise ValueError("Tags use letters, digits, _ and -.")
    lower = tag.lower()
    # Both: lib/tags.ts counts before lower-casing, and the stored tag (String(30)) is the lower-cased one, which can
    # be longer ("İ" → "i̇", two code points).
    if len(tag) > TAG_MAX or len(lower) > TAG_MAX:
        raise ValueError(f"Tags can be up to {TAG_MAX} characters.")
    if not any(unicodedata.category(c).startswith("L") for c in tag):
        raise ValueError("A tag needs at least one letter.")
    return lower


def _money(value, message: str, *, allow_zero: bool) -> Decimal:
    """A rupee amount with at most 2 decimals, as a number or a decimal string; one message for every bad one."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(message)
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError(message)
    too_small = amount < 0 if allow_zero else amount <= 0
    if not amount.is_finite() or too_small or amount > MAX_AMOUNT or amount != amount.quantize(CENTS):
        raise ValueError(message)
    return amount.quantize(CENTS)


class GoalIn(BaseModel):
    """POST and PUT; PUT replaces every field (one left out takes its default)."""

    name: str
    target: Decimal = Field(description="More than zero")
    target_month: Optional[str] = Field(default=None, description='"YYYY-MM", or null for no target month')
    tag: str = Field(description='"#phone" or "phone"; stored normalised')
    manual_saved: Decimal = Field(default=Decimal(0), description="Saved so far, added by hand; zero or more")
    archived: bool = False

    @model_validator(mode="before")
    @classmethod
    def _mark_invalid_characters(cls, data):
        return _encodable(data)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if merchants.has_invalid_characters(value):
            raise ValueError("Names can't contain invalid characters.")
        value = " ".join(value.split())
        if not value:
            raise ValueError("Enter a name.")
        if len(value) > MAX_NAME:
            raise ValueError(f"Names can be up to {MAX_NAME} characters.")
        return value

    @field_validator("target", mode="before")
    @classmethod
    def _target(cls, value) -> Decimal:
        return _money(value, "Enter a target above zero.", allow_zero=False)

    @field_validator("manual_saved", mode="before")
    @classmethod
    def _manual_saved(cls, value) -> Decimal:
        return _money(value, "Saved so far can't be below zero.", allow_zero=True)

    @field_validator("target_month")
    @classmethod
    def _target_month(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not MONTH.fullmatch(value):
            raise ValueError("Pick a month like 2027-03.")
        return value

    @field_validator("tag")
    @classmethod
    def _tag(cls, value: str) -> str:
        if merchants.has_invalid_characters(value):
            raise ValueError("Tags use letters, digits, _ and -.")
        return clean_tag(value)


class GoalOut(BaseModel):
    id: uuid.UUID
    name: str
    target: float
    target_month: Optional[str]
    tag: str
    manual_saved: float
    archived: bool
    created_at: datetime  # aware UTC

    model_config = ConfigDict(from_attributes=True)

    @field_validator("created_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _utc(value)


async def _goal(db: AsyncSession, user: User, goal_id: uuid.UUID) -> SavingsGoal:
    goal = await db.scalar(select(SavingsGoal).where(SavingsGoal.id == goal_id, SavingsGoal.user_id == user.id))
    if goal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Goal not found.")
    return goal


async def _check_tag(db: AsyncSession, user: User, body: GoalIn, except_id: Optional[uuid.UUID] = None) -> None:
    """409 when this goal is (or stays) active and another active goal uses its tag.

    Two saves at once (a double submit) would both pass a plain check, so it runs under a lock on your users row,
    held to the commit: the second waits, then sees the first's goal. The UPDATE changes nothing (it sets updated_at
    to itself, so onupdate doesn't fire) but takes the row lock on Postgres and TiDB and the write lock on SQLite;
    the check is FOR UPDATE so TiDB and MySQL read the current rows rather than the transaction's snapshot."""
    if body.archived:
        return
    await db.execute(update(User).where(User.id == user.id).values(updated_at=User.updated_at)
                     .execution_options(synchronize_session=False))
    query = select(SavingsGoal.id).where(SavingsGoal.user_id == user.id, SavingsGoal.tag == body.tag,
                                         SavingsGoal.archived.is_(False)).with_for_update()
    if except_id is not None:
        query = query.where(SavingsGoal.id != except_id)
    if await db.scalar(query.limit(1)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Another goal uses #{body.tag}.")


@router.get("", response_model=list[GoalOut])
async def list_goals(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Your goals: active ones oldest first, then archived ones oldest first."""
    return list(await db.scalars(select(SavingsGoal).where(SavingsGoal.user_id == user.id)
                                 .order_by(SavingsGoal.archived, SavingsGoal.created_at, SavingsGoal.id)))


@router.post("", response_model=GoalOut, status_code=status.HTTP_201_CREATED, dependencies=[Depends(same_origin)])
async def create_goal(body: GoalIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await _check_tag(db, user, body)
    goal = SavingsGoal(user_id=user.id, **body.model_dump())
    db.add(goal)
    await db.commit()
    await db.refresh(goal)
    return goal


@router.put("/{goal_id}", response_model=GoalOut, dependencies=[Depends(same_origin)])
async def update_goal(goal_id: uuid.UUID, body: GoalIn, user: User = Depends(current_user),
                      db: AsyncSession = Depends(get_db)):
    """Replaces every field: archive, unarchive, rename, change the target or "Saved so far"."""
    goal = await _goal(db, user, goal_id)
    await _check_tag(db, user, body, goal.id)
    for field, value in body.model_dump().items():
        setattr(goal, field, value)
    await db.commit()
    await db.refresh(goal)
    return goal


@router.delete("/{goal_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(same_origin)])
async def delete_goal(goal_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Deletes the goal. Tagged payments keep their tags."""
    goal = await _goal(db, user, goal_id)
    await db.delete(goal)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
