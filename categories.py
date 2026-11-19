"""Categories of your own ("Pets", "Gym"), next to the built-in list (parser.CATEGORIES).

A custom category has a name (1–40 characters: letters, digits, spaces and & - ' . , ( ) /, with at least one
letter or digit), an icon and a colour from fixed lists (the app draws them; ICONS / COLOURS match CUSTOM_ICONS /
PALETTE in expense-tracker-client/lib/categories.ts, and tests compare them). Up to 30. Names are unique without
case, and can't be a built-in name (Uncategorized and Self Transfer included), "All" (the overall limit) or
"Other" (the app's catch-all slice).

A custom category counts as spending (debits) or income (credits), like Transfer. It can be picked when adding or
editing a transaction, in category rules and for spend limits, and merchant memory remembers it, so the next SMS
from that merchant gets it (parsed_by "regex+user"). It is never sent to Gemini: the parser and its prompts only
know the built-ins.

Deleting one moves its payments to another category (`move_to`, default Uncategorized), moves its merchant memory
(forgotten when moving to Uncategorized), and deletes its limit, rollover and rules, in one commit.

`canonical(name, custom)` is the one place a typed category name is matched (main.py, rules.py, limits.py). This
module imports only parser, models, auth, database, dashboard and merchants: limits imports it, and rules imports
limits, so importing either here would be a cycle.
"""
import unicodedata
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from sqlalchemy import case, delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import merchants
from auth import current_user, same_origin
from dashboard import ownership
from database import get_db
from models import CategoryLimit, CategoryRule, CustomCategory, LimitRollover, MerchantCategory, Transaction, User
from parser import CATEGORIES, UNCATEGORIZED

router = APIRouter(prefix="/api/v1/categories", tags=["Categories"])

MAX_CUSTOM = 30
RULE_SET, PICKED = "regex+rule", "regex+user"  # parsed_by: a category rule set the category / you picked it
MAX_NAME = 40  # String(40)
# Matches CUSTOM_ICONS and PALETTE in expense-tracker-client/lib/categories.ts; tests compare them.
ICONS = ("PawPrint", "Dumbbell", "Gift", "Baby", "House", "Plane", "Coffee", "Shirt", "Wrench", "BookOpen", "Tag",
         "Sparkles")
COLOURS = ("violet", "blue", "green", "orange", "pink", "amber", "teal", "red", "slate", "gray")
RESERVED = frozenset({c.casefold() for c in CATEGORIES} | {"all", "other"})
PUNCTUATION = frozenset(" &-'.,()/")
CHARACTERS_MESSAGE = "Use letters, digits, spaces and & - ' . , ( ) /."


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _key(name: str) -> str:
    return " ".join(unicodedata.normalize("NFC", name).split()).casefold()


def clean_name(value: str) -> str:
    """The name as stored: NFC, spaces collapsed. Raises ValueError with the message to show."""
    if merchants.has_invalid_characters(value):
        raise ValueError(CHARACTERS_MESSAGE)
    name = " ".join(unicodedata.normalize("NFC", value).split())
    if not name:
        raise ValueError("Enter a name.")
    if len(name) > MAX_NAME or len(name.casefold()) > MAX_NAME:
        raise ValueError(f"Names can be up to {MAX_NAME} characters.")
    if not all(unicodedata.category(c)[0] in "LMN" or c in PUNCTUATION for c in name):
        raise ValueError(CHARACTERS_MESSAGE)
    if not any(unicodedata.category(c)[0] in "LN" for c in name):
        raise ValueError(CHARACTERS_MESSAGE)  # "&&" isn't a name
    return name


async def names_for(db: AsyncSession, user: User) -> list[str]:
    """Your custom category names, oldest first."""
    return list(await db.scalars(select(CustomCategory.name).where(CustomCategory.user_id == user.id)
                                 .order_by(CustomCategory.created_at, CustomCategory.id)))


def canonical(name: Optional[str], custom: Sequence[str] = ()) -> Optional[str]:
    """The category's stored spelling, matched without case: a built-in (Uncategorized included), else one of
    `custom`; None for anything else."""
    wanted = _key(name or "")
    if not wanted:
        return None
    return next((c for c in [*CATEGORIES, *custom] if _key(c) == wanted), None)


# ---------- API ----------

class _Style(BaseModel):
    icon: str
    colour: str

    @model_validator(mode="before")
    @classmethod
    def _mark_invalid_characters(cls, data):
        # A 422 echoes the value it rejects, and a lone surrogate couldn't be encoded; so every text field gets
        # U+FFFD in its place first, as limits._encodable does (not imported: limits imports this module).
        if not isinstance(data, dict):
            return data
        return {key: merchants.mark_invalid_characters(value) if isinstance(value, str) else value
                for key, value in data.items()}

    @field_validator("icon")
    @classmethod
    def _icon(cls, value: str) -> str:
        if value not in ICONS:
            raise ValueError("Pick an icon from the list.")
        return value

    @field_validator("colour")
    @classmethod
    def _colour(cls, value: str) -> str:
        if value not in COLOURS:
            raise ValueError("Pick a colour from the list.")
        return value


