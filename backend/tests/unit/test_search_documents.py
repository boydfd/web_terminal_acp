from uuid import uuid4

import pytest
from elastic_transport import TransportError

from app.main import app
from app.models import LOCAL_CLIENT_ID
from app.routers import search as search_router
from app.services.search_index import (
    AI_EVENTS_INDEX,
    INDEX_MAPPINGS,
    MAX_INDEXED_RAW_BYTES,
    MAX_INDEXED_TEXT_CHARS,
    SUMMARIES_INDEX,
    TERMINAL_INDEX,
    ai_event_doc,
    ensure_indexes,
    index_ai_event,
    index_terminal_chunk,
    index_terminal_chunk_without_event,
    is_flood_stage_index_block,
    search_all,
    summary_doc,
    terminal_chunk_doc,
)


def test_terminal_chunk_doc_contains_client_and_searchable_text():
    client_id = uuid4()
    window_id = uuid4()
    doc = terminal_chunk_doc(
        client_id=client_id,
        window_id=window_id,
        text="nginx 403 permission denied",
        source_event_ids=["evt1"],
    )
    assert doc["client_id"] == str(client_id)
    assert doc["virtual_window_id"] == str(window_id)
    assert doc["text"] == "nginx 403 permission denied"
    assert doc["source_event_ids"] == ["evt1"]


def test_ai_event_doc_preserves_client_and_event_context():
    raw = {"model": "claude", "token_count": 42}
    client_id = uuid4()
    window_id = uuid4()

    doc = ai_event_doc(
        provider="claude",
        session_id="session-1",
        kind="message",
        text="permission denied root cause",
        raw=raw,
        client_id=client_id,
        virtual_window_id=window_id,
    )

    assert doc == {
        "client_id": str(client_id),
        "provider": "claude",
        "session_id": "session-1",
        "kind": "message",
        "text": "permission denied root cause",
        "raw": raw,
        "virtual_window_id": str(window_id),
    }


def test_ai_event_doc_allows_unlinked_event():
    client_id = uuid4()
    doc = ai_event_doc(
        provider="claude",
        session_id="session-1",
        kind="message",
        text="permission denied root cause",
        raw={},
        client_id=client_id,
        virtual_window_id=None,
    )

    assert doc["client_id"] == str(client_id)
    assert doc["virtual_window_id"] is None


def test_ai_event_doc_caps_large_index_payload_fields():
    client_id = uuid4()
    doc = ai_event_doc(
        provider="claude",
        session_id="session-1",
        kind="message",
        text="x" * (MAX_INDEXED_TEXT_CHARS + 10),
        raw={"data": "x" * MAX_INDEXED_RAW_BYTES},
        client_id=client_id,
        virtual_window_id=None,
    )

    assert doc["client_id"] == str(client_id)
    assert doc["text"] == f"{'x' * MAX_INDEXED_TEXT_CHARS}…"
    assert doc["raw"] == {"_truncated": True, "size_bytes": MAX_INDEXED_RAW_BYTES + 11}


def test_summary_doc_combines_fields_into_searchable_text():
    client_id = uuid4()
    window_id = uuid4()

    doc = summary_doc(
        client_id=client_id,
        window_id=window_id,
        title="Nginx incident",
        tags=["prod", "403"],
        folder_path="/ops/web",
        summary="Fixed permissions on static directory.",
    )

    assert doc["client_id"] == str(client_id)
    assert doc["virtual_window_id"] == str(window_id)
    assert doc["title"] == "Nginx incident"
    assert doc["tags"] == ["prod", "403"]
    assert doc["folder_path"] == "/ops/web"
    assert doc["summary"] == "Fixed permissions on static directory."
    assert doc["text"] == "Nginx incident prod 403 /ops/web Fixed permissions on static directory."


def test_flood_stage_index_block_detects_elasticsearch_disk_watermark_error():
    assert is_flood_stage_index_block(
        "ApiError(429, 'cluster_block_exception', "
        "'index [summaries] blocked by: "
        "[TOO_MANY_REQUESTS/12/disk usage exceeded flood-stage watermark, "
        "index has read-only-allow-delete block];')"
    )


def test_flood_stage_index_block_does_not_match_generic_search_failure():
    assert not is_flood_stage_index_block("Elasticsearch unavailable")


class FakeIndices:
    def __init__(self, existing_indexes):
        self.existing_indexes = set(existing_indexes)
        self.created = []
        self.updated_mappings = []

    async def exists(self, index):
        return index in self.existing_indexes

    async def create(self, index, **body):
        self.created.append((index, body))
        self.existing_indexes.add(index)

    async def put_mapping(self, index, **body):
        self.updated_mappings.append((index, body))


class FakeIndexClient:
    def __init__(self, existing_indexes=()):
        self.indices = FakeIndices(existing_indexes)
        self.indexed_documents = []

    async def index(self, **kwargs):
        self.indexed_documents.append(kwargs)
        return {"result": "created"}


