from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.model_base import Base
from app.models import Event, EventSourceType, Folder, FolderSplitJob, FolderSplitJobStatus, VirtualWindow
from app.repositories.clients import create_client, ensure_local_client
from app.repositories.folder_split_jobs import enqueue_folder_split_job
from app.repositories.folders import MAX_FOLDER_PATH_LENGTH, get_or_create_folder_by_path
from app.repositories.windows import create_window
from app.services import folder_split_worker as folder_split_worker_module
from app.services.folder_split_worker import process_folder_split_jobs_once, process_next_folder_split_job
from app.services.folder_splitter import FolderSplitChild, FolderSplitResult
from app.services.search_index import SUMMARIES_INDEX


@dataclass
class FakeFolderSplitter:
    seen_parent_path: str | None = None
    seen_parent_name: str | None = None
    seen_summary_output_language: str | None = None
    seen_terminals: list[dict] | None = None

    async def split(
        self,
        parent_path: str,
        parent_name: str,
        summary_output_language: str,
        terminals: list[dict],
    ) -> FolderSplitResult:
        self.seen_parent_path = parent_path
        self.seen_parent_name = parent_name
        self.seen_summary_output_language = summary_output_language
        self.seen_terminals = terminals
        return FolderSplitResult(
            children=[
                FolderSplitChild(
                    name="前端展示",
                    terminal_ids=[terminal["id"] for terminal in terminals[:3]],
                ),
                FolderSplitChild(
                    name="后端摘要",
                    terminal_ids=[terminal["id"] for terminal in terminals[3:]],
                ),
            ]
        )


class FailingFolderSplitter:
    async def split(
        self,
        parent_path: str,
        parent_name: str,
        summary_output_language: str,
        terminals: list[dict],
    ) -> FolderSplitResult:
        raise ValueError("split failed")


class MissingTerminalFolderSplitter:
    async def split(
        self,
        parent_path: str,
        parent_name: str,
        summary_output_language: str,
        terminals: list[dict],
    ) -> FolderSplitResult:
        return FolderSplitResult(
            children=[
                FolderSplitChild(name="前端展示", terminal_ids=[terminal["id"] for terminal in terminals[:3]]),
                FolderSplitChild(name="后端摘要", terminal_ids=[terminal["id"] for terminal in terminals[3:5]]),
            ]
        )


class UnknownTerminalFolderSplitter:
    async def split(
        self,
        parent_path: str,
        parent_name: str,
        summary_output_language: str,
        terminals: list[dict],
    ) -> FolderSplitResult:
        return FolderSplitResult(
            children=[
                FolderSplitChild(name="前端展示", terminal_ids=[terminal["id"] for terminal in terminals[:3]]),
                FolderSplitChild(
                    name="后端摘要",
                    terminal_ids=[*([terminal["id"] for terminal in terminals[3:]]), uuid4()],
                ),
            ]
        )


class ExistingNonLeafChildFolderSplitter:
    async def split(
        self,
        parent_path: str,
        parent_name: str,
        summary_output_language: str,
        terminals: list[dict],
    ) -> FolderSplitResult:
        return FolderSplitResult(
            children=[
                FolderSplitChild(name="已有", terminal_ids=[terminal["id"] for terminal in terminals[:3]]),
                FolderSplitChild(name="新建", terminal_ids=[terminal["id"] for terminal in terminals[3:]]),
            ]
        )


class NearMaxPathFolderSplitter:
    async def split(
        self,
        parent_path: str,
        parent_name: str,
        summary_output_language: str,
        terminals: list[dict],
    ) -> FolderSplitResult:
        return FolderSplitResult(
            children=[
                FolderSplitChild(name="x", terminal_ids=[terminal["id"] for terminal in terminals[:3]]),
                FolderSplitChild(name="yy", terminal_ids=[terminal["id"] for terminal in terminals[3:]]),
            ]
        )


@dataclass
class RecordingFolderSplitter:
    called: bool = False

    async def split(
        self,
        parent_path: str,
        parent_name: str,
        summary_output_language: str,
        terminals: list[dict],
    ) -> FolderSplitResult:
        self.called = True
        return FolderSplitResult(children=[])


