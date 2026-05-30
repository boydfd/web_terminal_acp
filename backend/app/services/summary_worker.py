from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
import logging
from typing import Any
from uuid import UUID

from elasticsearch import AsyncElasticsearch
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Folder, VirtualWindow
from app.repositories.folder_split_jobs import enqueue_folder_split_job
from app.repositories.folders import (
    count_direct_windows_in_folder,
    folder_has_children,
    folder_path_would_create_child_under_occupied_leaf,
    get_or_create_folder_by_path,
)
from app.repositories.summary_jobs import (
    claim_next_summary_job,
    collect_summary_context,
    mark_summary_job_failed,
    mark_summary_job_retryable,
    mark_summary_job_succeeded,
)
from app.repositories.windows import patch_window
from app.services.search_index import get_es_client, index_summary
from app.services.summarizer import OpenAICompatibleSummarizer
from app.services.ui_events import UiEventHub

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
NON_LEAF_SUMMARY_FALLBACK_SEGMENT = "未分类"
MAX_NON_LEAF_SUMMARY_FALLBACK_ATTEMPTS = 100


async def process_summary_jobs_once(
    session_factory: SessionFactory,
    summarizer: Any | None = None,
    es_client: AsyncElasticsearch | None = None,
    ui_event_hub: UiEventHub | None = None,
) -> bool:
    async with session_factory() as session:
        try:
            processed = await process_next_summary_job(session, summarizer, es_client)
            await session.commit()
            if ui_event_hub is not None:
                await _publish_queued_ui_invalidations(session, ui_event_hub)
            return processed
        except Exception:
            await session.rollback()
            raise


async def run_summary_job_worker_loop(
    session_factory: SessionFactory,
    interval_seconds: float = 2.0,
    processed_interval_seconds: float = 2.0,
    es_client: AsyncElasticsearch | None = None,
    ui_event_hub: UiEventHub | None = None,
) -> None:
    while True:
        try:
            processed = await process_summary_jobs_once(
                session_factory,
                es_client=es_client,
                ui_event_hub=ui_event_hub,
            )
        except Exception:
            logger.exception("failed to process summary job")
            processed = False

        if processed:
            await asyncio.sleep(processed_interval_seconds)
        else:
            await asyncio.sleep(interval_seconds)


async def process_next_summary_job(
    session: AsyncSession,
    summarizer: Any | None = None,
    es_client: AsyncElasticsearch | None = None,
) -> bool:
    job = await claim_next_summary_job(session)
    if job is None:
        return False

    window = await _get_window(session, job.virtual_window_id)
    if window is None:
        await mark_summary_job_failed(session, job, "window not found")
        return True

    summarizer = summarizer or OpenAICompatibleSummarizer()
    try:
        context_items = await collect_summary_context(session, window)
        result = await summarizer.summarize(context_items)
        allow_override = job.allow_title_folder_override
        update_title = allow_override or not window.title_manually_overridden
        update_folder = allow_override or not window.folder_manually_overridden
        folder: Folder | None = None
        if update_folder:
            folder = await _resolve_summary_target_folder(session, window.client_id, result.folder_path)
        patch_values: dict[str, Any] = {
            "summary": result.summary,
            "title_tags": result.tags,
        }
        if update_title:
            patch_values["title"] = result.title
        if update_folder and folder is not None:
            patch_values["folder_id"] = folder.id

        updated_window = await patch_window(
            session,
            window.client_id,
            window.id,
            title_history_source="summary",
            **patch_values,
        )
        if updated_window is None:
            await mark_summary_job_failed(session, job, "window not found")
            return True
        if update_title:
            updated_window.title_manually_overridden = False
        if update_folder:
            updated_window.folder_manually_overridden = False
        await session.flush()
        if update_folder and folder is not None:
            direct_window_count = await count_direct_windows_in_folder(
                session, updated_window.client_id, folder.id
            )
            if direct_window_count > 5:
                await enqueue_folder_split_job(session, updated_window.client_id, folder.id)
    except SQLAlchemyError:
        raise
    except Exception as exc:
        await mark_summary_job_retryable(session, job, exc)
        _queue_ui_invalidation(
            session,
            ["window"],
            client_id=window.client_id,
            window_id=window.id,
            reason="summary_retryable",
        )
        return True

    owns_es_client = es_client is None
    active_es_client = es_client
    try:
        if active_es_client is None:
            active_es_client = get_es_client()
        index_folder_path = await _folder_path_for_window(session, updated_window)
        await index_summary(
            active_es_client,
            updated_window.client_id,
            updated_window.id,
            updated_window.title,
            updated_window.title_tags or [],
            index_folder_path,
            updated_window.summary or "",
            document_id=str(updated_window.id),
        )
        await mark_summary_job_succeeded(session, job)
        _queue_ui_invalidation(
            session,
            ["window", "tree", "search", "title_history"],
            client_id=updated_window.client_id,
            window_id=updated_window.id,
            reason="summary_succeeded",
        )
    except SQLAlchemyError:
        raise
    except Exception as exc:
        logger.exception("failed to index summary document", extra={"window_id": str(updated_window.id)})
        await mark_summary_job_retryable(session, job, f"summary indexing failed: {exc}")
        _queue_ui_invalidation(
            session,
            ["window"],
            client_id=updated_window.client_id,
            window_id=updated_window.id,
            reason="summary_index_retryable",
        )
    finally:
        if owns_es_client and active_es_client is not None:
            try:
                await active_es_client.close()
            except Exception:
                logger.exception("failed to close summary Elasticsearch client")

    return True


