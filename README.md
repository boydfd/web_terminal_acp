[中文](README_CN.md)

# Web Terminal ACP

Web Terminal ACP is a browser-based control plane for shell and AI coding agent work. It gives you tmux-backed terminals in the browser, a durable folder tree for long-running sessions, searchable terminal and agent history, and optional remote clients that connect back to the server over WebSocket.

It is built for teams and solo operators who want a persistent record of agent work without replacing the local tools they already use: shells, tmux, Claude Code, Codex, Cursor CLI, and OpenAI-compatible model endpoints.

## Highlights

- **Browser terminal workspace**: xterm.js terminal panes backed by tmux, with reconnectable sessions.
- **Multi-client runtime**: use the server host directly, or register other machines as remote clients.
- **Agent-aware records**: ingest Claude Code JSONL, Codex traces, Cursor adapter events, terminal output, and summaries.
- **Search and summaries**: Elasticsearch indexes terminal output and agent events; an OpenAI-compatible API can generate titles, tags, summaries, and folder suggestions.
- **Agent worktree tracking**: Web Terminal-managed shells expose `WEB_TERMINAL_WINDOW_ID` so coding agents can work in linked git worktrees and surface status in the UI.
- **Direct remote-client registration**: generate a one-time token in Settings, then let the remote host pull its own install script and client bundle from the server.

## Architecture

| Layer | Role |
| --- | --- |
| React + Vite | Browser UI, terminals, settings, search, client registration |
| FastAPI | REST API, WebSockets, tmux orchestration, auth, workers |
| PostgreSQL | Folders, windows, clients, events, sessions, jobs |
| Elasticsearch | Full-text search over terminal chunks, summaries, and agent events |
| Redis | Fast state/cache support for UI polling and runtime paths |
| tmux | Process host for local and remote shell sessions |
| client-agent | Optional Python daemon installed on remote machines |

## Requirements

For the Web Terminal server:

- Docker Engine and Docker Compose v2.
- Linux is the primary deployment target. macOS can run the stack for development, but host shell integration differs.
- At least 4 GB RAM available for the app stack.
- On Linux, Elasticsearch usually requires `vm.max_map_count >= 262144`.
- Optional: an OpenAI-compatible API for generated summaries.
- Optional: Claude Code, Codex, Cursor CLI, or other agent CLIs installed and authenticated on the machine where you want to run them.

For a direct remote client:

- `bash`
- `tmux`
- `python3`
- Python venv/ensurepip support. On Debian/Ubuntu this is usually `python3-venv`.
- Network access from the remote host to the Web Terminal backend URL.
- Optional: Codex / Claude Code / Cursor CLI installed on that remote host if you want to launch those tools there.

The direct registration script checks these dependencies before installing. If a package is missing and the current user cannot install it, stop and ask the machine owner/admin to install it; do not bypass dependency checks with a partial install.

## Quick Start With Docker

```bash
git clone https://github.com/boydfd/web_terminal_acp.git
cd web_terminal_acp
cp .env.example .env
```

Edit `.env` before starting:

- Set `WEB_TERMINAL_AUTH_SECRET` to a strong secret if the UI/API is reachable by anyone except you.
- Set `WORKSPACE_DIR` to the host directory you want exposed inside Web Terminal as `/workspace`.
- Set `OPENAI_COMPAT_BASE_URL`, `OPENAI_COMPAT_API_KEY`, and `OPENAI_COMPAT_MODEL` if you want generated summaries.
- Review the mounted agent config directories (`~/.claude`, `~/.codex`, `~/.agents`, `~/.acpx`) because they can expose host credentials inside the backend container.

Build and start:

```bash
docker compose --profile build-base build backend-base
docker compose build
docker compose up -d --wait
```

Open:

- UI: http://localhost:5173
- API health: http://localhost:8001/healthz

If Elasticsearch fails on Linux:

```bash
sudo sysctl -w vm.max_map_count=262144
docker compose up -d --wait elasticsearch
```

## Configuration

Important `.env` values:

| Variable | Purpose | Default |
| --- | --- | --- |
| `WEB_TERMINAL_AUTH_SECRET` | Enables built-in UI/API login when non-empty | empty |
| `WEB_TERMINAL_AUTH_SESSION_TTL_SECONDS` | Login session lifetime | `604800` |
| `BACKEND_PUBLISHED_PORT` | Backend host port | `8001` |
| `WORKSPACE_DIR` | Host path mounted into backend as `/workspace` | `~/workspace` |
| `CLAUDE_PROJECTS_DIR` | Claude Code projects directory for JSONL ingest | `~/.claude/projects` |
| `DEFAULT_SHELL` | Shell for new terminals; `auto` uses the runtime user's login shell | `auto` |
| `OPENAI_COMPAT_BASE_URL` | OpenAI-compatible API base URL | `http://127.0.0.1:11434/v1` |
| `OPENAI_COMPAT_API_KEY` | API key for summary generation | `dev-local-key` |
| `OPENAI_COMPAT_MODEL` | Model used for generated summaries | `local-summarizer` |
| `VITE_API_BASE` | Frontend build-time fallback API origin; leave empty for Docker nginx proxying | empty |
| `VITE_CLIENT_AGENT_SERVER_URL` | Optional override for the URL written into SSH/direct remote client configs | empty |
| `VITE_ENABLE_ONBOARDING` | Frontend build-time switch for the new-user guide; set `true` to enable | empty |

Do not commit `.env`. Before exposing the app beyond localhost, set `WEB_TERMINAL_AUTH_SECRET`, use strong database passwords, and put a TLS reverse proxy in front of the UI/backend.

## Desktop App Builds

Unsigned Electron packages can be built from the frontend project:

