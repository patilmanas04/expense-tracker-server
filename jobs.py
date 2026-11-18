"""Daily jobs: work that runs once a day at 21:00 IST, whether or not an SMS arrives.

A job is due when it hasn't run since the most recent 21:00 IST (`slot`). The same rule serves three callers:
the scheduler inside the server process (started by main.lifespan), its start-up catch-up (CATCH_UP_DELAY
seconds after start, so an SMS that woke a sleeping host is saved before a job looks for silence), and the
external trigger `POST /api/v1/jobs/daily` for a cron service when the host sleeps (header X-Jobs-Secret =
env JOBS_SECRET; the webhook secret is never accepted). `force=true` skips the "due" test.

Before a job runs, the caller claims the slot in `job_runs` (a conditional update, or the first insert), so a
job runs at most once per slot even with several workers or a cron added later. A job that fails after its
claim waits for the next slot.

Jobs are registered in one block in main.py, run in that order, each in its own session with the owner and
today's IST date. This module never imports main (main imports it). Tests patch `now` and `_sleep`.
"""
import asyncio
import logging
import os
import secrets
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth import OWNER_EMAIL
from database import AsyncSessionLocal
from models import JobRun, User

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
RUN_AT = time(21, 0)  # 21:00 IST, every day
CATCH_UP_DELAY = 120  # seconds after start-up before the catch-up run
NAME_LENGTH = 64  # JobRun.name

Job = Callable[[AsyncSession, User, date], Awaitable[None]]
_JOBS: list[tuple[str, Job]] = []
_sleep = asyncio.sleep  # patched in tests


def register(name: str, job: Job) -> None:
    """Adds a daily job, run after the ones registered before it. ValueError for a repeated name or one longer
    than 64 characters."""
    if len(name) > NAME_LENGTH:
        raise ValueError(f"Job name longer than {NAME_LENGTH} characters: {name[:NAME_LENGTH]}…")
    if any(existing == name for existing, _ in _JOBS):
        raise ValueError(f"Job already registered: {name}")
    _JOBS.append((name, job))


def now() -> datetime:
    """The current UTC time (tests patch this)."""
    return datetime.now(timezone.utc)


def slot(moment: datetime) -> datetime:
    """The most recent 21:00 IST at or before `moment`, as an aware UTC datetime."""
    local = moment.astimezone(IST)
    run = datetime.combine(local.date(), RUN_AT, IST)
    if local < run:
        run = datetime.combine(local.date() - timedelta(days=1), RUN_AT, IST)
    return run.astimezone(timezone.utc)


def _utc(value: datetime) -> datetime:
    # SQLite drops the offset; values are always stored in UTC (as notifications._utc).
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


async def _owner(db: AsyncSession) -> Optional[User]:
    """The dashboard owner (DASHBOARD_OWNER_EMAIL), as post_save.load finds it; None if unset or not signed up."""
    if not OWNER_EMAIL:
        return None
    return await db.scalar(select(User).where(User.email == OWNER_EMAIL))


async def _exists(db: AsyncSession, name: str) -> bool:
    # Its own helper so a test can force the insert race.
    return await db.scalar(select(JobRun.name).where(JobRun.name == name)) is not None


async def _claim(db: AsyncSession, name: str, moment: datetime, due_after: Optional[datetime]) -> bool:
    """Marks `name` as run at `moment` if it's due (last_run < due_after; None = forced). True if this caller
    won the claim. Commits. Both datetimes must be UTC-aware: SQLite compares them as text."""
    statement = update(JobRun).where(JobRun.name == name)
    if due_after is not None:
        statement = statement.where(JobRun.last_run < due_after)
    result = await db.execute(statement.values(last_run=moment))
    await db.commit()
    if result.rowcount == 1:
        return True
    if await _exists(db, name):  # ran already this slot (or another worker just did)
        return False
    db.add(JobRun(name=name, last_run=moment))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()  # another worker inserted it first
        return False
    return True


async def run_due(force: bool = False) -> list[str]:
    """Runs every registered job that's due (all of them when `force`), each in its own session, and returns
    the names that completed. No owner → [] and nothing is claimed. A job's exception is logged, never raised."""
    moment = now().astimezone(timezone.utc)
    due_after = None if force else slot(moment)
    today = moment.astimezone(IST).date()
    async with AsyncSessionLocal() as db:
        owner = await _owner(db)
        if owner is None:
            return []
        db.expunge(owner)  # detached: survives a job's rollback
    ran = []
    for name, job in list(_JOBS):
        try:
            async with AsyncSessionLocal() as db:
                if not await _claim(db, name, moment, due_after):
                    continue
                await job(db, owner, today)
            ran.append(name)
        except Exception:
            logger.exception("Daily job %s failed", name)
    return ran


async def scheduler() -> None:
    """Started by main.lifespan; cancelled on shutdown. Waits CATCH_UP_DELAY, runs what's due (the catch-up),
    then sleeps until each next 21:00 IST and runs again. Never raises (except CancelledError)."""
    await _sleep(CATCH_UP_DELAY)
    while True:
        try:
            await run_due()
        except Exception:
            logger.exception("Daily jobs failed")
        moment = now()
        # One extra second so an early timer wake-up never lands just before 21:00 and finds nothing due.
        await _sleep((slot(moment) + timedelta(days=1) - moment).total_seconds() + 1)


# ---------- API ----------

router = APIRouter(prefix="/api/v1/jobs", tags=["Jobs"])


class JobsRun(BaseModel):
    ran: list[str]  # the jobs that completed in this call


@router.post("/daily", response_model=JobsRun)
async def run_daily(force: bool = Query(False, description="Run every job even if it already ran since 21:00 IST"),
                    x_jobs_secret: Optional[str] = Header(None, alias="X-Jobs-Secret")):
    """For a cron service or the host's cron when the server sleeps: runs the daily jobs that are due. Needs
    X-Jobs-Secret = JOBS_SECRET (never the webhook secret). Server to server, so no session and no Origin check."""
    secret = os.getenv("JOBS_SECRET", "").strip()
    if not secret:
        raise HTTPException(503, "JOBS_SECRET is not configured.")
    given = (x_jobs_secret or "").strip().encode()
    webhook = os.getenv("WEBHOOK_SECRET", "").strip().encode()
    # The webhook secret is refused even if JOBS_SECRET was set to the same value: a cron service must never
    # hold the secret that can post bank SMS.
    if not given or not secrets.compare_digest(given, secret.encode()) or (
            webhook and secrets.compare_digest(given, webhook)):
        raise HTTPException(401, "Invalid or missing jobs secret.")
    return JobsRun(ran=await run_due(force=force))
