"""Merged names and category rules: two things you make so payments get the name and category you want.

**Merged names** (`merchant_aliases`): "use 'Swiggy' for every payment named 'SWIGGY'". A merge is for one
exact name, in any case with extra spaces ignored (`categorize.merchant_key`), looked up by its hash so no
database collation changes which name it's for. Saving one renames the past payments with that name (yours
and the Shortcut's, hand-added ones included) in the same commit, moves the name's merchant memory to the
new name, and keeps every stored merge pointing at a final name (merges never chain).

**Category rules** (`category_rules`): conditions, all of which must hold, and a category. Words in the
merchant name and words in the SMS match as whole words, in order and next to each other, in any case
(`merchants.matches_any_case`; "rahul@okaxis" matches "to VPA rahul@okaxis on"). A smallest amount matches
INR payments only, inclusive. A direction (DEBIT or CREDIT) must equal the payment's type. SMS words never
match a payment added by hand: it has no SMS. Rules are tried newest first (`created_at`, then id) and the
first match sets the category, so a narrower rule added after a broad one makes an exception.

Where they run, in order: the merged name; then the category rules (on the merged name), which skip merchant
memory and Gemini (`parsed_by` "regex+rule"); then merchant memory, keywords, earlier payments and Gemini as
before (parser.parse_sms, categorize.py). A Gemini full parse gets the merged name and a rule's category
(`parsed_by` stays "ai_agent"). A payment added by hand always gets the merged name and, when its category is
"Auto", the rules (without SMS words). Unreadable SMS and edits never run either.

**Apply to past** (a rule, opt-in after a preview): parsed SMS rows (not "manual" or "edited", whose category
you chose) for which this rule is the first match of your whole list, with it in its place, and whose category
differs. They're written in at most two UPDATEs and one commit, each guarded by `updated_at <= started` so an
edit made meanwhile wins; merchant memory is never written. After a merge or an apply that changed rows, one
limit check runs for the newest changed parsed debit.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from collections.abc import Sequence
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import Numeric, String, cast, delete, literal, null, select, union_all, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import categories
import merchants
from auth import current_user, same_origin
from categorize import merchant_key
from dashboard import ownership
from database import get_db
from limits import _encodable, _utc, check_spend_limits
from models import CategoryRule, MerchantAlias, MerchantCategory, Transaction, User
from parser import UNCATEGORIZED

HAND_SET = ("manual", "edited")  # parsed_by of rows whose category you chose: main.MANUAL and main.EDITED
MAX_WORDS = 255  # String(255)
MAX_NAME = 100  # String(100), as a typed merchant name
MAX_AMOUNT = Decimal("9999999999.99")  # Numeric(12, 2)
CENTS = Decimal("0.01")
EXAMPLES = 5  # past payments shown in a preview

names_router = APIRouter(prefix="/api/v1/merchant-names", tags=["Names and rules"])
rules_router = APIRouter(prefix="/api/v1/rules", tags=["Names and rules"])


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- Matching (pure) ----------

def valid_category(name: Optional[str], custom: Sequence[str] = ()) -> Optional[str]:
    """The category's canonical spelling, matched without case: a built-in (parser.CATEGORIES) or one of your
    `custom` categories (categories.names_for); None for an unknown name or Uncategorized (a rule must set a
    category)."""
    category = categories.canonical(name, custom)
    return None if category == UNCATEGORIZED else category


@dataclass(frozen=True)
class RuleView:
    """A category rule as plain values. `id` is None for an unsaved rule (a preview)."""

    id: Optional[uuid.UUID]
    merchant_words: Optional[str]
    sms_words: Optional[str]
    min_amount: Optional[Decimal]
    direction: Optional[str]
    category: str
    created_at: datetime  # aware UTC


def view_of(row: CategoryRule) -> RuleView:
    return RuleView(row.id, row.merchant_words, row.sms_words, row.min_amount, row.direction, row.category,
                    _utc(row.created_at))


def order(views) -> list[RuleView]:
    """Newest first, then by id, so two rules saved at the same moment always come in the same order."""
    return sorted(views, key=lambda rule: (rule.created_at, rule.id.int if rule.id else 0), reverse=True)


def rule_matches(rule: RuleView, merchant: Optional[str], sms_text: Optional[str], amount, currency: str,
                 tx_type: str) -> bool:
    """Every condition the rule sets holds. A rule with no word or amount condition matches nothing;
    `sms_text` is None for a payment added by hand, which SMS words never match."""
    if not (rule.merchant_words or rule.sms_words or rule.min_amount is not None):
        return False
    if rule.direction and rule.direction != tx_type:
        return False
    if rule.min_amount is not None and (currency != "INR" or Decimal(str(amount)) < rule.min_amount):
        return False
    if rule.merchant_words and not merchants.matches_any_case(rule.merchant_words, merchant):
        return False
    if rule.sms_words and (sms_text is None or not merchants.matches_any_case(rule.sms_words, sms_text)):
        return False
    return True


def first_match(rules, merchant: Optional[str], sms_text: Optional[str], amount, currency: str,
                tx_type: str) -> Optional[RuleView]:
    """The first rule (in the order given: newest first) that matches, or None."""
    return next((rule for rule in rules if rule_matches(rule, merchant, sms_text, amount, currency, tx_type)), None)


def with_rule(existing: list[RuleView], target: RuleView, replace_id: Optional[uuid.UUID] = None) -> list[RuleView]:
    """The full list with `target` in its place: instead of the rule `replace_id` (an edit, which keeps its
    place), or first (a new rule is the newest). LookupError when `replace_id` isn't in the list."""
    if replace_id is None:
        return [target, *existing]
    if not any(rule.id == replace_id for rule in existing):
        raise LookupError(replace_id)
    return [target if rule.id == replace_id else rule for rule in existing]


