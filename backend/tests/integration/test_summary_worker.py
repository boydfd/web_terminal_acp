import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from app.model_base import Base
from app.models import (
    Event,
    EventSourceType,
    Folder,
    FolderSplitJob,
    FolderSplitJobStatus,
    SummaryJob,
    SummaryJobStatus,
    VirtualWindow,
    WindowTitleHistory,
)
from app.repositories.clients import ensure_local_client
from app.repositories.folders import get_or_create_folder_by_path
from app.repositories.summary_jobs import (
    MAX_SUMMARY_JOB_ATTEMPTS,
    claim_next_summary_job,
    collect_summary_context,
    enqueue_summary_job,
)
from app.repositories.windows import create_window
from app.services import summary_worker
from app.services.search_index import SUMMARIES_INDEX
from app.services.summarizer import SummaryResult
from app.services.summary_worker import process_next_summary_job, process_summary_jobs_once


@dataclass
class FakeSummarizer:
    seen_context: list[dict] | None = None

    async def summarize(self, context_items):
        self.seen_context = context_items
        return SummaryResult(
            title="[Claude] 修复 Nginx 403",
            summary="Fixed an nginx permission issue.",
            tags=["nginx", "403"],
            folder_path="/2026-05/生产排障",
        )


class FailingSummarizer:
    async def summarize(self, context_items):
        raise RuntimeError("LLM failed " + ("x" * 3000))


@dataclass
class ResultSummarizer:
    result: SummaryResult
    seen_context: list[dict] | None = None

    async def summarize(self, context_items):
        self.seen_context = context_items
        return self.result


class FakeElasticsearch:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.indexed_documents = []
        self.closed = False

    async def index(self, **kwargs):
        if self.fail:
            raise RuntimeError("Elasticsearch unavailable")
        self.indexed_documents.append(kwargs)
        return {"result": "created"}

    async def close(self):
        self.closed = True


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


async def create_local_window(session):
    client = await ensure_local_client(session)
    return await create_window(session, client.id, cwd="/tmp", shell_command="/bin/bash")


async def create_window_in_folder(session, client_id, folder_id):
    window = await create_window(session, client_id, cwd="/tmp", shell_command="/bin/bash")
    window.folder_id = folder_id
    await session.flush()
    return window


@pytest.mark.asyncio
async def test_process_summary_job_moves_window_to_llm_folder(session_factory):
    summarizer = FakeSummarizer()

    async with session_factory() as session:
        window = await create_local_window(session)
        await enqueue_summary_job(session, window.id)
        await session.commit()

    async with session_factory() as session:
        processed = await process_next_summary_job(session, summarizer, es_client=FakeElasticsearch())
        await session.commit()
        assert processed is True

    async with session_factory() as session:
        window = (
            await session.execute(
                select(VirtualWindow).options(selectinload(VirtualWindow.folder))
            )
        ).scalar_one()
        job = (await session.execute(select(SummaryJob))).scalar_one()
        assert window.title == "[Claude] 修复 Nginx 403"
        assert window.summary == "Fixed an nginx permission issue."
        assert window.title_tags == ["nginx", "403"]
        assert job.status == SummaryJobStatus.succeeded
        assert job.attempts == 1
        assert window.folder.path == "/2026-05/生产排障"
        title_history = (
            await session.execute(
                select(WindowTitleHistory).order_by(WindowTitleHistory.created_at)
            )
        ).scalars().all()
        assert len(title_history) == 2
        assert title_history[0].title.startswith("Terminal-")
        assert title_history[0].summary is None
        assert title_history[0].source == "initial"
        assert (title_history[1].title, title_history[1].summary, title_history[1].source) == (
            "[Claude] 修复 Nginx 403",
            "Fixed an nginx permission issue.",
            "summary",
        )

    assert summarizer.seen_context is not None
    assert len(summarizer.seen_context) == 1
    fallback_context = summarizer.seen_context[0]
    assert fallback_context["source_type"] == "terminal"
    assert fallback_context["kind"] == "terminal_input_context"
    assert fallback_context["payload"]["window"]["title"].startswith("Terminal-")
    assert fallback_context["payload"]["window"]["cwd"] == "/tmp"
    assert fallback_context["payload"]["window"]["shell_command"] == "/bin/bash"
    assert fallback_context["payload"]["window"]["summary"] is None
    assert fallback_context["payload"]["window"]["title_tags"] is None
    assert fallback_context["payload"]["commands"] == []


