# Web Terminal ACP Design

## Goal

Build a standalone prototype in `web_terminal_acp` for an artifact-centric agentic terminal: a LAN-accessible, Nginx-protected web console that treats terminal sessions, AI conversations, tool calls, summaries, and search results as durable artifacts.

The MVP is a complete thin slice, not just a tmux experiment. It must support web terminals, virtual folder organization, PostgreSQL metadata, Elasticsearch search, Claude JSONL ingestion, Codex trace ingestion, and automatic title/tag/folder summarization through a configurable OpenAI-compatible LLM endpoint.

## Confirmed constraints

- Project location: current empty directory `web_terminal_acp`.
- Product shape: standalone prototype.
- Architecture choice: complete thin-slice monolith.
- Runtime boundary: LAN single-user deployment.
- External access and authentication: handled by the user's existing Nginx. The app should bind locally by default and not implement its own login in MVP.
- Stack: React frontend, FastAPI backend, PostgreSQL metadata store, Elasticsearch search store.
- Terminal implementation: custom FastAPI WebSocket + PTY bridge, rendered with `xterm.js`; no `ttyd` dependency in MVP.
- AI capture: Claude Code JSONL watcher and Codex OpenTelemetry-style trace receiver are both in scope.
- LLM summarization: OpenAI-compatible API, configured by base URL, model, and API key. MVP should not assume Anthropic SDK-specific APIs.
- Docker Compose is expected for PostgreSQL and Elasticsearch in local development.
- Persistent container data should live under `./data/web_terminal_acp/...` rather than under the project directory.

## Architecture

The MVP uses one FastAPI backend as the control plane. It exposes REST APIs, terminal WebSockets, tmux orchestration, ingestion endpoints, background watchers, indexing workers, and summary workers. This keeps deployment simple while still maintaining clear internal module boundaries.

React is the browser control surface. It renders the virtual folder tree, terminal workspace, search results, and session detail views. Terminal panes use `xterm.js` and connect to backend WebSocket endpoints.

tmux is the execution plane. It hosts the shell, Claude Code, Codex, and other terminal processes. The web app treats tmux as a process host and view target, not as the source of truth for product metadata.

PostgreSQL is the source of truth for control-plane state: folders, virtual windows, AI sessions, events, and background job state. Elasticsearch is a search projection for terminal chunks, AI events, and generated summaries.

## Backend modules

- `api`: REST and WebSocket routes for folders, windows, search, traces, terminal connections, and health checks.
- `tmux`: main pool creation, tmux window lifecycle, shadow view/session binding, capture-pane snapshots, and archive detection.
- `terminal`: WebSocket bridge for terminal input/output, resize events, and tmux target attachment.
- `ingest`: Claude JSONL tailer, Codex trace receiver, event normalization, source offset tracking, and idempotency.
- `summarizer`: OpenAI-compatible client, batching policy, prompt construction, structured JSON validation, and retryable summary jobs.
- `stores`: PostgreSQL repositories and Elasticsearch indexing/search adapters.
- `config`: environment-driven settings for bind host/port, database URLs, Elasticsearch URL, tmux names, JSONL paths, and LLM endpoint.

## Frontend surfaces

- Folder tree sidebar: renders nested folders and virtual windows.
- Terminal workspace: opens one or more terminal panes using `xterm.js`.
- Window detail panel: shows title, status, folder, tags, summary, linked AI sessions, and recent events.
- Search page: queries Elasticsearch and displays terminal chunks, AI events, and summaries with links back to virtual windows.
- Basic operations: create terminal, open terminal, rename/move folder, move window, archive window, trigger summary retry.

## Folder model

Folders are a first-class concept. `virtual_windows` must reference `folders.id`; it must not store folder paths as the authoritative directory model.

### `folders`

- `id`: UUID primary key.
- `parent_id`: nullable UUID referencing `folders.id`.
- `name`: folder segment name.
- `path`: materialized path such as `/2026-05/生产排障`.
- `sort_order`: integer for manual ordering.
- `created_at`, `updated_at`.
- Unique constraint: `(parent_id, name)`.

