"""Read-only connectivity check. Does not display transaction contents or credentials."""
import asyncio
from sqlalchemy import func, select
from database import AsyncSessionLocal, engine
from models import Transaction


async def check():
    try:
        async with AsyncSessionLocal() as db:
            count = await db.scalar(select(func.count()).select_from(Transaction))
            print(f"Database reachable. Transaction count: {count}.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(check())