@pytest.mark.asyncio
async def test_manual_title_lock_prevents_auto_title_overwrite_but_updates_summary_and_tags(
    session_factory,
):
    summarizer = ResultSummarizer(
        SummaryResult(
            title="Auto Title",
            summary="Auto summary.",
            tags=["auto", "summary"],
            folder_path="/auto-folder",
        )
    )

    async with session_factory() as session:
        window = await create_local_window(session)
        window.title = "Manual Title"
        window.title_manually_overridden = True
        await enqueue_summary_job(session, window.id)
        await session.commit()

    async with session_factory() as session:
        processed = await process_next_summary_job(session, summarizer, es_client=FakeElasticsearch())
        await session.commit()
        assert processed is True

    async with session_factory() as session:
        window = (await session.execute(select(VirtualWindow))).scalar_one()
        assert window.title == "Manual Title"
        assert window.summary == "Auto summary."
        assert window.title_tags == ["auto", "summary"]


@pytest.mark.asyncio
async def test_manual_folder_lock_prevents_auto_folder_overwrite_but_updates_summary_and_tags(
    session_factory,
):
    summarizer = ResultSummarizer(
        SummaryResult(
            title="Auto Title",
            summary="Auto summary.",
            tags=["auto", "summary"],
            folder_path="/auto-folder",
        )
    )

    async with session_factory() as session:
        window = await create_local_window(session)
        manual_folder = await get_or_create_folder_by_path(session, window.client_id, "/manual-folder")
        window.folder_id = manual_folder.id
        window.folder_manually_overridden = True
        await enqueue_summary_job(session, window.id)
        await session.commit()

    async with session_factory() as session:
        processed = await process_next_summary_job(session, summarizer, es_client=FakeElasticsearch())
        await session.commit()
        assert processed is True

    async with session_factory() as session:
        window = (
            await session.execute(select(VirtualWindow).options(selectinload(VirtualWindow.folder)))
        ).scalar_one()
        assert window.folder.path == "/manual-folder"
        assert window.summary == "Auto summary."
        assert window.title_tags == ["auto", "summary"]


@pytest.mark.asyncio
async def test_override_job_overwrites_manual_title_and_folder_and_clears_locks(session_factory):
    summarizer = ResultSummarizer(
        SummaryResult(
            title="Auto Title",
            summary="Auto summary.",
            tags=["auto"],
            folder_path="/auto-folder",
        )
    )

    async with session_factory() as session:
        window = await create_local_window(session)
        manual_folder = await get_or_create_folder_by_path(session, window.client_id, "/manual-folder")
        window.title = "Manual Title"
        window.folder_id = manual_folder.id
        window.title_manually_overridden = True
        window.folder_manually_overridden = True
        job = await enqueue_summary_job(session, window.id)
        job.allow_title_folder_override = True
        await session.commit()

    async with session_factory() as session:
        processed = await process_next_summary_job(session, summarizer, es_client=FakeElasticsearch())
        await session.commit()
        assert processed is True

    async with session_factory() as session:
        window = (
            await session.execute(select(VirtualWindow).options(selectinload(VirtualWindow.folder)))
        ).scalar_one()
        assert window.title == "Auto Title"
        assert window.folder.path == "/auto-folder"
        assert window.title_manually_overridden is False
        assert window.folder_manually_overridden is False


@pytest.mark.asyncio
async def test_invalid_folder_path_marks_summary_job_retryable_with_last_error(session_factory):
    summarizer = ResultSummarizer(
        SummaryResult(
            title="Auto Title",
            summary="Auto summary.",
            tags=["auto"],
            folder_path="relative-folder",
        )
    )

    async with session_factory() as session:
        window = await create_local_window(session)
        await enqueue_summary_job(session, window.id)
        await session.commit()

    async with session_factory() as session:
        processed = await process_next_summary_job(session, summarizer, es_client=FakeElasticsearch())
        await session.commit()
        assert processed is True

    async with session_factory() as session:
        job = (await session.execute(select(SummaryJob))).scalar_one()
        window = (await session.execute(select(VirtualWindow))).scalar_one()
        assert job.status == SummaryJobStatus.pending
        assert "folder path must be absolute" in job.last_error
        assert window.summary is None


