from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from macro_platform.storage.database import Database


class UnitOfWork:
    """Owns one database transaction; repositories never commit independently."""

    def __init__(self, database: Database) -> None:
        self._database = database

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self._database.session() as session, session.begin():
            yield session