```bash
cd frontend
npm run electron:dist:win:portable
npm run electron:dist:mac:zip
```

The desktop app defaults to `http://127.0.0.1:8001` for the backend. To connect it to a different server after launch, open **Settings** and set **Backend address** to the reachable backend URL. This runtime setting is stored locally by the desktop app, so release builds do not need a baked `VITE_API_BASE` for every deployment.

## Android App Builds

Debug and local release APKs can be built from the frontend project:

```bash
cd frontend
npm run android:build:debug
npm run android:build:local-release
```

Use `android:build:local-release` when you need a release-mode APK that can be installed on a device for local validation. The standard `android:build:release` command is for real release artifacts and requires a release keystore; unsigned release APKs are rejected by Android during installation.

Set these environment variables or matching Gradle properties before building a signed release:

```bash
export WEB_TERMINAL_ANDROID_RELEASE_STORE_FILE=/path/to/release.keystore
export WEB_TERMINAL_ANDROID_RELEASE_STORE_PASSWORD=...
export WEB_TERMINAL_ANDROID_RELEASE_KEY_ALIAS=...
export WEB_TERMINAL_ANDROID_RELEASE_KEY_PASSWORD=...
npm run android:build:release
```

If you intentionally need an unsigned APK for external signing, use:

```bash
npm run android:build:unsigned-release
```

## Install A Remote Client

Remote clients let another machine host shells and agent CLIs while Web Terminal remains the control plane. There are two supported paths.

### Option A: Direct Registration

Use this when the remote host should install itself and you do not want the server to SSH into it.

1. Start the Web Terminal server.
2. Open the UI, then open **Settings** -> **Client registration**.
3. Generate a one-time registration key.
4. On the remote host, run the script shown in Settings.

The command has this shape:

```bash
curl -fsSL http://your-server:8001/api/clients/register-script -o register-client-direct.sh
chmod +x register-client-direct.sh
WEB_TERMINAL_SERVER_URL=http://your-server:8001 \
WEB_TERMINAL_REGISTRATION_KEY=wtr_xxx \
./register-client-direct.sh
```

`WEB_TERMINAL_SERVER_URL` is the HTTP origin the installed client keeps using for API calls and WebSocket reconnects. It can be the backend URL directly, or a frontend/reverse-proxy URL such as `:5173` when that proxy forwards `/api` and WebSocket upgrades.

What the script does:

- Verifies `bash`, `tmux`, `python3`, and Python venv/pip support.
- Calls `POST /api/clients/register` with the one-time key.
- Receives a generated client token, `config.json`, requirements, and a minimal Python client bundle.
- Installs the client under `~/.web-terminal-acp` by default.
- Creates a Python virtual environment and installs the returned requirements.
- Starts `python -m app.client_agent` in a tmux session.

Useful optional flags:

```bash
./register-client-direct.sh \
  --server-url http://your-server:8001 \
  --registration-key wtr_xxx \
  --name build-host-1 \
  --install-path ~/.web-terminal-acp
```

If the script exits with missing dependencies, install the named packages first. For example, on Debian/Ubuntu:

```bash
sudo apt-get update
sudo apt-get install -y bash tmux python3 python3-venv
```

If you do not have permission to install packages, ask an administrator to install them, then rerun the registration command. A remote client that cannot create its venv or run tmux is not considered installed.

### Option B: SSH Bootstrap

Use this when the Web Terminal server is allowed to SSH into the remote host.

1. Open **Clients** -> **Bootstrap remote client** in the UI.
2. Provide the SSH host, user, port, private key, optional passphrase, install path, and server URL.
3. The backend connects over SSH, checks dependencies, writes the client config/bundle, installs requirements, and starts the remote client daemon.

The same dependency rule applies: missing `tmux`, `python3`, or venv support must be installed on the remote host before bootstrap can complete.

### Verify Remote Client Health

On the remote host:

```bash
tmux ls | grep web_terminal_acp_client
tail -n 100 ~/.web-terminal-acp/logs/client.log
```

In the UI, the client should appear as **ONLINE**. If it remains offline, check that the remote host can reach the backend URL and that the backend URL is the same URL the browser/server can use externally, not a container-only hostname.

## Agent-Facing Setup Guide

If you are asking an AI agent to install or operate this project, give it [AGENT_README.md](AGENT_README.md). That guide is intentionally procedural and interactive: it tells the agent to confirm Docker, ask whether this host should also become a remote client, generate the registration key through the UI/API, fetch the install script from the server, and stop for human package installation when it lacks permission.

For agents changing this repository, see [AGENTS.md](AGENTS.md) for project-specific development rules and versioning requirements.

## Local Development

Run shared services:

```bash
make services-up
```

Run the backend:

```bash
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Run the frontend:

```bash
cd frontend
npm install
npm run dev -- --host 127.0.0.1
```

Verification:

```bash
make backend-test
make frontend-build
```

## Project Layout

```text
web_terminal_acp/
├── backend/              # FastAPI app, client-agent, migrations, tests
├── frontend/             # React, Vite, xterm.js, Electron support
├── scripts/              # Build/release/helper scripts
├── docker-compose.yml    # Docker deployment stack
├── Makefile              # Local service/test/deploy targets
├── AGENT_README.md       # Operator guide for installation agents
└── AGENTS.md             # Contributor/agent development rules
```

## Versioning

Client protocol and UI version sources are kept in sync:

- `backend/app/version.py`
- `frontend/package.json`
- `frontend/package-lock.json`

Use SemVer:

- `PATCH` for compatible fixes and documentation-only release updates.
- `MINOR` for compatible features or behavior additions.
- `MAJOR` for incompatible protocol, API, storage, or deployment changes.

## License

MIT. See [LICENSE](LICENSE).