@pytest.mark.asyncio
async def test_index_terminal_chunk_passes_client_and_deterministic_document_id():
    client = FakeIndexClient()
    client_id = uuid4()
    window_id = uuid4()

    await index_terminal_chunk(
        client,
        client_id=client_id,
        window_id=window_id,
        text="nginx 403 token",
        source_event_ids=["event-1"],
        document_id="terminal-1",
    )

    assert client.indexed_documents == [
        {
            "index": TERMINAL_INDEX,
            "id": "terminal-1",
            "document": {
                "client_id": str(client_id),
                "virtual_window_id": str(window_id),
                "text": "nginx 403 token",
                "source_event_ids": ["event-1"],
            },
        }
    ]


@pytest.mark.asyncio
async def test_index_terminal_chunk_without_event_uses_stable_prefix_and_empty_source_events():
    client = FakeIndexClient()
    client_id = uuid4()
    window_id = uuid4()

    await index_terminal_chunk_without_event(
        client,
        client_id=client_id,
        window_id=window_id,
        text="screen bytes",
    )

    indexed = client.indexed_documents[0]
    assert str(indexed["id"]).startswith(f"terminal-chunk:{window_id}:")
    assert indexed["document"] == {
        "client_id": str(client_id),
        "virtual_window_id": str(window_id),
        "text": "screen bytes",
        "source_event_ids": [],
    }


@pytest.mark.asyncio
async def test_index_ai_event_passes_client_and_deterministic_document_id():
    client = FakeIndexClient()
    client_id = uuid4()

    await index_ai_event(
        client,
        client_id=client_id,
        provider="codex",
        session_id="trace-1",
        kind="tool_call",
        text="bash",
        raw={},
        document_id="event-1",
    )

    assert client.indexed_documents == [
        {
            "index": AI_EVENTS_INDEX,
            "id": "event-1",
            "document": {
                "client_id": str(client_id),
                "provider": "codex",
                "session_id": "trace-1",
                "kind": "tool_call",
                "virtual_window_id": None,
                "text": "bash",
                "raw": {},
            },
        }
    ]


@pytest.mark.asyncio
async def test_ensure_indexes_creates_client_id_keyword_mappings_for_missing_indexes():
    client = FakeIndexClient(existing_indexes=[TERMINAL_INDEX])

    await ensure_indexes(client)

    assert [index for index, _body in client.indices.created] == [AI_EVENTS_INDEX, SUMMARIES_INDEX]
    for _index, body in client.indices.created:
        assert body["mappings"]["properties"]["client_id"] == {"type": "keyword"}
        assert body["mappings"]["properties"]["virtual_window_id"] == {"type": "keyword"}


@pytest.mark.asyncio
async def test_ensure_indexes_updates_client_id_keyword_mappings_for_existing_indexes():
    client = FakeIndexClient(existing_indexes=[TERMINAL_INDEX, AI_EVENTS_INDEX, SUMMARIES_INDEX])

    await ensure_indexes(client)

    assert client.indices.created == []
    assert client.indices.updated_mappings == [
        (TERMINAL_INDEX, INDEX_MAPPINGS[TERMINAL_INDEX]["mappings"]),
        (AI_EVENTS_INDEX, INDEX_MAPPINGS[AI_EVENTS_INDEX]["mappings"]),
        (SUMMARIES_INDEX, INDEX_MAPPINGS[SUMMARIES_INDEX]["mappings"]),
    ]


class FakeSearchClient:
    def __init__(self, hits=None):
        self.calls = []
        self.hits = hits or [
            {
                "_index": TERMINAL_INDEX,
                "_score": 2.5,
                "_source": {"text": "nginx 403 permission denied"},
            },
            {
                "_index": SUMMARIES_INDEX,
                "_score": None,
                "_source": {"title": "Nginx incident"},
            },
        ]

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        return {"hits": {"hits": self.hits}}


@pytest.mark.asyncio
async def test_search_all_filters_by_client_id_and_parses_hits():
    client = FakeSearchClient()
    client_id = uuid4()

    results = await search_all(client, "nginx 403", client_id=client_id)

    assert client.calls == [
        {
            "index": [TERMINAL_INDEX, AI_EVENTS_INDEX, SUMMARIES_INDEX],
            "query": {
                "bool": {
                    "must": [{"multi_match": {"query": "nginx 403", "fields": ["text"]}}],
                    "filter": [{"term": {"client_id": str(client_id)}}],
                }
            },
            "size": 25,
            "source_excludes": ["raw", "source_event_ids", "session_id"],
            "ignore_unavailable": True,
            "allow_no_indices": True,
        }
    ]
    assert results == [
        {
            "id": "",
            "index": TERMINAL_INDEX,
            "score": 2.5,
            "snippet": "nginx 403 permission denied",
            "source": {},
        },
        {
            "id": "",
            "index": SUMMARIES_INDEX,
            "score": None,
            "snippet": "Nginx incident",
            "source": {"title": "Nginx incident"},
        },
    ]