@pytest.mark.asyncio
async def test_non_leaf_folder_path_moves_window_to_summary_fallback_leaf(session_factory):
    summarizer = ResultSummarizer(
        SummaryResult(
            title="Auto Title",
            summary="Auto summary.",
            tags=["auto"],
            folder_path="/开发调试",
        )
    )
    es_client = FakeElasticsearch()

    async with session_factory() as session:
        window = await create_local_window(session)
        await get_or_create_folder_by_path(session, window.client_id, "/开发调试")
        await get_or_create_folder_by_path(session, window.client_id, "/开发调试/后端摘要")
        await enqueue_summary_job(session, window.id)
        await session.commit()
        client_id = str(window.client_id)
        window_id = str(window.id)

    async with session_factory() as session:
        processed = await process_next_summary_job(session, summarizer, es_client=es_client)
        await session.commit()
        assert processed is True

    async with session_factory() as session:
        job = (await session.execute(select(SummaryJob))).scalar_one()
        window = (
            await session.execute(select(VirtualWindow).options(selectinload(VirtualWindow.folder)))
        ).scalar_one()
        assert job.status == SummaryJobStatus.succeeded
        assert job.last_error is None
        assert window.summary == "Auto summary."
        assert window.folder.path == "/开发调试/未分类"
    assert es_client.indexed_documents == [
        {
            "index": SUMMARIES_INDEX,
            "id": window_id,
            "document": {
                "client_id": client_id,
                "virtual_window_id": window_id,
                "title": "Auto Title",
                "tags": ["auto"],
                "folder_path": "/开发调试/未分类",
                "summary": "Auto summary.",
                "text": "Auto Title auto /开发调试/未分类 Auto summary.",
            },
        }
    ]


@pytest.mark.asyncio
async def test_manual_folder_lock_skips_invalid_llm_folder_but_updates_summary(session_factory):
    summarizer = ResultSummarizer(
        SummaryResult(
            title="Auto Title",
            summary="Auto summary.",
            tags=["auto"],
            folder_path="/开发调试",
        )
    )

    async with session_factory() as session:
        window = await create_local_window(session)
        manual_folder = await get_or_create_folder_by_path(session, window.client_id, "/manual-folder")
        window.folder_id = manual_folder.id
        window.folder_manually_overridden = True
        await get_or_create_folder_by_path(session, window.client_id, "/开发调试")
        await get_or_create_folder_by_path(session, window.client_id, "/开发调试/后端摘要")
        await enqueue_summary_job(session, window.id)
        await session.commit()

    async with session_factory() as session:
        processed = await process_next_summary_job(session, summarizer, es_client=FakeElasticsearch())
        await session.commit()
        assert processed is True

    async with session_factory() as session:
        job = (await session.execute(select(SummaryJob))).scalar_one()
        window = (
            await session.execute(select(VirtualWindow).options(selectinload(VirtualWindow.folder)))
        ).scalar_one()
        assert job.status == SummaryJobStatus.succeeded
        assert job.last_error is None
        assert window.folder.path == "/manual-folder"
        assert window.folder_manually_overridden is True
        assert window.summary == "Auto summary."
        assert window.title_tags == ["auto"]


@pytest.mark.asyncio
async def test_new_child_path_under_occupied_leaf_marks_summary_job_retryable_without_creating_child(
    session_factory,
):
    summarizer = ResultSummarizer(
        SummaryResult(
            title="Auto Title",
            summary="Auto summary.",
            tags=["auto"],
            folder_path="/开发调试/后端摘要",
        )
    )

    async with session_factory() as session:
        window = await create_local_window(session)
        occupied_leaf = await get_or_create_folder_by_path(session, window.client_id, "/开发调试")
        existing_window = await create_window_in_folder(session, window.client_id, occupied_leaf.id)
        await enqueue_summary_job(session, window.id)
        await session.commit()
        existing_window_id = existing_window.id

    async with session_factory() as session:
        processed = await process_next_summary_job(session, summarizer, es_client=FakeElasticsearch())
        await session.commit()
        assert processed is True

    async with session_factory() as session:
        job = (await session.execute(select(SummaryJob))).scalar_one()
        folders = list(await session.scalars(select(Folder).order_by(Folder.path)))
        existing_window = await session.get(VirtualWindow, existing_window_id)
        assert existing_window is not None
        occupied_folder = next(folder for folder in folders if folder.path == "/开发调试")
        assert job.status == SummaryJobStatus.pending
        assert "folder_path would create a child under an occupied leaf topic" in job.last_error
        assert "/开发调试/后端摘要" not in {folder.path for folder in folders}
        assert existing_window.folder_id == occupied_folder.id


