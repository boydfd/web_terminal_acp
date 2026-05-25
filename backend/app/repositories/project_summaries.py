from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ProjectSummary, ProjectSummaryStatus


async def list_project_summaries(session: AsyncSession, client_id: UUID) -> list[ProjectSummary]:
    return list(
        await session.scalars(
            select(ProjectSummary)
            .where(ProjectSummary.client_id == client_id)
            .order_by(ProjectSummary.project_path)
        )
    )


async def get_project_summary(
    session: AsyncSession, client_id: UUID, project_path: str
) -> ProjectSummary | None:
    return await session.scalar(
        select(ProjectSummary).where(
            ProjectSummary.client_id == client_id,
            ProjectSummary.project_path == project_path,
        )
    )


async def upsert_project_summary_pending(
    session: AsyncSession, client_id: UUID, project_path: str
) -> ProjectSummary:
    summary = await get_project_summary(session, client_id, project_path)
    if summary is None:
        summary = ProjectSummary(
            client_id=client_id,
            project_path=project_path,
            status=ProjectSummaryStatus.pending,
        )
        session.add(summary)
    else:
        summary.status = ProjectSummaryStatus.pending
        summary.last_error = None
    await session.flush()
    return summary


async def mark_project_summary_running(session: AsyncSession, summary: ProjectSummary) -> None:
    summary.status = ProjectSummaryStatus.running
    summary.last_error = None
    await session.flush()


async def mark_project_summary_succeeded(
    session: AsyncSession, summary: ProjectSummary, display_name: str
) -> None:
    summary.status = ProjectSummaryStatus.succeeded
    summary.display_name = display_name
    summary.last_error = None
    await session.flush()


async def mark_project_summary_failed(session: AsyncSession, summary: ProjectSummary, error: str) -> None:
    summary.status = ProjectSummaryStatus.failed
    summary.last_error = error[:4096] if error else "unknown error"
    await session.flush()