@dataclass
class MutatingFolderSplitter:
    session: AsyncSession
    target_folder_id: object | None = None
    target_window_id: object | None = None

    async def split(
        self,
        parent_path: str,
        parent_name: str,
        summary_output_language: str,
        terminals: list[dict],
    ) -> FolderSplitResult:
        first_terminal_id = terminals[0]["id"]
        self.target_window_id = first_terminal_id
        first_window = await self.session.get(VirtualWindow, first_terminal_id)
        assert first_window is not None
        external_folder = await get_or_create_folder_by_path(
            self.session,
            first_window.client_id,
            "/外部移动",
        )
        first_window.folder_id = external_folder.id
        await self.session.flush()
        self.target_folder_id = external_folder.id
        return FolderSplitResult(
            children=[
                FolderSplitChild(
                    name="前端展示",
                    terminal_ids=[terminal["id"] for terminal in terminals[:3]],
                ),
                FolderSplitChild(
                    name="后端摘要",
                    terminal_ids=[terminal["id"] for terminal in terminals[3:]],
                ),
            ]
        )


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


def near_max_child_path_parent() -> str:
    segment_lengths = [255, 255, 255, MAX_FOLDER_PATH_LENGTH - 6 - (255 * 3)]
    path = "/" + "/".join("a" * length for length in segment_lengths)
    assert len(path) == MAX_FOLDER_PATH_LENGTH - 2
    return path


async def create_windows_in_folder(
    session: AsyncSession,
    path: str,
    count: int,
    client_id=None,
) -> list[VirtualWindow]:
    if client_id is None:
        client = await ensure_local_client(session)
        client_id = client.id
    folder = await get_or_create_folder_by_path(session, client_id, path)
    windows = []
    for index in range(count):
        window = await create_window(
            session,
            client_id,
            cwd=f"/workspace/project-{index}",
            shell_command="/bin/bash",
        )
        window.folder_id = folder.id
        window.title = f"Terminal {index}"
        window.summary = f"Summary {index}"
        window.title_tags = ["tag", f"tag-{index}"]
        windows.append(window)
    await session.flush()
    return windows


@pytest.mark.asyncio
async def test_process_folder_split_job_creates_children_and_moves_windows(session_factory):
    splitter = FakeFolderSplitter()

    async with session_factory() as session:
        windows = await create_windows_in_folder(session, "/开发调试", 6)
        parent_folder = await get_or_create_folder_by_path(session, windows[0].client_id, "/开发调试")
        session.add(
            Event(
                client_id=windows[0].client_id,
                source_type=EventSourceType.terminal,
                source_id="terminal-command-0",
                kind="terminal_input_command",
                virtual_window_id=windows[0].id,
                payload_json={
                    "sequence": 1,
                    "command": "pytest backend/tests/integration/test_folder_split_worker.py",
                    "shell": "/bin/bash",
                    "cwd": "/workspace/project-0",
                    "captured_at": datetime(2026, 5, 22, tzinfo=timezone.utc).isoformat(),
                },
                fingerprint="terminal-command-0",
            )
        )
        session.add(
            Event(
                client_id=windows[0].client_id,
                source_type=EventSourceType.claude_jsonl,
                source_id="claude-event-0",
                kind="assistant",
                virtual_window_id=windows[0].id,
                payload_json={"type": "assistant", "message": {"content": "split worker context"}},
                fingerprint="claude-event-0",
            )
        )
        await enqueue_folder_split_job(session, windows[0].client_id, parent_folder.id)
        await session.commit()
        parent_id = parent_folder.id
        window_ids = [window.id for window in windows]

    async with session_factory() as session:
        processed = await process_next_folder_split_job(session, splitter)
        await session.commit()

    assert processed is True
    assert splitter.seen_parent_path == "/开发调试"
    assert splitter.seen_parent_name == "开发调试"
    assert splitter.seen_summary_output_language == "中文"
    assert splitter.seen_terminals is not None
    assert {terminal["id"] for terminal in splitter.seen_terminals} == set(window_ids)
    assert len(splitter.seen_terminals) == 6
    terminal_zero = next(terminal for terminal in splitter.seen_terminals if terminal["title"] == "Terminal 0")
    assert terminal_zero["summary"] == "Summary 0"
    assert terminal_zero["tags"] == ["tag", "tag-0"]
    assert terminal_zero["cwd"] == "/workspace/project-0"
    assert terminal_zero["created_at"] is not None
    assert terminal_zero["commands"][0]["command"] == "pytest backend/tests/integration/test_folder_split_worker.py"
    assert terminal_zero["ai_events"][0]["source_type"] == "claude_jsonl"

    async with session_factory() as session:
        folders = {folder.path: folder for folder in await session.scalars(select(Folder))}
        assert "/开发调试/前端展示" in folders
        assert "/开发调试/后端摘要" in folders

        refreshed_windows = list(
            await session.scalars(select(VirtualWindow).order_by(VirtualWindow.created_at, VirtualWindow.id))
        )
        assert {window.folder_id for window in refreshed_windows[:3]} == {
            folders["/开发调试/前端展示"].id
        }
        assert {window.folder_id for window in refreshed_windows[3:]} == {
            folders["/开发调试/后端摘要"].id
        }
        assert all(window.folder_id != parent_id for window in refreshed_windows)

        job = (await session.execute(select(FolderSplitJob))).scalar_one()
        assert job.status == FolderSplitJobStatus.succeeded
        assert job.last_error is None


