# Agent Installation Guide

This guide is for an AI agent that is asked to install or operate Web Terminal ACP. Follow it as an interactive runbook. Do not assume the human wants every optional component; ask at the decision points below.

## Operating Rules

- Prefer Docker Compose for the Web Terminal server.
- Do not expose the app on a public network without `WEB_TERMINAL_AUTH_SECRET` and a reverse proxy/TLS plan.
- Do not print registration keys, client tokens, private keys, or `.env` contents into public logs.
- If a required OS package is missing and you do not have permission to install it, stop and ask the user/admin to install the named package.
- If this host should become a remote client, use the direct registration flow from Settings/API. Do not hand-write client config or tokens.

## Questions To Ask First

Ask these before making changes:

1. What repository URL and branch should I install?
2. What public or LAN URL should users and remote clients use for the backend, for example `http://host:8001`?
3. Should this machine also be registered as a remote client?

If the user does not know whether this machine should be a remote client, recommend **no** when Docker is running on the same host as the server. The server already creates a local client. Recommend **yes** only when they want this host to run the standalone client-agent path or when the server is installed elsewhere.

## Server Install With Docker

Check prerequisites:

```bash
docker --version
docker compose version
```

If Docker or Compose is missing and you have sudo/root permission, install them using the operating system's official package flow. If you do not have permission, stop and ask the user to install Docker Engine and Docker Compose v2.

Clone and configure:

```bash
git clone <repo-url> web_terminal_acp
cd web_terminal_acp
cp .env.example .env
```

Edit `.env` with the user's values:

- `WEB_TERMINAL_AUTH_SECRET`: set a strong secret unless the app is strictly local.
- `WORKSPACE_DIR`: host workspace path mounted as `/workspace`.
- `OPENAI_COMPAT_BASE_URL`, `OPENAI_COMPAT_API_KEY`, `OPENAI_COMPAT_MODEL`: set only if summaries are needed.
- `VITE_ENABLE_ONBOARDING`: set to `true` before `docker compose build` only when the user wants the new-user guide enabled. Leave it empty for the default no-guide UI.
- Agent config mounts: confirm before mounting credential directories such as `~/.claude` or `~/.codex`.

On Linux, check Elasticsearch's kernel requirement:

```bash
cat /proc/sys/vm/max_map_count
```

If it is below `262144` and you have permission:

```bash
sudo sysctl -w vm.max_map_count=262144
```

If you do not have permission, ask the user/admin to run that command before starting Elasticsearch.

Start the stack:

```bash
docker compose --profile build-base build backend-base
docker compose build
docker compose up -d --wait
```

Verify:

```bash
curl -fsS http://127.0.0.1:8001/healthz
docker compose ps
```

Tell the user the UI URL, usually:

```text
http://localhost:5173
```

## Optional: Register This Host As A Remote Client

Ask: "Do you want this host to install the standalone remote client-agent too?"

If the answer is no, stop this section.

If the answer is yes, first check dependencies on the host where the client-agent will run:

```bash
command -v bash
command -v tmux
command -v python3
python3 -m venv /tmp/web-terminal-venv-check
/tmp/web-terminal-venv-check/bin/python -m pip --version
rm -rf /tmp/web-terminal-venv-check
```

If anything fails, install the missing packages when permitted. On Debian/Ubuntu the common command is:

```bash
sudo apt-get update
sudo apt-get install -y bash tmux python3 python3-venv
```

If you cannot install packages, ask the user/admin to install `bash`, `tmux`, `python3`, and `python3-venv`, then rerun the check.

## Generate The Registration Key

Preferred interactive path:

1. Open the Web Terminal UI.
2. Open **Settings**.
3. Open **Client registration**.
4. Click **Generate one-time registration key**.
5. Use the generated script shown in the UI.

API path when the user explicitly wants agent automation:

```bash
curl -fsS \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <ui-session-token-if-auth-is-enabled>" \
  -d '{"label":"local remote client"}' \
  http://127.0.0.1:8001/api/clients/registration-keys
```

If auth is enabled, get the UI session token by logging in through `/api/auth/login` with the user's `WEB_TERMINAL_AUTH_SECRET`. Do not persist or print this token unnecessarily.

## Install Through The Server-Generated Script

Always fetch the script from the running server:

```bash
curl -fsSL http://127.0.0.1:8001/api/clients/register-script -o register-client-direct.sh
chmod +x register-client-direct.sh
WEB_TERMINAL_SERVER_URL=http://127.0.0.1:8001 \
WEB_TERMINAL_REGISTRATION_KEY=wtr_xxx \
WEB_TERMINAL_CLIENT_NAME="$(hostname)-remote" \
./register-client-direct.sh
```

Use the externally reachable backend URL for `WEB_TERMINAL_SERVER_URL` when the remote client is not on the same host. Do not use a Docker service hostname such as `backend`; the standalone client runs outside the Compose network.

The script will:

- Check required host dependencies.
- Exchange the one-time registration key for a client-specific token.
- Download the minimal client-agent Python bundle from the server response.
- Create `~/.web-terminal-acp/venv`.
- Install returned Python requirements.
- Start `python -m app.client_agent` inside tmux.

## Verify The Remote Client

On the client host:

```bash
tmux ls | grep web_terminal_acp_client
tail -n 100 ~/.web-terminal-acp/logs/client.log
```

From the server/API:

```bash
curl -fsS http://127.0.0.1:8001/api/clients
```

In the UI, confirm the client is **ONLINE**. Then create a terminal on that client and run:

```bash
echo shell-ok
```

If the user wants Codex, Claude Code, or Cursor on the remote client, verify those CLIs are installed and authenticated on the remote host:

```bash
command -v codex || true
command -v claude || true
command -v cursor || true
```

Install or authenticate those tools only with the user's explicit approval, because they may require credentials.

## Troubleshooting

- **Missing dependencies**: install the exact package named by the registration script. Without permission, ask the user/admin to install it.
- **Registration key invalid**: generate a new key. Keys are one-time use.
- **Client stays offline**: check `~/.web-terminal-acp/logs/client.log`, confirm the backend URL is reachable from the client host, and confirm firewalls allow outbound WebSocket traffic.
- **ImportError before connection**: the client bundle is incomplete for the current server version. Reinstall using a fresh script from the same running server.
- **Agent CLI not found**: install the CLI on the client host. The remote client adds `~/.web-terminal-acp/npm-global/bin` to managed shell `PATH`, which is suitable for user-local npm installs.