async def _get_window(session: AsyncSession, window_id: UUID) -> VirtualWindow | None:
    return await session.get(VirtualWindow, window_id)


async def _resolve_summary_target_folder(
    session: AsyncSession,
    client_id: UUID,
    folder_path: str,
) -> Folder:
    if await folder_path_would_create_child_under_occupied_leaf(session, client_id, folder_path):
        raise ValueError("folder_path would create a child under an occupied leaf topic")

    folder = await get_or_create_folder_by_path(session, client_id, folder_path)
    if not await folder_has_children(session, folder.id):
        return folder

    return await _resolve_non_leaf_summary_fallback_folder(session, client_id, folder)


async def _resolve_non_leaf_summary_fallback_folder(
    session: AsyncSession,
    client_id: UUID,
    folder: Folder,
) -> Folder:
    for attempt in range(1, MAX_NON_LEAF_SUMMARY_FALLBACK_ATTEMPTS + 1):
        suffix = "" if attempt == 1 else f" {attempt}"
        candidate_path = (
            f"{folder.path.rstrip('/')}/{NON_LEAF_SUMMARY_FALLBACK_SEGMENT}{suffix}"
        )
        candidate = await get_or_create_folder_by_path(session, client_id, candidate_path)
        if not await folder_has_children(session, candidate.id):
            return candidate

    raise ValueError("folder_path non-leaf fallback exhausted")


async def _folder_path_for_window(session: AsyncSession, window: VirtualWindow) -> str:
    if window.folder_id is None:
        return ""
    folder = await session.get(Folder, window.folder_id)
    return folder.path if folder is not None else ""


def _queue_ui_invalidation(
    session: AsyncSession,
    resources: list[str],
    *,
    client_id: UUID | None = None,
    window_id: UUID | None = None,
    reason: str | None = None,
) -> None:
    session.info.setdefault("ui_invalidations", []).append(
        {
            "resources": resources,
            "client_id": client_id,
            "window_id": window_id,
            "reason": reason,
        }
    )


async def _publish_queued_ui_invalidations(session: AsyncSession, ui_event_hub: UiEventHub) -> None:
    invalidations = session.info.pop("ui_invalidations", [])
    for invalidation in invalidations:
        await ui_event_hub.publish_invalidation(
            invalidation["resources"],
            client_id=invalidation["client_id"],
            window_id=invalidation["window_id"],
            reason=invalidation["reason"],
        )