@pytest.mark.asyncio
async def test_process_summary_job_enqueues_split_when_leaf_exceeds_five_windows(session_factory):
    summarizer = ResultSummarizer(
        SummaryResult(
            title="Auto Title",
            summary="Auto summary.",
            tags=["auto"],
            folder_path="/开发调试",
        )
    )

    async with session_factory() as session:
        target_window = await create_local_window(session)
        target_folder = await get_or_create_folder_by_path(
            session, target_window.client_id, "/开发调试"
        )
        for _ in range(5):
            await create_window_in_folder(session, target_window.client_id, target_folder.id)
        await enqueue_summary_job(session, target_window.id)
        await session.commit()

    async with session_factory() as session:
        processed = await process_next_summary_job(
            session, summarizer, es_client=FakeElasticsearch()
        )
        await session.commit()
        assert processed is True

    async with session_factory() as session:
        split_job = (await session.execute(select(FolderSplitJob))).scalar_one()
        assert split_job.status == FolderSplitJobStatus.pending
        assert split_job.folder_id == target_folder.id
        assert split_job.client_id == target_window.client_id


@pytest.mark.asyncio
async def test_process_summary_job_does_not_enqueue_split_at_exactly_five_windows(session_factory):
    summarizer = ResultSummarizer(
        SummaryResult(
            title="Auto Title",
            summary="Auto summary.",
            tags=["auto"],
            folder_path="/开发调试",
        )
    )

    async with session_factory() as session:
        target_window = await create_local_window(session)
        target_folder = await get_or_create_folder_by_path(
            session, target_window.client_id, "/开发调试"
        )
        for _ in range(4):
            await create_window_in_folder(session, target_window.client_id, target_folder.id)
        await enqueue_summary_job(session, target_window.id)
        await session.commit()

    async with session_factory() as session:
        processed = await process_next_summary_job(
            session, summarizer, es_client=FakeElasticsearch()
        )
        await session.commit()
        assert processed is True

    async with session_factory() as session:
        split_jobs = list(await session.scalars(select(FolderSplitJob)))
        assert split_jobs == []


@pytest.mark.asyncio
async def test_manual_folder_lock_does_not_enqueue_split_for_llm_target(session_factory):
    summarizer = ResultSummarizer(
        SummaryResult(
            title="Auto Title",
            summary="Auto summary.",
            tags=["auto"],
            folder_path="/开发调试",
        )
    )

    async with session_factory() as session:
        target_window = await create_local_window(session)
        manual_folder = await get_or_create_folder_by_path(
            session, target_window.client_id, "/manual-folder"
        )
        target_window.folder_id = manual_folder.id
        target_window.folder_manually_overridden = True
        llm_folder = await get_or_create_folder_by_path(
            session, target_window.client_id, "/开发调试"
        )
        for _ in range(6):
            await create_window_in_folder(session, target_window.client_id, llm_folder.id)
        await enqueue_summary_job(session, target_window.id)
        await session.commit()

    async with session_factory() as session:
        processed = await process_next_summary_job(
            session, summarizer, es_client=FakeElasticsearch()
        )
        await session.commit()
        assert processed is True

    async with session_factory() as session:
        split_jobs = list(await session.scalars(select(FolderSplitJob)))
        window = (
            await session.execute(
                select(VirtualWindow).where(VirtualWindow.id == target_window.id)
            )
        ).scalar_one()
        assert split_jobs == []
        assert window.folder_id == manual_folder.id


@pytest.mark.asyncio
async def test_process_summary_job_returns_false_without_pending_job(session_factory):
    async with session_factory() as session:
        processed = await process_next_summary_job(session, FakeSummarizer())
        await session.commit()

    assert processed is False


