"""Your settings: one list, one table, one API.

Every setting in Folio is a row in `KEYS` below, with its default and a `validate` that returns the cleaned
value or raises ValueError(message). To add a setting: add its row here and its type to the app's `Settings`
(expense-tracker-client/lib/types.ts). Nothing else changes: values are stored as JSON text in
`user_settings` (models.UserSetting), one row per setting, so a new setting needs no new column.

A missing row means the default. A stored JSON null is a value: for a key that allows it, it means Off. So
"never set" and "turned off" stay apart.

Only preferences live here; a feature's own state belongs in the feature's own table.

API: `GET /api/v1/settings` gives every key with its value, in `KEYS` order. `PUT /api/v1/settings` with a JSON
object of the keys to change (partial) saves them and gives every key back. An unknown key or invalid value is
a 422 ("Unknown setting: <key>" or "<key>: <message>"), and then nothing is written.
"""
import json
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth import current_user, same_origin
from database import get_db
from models import User, UserSetting

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/settings", tags=["Settings"])

KEY_LENGTH = 64  # String(64)


@dataclass(frozen=True)
class Key:
    """A setting: its default and its validator (returns the cleaned value or raises ValueError(message))."""

    default: Any
    validate: Callable[[Any], Any]


class SettingError(ValueError):
    """An unknown key or an invalid value; str(error) is the 422 detail."""


def whole_number(low: int, high: int, *, nullable: bool, message: str, step: int = 1) -> Callable[[Any], Any]:
    """A JSON integer from `low` to `high` (a float with no fraction, like 5000.0, becomes 5000), or null when
    `nullable`. true/false, strings and fractions are rejected (Python's bool is an int). With `step`, only
    `low`, `low + step`, … are accepted (50, 55, … 95 for a step of 5)."""

    def validate(value: Any) -> Any:
        if value is None and nullable:
            return None
        if isinstance(value, bool):
            raise ValueError(message)
        if isinstance(value, float):
            if not value.is_integer():  # NaN and infinity aren't integers either
                raise ValueError(message)
            value = int(value)
        if not isinstance(value, int) or not low <= value <= high:
            raise ValueError(message)
        if (value - low) % step:
            raise ValueError(message)
        return value

    return validate


def boolean(*, message: str) -> Callable[[Any], Any]:
    """JSON true or false only."""

    def validate(value: Any) -> Any:
        if type(value) is not bool:
            raise ValueError(message)
        return value

    return validate


# Every setting, in the order GET lists them. Later designs add their rows here.
KEYS: dict[str, Key] = {
    # Whole rupees; a payment over this gets a notification (payment_alerts.py). null = Off.
    "alerts.large_amount": Key(None, whole_number(
        1, 10_000_000, nullable=True,
        message="Enter a whole number of rupees from 1 to 1,00,00,000, or turn it off.",
    )),
    # A payment at least 3x what you usually spend at the merchant gets a notification (payment_alerts.py).
    "alerts.unusual": Key(True, boolean(message="Choose on or off.")),
    # Warn once when a spend limit is this full (limits.py), 50–95 in steps of 5. null = Off.
    "limits.warn_percent": Key(80, whole_number(
        50, 95, step=5, nullable=True,
        message="Choose 50% to 95% in steps of 5, or Off.",
    )),
}


def _printable(key: str) -> str:
    """A key that's safe to echo in an error: at most 64 characters, and a lone surrogate (valid JSON, but it
    can't be encoded in the response) becomes "?"."""
    return key[:KEY_LENGTH].encode("utf-8", "replace").decode("utf-8")


def _clean(changes: Mapping[str, Any]) -> dict[str, Any]:
    """Every change validated, in the request's order; the first bad one raises SettingError."""
    cleaned = {}
    for key, value in changes.items():
        if key not in KEYS:
            raise SettingError(f"Unknown setting: {_printable(key)}")
        try:
            cleaned[key] = KEYS[key].validate(value)
        except ValueError as error:
            raise SettingError(f"{key}: {error}") from None
    return cleaned


def _stored(key: str, text: str) -> Any:
    """A stored value read back through its key's rules; the default if it no longer passes them (a key whose
    rules tightened later can never break a reader)."""
    try:
        return KEYS[key].validate(json.loads(text))
    except (ValueError, TypeError):
        logger.warning("Ignoring stored setting %s", key)
        return KEYS[key].default


async def _existing(db: AsyncSession, user_id: uuid.UUID, keys: list[str]) -> dict[str, UserSetting]:
    """The rows already stored for these keys, in one select."""
    rows = await db.scalars(select(UserSetting).where(UserSetting.user_id == user_id, UserSetting.key.in_(keys)))
    return {row.key: row for row in rows}


async def load(db: AsyncSession, user_id: uuid.UUID) -> dict[str, Any]:
    """Every setting, in `KEYS` order: the stored value, else the default. One select. Rows for keys no longer
    in `KEYS` are ignored."""
    rows = (await db.execute(select(UserSetting.key, UserSetting.value).where(UserSetting.user_id == user_id))).all()
    values = {key: spec.default for key, spec in KEYS.items()}
    for key, text in rows:
        if key in KEYS:
            values[key] = _stored(key, text)
    return values


async def get(db: AsyncSession, user_id: uuid.UUID, key: str) -> Any:
    """One setting, by the same rules as load(). KeyError for a key not in `KEYS`."""
    if key not in KEYS:
        raise KeyError(key)
    text = await db.scalar(select(UserSetting.value).where(UserSetting.user_id == user_id, UserSetting.key == key))
    return KEYS[key].default if text is None else _stored(key, text)


async def save(db: AsyncSession, user_id: uuid.UUID, changes: Mapping[str, Any]) -> dict[str, Any]:
    """Saves the given keys (partial) in one commit and returns every setting. Raises SettingError before
    writing anything if a key is unknown or a value invalid.

    There's no portable upsert across SQLite, Postgres and TiDB, so this reads the existing rows (one select),
    then updates or inserts. Two first saves racing on the same key: the loser's commit fails, and it runs once
    more, now updating the winner's row."""
    cleaned = _clean(changes)
    if cleaned:
        for attempt in range(2):
            rows = await _existing(db, user_id, list(cleaned))
            now = datetime.now(timezone.utc)
            for key, value in cleaned.items():
                text = json.dumps(value)
                row = rows.get(key)
                if row is None:
                    db.add(UserSetting(user_id=user_id, key=key, value=text, updated_at=now))
                else:
                    row.value, row.updated_at = text, now
            try:
                await db.commit()
                break
            except IntegrityError:
                await db.rollback()
                if attempt:
                    raise
    return await load(db, user_id)


# ---------- API ----------

@router.get("", response_model=dict[str, Any])
async def read_settings(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Every setting with its value (the default when never set), in a fixed order."""
    return await load(db, user.id)


@router.put("", response_model=dict[str, Any], dependencies=[Depends(same_origin)])
async def write_settings(changes: dict[str, Any] = Body(...), user: User = Depends(current_user),
                         db: AsyncSession = Depends(get_db)):
    """Changes only the keys given ({} changes nothing) and returns every setting. 422 names the first bad key:
    "Unknown setting: <key>" or "<key>: <message>"; nothing is written then."""
    user_id = user.id  # read now: a retried save rolls back, which expires the object
    try:
        return await save(db, user_id, changes)
    except SettingError as error:
        raise HTTPException(422, str(error))
