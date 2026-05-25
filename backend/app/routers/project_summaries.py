from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Client, ProjectSummary
from app.repositories.clients import get_client
from app.repositories.project_summaries import (
    list_project_summaries,
    mark_project_summary_failed,
    mark_project_summary_running,
    mark_project_summary_succeeded,
    upsert_project_summary_pending,
)
from app.schemas import ProjectSummaryOut, ProjectSummarySummarizeIn
from app.services.project_summarizer import (
    ProjectSummarizer,
    collect_project_summary_context,
)

router = APIRouter(prefix="/api/clients", tags=["project-summaries"])


async def _require_client(session: AsyncSession, client_id: UUID) -> Client:
    client = await get_client(session, client_id)
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="client not found")
    return client


@router.get("/{client_id}/project-summaries", response_model=list[ProjectSummaryOut])
async def get_project_summaries(
    client_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> list[ProjectSummaryOut]:
    await _require_client(session, client_id)
    summaries = await list_project_summaries(session, client_id)
    return [_to_out(summary) for summary in summaries]


@router.post("/{client_id}/project-summaries/summarize", response_model=ProjectSummaryOut)
async def summarize_project(
    client_id: UUID,
    payload: ProjectSummarySummarizeIn,
    session: AsyncSession = Depends(get_session),
) -> ProjectSummaryOut:
    await _require_client(session, client_id)
    project_path = payload.project_path.strip()
    if not project_path.startswith("/"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="project_path must be absolute")

    summary = await upsert_project_summary_pending(session, client_id, project_path)
    await session.commit()

    await mark_project_summary_running(session, summary)
    await session.commit()

    try:
        context = await collect_project_summary_context(session, client_id, project_path)
        result = await ProjectSummarizer().summarize(
            context,
            output_language=payload.output_language,
        )
        await mark_project_summary_succeeded(session, summary, result.name)
        await session.commit()
        await session.refresh(summary)
    except Exception as exc:
        await mark_project_summary_failed(session, summary, str(exc))
        await session.commit()
        await session.refresh(summary)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"project summarization failed: {exc}",
        ) from exc

    return _to_out(summary)


def _to_out(summary: ProjectSummary) -> ProjectSummaryOut:
    return ProjectSummaryOut(
        project_path=summary.project_path,
        display_name=summary.display_name,
        status=summary.status.value,
        last_error=summary.last_error,
        updated_at=summary.updated_at,
    )