@pytest.mark.asyncio
async def test_process_folder_split_job_failure_keeps_windows_in_parent(session_factory):
    async with session_factory() as session:
        windows = await create_windows_in_folder(session, "/开发调试", 6)
        parent_folder = await get_or_create_folder_by_path(session, windows[0].client_id, "/开发调试")
        await enqueue_folder_split_job(session, windows[0].client_id, parent_folder.id)
        await session.commit()
        parent_id = parent_folder.id

    async with session_factory() as session:
        processed = await process_next_folder_split_job(session, FailingFolderSplitter())
        await session.commit()

    assert processed is True

    async with session_factory() as session:
        job = (await session.execute(select(FolderSplitJob))).scalar_one()
        windows = list(await session.scalars(select(VirtualWindow)))
        assert job.status == FolderSplitJobStatus.pending
        assert job.run_after is not None
        assert job.last_error == "split failed"
        assert all(window.folder_id == parent_id for window in windows)


@pytest.mark.asyncio
async def test_process_folder_split_job_succeeds_without_children_for_small_folder(session_factory):
    splitter = FakeFolderSplitter()

    async with session_factory() as session:
        windows = await create_windows_in_folder(session, "/开发调试", 5)
        parent_folder = await get_or_create_folder_by_path(session, windows[0].client_id, "/开发调试")
        await enqueue_folder_split_job(session, windows[0].client_id, parent_folder.id)
        await session.commit()
        parent_id = parent_folder.id

    processed = await process_folder_split_jobs_once(session_factory, splitter=splitter)

    assert processed is True
    assert splitter.seen_terminals is None

    async with session_factory() as session:
        folders = list(await session.scalars(select(Folder)))
        windows = list(await session.scalars(select(VirtualWindow)))
        job = (await session.execute(select(FolderSplitJob))).scalar_one()
        assert [folder.path for folder in folders if folder.path.startswith("/开发调试/")] == []
        assert all(window.folder_id == parent_id for window in windows)
        assert job.status == FolderSplitJobStatus.succeeded
        assert job.last_error is None


@pytest.mark.asyncio
async def test_process_folder_split_job_returns_false_without_pending_job(session_factory):
    async with session_factory() as session:
        processed = await process_next_folder_split_job(session, FakeFolderSplitter())
        await session.commit()

    assert processed is False


@pytest.mark.asyncio
async def test_process_folder_split_job_fails_when_folder_belongs_to_different_client(session_factory):
    splitter = FakeFolderSplitter()

    async with session_factory() as session:
        local_client = await ensure_local_client(session)
        other_client, _token = await create_client(session, name="remote-client")
        other_windows = await create_windows_in_folder(session, "/远端调试", 6, client_id=other_client.id)
        other_folder = await get_or_create_folder_by_path(session, other_client.id, "/远端调试")
        session.add(
            FolderSplitJob(
                client_id=local_client.id,
                folder_id=other_folder.id,
                status=FolderSplitJobStatus.pending,
            )
        )
        await session.commit()
        original_folder_id_by_window_id = {window.id: window.folder_id for window in other_windows}

    async with session_factory() as session:
        processed = await process_next_folder_split_job(session, splitter)
        await session.commit()

    assert processed is True
    assert splitter.seen_terminals is None

    async with session_factory() as session:
        job = (await session.execute(select(FolderSplitJob))).scalar_one()
        other_windows = list(await session.scalars(select(VirtualWindow).order_by(VirtualWindow.created_at)))
        assert job.status == FolderSplitJobStatus.failed
        assert job.last_error == "folder does not belong to split job client"
        assert {window.id: window.folder_id for window in other_windows} == original_folder_id_by_window_id
        assert not [folder for folder in await session.scalars(select(Folder)) if folder.path.startswith("/远端调试/")]


