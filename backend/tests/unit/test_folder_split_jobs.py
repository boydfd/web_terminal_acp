from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.model_base import Base
from app.models import FolderSplitJob, FolderSplitJobStatus
from app.repositories.clients import ensure_local_client
from app.repositories.folder_split_jobs import (
    FOLDER_SPLIT_JOB_RETRY_DELAY_SECONDS,
    MAX_FOLDER_SPLIT_JOB_ATTEMPTS,
    claim_next_folder_split_job,
    enqueue_folder_split_job,
    mark_folder_split_job_failed,
    mark_folder_split_job_retryable,
    mark_folder_split_job_succeeded,
)
from app.repositories.folders import get_or_create_folder_by_path


@pytest.fixture
async def session_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield Session
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_enqueue_folder_split_job_deduplicates_active_pending_job_for_same_folder(session_factory):
    async with session_factory() as session:
        client = await ensure_local_client(session)
        folder = await get_or_create_folder_by_path(session, client.id, "/开发调试")
        first = await enqueue_folder_split_job(session, client.id, folder.id)
        second = await enqueue_folder_split_job(session, client.id, folder.id)
        await session.commit()

    assert first.id == second.id

    async with session_factory() as session:
        jobs = list(await session.scalars(select(FolderSplitJob)))
        assert len(jobs) == 1
        assert jobs[0].status == FolderSplitJobStatus.pending


@pytest.mark.asyncio
async def test_claim_next_folder_split_job_marks_pending_due_job_running_and_increments_attempts(session_factory):
    future_run_after = datetime.now(timezone.utc) + timedelta(hours=1)
    async with session_factory() as session:
        client = await ensure_local_client(session)
        due_folder = await get_or_create_folder_by_path(session, client.id, "/开发调试")
        future_folder = await get_or_create_folder_by_path(session, client.id, "/未来任务")
        due_job = await enqueue_folder_split_job(
            session, client.id, due_folder.id, run_after=datetime.now(timezone.utc)
        )
        await enqueue_folder_split_job(session, client.id, future_folder.id, run_after=future_run_after)
        await session.commit()

    async with session_factory() as session:
        job = await claim_next_folder_split_job(session)
        await session.commit()

    assert job is not None
    assert job.id == due_job.id
    assert job.status == FolderSplitJobStatus.running
    assert job.attempts == 1

    async with session_factory() as session:
        remaining = await claim_next_folder_split_job(session)
        assert remaining is None


@pytest.mark.asyncio
async def test_mark_folder_split_job_status_transitions(session_factory):
    async with session_factory() as session:
        client = await ensure_local_client(session)
        folder = await get_or_create_folder_by_path(session, client.id, "/开发调试")
        job = await enqueue_folder_split_job(session, client.id, folder.id)
        claimed = await claim_next_folder_split_job(session)
        assert claimed is not None
        await mark_folder_split_job_retryable(session, claimed, "temporary failure")
        assert claimed.status == FolderSplitJobStatus.pending
        assert claimed.last_error == "temporary failure"
        assert claimed.run_after is not None
        assert claimed.run_after > datetime.now(timezone.utc) + timedelta(
            seconds=FOLDER_SPLIT_JOB_RETRY_DELAY_SECONDS - 5
        )

        claimed = await claim_next_folder_split_job(session)
        assert claimed is None
        claimed = job
        claimed.run_after = datetime.now(timezone.utc)
        claimed.attempts = MAX_FOLDER_SPLIT_JOB_ATTEMPTS
        await session.flush()
        claimed = await claim_next_folder_split_job(session)
        assert claimed is not None
        await mark_folder_split_job_retryable(session, claimed, "final retry failure")
        assert claimed.status == FolderSplitJobStatus.failed
        assert claimed.last_error == "final retry failure"

        await mark_folder_split_job_failed(session, claimed, "final failure")
        assert claimed.status == FolderSplitJobStatus.failed
        assert claimed.last_error == "final failure"

        await mark_folder_split_job_succeeded(session, job)
        assert job.status == FolderSplitJobStatus.succeeded
        assert job.last_error is None
        assert job.run_after is None
