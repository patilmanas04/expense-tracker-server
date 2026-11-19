"""A SQLite file database for race tests. The tests' in-memory database is one connection shared by every session,
so two requests on it interleave inside a single transaction and no lock can ever hold. A file gives each request
its own connection, and SQLite's write lock then serialises them the way a row lock does on Postgres and TiDB."""
import os
import tempfile

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Base, get_db
from main import app


async def use_file_db(case) -> async_sessionmaker:
    """Points the app's get_db at a fresh SQLite file for this test (undone on cleanup) and returns its sessions."""
    folder = tempfile.TemporaryDirectory()
    case.addCleanup(folder.cleanup)
    engine = create_async_engine(f"sqlite+aiosqlite:///{os.path.join(folder.name, 'race.db')}")
    case.addAsyncCleanup(engine.dispose)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)

    async def file_db():
        async with sessions() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = file_db
    return sessions