@pytest.mark.asyncio
async def test_process_summary_jobs_once_opens_session_and_commits_processed_job(session_factory):
    async with session_factory() as session:
        window = await create_local_window(session)
        await enqueue_summary_job(session, window.id)
        await session.commit()

    processed = await process_summary_jobs_once(
        session_factory,
        summarizer=FakeSummarizer(),
        es_client=FakeElasticsearch(),
    )

    assert processed is True
    async with session_factory() as session:
        job = (await session.execute(select(SummaryJob))).scalar_one()
        assert job.status == SummaryJobStatus.succeeded


@pytest.mark.asyncio
async def test_folder_creation_race_does_not_rollback_claimed_summary_job(session_factory):
    async with session_factory() as session:
        window = await create_local_window(session)
        await enqueue_summary_job(session, window.id)
        await session.commit()

    inserted_conflict = False

    def insert_conflicting_folder(mapper, connection, target):
        nonlocal inserted_conflict
        if target.path != "/race" or inserted_conflict:
            return
        inserted_conflict = True
        connection.execute(
            Folder.__table__.insert().values(
                id=uuid4(),
                client_id=target.client_id,
                parent_id=target.parent_id,
                name=target.name,
                path=target.path,
            )
        )

    event.listen(Folder, "before_insert", insert_conflicting_folder)
    try:
        async with session_factory() as session:
            job = await claim_next_summary_job(session)
            assert job is not None

            window = await session.get(VirtualWindow, job.virtual_window_id)
            assert window is not None
            folder = await get_or_create_folder_by_path(session, window.client_id, "/race")
            await session.refresh(job)

            assert folder.path == "/race"
            assert job.status == SummaryJobStatus.running
            assert job.attempts == 1
    finally:
        event.remove(Folder, "before_insert", insert_conflicting_folder)


@pytest.mark.asyncio
async def test_process_summary_job_marks_missing_window_failed(session_factory):
    missing_window_id = uuid4()

    async with session_factory() as session:
        session.add(SummaryJob(virtual_window_id=missing_window_id, status=SummaryJobStatus.pending))
        await session.commit()

    async with session_factory() as session:
        processed = await process_next_summary_job(session, FakeSummarizer())
        await session.commit()
        assert processed is True

    async with session_factory() as session:
        job = (await session.execute(select(SummaryJob))).scalar_one()
        assert job.status == SummaryJobStatus.failed
        assert job.attempts == 1
        assert job.run_after is not None
        assert "window not found" in job.last_error


@pytest.mark.asyncio
async def test_process_summary_job_marks_summarizer_failure_retryable_with_bounded_error(session_factory):
    async with session_factory() as session:
        window = await create_local_window(session)
        await enqueue_summary_job(session, window.id)
        await session.commit()

    async with session_factory() as session:
        processed = await process_next_summary_job(session, FailingSummarizer())
        await session.commit()
        assert processed is True

    async with session_factory() as session:
        job = (await session.execute(select(SummaryJob))).scalar_one()
        window = (await session.execute(select(VirtualWindow))).scalar_one()
        assert job.status == SummaryJobStatus.pending
        assert job.attempts == 1
        assert job.run_after is not None
        assert job.last_error.startswith("LLM failed")
        assert len(job.last_error) <= 2000
        assert window.summary is None


@pytest.mark.asyncio
async def test_process_summary_job_marks_summarizer_failure_failed_after_max_attempts(session_factory):
    async with session_factory() as session:
        window = await create_local_window(session)
        job = await enqueue_summary_job(session, window.id)
        job.attempts = MAX_SUMMARY_JOB_ATTEMPTS - 1
        await session.commit()

    async with session_factory() as session:
        processed = await process_next_summary_job(session, FailingSummarizer())
        await session.commit()
        assert processed is True

    async with session_factory() as session:
        job = (await session.execute(select(SummaryJob))).scalar_one()
        assert job.status == SummaryJobStatus.failed
        assert job.attempts == MAX_SUMMARY_JOB_ATTEMPTS
        assert job.run_after is not None
        assert job.last_error.startswith("LLM failed")
        assert len(job.last_error) <= 2000


