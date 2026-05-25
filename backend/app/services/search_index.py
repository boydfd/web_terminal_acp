import json
from typing import Any
from uuid import UUID

from elasticsearch import AsyncElasticsearch

from app.config import get_settings
from app.models import LOCAL_CLIENT_ID

TERMINAL_INDEX = "terminal_chunks"
AI_EVENTS_INDEX = "ai_events"
SUMMARIES_INDEX = "summaries"
SEARCH_INDEXES = [TERMINAL_INDEX, AI_EVENTS_INDEX, SUMMARIES_INDEX]
SEARCH_RESULT_SIZE = 25
MAX_INDEXED_TEXT_CHARS = 32 * 1024
MAX_INDEXED_RAW_BYTES = 64 * 1024
MAX_SEARCH_SNIPPET_CHARS = 500
SAFE_SEARCH_SOURCE_FIELDS = ("virtual_window_id", "title", "tags", "folder_path", "provider", "kind")
SEARCH_SOURCE_EXCLUDES = ["raw", "source_event_ids", "session_id"]


INDEX_MAPPINGS: dict[str, dict[str, Any]] = {
    TERMINAL_INDEX: {
        "mappings": {
            "properties": {
                "client_id": {"type": "keyword"},
                "virtual_window_id": {"type": "keyword"},
                "text": {"type": "text"},
                "source_event_ids": {"type": "keyword"},
            }
        }
    },
    AI_EVENTS_INDEX: {
        "mappings": {
            "properties": {
                "client_id": {"type": "keyword"},
                "provider": {"type": "keyword"},
                "session_id": {"type": "keyword"},
                "kind": {"type": "keyword"},
                "virtual_window_id": {"type": "keyword"},
                "text": {"type": "text"},
                "raw": {"enabled": False},
            }
        }
    },
    SUMMARIES_INDEX: {
        "mappings": {
            "properties": {
                "client_id": {"type": "keyword"},
                "virtual_window_id": {"type": "keyword"},
                "title": {"type": "text"},
                "tags": {"type": "keyword"},
                "folder_path": {"type": "keyword"},
                "summary": {"type": "text"},
                "text": {"type": "text"},
            }
        }
    },
}


def get_es_client() -> AsyncElasticsearch:
    return AsyncElasticsearch(get_settings().elasticsearch_url)


def terminal_chunk_doc(
    client_id: UUID,
    window_id: UUID,
    text: str,
    source_event_ids: list[str],
) -> dict[str, Any]:
    return {
        "client_id": str(client_id),
        "virtual_window_id": str(window_id),
        "text": text,
        "source_event_ids": source_event_ids,
    }


def _cap_text_for_index(text: str) -> str:
    if len(text) <= MAX_INDEXED_TEXT_CHARS:
        return text
    return f"{text[:MAX_INDEXED_TEXT_CHARS]}…"


def _cap_raw_for_index(raw: dict[str, Any]) -> dict[str, Any]:
    raw_bytes = json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    raw_size = len(raw_bytes)
    if raw_size <= MAX_INDEXED_RAW_BYTES:
        return raw
    return {"_truncated": True, "size_bytes": raw_size}


def _truncate_display_text(text: str, max_chars: int = MAX_SEARCH_SNIPPET_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 1]}…"


def _safe_display_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _truncate_display_text(value)
    if isinstance(value, list):
        return [_safe_display_value(item) for item in value[:20]]
    return _truncate_display_text(str(value))


def _display_safe_source(source: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _safe_display_value(source[key])
        for key in SAFE_SEARCH_SOURCE_FIELDS
        if key in source
    }


def _search_snippet(source: dict[str, Any]) -> str:
    for key in ("text", "summary", "title"):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return _truncate_display_text(value.strip())
    return ""


def ai_event_doc(
    provider: str,
    session_id: str,
    kind: str,
    text: str,
    raw: dict[str, Any],
    client_id: UUID,
    virtual_window_id: UUID | None = None,
) -> dict[str, Any]:
    return {
        "client_id": str(client_id),
        "provider": provider,
        "session_id": session_id,
        "kind": kind,
        "virtual_window_id": str(virtual_window_id) if virtual_window_id is not None else None,
        "text": _cap_text_for_index(text),
        "raw": _cap_raw_for_index(raw),
    }


