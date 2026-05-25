from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import Select, asc, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import FolderSplitJob, FolderSplitJobStatus

MAX_FOLDER_SPLIT_JOB_ATTEMPTS = 3
FOLDER_SPLIT_JOB_RETRY_DELAY_SECONDS = 30
MAX_FOLDER_SPLIT_JOB_ERROR_LENGTH = 2000

_ACTIVE_FOLDER_SPLIT_JOB_STATUSES = (
    FolderSplitJobStatus.pending,
    FolderSplitJobStatus.running,
)


def _active_folder_split_job_query(folder_id: UUID) -> Select[tuple[FolderSplitJob]]:
    return (
        select(FolderSplitJob)
        .where(
            FolderSplitJob.folder_id == folder_id,
            FolderSplitJob.status.in_(_ACTIVE_FOLDER_SPLIT_JOB_STATUSES),
        )
        .order_by(FolderSplitJob.created_at, FolderSplitJob.id)
    )


async def enqueue_folder_split_job(
    session: AsyncSession,
    client_id: UUID,
    folder_id: UUID,
    *,
    run_after: datetime | None = None,
) -> FolderSplitJob:
    existing_job = await session.scalar(_active_folder_split_job_query(folder_id))
    if existing_job is not None:
        return existing_job

    job = FolderSplitJob(
        client_id=client_id,
        folder_id=folder_id,
        status=FolderSplitJobStatus.pending,
        run_after=run_after,
    )
    try:
        async with session.begin_nested():
            session.add(job)
            await session.flush()
    except IntegrityError as exc:
        existing_job = await session.scalar(_active_folder_split_job_query(folder_id))
        if existing_job is not None:
            return existing_job
        await session.rollback()
        raise exc
    return job


async def claim_next_folder_split_job(session: AsyncSession) -> FolderSplitJob | None:
    now = datetime.now(timezone.utc)
    running_job = aliased(FolderSplitJob)
    running_same_folder = (
        select(running_job.id)
        .where(
            running_job.folder_id == FolderSplitJob.folder_id,
            running_job.status == FolderSplitJobStatus.running,
        )
        .exists()
    )
    statement = (
        select(FolderSplitJob)
        .where(
            FolderSplitJob.status == FolderSplitJobStatus.pending,
            or_(FolderSplitJob.run_after.is_(None), FolderSplitJob.run_after <= now),
            ~running_same_folder,
        )
        .order_by(
            asc(FolderSplitJob.run_after).nulls_first(),
            FolderSplitJob.created_at,
            FolderSplitJob.id,
        )
        .limit(1)
    )

    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        statement = statement.with_for_update(skip_locked=True)

    job = await session.scalar(statement)
    if job is None:
        return None

    job.status = FolderSplitJobStatus.running
    job.attempts += 1
    job.last_error = None
    await session.flush()
    return job


async def mark_folder_split_job_succeeded(session: AsyncSession, job: FolderSplitJob) -> None:
    job.status = FolderSplitJobStatus.succeeded
    job.last_error = None
    job.run_after = None
    await session.flush()


async def mark_folder_split_job_failed(
    session: AsyncSession, job: FolderSplitJob, error: BaseException | str
) -> None:
    job.status = FolderSplitJobStatus.failed
    job.last_error = _bounded_error_message(error)
    job.run_after = datetime.now(timezone.utc)
    await session.flush()


async def mark_folder_split_job_retryable(
    session: AsyncSession, job: FolderSplitJob, error: BaseException | str
) -> None:
    if job.attempts >= MAX_FOLDER_SPLIT_JOB_ATTEMPTS:
        await mark_folder_split_job_failed(session, job, error)
        return

    job.status = FolderSplitJobStatus.pending
    job.last_error = _bounded_error_message(error)
    job.run_after = datetime.now(timezone.utc) + timedelta(
        seconds=FOLDER_SPLIT_JOB_RETRY_DELAY_SECONDS
    )
    await session.flush()


def _bounded_error_message(error: BaseException | str) -> str:
    message = str(error)
    if len(message) <= MAX_FOLDER_SPLIT_JOB_ERROR_LENGTH:
        return message
    return message[:MAX_FOLDER_SPLIT_JOB_ERROR_LENGTH]
