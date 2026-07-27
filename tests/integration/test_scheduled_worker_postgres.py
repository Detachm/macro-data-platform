from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date

import pytest
from docker.errors import DockerException
from testcontainers.postgres import PostgresContainer

from macro_platform.jobs.scheduler import PostgresReportDateLock
from macro_platform.storage.database import Database

pytestmark = pytest.mark.integration


@pytest.fixture
def postgresql_url() -> Iterator[str]:
    supplied_url = os.environ.get("CONTRACT_TEST_DATABASE_URL")
    if supplied_url is not None:
        yield supplied_url
        return
    try:
        with PostgresContainer(
            "postgres:16-alpine", username="macro", password="macro", dbname="macro_data_test"
        ) as postgres:
            yield postgres.get_connection_url().replace("psycopg2", "asyncpg")
    except DockerException as error:
        pytest.skip(f"Docker is unavailable for scheduled worker PostgreSQL contracts: {error}")


async def test_job_029_postgres_advisory_lock_excludes_the_second_worker(
    postgresql_url: str,
) -> None:
    first_database = Database(postgresql_url)
    second_database = Database(postgresql_url)
    report_date = date(2026, 7, 27)
    try:
        first_lock = PostgresReportDateLock(first_database)
        second_lock = PostgresReportDateLock(second_database)
        async with (
            first_lock.hold(report_date) as first_acquired,
            second_lock.hold(report_date) as second_acquired,
        ):
            assert first_acquired is True
            assert second_acquired is False
        async with second_lock.hold(report_date) as acquired_after_release:
            assert acquired_after_release is True
    finally:
        await first_database.dispose()
        await second_database.dispose()