# ---------- Database ----------

async def load_owner_rules(db: AsyncSession, owner, merchant: Optional[str]) -> tuple[Optional[str], list[RuleView]]:
    """(the merged name for `merchant`, or None; the owner's category rules, newest first), in one statement.
    `owner` is a user id, or `categorize._owner_id(None)` (the dashboard owner, for an SMS); with no owner
    row nothing comes back."""
    rule_rows = select(
        literal("rule", String).label("kind"), CategoryRule.id.label("id"), CategoryRule.category.label("value"),
        CategoryRule.merchant_words, CategoryRule.sms_words, CategoryRule.min_amount, CategoryRule.direction,
        CategoryRule.created_at,
    ).where(CategoryRule.user_id == owner)
    statement = rule_rows
    key = merchant_key(merchant)
    if key is not None:
        # Rules come first in the statement, so their column types decide how every row is read.
        statement = union_all(rule_rows, select(
            literal("alias", String), MerchantAlias.id, MerchantAlias.to_name,
            cast(null(), String(255)), cast(null(), String(255)), cast(null(), Numeric(12, 2)), cast(null(), String(6)),
            MerchantAlias.created_at,
        ).where(MerchantAlias.user_id == owner, MerchantAlias.from_hash == merchants.name_hash(key)))
    to_name, views = None, []
    for row in (await db.execute(statement)).all():
        if row.kind == "alias":
            to_name = row.value
        else:
            views.append(RuleView(row.id, row.merchant_words, row.sms_words, row.min_amount, row.direction, row.value,
                                  _utc(row.created_at)))
    return to_name, order(views)


async def _owner_rules(db: AsyncSession, user_id: uuid.UUID) -> list[RuleView]:
    return order(view_of(row) for row in await db.scalars(select(CategoryRule).where(CategoryRule.user_id == user_id)))


async def _past_candidates(db: AsyncSession, user: User, target: RuleView, ordered: list[RuleView]) -> list:
    """Your parsed SMS rows (not ones whose category you chose) that `target` would change: it's the first
    match of the full list `ordered` (which holds `target` itself) and their category differs. One read."""
    rows = (await db.execute(select(
        Transaction.id, Transaction.merchant, Transaction.amount, Transaction.currency, Transaction.type,
        Transaction.category, Transaction.parsed_by, Transaction.raw_message, Transaction.transaction_time,
    ).where(
        ownership(user),
        Transaction.is_parsed.is_(True),
        Transaction.amount > 0,
        Transaction.parsed_by.not_in(HAND_SET),
    ))).all()
    return [row for row in rows
            if first_match(ordered, row.merchant, row.raw_message, row.amount, row.currency, row.type) is target
            and (row.category or UNCATEGORIZED) != target.category]