def summary_doc(
    client_id: UUID,
    window_id: UUID,
    title: str,
    tags: list[str],
    folder_path: str,
    summary: str,
) -> dict[str, Any]:
    searchable_text = " ".join([title, *tags, folder_path, summary])
    return {
        "client_id": str(client_id),
        "virtual_window_id": str(window_id),
        "title": title,
        "tags": tags,
        "folder_path": folder_path,
        "summary": summary,
        "text": searchable_text,
    }


async def ensure_indexes(client: AsyncElasticsearch) -> None:
    for index_name in SEARCH_INDEXES:
        exists = await client.indices.exists(index=index_name)
        if not exists:
            await client.indices.create(index=index_name, **INDEX_MAPPINGS[index_name])
            continue
        await client.indices.put_mapping(index=index_name, **INDEX_MAPPINGS[index_name]["mappings"])


async def index_terminal_chunk(
    client: AsyncElasticsearch,
    client_id: UUID,
    window_id: UUID,
    text: str,
    source_event_ids: list[str],
    document_id: str | None = None,
) -> Any:
    index_kwargs: dict[str, Any] = {
        "index": TERMINAL_INDEX,
        "document": terminal_chunk_doc(client_id, window_id, text, source_event_ids),
    }
    if document_id is not None:
        index_kwargs["id"] = document_id
    return await client.index(**index_kwargs)


async def index_ai_event(
    client: AsyncElasticsearch,
    client_id: UUID,
    provider: str,
    session_id: str,
    kind: str,
    text: str,
    raw: dict[str, Any],
    virtual_window_id: UUID | None = None,
    document_id: str | None = None,
) -> Any:
    index_kwargs: dict[str, Any] = {
        "index": AI_EVENTS_INDEX,
        "document": ai_event_doc(provider, session_id, kind, text, raw, client_id, virtual_window_id),
    }
    if document_id is not None:
        index_kwargs["id"] = document_id
    return await client.index(**index_kwargs)


async def index_summary(
    client: AsyncElasticsearch,
    client_id: UUID,
    window_id: UUID,
    title: str,
    tags: list[str],
    folder_path: str,
    summary: str,
    document_id: str | None = None,
) -> Any:
    index_kwargs: dict[str, Any] = {
        "index": SUMMARIES_INDEX,
        "document": summary_doc(client_id, window_id, title, tags, folder_path, summary),
    }
    if document_id is not None:
        index_kwargs["id"] = document_id
    return await client.index(**index_kwargs)


def _client_scope_filter(client_id: UUID, *, include_legacy_local_documents: bool = False) -> dict[str, Any]:
    if include_legacy_local_documents and client_id == LOCAL_CLIENT_ID:
        return {
            "bool": {
                "should": [
                    {"term": {"client_id": str(client_id)}},
                    {"bool": {"must_not": [{"exists": {"field": "client_id"}}]}},
                ],
                "minimum_should_match": 1,
            }
        }
    return {"term": {"client_id": str(client_id)}}


async def search_all(
    client: AsyncElasticsearch,
    query: str,
    client_id: UUID,
    *,
    include_legacy_local_documents: bool = False,
) -> list[dict[str, Any]]:
    response = await client.search(
        index=SEARCH_INDEXES,
        query={
            "bool": {
                "must": [{"multi_match": {"query": query, "fields": ["text"]}}],
                "filter": [
                    _client_scope_filter(
                        client_id,
                        include_legacy_local_documents=include_legacy_local_documents,
                    )
                ],
            }
        },
        size=SEARCH_RESULT_SIZE,
        source_excludes=SEARCH_SOURCE_EXCLUDES,
        ignore_unavailable=True,
        allow_no_indices=True,
    )
    hits = response["hits"]["hits"]
    results = []
    for hit in hits:
        source = hit.get("_source", {}) or {}
        results.append(
            {
                "id": str(hit.get("_id", "")),
                "index": hit.get("_index"),
                "score": hit.get("_score"),
                "snippet": _search_snippet(source),
                "source": _display_safe_source(source),
            }
        )
    return results
