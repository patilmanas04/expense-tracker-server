"""SMS watchdog: a daily job (registered in main.py as "watchdog", run by jobs.py at 21:00 IST) that warns when the
SMS pipeline looks broken, since every number in Folio depends on the iPhone Shortcut forwarding bank SMS.

"Received" SMS are stored rows not added by hand (parsed_by != "manual"), readable or not, held-out possible
duplicates and edited rows included, yours or unassigned. Ignored SMS (OTPs, offers, exact repeats) are never
stored, so they don't count: a working Shortcut looks quiet only after days with no stored bank SMS at all.

The checks (settings watchdog.*; nothing runs when watchdog.enabled is off):
1. Quiet pipeline: no SMS received for more than watchdog.quiet_days days.
2. Quiet bank (skipped while the pipeline is quiet): a bank (parser.detect_bank) with at least 3 SMS in the last
   60 days, none for longer than its own usual silence, while another bank has sent one within quiet_days. Its
   usual silence is the larger of quiet_days and twice its longest gap between SMS in those 60 days, so a
   low-traffic account (salary on the 31st, two SIPs on the 10th) isn't reported quiet between its SMS.
3. Salary late (watchdog.salary_check): with at least 2 salary days in the last 120 days, today is more than 3
   days past the expected payday (pay_periods.usual_offset / expected_after).
4. Unreadable: 5 or more unparsed SMS received in the last 24 hours.

Each warning is a bell notification (kind "watchdog"), once per episode: its dedupe key names the episode (the
newest SMS's IST date, the expected payday, today), so a new SMS or salary clears it and a later silence warns
again. A newly created warning is also emailed when watchdog.email is on and Gmail is configured; a failed email
adds "Email alerts aren't being sent" once a day.

Statements: settings 1, newest SMS 1, banks 1 (skipped while the pipeline is quiet), salary 1, unreadable 1 = 5
when nothing fires; 1 when off; create_once adds 2 per new warning.
"""
import html
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import jobs
import limits
import mailer
import pay_periods
import settings_store
from dashboard import ownership
from duplicates import MANUAL
from models import Transaction, User
from notifications import create_once
from parser import detect_bank

logger = logging.getLogger(__name__)

KIND = "watchdog"
LINK = "/settings#alerts"  # the Alerts section, with the Shortcut help
REVIEW_LINK = "/activity?kind=review"  # Activity → Needs review, where unreadable SMS are
BANK_WINDOW = timedelta(days=60)
BANK_MIN_SMS = 3
BANK_GAP_FACTOR = 2  # a bank is quiet after twice its longest gap between SMS (never less than quiet_days)
SALARY_WINDOW = timedelta(days=120)
SALARY_MIN = 2
LATE_AFTER = timedelta(days=3)
UNREADABLE_WINDOW = timedelta(hours=24)
UNREADABLE_MIN = 5

Settings = dict[str, Any]


def _received(owner: User):
    """SMS rows that reached Folio: yours or unassigned, not added by hand (readable or not)."""
    return ownership(owner), Transaction.parsed_by != MANUAL


def _utc(value: datetime) -> datetime:
    # SQLite drops the offset; values are always stored in UTC (as notifications._utc).
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _ist_day(moment: datetime) -> date:
    return moment.astimezone(pay_periods.IST).date()


def _day(day: date) -> str:
    """ "30 Nov"."""
    return f"{day.day} {day:%b}"


async def _warn(db: AsyncSession, owner: User, settings: Settings, today: date, *, key: str, title: str, body: str,
                link: str = LINK) -> None:
    """The bell notification once per key; emailed too when it's new, watchdog.email is on and Gmail is set up."""
    created = await create_once(db, owner.id, kind=KIND, title=title, body=body, link=link, dedupe_key=key)
    if not (created and settings["watchdog.email"] and mailer.configured()):
        return
    try:
        await mailer.send_email(mailer.recipient(), title, f"{body}\n\nOpen Folio: Settings → Alerts.",
                                limits._html_email(html.escape(title), f"<p>{html.escape(body)}</p>"))
    except Exception as error:  # noqa: BLE001 - told in the bell instead
        logger.warning("Watchdog email failed: %s", error)
        await create_once(db, owner.id, kind=KIND, dedupe_key=f"watchdog:email-failed:{today.isoformat()}",
                          title="Email alerts aren't being sent",
                          body="Gmail refused the last email. Run scripts/gmail_authorize.py again (see the server README).",
                          link=LINK)