def _newest_debit(rows) -> Optional[uuid.UUID]:
    # The limit check runs once, for the newest parsed debit changed: it alerts only for the current
    # week or month, so older ones never would.
    debits = [row for row in rows if row.type == "DEBIT" and getattr(row, "is_parsed", True)]
    return max(debits, key=lambda row: _utc(row.transaction_time)).id if debits else None


async def _apply(db: AsyncSession, candidates: list, target: RuleView, started: datetime) -> tuple[int, Optional[uuid.UUID]]:
    """Sets the rule's category on the candidates: at most two UPDATEs (rows the regex parser read also get
    parsed_by "regex+rule"; Gemini's keep theirs), one timestamp, skipping any row changed since `started`.
    Doesn't commit. (rows changed, newest parsed debit among the candidates)."""
    stamp = utc_now()
    regex_ids = [row.id for row in candidates if (row.parsed_by or "").startswith("regex")]
    other_ids = [row.id for row in candidates if not (row.parsed_by or "").startswith("regex")]
    changed = 0
    for ids, values in ((regex_ids, {"parsed_by": "regex+rule"}), (other_ids, {})):
        if not ids:
            continue
        result = await db.execute(
            update(Transaction)
            .where(Transaction.id.in_(ids), Transaction.updated_at <= started)
            .values(category=target.category, updated_at=stamp, **values)
            .execution_options(synchronize_session=False)
        )
        changed += result.rowcount
    return changed, _newest_debit(candidates) if candidates else None


async def merge_names(db: AsyncSession, user: User, from_name: str, to_name: str) -> tuple[MerchantAlias, int, Optional[uuid.UUID]]:
    """Saves the merge of `from_name` into `to_name` and renames past payments (design steps 1–5). Doesn't
    commit. (the merge, rows renamed, newest renamed parsed debit or None)."""
    started = utc_now()  # rows changed after this (an edit made meanwhile) are left alone
    from_key, to_key = merchant_key(from_name), merchant_key(to_name)
    from_hash = merchants.name_hash(from_key)
    aliases = list(await db.scalars(select(MerchantAlias).where(MerchantAlias.user_id == user.id)))
    # 1. Merging into a merged name ends at its final name, unless that's this name: then it's the reverse
    # of that merge (renaming back), which step 3 points at the new name.
    if to_key != from_key:
        final = next((alias for alias in aliases if alias.from_hash == merchants.name_hash(to_key)), None)
        if final is not None and merchant_key(final.to_name) != from_key:
            to_name = final.to_name
    # 2. Create the merge for this name, or point it at the new name.
    now = utc_now()
    alias = next((alias for alias in aliases if alias.from_hash == from_hash), None)
    if alias is None:
        alias = MerchantAlias(user_id=user.id, from_name=from_name, from_key=from_key, from_hash=from_hash,
                              to_name=to_name, created_at=now, updated_at=now)
        db.add(alias)
    else:
        alias.from_name, alias.from_key, alias.to_name, alias.updated_at = from_name, from_key, to_name, now
    # 3. Merges into this name now end at the new one, so none chains.
    for other in aliases:
        if other is not alias and merchant_key(other.to_name) == from_key and other.to_name != to_name:
            other.to_name, other.updated_at = to_name, now
    # 4. Merchant memory moves with the name, per direction; one already under the new name wins.
    new_key = merchant_key(to_name)
    if new_key != from_key:
        memory = list(await db.scalars(select(MerchantCategory).where(
            MerchantCategory.user_id == user.id, MerchantCategory.merchant_key.in_((from_key, new_key)),
        )))
        kept = {row.type for row in memory if row.merchant_key == new_key}  # compared here, not by collation
        for row in memory:
            if row.merchant_key != from_key:
                continue
            if row.type in kept:
                await db.delete(row)
            else:
                row.merchant_key, row.updated_at = new_key, now
    # 5. Rename past payments: the exact name in any case, unless already exactly the new name.
    rows = (await db.execute(select(
        Transaction.id, Transaction.merchant, Transaction.type, Transaction.is_parsed, Transaction.transaction_time,
    ).where(ownership(user), Transaction.merchant.is_not(None)))).all()
    renamed = [row for row in rows if merchant_key(row.merchant) == from_key and row.merchant != to_name]
    changed = 0
    if renamed:
        result = await db.execute(
            update(Transaction)
            .where(Transaction.id.in_([row.id for row in renamed]), Transaction.updated_at <= started)
            .values(merchant=to_name, updated_at=utc_now())
            .execution_options(synchronize_session=False)
        )
        changed = result.rowcount
    return alias, changed, _newest_debit(renamed)