@pytest.mark.asyncio
async def test_process_folder_split_job_fails_when_folder_is_missing_without_calling_splitter(session_factory):
    splitter = RecordingFolderSplitter()

    async with session_factory() as session:
        client = await ensure_local_client(session)
        session.add(
            FolderSplitJob(
                client_id=client.id,
                folder_id=uuid4(),
                status=FolderSplitJobStatus.pending,
            )
        )
        await session.commit()

    async with session_factory() as session:
        processed = await process_next_folder_split_job(session, splitter)
        await session.commit()

    assert processed is True
    assert splitter.called is False

    async with session_factory() as session:
        job = (await session.execute(select(FolderSplitJob))).scalar_one()
        assert job.status == FolderSplitJobStatus.failed
        assert job.last_error == "folder not found"


@pytest.mark.asyncio
async def test_invalid_splitter_result_does_not_persist_child_folders_or_move_windows(session_factory):
    async with session_factory() as session:
        windows = await create_windows_in_folder(session, "/开发调试", 6)
        parent_folder = await get_or_create_folder_by_path(session, windows[0].client_id, "/开发调试")
        await enqueue_folder_split_job(session, windows[0].client_id, parent_folder.id)
        await session.commit()
        parent_id = parent_folder.id

    async with session_factory() as session:
        processed = await process_next_folder_split_job(session, MissingTerminalFolderSplitter())
        await session.commit()

    assert processed is True

    async with session_factory() as session:
        job = (await session.execute(select(FolderSplitJob))).scalar_one()
        windows = list(await session.scalars(select(VirtualWindow)))
        child_folders = [folder for folder in await session.scalars(select(Folder)) if folder.path.startswith("/开发调试/")]
        assert job.status == FolderSplitJobStatus.pending
        assert job.run_after is not None
        assert job.last_error == "must assign every terminal exactly once"
        assert child_folders == []
        assert all(window.folder_id == parent_id for window in windows)


@pytest.mark.asyncio
async def test_unknown_terminal_splitter_result_does_not_persist_child_folders_or_move_windows(session_factory):
    async with session_factory() as session:
        windows = await create_windows_in_folder(session, "/开发调试", 6)
        parent_folder = await get_or_create_folder_by_path(session, windows[0].client_id, "/开发调试")
        await enqueue_folder_split_job(session, windows[0].client_id, parent_folder.id)
        await session.commit()
        parent_id = parent_folder.id

    async with session_factory() as session:
        processed = await process_next_folder_split_job(session, UnknownTerminalFolderSplitter())
        await session.commit()

    assert processed is True

    async with session_factory() as session:
        job = (await session.execute(select(FolderSplitJob))).scalar_one()
        windows = list(await session.scalars(select(VirtualWindow)))
        child_folders = [folder for folder in await session.scalars(select(Folder)) if folder.path.startswith("/开发调试/")]
        assert job.status == FolderSplitJobStatus.pending
        assert job.run_after is not None
        assert job.last_error == "terminal id is not allowed"
        assert child_folders == []
        assert all(window.folder_id == parent_id for window in windows)


@pytest.mark.asyncio
async def test_split_targeting_existing_non_leaf_child_retries_without_moving_windows(session_factory):
    async with session_factory() as session:
        windows = await create_windows_in_folder(session, "/开发调试", 6)
        parent_folder = await get_or_create_folder_by_path(session, windows[0].client_id, "/开发调试")
        await get_or_create_folder_by_path(session, windows[0].client_id, "/开发调试/已有")
        await get_or_create_folder_by_path(session, windows[0].client_id, "/开发调试/已有/深层")
        await enqueue_folder_split_job(session, windows[0].client_id, parent_folder.id)
        await session.commit()
        parent_id = parent_folder.id

    async with session_factory() as session:
        processed = await process_next_folder_split_job(session, ExistingNonLeafChildFolderSplitter())
        await session.commit()

    assert processed is True

    async with session_factory() as session:
        job = (await session.execute(select(FolderSplitJob))).scalar_one()
        windows = list(await session.scalars(select(VirtualWindow)))
        assert job.status == FolderSplitJobStatus.pending
        assert job.run_after is not None
        assert job.last_error == "split child targets an existing non-leaf topic"
        assert all(window.folder_id == parent_id for window in windows)