The materialized path makes tree rendering and search display simple. Folder rename or move recalculates descendant paths inside one transaction.

### Tree API

`GET /api/tree` returns nested folders with child folders and child windows. The frontend renders this as a directory tree:

```text
/
├── 2026-05
│   ├── 生产排障
│   │   ├── [Claude] 修复 Nginx 403
│   │   └── [Codex] 排查 API CORS
│   └── ACAS项目
└── 未分类
    └── Terminal-15:30
```

When the summarizer returns a `folder_path`, the backend runs `get_or_create_folder_by_path()`, creating missing ancestors and moving the window to the resulting folder.

## Core data model

### PostgreSQL tables

#### `virtual_windows`

Represents durable terminal artifacts shown in the UI.

Fields:

- `id`: UUID primary key.
- `title`: current display title.
- `folder_id`: FK to `folders.id`.
- `status`: `ACTIVE`, `ARCHIVED`, or `ERROR`.
- `tmux_session`: tmux pool or shadow session name when active.
- `tmux_window_id`: tmux window identifier when active.
- `cwd`: initial working directory.
- `shell_command`: command used to create the terminal.
- `created_at`, `updated_at`, `archived_at`.

#### `ai_sessions`

Represents detected Claude/Codex sessions that can be linked to virtual windows.

Fields:

- `id`: UUID primary key.
- `provider`: `claude` or `codex`.
- `source_id`: provider-specific session or trace ID.
- `source_path`: JSONL path or trace source when applicable.
- `project_path`: inferred project path.
- `virtual_window_id`: nullable FK to `virtual_windows.id`.
- `title`, `tags`, `summary`.
- `created_at`, `updated_at`.

#### `events`

Stores normalized metadata for terminal and AI events.

Fields:

- `id`: UUID primary key.
- `source_type`: `terminal`, `claude_jsonl`, `codex_trace`, or `summary`.
- `source_id`: stable source identifier.
- `kind`: normalized event kind such as `terminal_output`, `user_message`, `assistant_message`, `tool_call`, `tool_result`, or `summary_update`.
- `virtual_window_id`: nullable FK.
- `ai_session_id`: nullable FK.
- `payload_json`: original or normalized JSON payload.
- `fingerprint`: idempotency key.
- `indexed_at`: nullable timestamp.
- `created_at`.

#### `summary_jobs`

Tracks retryable LLM summarization work.

Fields:

- `id`: UUID primary key.
- `virtual_window_id`: FK.
- `status`: `PENDING`, `RUNNING`, `SUCCEEDED`, or `FAILED`.
- `attempts`: integer.
- `last_error`: nullable text.
- `created_at`, `updated_at`, `run_after`.

### Elasticsearch indexes

#### `terminal_chunks`

Searchable terminal output chunks.

Fields include `virtual_window_id`, `text`, timestamp range, tmux target, command context when available, and source event IDs.

#### `ai_events`

Searchable Claude/Codex events.

Fields include provider, AI session ID, virtual window ID, event kind, extracted text, raw JSON, and timestamps.

#### `summaries`

Searchable generated metadata.

Fields include virtual window ID, title, tags, folder path, summary, provider/session references, and timestamps.

## Core flows

### Create terminal

1. User clicks create terminal.
2. FastAPI creates or verifies the tmux main pool.
3. Backend creates a new tmux window in the pool.
4. Backend inserts a `virtual_windows` row in the default `未分类` folder.
5. Frontend refreshes tree and can open the terminal.

### Open terminal

1. Frontend opens a WebSocket for a selected `virtual_window_id`.
2. Backend resolves the active tmux target.
3. Backend creates or reuses a shadow view target as needed.
4. Backend bridges terminal bytes between the WebSocket and tmux target.
5. Frontend renders output in `xterm.js` and sends input/resize events back.

### Ingest Claude JSONL

1. Watcher tails configured Claude Code JSONL paths.
2. New lines are parsed and normalized into `events`.
3. Event metadata is written to PostgreSQL with fingerprint-based dedupe.
4. Extracted text is indexed into Elasticsearch.
5. New meaningful events enqueue or refresh a summary job.

### Ingest Codex traces

