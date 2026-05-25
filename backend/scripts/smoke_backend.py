from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import delete, inspect, or_

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal, engine
from app.models import Event, SummaryJob, VirtualWindow
from app.repositories.clients import ensure_local_client
from app.repositories.folders import build_tree
from app.repositories.summary_jobs import enqueue_summary_job
from app.repositories.windows import create_window
from app.services.ingest.claude_watcher import ingest_claude_jsonl_file
from app.services.search_index import (
    TERMINAL_INDEX,
    ensure_indexes,
    get_es_client,
    index_terminal_chunk,
    search_all,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]
SMOKE_JSONL_DIR = Path("/tmp")
SMOKE_PREFIX = "web-terminal-acp-smoke"
SEARCH_TEXT = "nginx 403 permission denied"
INITIAL_SCHEMA_TABLES = frozenset({"folders", "virtual_windows", "ai_sessions", "events", "summary_jobs"})


@dataclass(frozen=True)
class SmokeRun:
    token: str
    session_id: str
    jsonl_path: Path
    terminal_document_id: str

    @classmethod
    def create(cls, token: str | None = None) -> SmokeRun:
        run_token = token or uuid4().hex
        run_id = f"{SMOKE_PREFIX}-{run_token}"
        return cls(
            token=run_token,
            session_id=run_id,
            jsonl_path=SMOKE_JSONL_DIR / f"{run_id}.jsonl",
            terminal_document_id=run_id,
        )


def smoke_search_text(run: SmokeRun) -> str:
    return f"{SEARCH_TEXT} smoke_token={run.token}"


def write_claude_smoke_jsonl(path: Path, window_id: UUID, run: SmokeRun) -> None:
    payload = {
        "type": "assistant",
        "sessionId": run.session_id,
        "virtual_window_id": str(window_id),
        "message": {"content": f"smoke summary event token={run.token}"},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def smoke_search_result_matches_run(result: dict[str, Any], window_id: UUID, run: SmokeRun) -> bool:
    source = result.get("source")
    if not isinstance(source, dict):
        return False
    return (
        result.get("index") == TERMINAL_INDEX
        and result.get("id") == run.terminal_document_id
        and source.get("virtual_window_id") == str(window_id)
        and run.token in str(result.get("snippet", ""))
    )


def should_stamp_existing_initial_schema(table_names: set[str], has_alembic_version: bool) -> bool:
    return not has_alembic_version and INITIAL_SCHEMA_TABLES.issubset(table_names)


@contextmanager
def _pushd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _alembic_config() -> Config:
    return Config(str(BACKEND_DIR / "alembic.ini"))


def _upgrade_database_to_head_sync() -> None:
    with _pushd(BACKEND_DIR):
        command.upgrade(_alembic_config(), "head")


def _stamp_database_head_sync() -> None:
    with _pushd(BACKEND_DIR):
        command.stamp(_alembic_config(), "head")


async def _database_table_names() -> set[str]:
    async with engine.begin() as connection:
        return await connection.run_sync(lambda sync_connection: set(inspect(sync_connection).get_table_names()))


async def _upgrade_database_to_head() -> None:
    table_names = await _database_table_names()
    if should_stamp_existing_initial_schema(table_names, has_alembic_version="alembic_version" in table_names):
        # Task 17's old smoke used SQLAlchemy create_all against this persistent dev DB.
        # If that exact initial schema is already present but Alembic was never recorded,
        # stamp it once so future smoke runs use normal Alembic upgrade checks.
        await asyncio.to_thread(_stamp_database_head_sync)
    await asyncio.to_thread(_upgrade_database_to_head_sync)


async def _cleanup_database_rows(run: SmokeRun, window_id: UUID | None) -> None:
    if window_id is None:
        return

    async with SessionLocal() as session:
        await session.execute(
            delete(Event).where(
                or_(Event.virtual_window_id == window_id, Event.source_id == run.session_id)
            )
        )
        await session.execute(delete(SummaryJob).where(SummaryJob.virtual_window_id == window_id))
        await session.execute(delete(VirtualWindow).where(VirtualWindow.id == window_id))
        await session.commit()


async def _cleanup_elasticsearch_document(es_client: Any, run: SmokeRun) -> None:
    await es_client.options(ignore_status=[404]).delete(
        index=TERMINAL_INDEX,
        id=run.terminal_document_id,
    )


async def main() -> None:
    run = SmokeRun.create()
    client_id: UUID | None = None
    window_id: UUID | None = None
    es_client = None

    try:
        await _upgrade_database_to_head()

        async with SessionLocal() as session:
            client = await ensure_local_client(session)
            window = await create_window(
                session,
                client.id,
                cwd="/tmp",
                shell_command="echo web-terminal-acp smoke",
                tmux_session=f"smoke-session-{run.token}",
                tmux_window_id=f"smoke-window-{run.token}",
            )
            await enqueue_summary_job(session, window.id)
            tree_roots = await build_tree(session, client.id)
            await session.commit()
            client_id = client.id
            window_id = window.id
            print(f"tree_roots={len(tree_roots)} window_id={window_id}")

        write_claude_smoke_jsonl(run.jsonl_path, window_id, run)
        async with SessionLocal() as session:
            claude_offset = await ingest_claude_jsonl_file(session, run.jsonl_path, 0)
            await session.commit()
            print(f"claude_offset={claude_offset}")

        es_client = get_es_client()
        await ensure_indexes(es_client)
        if client_id is None:
            raise RuntimeError("smoke client was not created")
        await index_terminal_chunk(
            es_client,
            client_id=client_id,
            window_id=window_id,
            text=smoke_search_text(run),
            source_event_ids=[run.terminal_document_id],
            document_id=run.terminal_document_id,
        )
        await es_client.indices.refresh(index=TERMINAL_INDEX)
        search_results = await search_all(es_client, run.token, client_id)
        print(f"search_results={len(search_results)}")
        if not any(smoke_search_result_matches_run(result, window_id, run) for result in search_results):
            raise RuntimeError("smoke search returned no current-run result")
    finally:
        if es_client is not None:
            try:
                await _cleanup_elasticsearch_document(es_client, run)
            finally:
                await es_client.close()
        await _cleanup_database_rows(run, window_id)
        run.jsonl_path.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