# ---------- API models ----------

class MerchantNameIn(BaseModel):
    from_name: str = Field(max_length=255, description="The payment's name as shown, e.g. SWIGGY")
    to_name: str = Field(description="The name to use instead, e.g. Swiggy")

    @model_validator(mode="before")
    @classmethod
    def _mark_invalid_characters(cls, data):
        return _encodable(data)

    @field_validator("from_name")
    @classmethod
    def _from_name(cls, value: str) -> str:
        return merchants.clean(value)

    @field_validator("to_name")
    @classmethod
    def _to_name(cls, value: str) -> str:
        if merchants.has_invalid_characters(value):
            raise ValueError("Merchant names can't contain invalid characters.")
        value = merchants.clean(value)
        if not merchants.words(value):
            raise ValueError("Enter a merchant name.")
        if len(value) > MAX_NAME:
            raise ValueError(f"Merchant names can be up to {MAX_NAME} characters.")
        return value


class MerchantNameOut(BaseModel):
    id: uuid.UUID
    from_name: str
    to_name: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_validator("created_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _utc(value)


class NameSaved(BaseModel):
    name: MerchantNameOut
    changed: int  # past payments renamed, the one being edited included


def _words(value: Optional[str], no_words: str) -> Optional[str]:
    if value is None:
        return None
    if merchants.has_invalid_characters(value):
        raise ValueError("Words can't contain invalid characters.")
    value = merchants.clean(value)
    if not value:
        return None
    if not merchants.words(value):
        raise ValueError(no_words)
    if len(value) > MAX_WORDS:
        raise ValueError(f"Use up to {MAX_WORDS} characters.")
    return value


class _RuleFields(BaseModel):
    merchant_words: Optional[str] = Field(default=None, description="Words in the merchant name")
    sms_words: Optional[str] = Field(default=None, description="Words in the SMS text")
    min_amount: Optional[Decimal] = Field(default=None, description="Smallest INR amount, inclusive")
    direction: Optional[Literal["DEBIT", "CREDIT"]] = Field(default=None, description="null = money out or in")
    category: str = Field(description="Any category except Uncategorized, yours included")

    @model_validator(mode="before")
    @classmethod
    def _mark_invalid_characters(cls, data):
        return _encodable(data)

    @field_validator("merchant_words")
    @classmethod
    def _merchant_words(cls, value: Optional[str]) -> Optional[str]:
        return _words(value, "Enter words that appear in the name.")

    @field_validator("sms_words")
    @classmethod
    def _sms_words(cls, value: Optional[str]) -> Optional[str]:
        return _words(value, "Enter words that appear in the SMS.")

    @field_validator("min_amount", mode="before")
    @classmethod
    def _min_amount(cls, value) -> Optional[Decimal]:
        # One message for every bad amount, rather than Pydantic's own.
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise ValueError("Enter an amount above zero.")
        try:
            amount = Decimal(str(value).strip())
        except InvalidOperation:
            raise ValueError("Enter an amount above zero.")
        if not amount.is_finite() or amount <= 0 or amount > MAX_AMOUNT or amount != amount.quantize(CENTS):
            raise ValueError("Enter an amount above zero.")
        return amount.quantize(CENTS)

    @field_validator("category")
    @classmethod
    def _category(cls, value: str) -> str:
        # Checked against the list in the routes (_category), which know your own categories.
        value = value.strip()
        if not value:
            raise ValueError("Pick a category from the list.")
        return value

    @model_validator(mode="after")
    def _a_condition(self):
        if not (self.merchant_words or self.sms_words or self.min_amount is not None):
            raise ValueError("Add at least one condition.")
        return self


class CategoryRuleIn(_RuleFields):
    apply_to_past: bool = Field(default=False, description="Also change past payments (preview them first)")


class RulePreviewIn(_RuleFields):
    id: Optional[uuid.UUID] = Field(default=None, description="The rule being edited: the preview takes its place")


class CategoryRuleOut(BaseModel):
    id: uuid.UUID
    merchant_words: Optional[str]
    sms_words: Optional[str]
    min_amount: Optional[Decimal]  # "15000.00"
    direction: Optional[str]
    category: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_validator("min_amount")
    @classmethod
    def _cents(cls, value: Optional[Decimal]) -> Optional[Decimal]:
        return None if value is None else Decimal(value).quantize(CENTS)

    @field_validator("created_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _utc(value)


class RuleSaved(BaseModel):
    rule: CategoryRuleOut
    changed: int  # past payments changed; 0 unless apply_to_past


class RuleExample(BaseModel):
    id: uuid.UUID
    merchant: Optional[str]
    amount: Decimal  # "18000.00"
    currency: str
    transaction_time: datetime  # aware UTC
    category: Optional[str]  # the current one


class RulePreviewOut(BaseModel):
    count: int
    examples: list[RuleExample]  # up to 5, newest first


def _example(row) -> RuleExample:
    return RuleExample(id=row.id, merchant=row.merchant, amount=Decimal(str(row.amount)).quantize(CENTS),
                       currency=row.currency, transaction_time=_utc(row.transaction_time), category=row.category)


async def _category(db: AsyncSession, user: User, body: _RuleFields) -> str:
    """The rule's category in its stored spelling, a built-in or one of yours; 422 otherwise."""
    category = valid_category(body.category, await categories.names_for(db, user))
    if category is None:
        raise HTTPException(422, "Pick a category from the list.")
    return category


def _fields(body: _RuleFields, category: str) -> dict:
    return {"merchant_words": body.merchant_words, "sms_words": body.sms_words, "min_amount": body.min_amount,
            "direction": body.direction, "category": category}


# ---------- Merged names API ----------

@names_router.get("", response_model=list[MerchantNameOut])
async def list_names(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Your merged names, newest first."""
    return list(await db.scalars(select(MerchantAlias).where(MerchantAlias.user_id == user.id)
                                 .order_by(MerchantAlias.created_at.desc(), MerchantAlias.id.desc())))


@names_router.post("", response_model=NameSaved, status_code=status.HTTP_201_CREATED,
                   dependencies=[Depends(same_origin)])
async def merge_name(body: MerchantNameIn, background_tasks: BackgroundTasks, user: User = Depends(current_user),
                     db: AsyncSession = Depends(get_db)):
    """Uses `to_name` for every payment named `from_name` (any case): saves the merge, renames past payments
    and moves the name's merchant memory, in one commit. Saving the same name again replaces its merge."""
    if merchant_key(body.from_name) is None:
        raise HTTPException(422, "This payment has no name to rename.")
    user_id = user.id  # read now: the rollback below expires `user`, which shares this session

    async def save(owner: User):
        saved = await merge_names(db, owner, body.from_name, body.to_name)
        await db.commit()
        return saved

    try:
        alias, changed, newest = await save(user)
    except IntegrityError:
        await db.rollback()  # two saves of the same name raced; this run updates the winner
        owner = await db.get(User, user_id)  # reloaded here: a lazy load of the expired `user` would fail
        if owner is None:
            raise HTTPException(401, "Please sign in with your configured owner account.")
        alias, changed, newest = await save(owner)
    await db.refresh(alias)
    if changed and newest:
        background_tasks.add_task(check_spend_limits, newest)  # the new name can count toward a merchant limit
    return NameSaved(name=MerchantNameOut.model_validate(alias), changed=changed)


@names_router.delete("/{alias_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(same_origin)])
async def delete_name(alias_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Stops renaming future payments. Past payments keep their new name."""
    result = await db.execute(delete(MerchantAlias).where(MerchantAlias.id == alias_id, MerchantAlias.user_id == user.id))
    if not result.rowcount:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Merged name not found.")
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------- Category rules API ----------

@rules_router.get("", response_model=list[CategoryRuleOut])
async def list_rules(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Your category rules, newest first: the order they're tried in."""
    return list(await db.scalars(select(CategoryRule).where(CategoryRule.user_id == user.id)
                                 .order_by(CategoryRule.created_at.desc(), CategoryRule.id.desc())))


@rules_router.post("/preview", response_model=RulePreviewOut, dependencies=[Depends(same_origin)])
async def preview_rule(body: RulePreviewIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """The past payments this rule would change if applied, and up to 5 of them, newest first. With `id`
    (editing a rule) it takes that rule's place in the order; without, it's the newest. Writes nothing."""
    category = await _category(db, user, body)
    existing = await _owner_rules(db, user.id)
    if body.id is not None:
        stored = next((rule for rule in existing if rule.id == body.id), None)
        if stored is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found.")
        target = RuleView(id=body.id, created_at=stored.created_at, **_fields(body, category))
        ordered = with_rule(existing, target, replace_id=body.id)
    else:
        target = RuleView(id=None, created_at=utc_now(), **_fields(body, category))
        ordered = with_rule(existing, target)
    candidates = await _past_candidates(db, user, target, ordered)
    newest = sorted(candidates, key=lambda row: _utc(row.transaction_time), reverse=True)[:EXAMPLES]
    return RulePreviewOut(count=len(candidates), examples=[_example(row) for row in newest])


async def _save_rule(db: AsyncSession, user: User, row: CategoryRule, existing: list[RuleView], replace: bool,
                     apply_to_past: bool, background_tasks: BackgroundTasks) -> RuleSaved:
    started = utc_now()
    target = view_of(row)
    ordered = with_rule(existing, target, replace_id=row.id if replace else None)
    changed, newest = 0, None
    if apply_to_past:
        changed, newest = await _apply(db, await _past_candidates(db, user, target, ordered), target, started)
    await db.commit()
    await db.refresh(row)
    if changed and newest:
        background_tasks.add_task(check_spend_limits, newest)  # it may now count toward a category limit
    return RuleSaved(rule=CategoryRuleOut.model_validate(row), changed=changed)


@rules_router.post("", response_model=RuleSaved, status_code=status.HTTP_201_CREATED,
                   dependencies=[Depends(same_origin)])
async def create_rule(body: CategoryRuleIn, background_tasks: BackgroundTasks, user: User = Depends(current_user),
                      db: AsyncSession = Depends(get_db)):
    """Adds a rule, the newest, so it's tried first. With `apply_to_past`, past payments it now decides get
    its category too (see the preview)."""
    category = await _category(db, user, body)
    existing = await _owner_rules(db, user.id)
    now = utc_now()
    row = CategoryRule(user_id=user.id, created_at=now, updated_at=now, **_fields(body, category))
    db.add(row)
    await db.flush()
    return await _save_rule(db, user, row, existing, False, body.apply_to_past, background_tasks)


@rules_router.put("/{rule_id}", response_model=RuleSaved, dependencies=[Depends(same_origin)])
async def update_rule(rule_id: uuid.UUID, body: CategoryRuleIn, background_tasks: BackgroundTasks,
                      user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Replaces every field (one left out is cleared) and keeps the rule's place in the order."""
    row = await db.scalar(select(CategoryRule).where(CategoryRule.id == rule_id, CategoryRule.user_id == user.id))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found.")
    category = await _category(db, user, body)
    existing = await _owner_rules(db, user.id)
    for field, value in _fields(body, category).items():
        setattr(row, field, value)
    row.updated_at = utc_now()
    await db.flush()
    return await _save_rule(db, user, row, existing, True, body.apply_to_past, background_tasks)


@rules_router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(same_origin)])
async def delete_rule(rule_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Deletes a rule. Payments it categorised keep their category."""
    result = await db.execute(delete(CategoryRule).where(CategoryRule.id == rule_id, CategoryRule.user_id == user.id))
    if not result.rowcount:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found.")
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