@pytest.mark.asyncio
async def test_collect_summary_context_uses_input_commands_in_chronological_order(session_factory):
    async with session_factory() as session:
        window = await create_local_window(session)
        await session.flush()
        created_at_base = datetime(2026, 5, 20, tzinfo=timezone.utc)
        for index in range(60):
            session.add(
                Event(
                    source_type=EventSourceType.terminal,
                    source_id=f"terminal-{index}",
                    kind="terminal_input_command",
                    virtual_window_id=window.id,
                    payload_json={
                        "sequence": index,
                        "command": f"command-{index}",
                        "shell": "/bin/bash",
                        "captured_at": (created_at_base + timedelta(seconds=index)).isoformat(),
                    },
                    fingerprint=f"terminal-{index}",
                    created_at=created_at_base + timedelta(seconds=index),
                )
            )
        await session.commit()

    async with session_factory() as session:
        window = (await session.execute(select(VirtualWindow))).scalar_one()
        context = await collect_summary_context(session, window)

    assert len(context) == 1
    assert [command["sequence"] for command in context[0]["payload"]["commands"]] == list(range(60))
    assert context[0]["source_type"] == "terminal"
    assert context[0]["kind"] == "terminal_input_context"


@pytest.mark.asyncio
async def test_collect_summary_context_does_not_use_large_terminal_output_as_primary_context(
    session_factory,
):
    async with session_factory() as session:
        window = await create_local_window(session)
        session.add(
            Event(
                source_type=EventSourceType.terminal,
                source_id="terminal-large",
                kind="terminal_output",
                virtual_window_id=window.id,
                payload_json={"text": "x" * 20000},
                fingerprint="terminal-large",
            )
        )
        await session.commit()

    async with session_factory() as session:
        window = (await session.execute(select(VirtualWindow))).scalar_one()
        context = await collect_summary_context(session, window)

    assert context[0]["kind"] == "terminal_input_context"
    assert context[0]["payload"]["commands"] == []
    assert "x" * 20000 not in json.dumps(context)


