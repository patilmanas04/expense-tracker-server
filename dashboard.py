"""Which transactions belong to a user: their own and unassigned SMS rows."""
from sqlalchemy import or_

from models import Transaction


def ownership(user):
    return or_(Transaction.user_id == user.id, Transaction.user_id.is_(None))