def _usual_silence(seen: list[datetime], quiet: timedelta) -> timedelta:
    """How long a bank may be silent before it counts as quiet: quiet_days, or twice its longest gap between
    consecutive SMS (`seen`, sorted) when that is longer."""
    longest = max(later - earlier for earlier, later in zip(seen, seen[1:]))
    return max(quiet, BANK_GAP_FACTOR * longest)


async def _quiet(db: AsyncSession, owner: User, settings: Settings, today: date, moment: datetime,
                 quiet: timedelta) -> None:
    """Quiet pipeline, then (only while SMS still arrive) quiet banks."""
    newest = await db.scalar(select(func.max(Transaction.created_at)).where(*_received(owner)))
    if newest is None:
        return  # no SMS ever: nothing to compare
    newest = _utc(newest)
    if moment - newest > quiet:
        last = _ist_day(newest)
        await _warn(db, owner, settings, today, key=f"watchdog:quiet:{last.isoformat()}",
                    title=f"No bank SMS for {(moment - newest).days} days — check your iPhone Shortcut",
                    body=f"The last bank SMS reached Folio on {_day(last)}. On your iPhone, open Shortcuts → "
                         "Automation and check that the bank-SMS automation is still on and runs without asking.")
        return

    rows = (await db.execute(
        select(Transaction.raw_message, Transaction.sender, Transaction.created_at)
        .where(*_received(owner), Transaction.created_at >= moment - BANK_WINDOW)
    )).all()
    times: dict[str, list[datetime]] = defaultdict(list)
    for raw_message, sender, created_at in rows:
        bank = detect_bank(raw_message or "", sender)
        if bank is not None:
            times[bank].append(_utc(created_at))
    active = {bank for bank, seen in times.items() if moment - max(seen) <= quiet}
    for bank in sorted(times):
        seen = sorted(times[bank])
        others = active - {bank}
        if len(seen) < BANK_MIN_SMS or moment - seen[-1] <= _usual_silence(seen, quiet) or not others:
            continue
        newest_bank = seen[-1]
        last = _ist_day(newest_bank)
        await _warn(db, owner, settings, today, key=f"watchdog:quiet:{bank}:{last.isoformat()}",
                    title=f"No {bank} SMS for {(moment - newest_bank).days} days",
                    body=f"{' and '.join(sorted(others))} SMS still arrive, but the last {bank} SMS reached Folio on "
                         f"{_day(last)}. Check that your iPhone automation still forwards {bank}'s messages.")


async def _salary(db: AsyncSession, owner: User, settings: Settings, today: date, moment: datetime,
                  quiet: timedelta) -> None:
    """Salary more than 3 days past its usual date (needs 2 salary days in the last 120)."""
    if not settings["watchdog.salary_check"]:
        return
    days = await pay_periods.salary_days(db, owner)
    if sum(day >= today - SALARY_WINDOW for day in days) < SALARY_MIN:
        return
    expected = pay_periods.expected_after(days, pay_periods.usual_offset(days))
    if today <= expected + LATE_AFTER:
        return
    await _warn(db, owner, settings, today, key=f"watchdog:salary:{expected.isoformat()}",
                title=f"Salary usually arrives around {_day(expected)}; not seen yet",
                body=f"No credit in the Salary category since {_day(days[-1])}. If it has arrived, set its category "
                     "to Salary in Activity.")


async def _unreadable(db: AsyncSession, owner: User, settings: Settings, today: date, moment: datetime,
                      quiet: timedelta) -> None:
    """5 or more SMS that couldn't be read in the last 24 hours."""
    count = await db.scalar(select(func.count()).select_from(Transaction).where(
        *_received(owner),
        Transaction.is_parsed.is_(False),
        Transaction.created_at >= moment - UNREADABLE_WINDOW,
    ))
    if count < UNREADABLE_MIN:
        return
    await _warn(db, owner, settings, today, key=f"watchdog:unreadable:{today.isoformat()}",
                title=f"{count} SMS couldn't be read today",
                body="They're in Activity under Needs review. If your bank changed its SMS format, more will fail "
                     "until Folio's parser is updated.",
                link=REVIEW_LINK)


async def daily(db: AsyncSession, owner: User, today: date) -> None:
    """The daily job (jobs.register("watchdog", watchdog.daily)). Each check runs on its own: one that fails is
    logged and rolled back, and the others still run."""
    settings = await settings_store.load(db, owner.id)
    if not settings["watchdog.enabled"]:
        return
    moment = jobs.now().astimezone(timezone.utc)
    quiet = timedelta(days=settings["watchdog.quiet_days"])
    for check in (_quiet, _salary, _unreadable):
        try:
            await check(db, owner, settings, today, moment, quiet)
        except Exception:
            logger.exception("Watchdog check %s failed", check.__name__)
            await db.rollback()