@pytest.mark.asyncio
async def test_split_child_path_length_failure_does_not_persist_earlier_valid_child(session_factory):
    parent_path = near_max_child_path_parent()

    async with session_factory() as session:
        windows = await create_windows_in_folder(session, parent_path, 6)
        parent_folder = await get_or_create_folder_by_path(session, windows[0].client_id, parent_path)
        await enqueue_folder_split_job(session, windows[0].client_id, parent_folder.id)
        await session.commit()
        parent_id = parent_folder.id

    async with session_factory() as session:
        processed = await process_next_folder_split_job(session, NearMaxPathFolderSplitter())
        await session.commit()

    assert processed is True

    async with session_factory() as session:
        job = (await session.execute(select(FolderSplitJob))).scalar_one()
        windows = list(await session.scalars(select(VirtualWindow)))
        child_folders = [
            folder
            for folder in await session.scalars(select(Folder))
            if folder.path.startswith(f"{parent_path}/")
        ]
        assert job.status == FolderSplitJobStatus.pending
        assert job.run_after is not None
        assert job.last_error in {"folder path exceeds 1024 characters", "split child path is invalid"}
        assert child_folders == []
        assert all(window.folder_id == parent_id for window in windows)


@pytest.mark.asyncio
async def test_split_target_revalidated_as_leaf_before_moving_windows(session_factory, monkeypatch):
    async with session_factory() as session:
        windows = await create_windows_in_folder(session, "/开发调试", 6)
        parent_folder = await get_or_create_folder_by_path(session, windows[0].client_id, "/开发调试")
        existing_child = await get_or_create_folder_by_path(session, windows[0].client_id, "/开发调试/已有")
        await get_or_create_folder_by_path(session, windows[0].client_id, "/开发调试/新建")
        await enqueue_folder_split_job(session, windows[0].client_id, parent_folder.id)
        await session.commit()
        parent_id = parent_folder.id
        existing_child_id = existing_child.id

    original_folder_has_children = folder_split_worker_module.folder_has_children
    mutation_done = False

    async def stale_leaf_check(session: AsyncSession, folder_id):
        nonlocal mutation_done
        has_children = await original_folder_has_children(session, folder_id)
        if folder_id == existing_child_id and not mutation_done:
            folder = await session.get(Folder, folder_id)
            assert folder is not None
            await get_or_create_folder_by_path(session, folder.client_id, f"{folder.path}/并发")
            await session.flush()
            mutation_done = True
            return False
        return has_children

    monkeypatch.setattr(folder_split_worker_module, "folder_has_children", stale_leaf_check)

    async with session_factory() as session:
        processed = await process_next_folder_split_job(session, ExistingNonLeafChildFolderSplitter())
        await session.commit()

    assert processed is True
    assert mutation_done is True

    async with session_factory() as session:
        job = (await session.execute(select(FolderSplitJob))).scalar_one()
        windows = list(await session.scalars(select(VirtualWindow)))
        assert job.status == FolderSplitJobStatus.pending
        assert job.run_after is not None
        assert job.last_error == "split child targets an existing non-leaf topic"
        assert all(window.folder_id == parent_id for window in windows)