@pytest.mark.asyncio
async def test_collect_summary_context_caps_total_serialized_budget(session_factory):
    max_total_bytes = 32768
    async with session_factory() as session:
        window = await create_local_window(session)
        created_at_base = datetime(2026, 5, 20, tzinfo=timezone.utc)
        for index in range(50):
            session.add(
                Event(
                    source_type=EventSourceType.terminal,
                    source_id=f"terminal-budget-{index}",
                    kind="terminal_input_command",
                    virtual_window_id=window.id,
                    payload_json={
                        "sequence": index,
                        "command": f"command-{index} " + ("x" * 1000),
                        "shell": "/bin/bash",
                        "captured_at": (created_at_base + timedelta(seconds=index)).isoformat(),
                    },
                    fingerprint=f"terminal-budget-{index}",
                    created_at=created_at_base + timedelta(seconds=index),
                )
            )
        await session.commit()

    async with session_factory() as session:
        window = (await session.execute(select(VirtualWindow))).scalar_one()
        context = await collect_summary_context(session, window)

    serialized_size = len(
        json.dumps(context, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    sequences = [command["sequence"] for command in context[0]["payload"]["commands"]]
    assert serialized_size <= max_total_bytes
    assert len(sequences) < 50
    assert sequences == list(range(50 - len(sequences), 50))
    assert context[0]["payload"]["truncation"] == {
        "total_commands": 50,
        "included_commands": len(sequences),
        "truncated": True,
        "budget_bytes": max_total_bytes,
    }


@pytest.mark.asyncio
async def test_process_summary_job_indexes_summary_by_default_and_closes_client(session_factory, monkeypatch):
    es_client = FakeElasticsearch()
    created_clients = []

    def fake_get_es_client():
        created_clients.append(es_client)
        return es_client

    monkeypatch.setattr(summary_worker, "get_es_client", fake_get_es_client, raising=False)

    async with session_factory() as session:
        window = await create_local_window(session)
        await enqueue_summary_job(session, window.id)
        await session.commit()
        client_id = str(window.client_id)
        window_id = str(window.id)

    async with session_factory() as session:
        processed = await process_next_summary_job(session, FakeSummarizer())
        await session.commit()

    assert processed is True
    assert created_clients == [es_client]
    assert es_client.closed is True
    assert es_client.indexed_documents == [
        {
            "index": SUMMARIES_INDEX,
            "id": window_id,
            "document": {
                "client_id": client_id,
                "virtual_window_id": window_id,
                "title": "[Claude] 修复 Nginx 403",
                "tags": ["nginx", "403"],
                "folder_path": "/2026-05/生产排障",
                "summary": "Fixed an nginx permission issue.",
                "text": "[Claude] 修复 Nginx 403 nginx 403 /2026-05/生产排障 Fixed an nginx permission issue.",
            },
        }
    ]


@pytest.mark.asyncio
async def test_process_summary_job_marks_retryable_when_es_client_construction_fails(
    session_factory, monkeypatch
):
    def fail_get_es_client():
        raise RuntimeError("Elasticsearch client unavailable " + ("x" * 3000))

    monkeypatch.setattr(summary_worker, "get_es_client", fail_get_es_client, raising=False)

    async with session_factory() as session:
        window = await create_local_window(session)
        await enqueue_summary_job(session, window.id)
        await session.commit()

    async with session_factory() as session:
        processed = await process_next_summary_job(session, FakeSummarizer())
        await session.commit()
        assert processed is True

    async with session_factory() as session:
        job = (await session.execute(select(SummaryJob))).scalar_one()
        assert job.status == SummaryJobStatus.pending
        assert job.attempts == 1
        assert job.run_after is not None
        assert "summary indexing failed" in job.last_error
        assert "Elasticsearch client unavailable" in job.last_error
        assert len(job.last_error) <= 2000


@pytest.mark.asyncio
async def test_process_summary_job_propagates_sqlalchemy_errors_without_marking_failed(
    session_factory, monkeypatch
):
    async def fail_collect_summary_context(session, window):
        raise SQLAlchemyError("transaction is aborted")

    monkeypatch.setattr(
        summary_worker, "collect_summary_context", fail_collect_summary_context, raising=False
    )

    async with session_factory() as session:
        window = await create_local_window(session)
        await enqueue_summary_job(session, window.id)
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(SQLAlchemyError, match="transaction is aborted"):
            await process_next_summary_job(session, FakeSummarizer())
        await session.rollback()

    async with session_factory() as session:
        job = (await session.execute(select(SummaryJob))).scalar_one()
        assert job.status == SummaryJobStatus.pending
        assert job.last_error is None


@pytest.mark.asyncio
async def test_process_summary_job_indexes_summary_with_deterministic_id(session_factory):
    es_client = FakeElasticsearch()

    async with session_factory() as session:
        window = await create_local_window(session)
        await enqueue_summary_job(session, window.id)
        await session.commit()
        client_id = str(window.client_id)
        window_id = str(window.id)

    async with session_factory() as session:
        processed = await process_next_summary_job(session, FakeSummarizer(), es_client=es_client)
        await session.commit()

    assert processed is True
    assert es_client.indexed_documents == [
        {
            "index": SUMMARIES_INDEX,
            "id": window_id,
            "document": {
                "client_id": client_id,
                "virtual_window_id": window_id,
                "title": "[Claude] 修复 Nginx 403",
                "tags": ["nginx", "403"],
                "folder_path": "/2026-05/生产排障",
                "summary": "Fixed an nginx permission issue.",
                "text": "[Claude] 修复 Nginx 403 nginx 403 /2026-05/生产排障 Fixed an nginx permission issue.",
            },
        }
    ]


@pytest.mark.asyncio
async def test_process_summary_job_marks_retryable_when_summary_indexing_fails(session_factory):
    async with session_factory() as session:
        window = await create_local_window(session)
        await enqueue_summary_job(session, window.id)
        await session.commit()

    async with session_factory() as session:
        processed = await process_next_summary_job(
            session, FakeSummarizer(), es_client=FakeElasticsearch(fail=True)
        )
        await session.commit()
        assert processed is True

    async with session_factory() as session:
        job = (await session.execute(select(SummaryJob))).scalar_one()
        window = (await session.execute(select(VirtualWindow))).scalar_one()
        assert job.status == SummaryJobStatus.pending
        assert job.run_after is not None
        assert "summary indexing failed" in job.last_error
        assert "Elasticsearch unavailable" in job.last_error
        assert window.summary == "Fixed an nginx permission issue."