1. Codex posts traces to the FastAPI receiver.
2. Receiver validates and normalizes spans/messages/tool calls.
3. Events are written to PostgreSQL and indexed into Elasticsearch.
4. Associated AI session/window metadata is updated when session identifiers are available.

### Summarize and classify

1. Summary worker batches recent terminal and AI context for a window/session.
2. Worker calls the configured OpenAI-compatible endpoint.
3. Response must be valid JSON with `title`, `summary`, `tags`, and `folder_path`.
4. Backend validates the response.
5. Backend creates missing folders from `folder_path`, updates `virtual_windows.folder_id`, title, tags, and summary metadata.
6. Backend indexes a `summaries` document in Elasticsearch.

## LLM summary contract

The first implementation should require this response shape:

```json
{
  "title": "[Claude] 修复 Nginx 403 权限问题",
  "summary": "One concise paragraph describing what happened and why it matters.",
  "tags": ["nginx", "403", "production-debugging"],
  "folder_path": "/2026-05/生产排障"
}
```

The backend validates types and rejects unknown top-level structures. If validation fails, the summary job is marked failed with the raw error; the window keeps its fallback title and remains in `未分类`.

## Docker and local services

Local development should use Docker Compose for PostgreSQL and Elasticsearch. Compose services should pin image versions, define health checks, set resource limits, and configure log rotation. Persistent bind mounts should use `./data/web_terminal_acp/postgres`, `./data/web_terminal_acp/elasticsearch`, and similar paths so large data does not accumulate under the repository or root filesystem.

The FastAPI and React development servers can run directly on the host during MVP development. Containerizing the app itself is optional after the thin slice works.

## Error handling

- tmux target missing: mark the window `ERROR` or `ARCHIVED` depending on whether the process exited; show reconnect/new-terminal actions in the UI.
- Elasticsearch unavailable: continue writing PostgreSQL events, leave `indexed_at` null, and retry indexing later.
- PostgreSQL unavailable: API returns failure because control-plane state cannot be safely updated.
- LLM endpoint unavailable: summary job remains retryable; window keeps fallback title and current folder.
- LLM invalid JSON: summary job fails with validation error; retry can be manually triggered.
- Watcher duplicates: use source offsets, provider event IDs, and fingerprints to avoid duplicate `events` rows.
- Nginx/auth: outside app scope for MVP. The app should document expected reverse proxy deployment assumptions but not implement login.

## Testing and verification

### Unit tests

- Folder path creation, rename, move, and descendant path recalculation.
- `get_or_create_folder_by_path()` behavior.
- Event normalization for Claude JSONL and Codex traces.
- Summary JSON validation and fallback handling.

### Integration tests

- PostgreSQL repository tests.
- Elasticsearch indexing and search tests.
- Trace ingest to event row to search result.
- Summary job to folder creation and window movement.

### tmux smoke tests

- Create tmux pool and window.
- Attach WebSocket to tmux target.
- Send input and observe output.
- Resize terminal.
- Archive or mark error after process exit.

### UI smoke tests

- Create a terminal from the UI.
- See a new window under `未分类`.
- Open terminal and run a simple command.
- Ingest a sample AI event.
- Search for indexed content and navigate back to the window detail.

## Non-goals for MVP

- Multi-user accounts, RBAC, or built-in authentication.
- Remote public exposure without Nginx.
- `ttyd` integration.
- Production-grade distributed worker topology.
- Full tmux pane management UI.
- Collaborative editing or shared sessions.
- Provider-specific non-OpenAI-compatible LLM SDK integrations.

## Implementation sequencing

1. Scaffold FastAPI, React, PostgreSQL, Elasticsearch, and Compose configuration with `./data/web_terminal_acp/...` persistent mounts.
2. Implement database schema and folder/window APIs.
3. Implement tmux manager and terminal WebSocket bridge.
4. Implement React tree and terminal workspace.
5. Implement Elasticsearch indexing/search adapters and search UI.
6. Implement Claude JSONL watcher and Codex trace receiver.
7. Implement summary worker with OpenAI-compatible client and folder auto-classification.
8. Add tests and smoke verification scripts.