@pytest.mark.asyncio
async def test_folder_window_changes_during_split_retry_without_split_children_or_undo(session_factory):
    async with session_factory() as session:
        windows = await create_windows_in_folder(session, "/开发调试", 6)
        parent_folder = await get_or_create_folder_by_path(session, windows[0].client_id, "/开发调试")
        await enqueue_folder_split_job(session, windows[0].client_id, parent_folder.id)
        await session.commit()
        parent_id = parent_folder.id

    async with session_factory() as session:
        splitter = MutatingFolderSplitter(session)
        processed = await process_next_folder_split_job(session, splitter)
        await session.commit()
        external_folder_id = splitter.target_folder_id
        moved_window_id = splitter.target_window_id

    assert processed is True
    assert external_folder_id is not None
    assert moved_window_id is not None

    async with session_factory() as session:
        job = (await session.execute(select(FolderSplitJob))).scalar_one()
        windows = list(await session.scalars(select(VirtualWindow).order_by(VirtualWindow.created_at, VirtualWindow.id)))
        child_folders = [folder for folder in await session.scalars(select(Folder)) if folder.path.startswith("/开发调试/")]
        moved_window = next(window for window in windows if window.id == moved_window_id)
        remaining_windows = [window for window in windows if window.id != moved_window_id]
        assert job.status == FolderSplitJobStatus.pending
        assert job.run_after is not None
        assert job.last_error == "folder windows changed during split"
        assert child_folders == []
        assert moved_window.folder_id == external_folder_id
        assert all(window.folder_id == parent_id for window in remaining_windows)


@pytest.mark.asyncio
async def test_successful_folder_split_reindexes_moved_window_summaries_with_child_paths(session_factory):
    splitter = FakeFolderSplitter()
    es_client = FakeElasticsearch()

    async with session_factory() as session:
        windows = await create_windows_in_folder(session, "/开发调试", 6)
        parent_folder = await get_or_create_folder_by_path(session, windows[0].client_id, "/开发调试")
        windows[1].summary = ""
        windows[4].summary = None
        await enqueue_folder_split_job(session, windows[0].client_id, parent_folder.id)
        await session.commit()
        expected_indexed_ids = {str(window.id) for window in windows if window.summary}

    async with session_factory() as session:
        processed = await process_next_folder_split_job(session, splitter, es_client=es_client)
        await session.commit()

    assert processed is True
    indexed_documents = es_client.indexed_documents
    assert {document["id"] for document in indexed_documents} == expected_indexed_ids
    assert {document["index"] for document in indexed_documents} == {SUMMARIES_INDEX}
    folder_path_by_window_id = {
        document["id"]: document["document"]["folder_path"] for document in indexed_documents
    }
    assert splitter.seen_terminals is not None
    frontend_ids = {str(terminal["id"]) for terminal in splitter.seen_terminals[:3]}
    backend_ids = {str(terminal["id"]) for terminal in splitter.seen_terminals[3:]}
    for window_id in expected_indexed_ids & frontend_ids:
        assert folder_path_by_window_id[window_id] == "/开发调试/前端展示"
    for window_id in expected_indexed_ids & backend_ids:
        assert folder_path_by_window_id[window_id] == "/开发调试/后端摘要"
    assert es_client.closed is False


@pytest.mark.asyncio
async def test_summary_index_failure_after_moves_marks_job_retryable(session_factory):
    splitter = FakeFolderSplitter()
    es_client = FakeElasticsearch(fail=True)

    async with session_factory() as session:
        windows = await create_windows_in_folder(session, "/开发调试", 6)
        parent_folder = await get_or_create_folder_by_path(session, windows[0].client_id, "/开发调试")
        await enqueue_folder_split_job(session, windows[0].client_id, parent_folder.id)
        await session.commit()
        expected_indexed_ids = {str(window.id) for window in windows}

    async with session_factory() as session:
        processed = await process_next_folder_split_job(session, splitter, es_client=es_client)
        await session.commit()

    assert processed is True

    async with session_factory() as session:
        job = (await session.execute(select(FolderSplitJob))).scalar_one()
        assert job.status == FolderSplitJobStatus.pending
        assert job.run_after is not None
        assert job.last_error == "summary indexing failed: Elasticsearch unavailable"
        job.run_after = None
        await session.commit()

    retry_es_client = FakeElasticsearch()
    async with session_factory() as session:
        processed = await process_next_folder_split_job(
            session,
            FailingFolderSplitter(),
            es_client=retry_es_client,
        )
        await session.commit()

    assert processed is True
    assert {document["id"] for document in retry_es_client.indexed_documents} == expected_indexed_ids

    async with session_factory() as session:
        job = (await session.execute(select(FolderSplitJob))).scalar_one()
        assert job.status == FolderSplitJobStatus.succeeded
        assert job.last_error is None