class CustomCategoryIn(_Style):
    name: str

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return clean_name(value)


class CustomCategoryStyle(_Style):
    """PUT: only the icon and colour change; the name stays (payments, rules and limits use it)."""


class CustomCategoryOut(BaseModel):
    id: uuid.UUID
    name: str
    icon: str
    colour: str

    model_config = ConfigDict(from_attributes=True)


class CategoriesOut(BaseModel):
    built_in: list[str]  # parser.CATEGORIES, Uncategorized included
    custom: list[CustomCategoryOut]  # oldest first


class Moved(BaseModel):
    moved: int  # payments moved to the other category


async def _owned(db: AsyncSession, user: User, category_id: uuid.UUID) -> CustomCategory:
    row = await db.scalar(select(CustomCategory).where(CustomCategory.id == category_id,
                                                       CustomCategory.user_id == user.id))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Category not found.")
    return row


@router.get("", response_model=CategoriesOut)
async def list_categories(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = await db.scalars(select(CustomCategory).where(CustomCategory.user_id == user.id)
                            .order_by(CustomCategory.created_at, CustomCategory.id))
    return CategoriesOut(built_in=list(CATEGORIES), custom=[CustomCategoryOut.model_validate(row) for row in rows])


@router.post("", response_model=CustomCategoryOut, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(same_origin)])
async def create_category(body: CustomCategoryIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """409 for a name that's taken (built-in, "All", "Other" or yours, in any case) or a 31st category."""
    key = body.name.casefold()
    exists = "A category with this name already exists."
    if key in RESERVED:
        raise HTTPException(status.HTTP_409_CONFLICT, exists)
    names = await names_for(db, user)
    if any(name.casefold() == key for name in names):
        raise HTTPException(status.HTTP_409_CONFLICT, exists)
    if len(names) >= MAX_CUSTOM:
        raise HTTPException(status.HTTP_409_CONFLICT, f"You can have up to {MAX_CUSTOM} categories.")
    row = CustomCategory(user_id=user.id, name=body.name, name_key=key, icon=body.icon, colour=body.colour,
                         created_at=utc_now())
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()  # the same name saved twice at once
        raise HTTPException(status.HTTP_409_CONFLICT, exists)
    return CustomCategoryOut.model_validate(row)


@router.put("/{category_id}", response_model=CustomCategoryOut, dependencies=[Depends(same_origin)])
async def update_category(category_id: uuid.UUID, body: CustomCategoryStyle, user: User = Depends(current_user),
                          db: AsyncSession = Depends(get_db)):
    row = await _owned(db, user, category_id)
    row.icon, row.colour = body.icon, body.colour
    out = CustomCategoryOut.model_validate(row)
    await db.commit()
    return out


@router.delete("/{category_id}", response_model=Moved, dependencies=[Depends(same_origin)])
async def delete_category(category_id: uuid.UUID,
                          move_to: str = Query(UNCATEGORIZED, description="Where its payments go; default Uncategorized"),
                          user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Deletes the category: its payments move to `move_to` (their updated_at moves, so the app's poll sees
    them), its merchant memory moves too (or is forgotten for Uncategorized), and its limit, rollover and rules
    go. One commit."""
    row = await _owned(db, user, category_id)
    name, user_id = row.name, user.id
    others = [other for other in await names_for(db, user) if other != name]
    target = canonical(move_to, others)
    if target is None or _key(target) == _key(name):
        raise HTTPException(422, "Pick another category to move its payments to.")
    now = utc_now()
    # Its rules go below, so a payment one of them set is now where you moved it ("regex+rule" becomes
    # "regex+user", as on an edit): the sheet stops pointing at a rule that no longer exists.
    picked = case((Transaction.parsed_by == RULE_SET, PICKED), else_=Transaction.parsed_by)
    moved = await db.execute(
        update(Transaction).where(ownership(user), Transaction.category == name)
        .values(category=target, parsed_by=picked, updated_at=now).execution_options(synchronize_session=False))
    memory = (MerchantCategory.user_id == user_id, MerchantCategory.category == name)
    if target == UNCATEGORIZED:
        await db.execute(delete(MerchantCategory).where(*memory))  # Uncategorized means "forget it"
    else:
        await db.execute(update(MerchantCategory).where(*memory).values(category=target, updated_at=now)
                         .execution_options(synchronize_session=False))
    for model in (CategoryLimit, LimitRollover, CategoryRule):
        await db.execute(delete(model).where(model.user_id == user_id, model.category == name))
    await db.delete(row)
    await db.commit()
    return Moved(moved=moved.rowcount or 0)
