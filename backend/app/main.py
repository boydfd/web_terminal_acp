import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
from pathlib import Path

from elastic_transport import TransportError
from elasticsearch import ApiError
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.config import get_settings
from app.auth import AuthMiddleware
from app.db import SessionLocal
from app.repositories.clients import ensure_local_client
from app.routers import (
    auth,
    client_agent,
    clients,
    folders,
    search,
    terminal,
    project_summaries,
    terminal_recents,
    traces,
    ui_settings,
    ui_events,
    windows,
)
from app.services.ingest.claude_watcher import poll_claude_jsonl_directory
from app.services.runtime.client_connections import ClientConnectionRegistry
from app.services.runtime.offline_monitor import (
    mark_all_remote_clients_disconnected,
    run_offline_monitor_loop,
)
from app.services.folder_split_worker import run_folder_split_job_worker_loop
from app.services.search_index import ensure_indexes, get_es_client
from app.services.summary_worker import run_summary_job_worker_loop
from app.services.tmux_manager import get_tmux_manager
from app.services.ui_events import UiEventHub
from app.version import __version__
from app.services.window_reconciler import mark_missing_tmux_windows_error

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    client = get_es_client()
    if getattr(app.state, "ui_event_hub", None) is None:
        app.state.ui_event_hub = UiEventHub()
    app.state.es_client = client
    app.state.es_indexes_ready = False
    app.state.es_startup_error = None

    try:
        await ensure_indexes(client)
        app.state.es_indexes_ready = True
    except (ApiError, TransportError) as exc:
        app.state.es_startup_error = exc

    async with SessionLocal() as session:
        await ensure_local_client(session)
        disconnected_count = await mark_all_remote_clients_disconnected(session)
        await session.commit()
        if disconnected_count:
            logger.info(
                "marked remote clients offline on startup",
                extra={"marked_count": disconnected_count},
            )

    await mark_missing_tmux_windows_error(SessionLocal, get_tmux_manager())

    background_tasks = [
        asyncio.create_task(
            poll_claude_jsonl_directory(
                SessionLocal,
                Path(settings.claude_projects_dir),
                es_client=app.state.es_client,
                ui_event_hub=app.state.ui_event_hub,
            )
        ),
        asyncio.create_task(
            run_summary_job_worker_loop(
                SessionLocal,
                es_client=app.state.es_client,
                ui_event_hub=app.state.ui_event_hub,
            )
        ),
        asyncio.create_task(
            run_folder_split_job_worker_loop(
                SessionLocal,
                es_client=app.state.es_client,
                ui_event_hub=app.state.ui_event_hub,
            )
        ),
        asyncio.create_task(run_offline_monitor_loop(SessionLocal, ui_event_hub=app.state.ui_event_hub)),
    ]
    app.state.background_tasks = background_tasks

    try:
        yield
    finally:
        for task in background_tasks:
            task.cancel()
        results = await asyncio.gather(*background_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.error(
                    "background task stopped with error",
                    exc_info=(type(result), result, result.__traceback__),
                )
        await client.close()


settings = get_settings()
app = FastAPI(title="Web Terminal ACP", version=__version__, lifespan=lifespan)
app.state.client_connections = ClientConnectionRegistry()
app.state.ui_event_hub = UiEventHub()
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(AuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_origin_regex=r"https?://(127\.0\.0\.1|localhost)(:\d+)?",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth.router)
app.include_router(client_agent.router)
app.include_router(clients.router)
app.include_router(folders.router)
app.include_router(windows.router)
app.include_router(project_summaries.router)
app.include_router(terminal_recents.router)
app.include_router(terminal.router)
app.include_router(search.router)
app.include_router(traces.router)
app.include_router(ui_settings.router)
app.include_router(ui_events.router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