@pytest.mark.asyncio
async def test_search_all_can_include_legacy_documents_missing_client_id_for_local_compatibility():
    client = FakeSearchClient()

    await search_all(
        client,
        "nginx 403",
        client_id=LOCAL_CLIENT_ID,
        include_legacy_local_documents=True,
    )

    assert client.calls[0]["query"] == {
        "bool": {
            "must": [{"multi_match": {"query": "nginx 403", "fields": ["text"]}}],
            "filter": [
                {
                    "bool": {
                        "should": [
                            {"term": {"client_id": str(LOCAL_CLIENT_ID)}},
                            {"bool": {"must_not": [{"exists": {"field": "client_id"}}]}},
                        ],
                        "minimum_should_match": 1,
                    }
                }
            ],
        }
    }


@pytest.mark.asyncio
async def test_search_all_returns_display_safe_results_with_stable_id_and_bounded_snippet():
    long_text = "tool call failed " + ("x" * 600)
    client_id = uuid4()
    client = FakeSearchClient(
        hits=[
            {
                "_id": "event-1",
                "_index": AI_EVENTS_INDEX,
                "_score": 1.5,
                "_source": {
                    "client_id": str(client_id),
                    "provider": "claude",
                    "session_id": "internal-session",
                    "kind": "tool_call",
                    "virtual_window_id": "window-1",
                    "text": long_text,
                    "raw": {"secret": "do-not-return"},
                    "source_event_ids": ["event-a"],
                    "extra_internal": "do-not-return",
                },
            }
        ]
    )

    results = await search_all(client, "tool call", client_id=client_id)

    assert results == [
        {
            "id": "event-1",
            "index": AI_EVENTS_INDEX,
            "score": 1.5,
            "snippet": long_text[:499] + "…",
            "source": {
                "provider": "claude",
                "kind": "tool_call",
                "virtual_window_id": "window-1",
            },
        }
    ]
    assert len(results[0]["snippet"]) == 500
    assert "client_id" not in results[0]["source"]
    assert "raw" not in results[0]["source"]
    assert "session_id" not in results[0]["source"]
    assert "source_event_ids" not in results[0]["source"]


class FakeRouteClient:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_legacy_search_route_trims_query_and_uses_local_client_scope(client, monkeypatch):
    es_client = FakeRouteClient()

    async def fake_search_all(client_arg, query, client_id, *, include_legacy_local_documents=False):
        assert client_arg is es_client
        assert query == "nginx 403"
        assert client_id == LOCAL_CLIENT_ID
        assert include_legacy_local_documents is True
        return [
            {
                "id": "chunk-1",
                "index": TERMINAL_INDEX,
                "score": 1.0,
                "snippet": "nginx 403",
                "source": {},
            }
        ]

    app.dependency_overrides[search_router.get_search_client] = lambda: es_client
    monkeypatch.setattr(search_router, "search_all", fake_search_all)

    try:
        response = await client.get("/api/search", params={"q": "  nginx 403  "})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "query": "nginx 403",
        "results": [
            {
                "id": "chunk-1",
                "index": TERMINAL_INDEX,
                "score": 1.0,
                "snippet": "nginx 403",
                "source": {},
            }
        ],
    }
    assert es_client.closed is False


@pytest.mark.asyncio
async def test_search_route_ensures_indexes_before_search(client, monkeypatch):
    es_client = FakeRouteClient()
    ensured = []

    async def fake_ensure_indexes(client_arg):
        ensured.append(client_arg)

    async def fake_search_all(client_arg, query, client_id, *, include_legacy_local_documents=False):
        assert query == "nginx"
        assert client_id == LOCAL_CLIENT_ID
        assert include_legacy_local_documents is True
        return []

    monkeypatch.setattr(app.state, "es_indexes_ready", False, raising=False)
    app.dependency_overrides[search_router.get_search_client] = lambda: es_client
    monkeypatch.setattr(search_router, "ensure_indexes", fake_ensure_indexes)
    monkeypatch.setattr(search_router, "search_all", fake_search_all)

    try:
        response = await client.get("/api/search", params={"q": "nginx"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert ensured == [es_client]
    assert app.state.es_indexes_ready is True


@pytest.mark.asyncio
async def test_search_route_rejects_blank_query_without_opening_client(client, monkeypatch):
    def fail_get_es_client():
        raise AssertionError("blank query should not open Elasticsearch client")

    app.dependency_overrides[search_router.get_search_client] = fail_get_es_client

    try:
        response = await client.get("/api/search", params={"q": "   "})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_search_route_rejects_overlong_query(client):
    response = await client.get("/api/search", params={"q": "x" * 513})

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_search_route_returns_503_when_search_fails(client, monkeypatch):
    es_client = FakeRouteClient()

    async def failing_search_all(client_arg, query, client_id, *, include_legacy_local_documents=False):
        assert include_legacy_local_documents is True
        raise TransportError("Elasticsearch unavailable")

    app.dependency_overrides[search_router.get_search_client] = lambda: es_client
    monkeypatch.setattr(search_router, "search_all", failing_search_all)

    try:
        response = await client.get("/api/search", params={"q": "nginx"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {"detail": "search service unavailable"}
    assert es_client.closed is False
