[English](README.md)

# Web Terminal ACP

Web Terminal ACP 是一个面向 shell 与 AI 编程 Agent 工作流的浏览器控制面。它提供基于 tmux 的浏览器终端、可长期留存的会话目录树、可搜索的终端与 Agent 历史，以及可选的 remote client，让其他机器通过 WebSocket 接回控制面。

它适合希望保留 Agent 工作记录、同时继续使用现有本地工具的个人或团队：shell、tmux、Claude Code、Codex、Cursor CLI，以及 OpenAI 兼容模型服务。

## 功能概览

- **浏览器终端工作台**：xterm.js 终端窗格，底层由 tmux 承载，可重连。
- **多客户端运行时**：可直接使用 server 所在主机，也可注册其他机器为 remote client。
- **Agent 工作记录**：采集 Claude Code JSONL、Codex trace、Cursor adapter 事件、终端输出与摘要。
- **搜索与摘要**：Elasticsearch 索引终端输出和 Agent 事件；OpenAI 兼容 API 可生成标题、标签、摘要和目录建议。
- **Agent worktree 追踪**：Web Terminal 管理的 shell 会设置 `WEB_TERMINAL_WINDOW_ID`，便于编码 Agent 在 linked git worktree 中工作，并把状态显示在 UI 里。
- **remote client 直接注册**：在 Settings 生成一次性 token，目标机器从 server 拉取安装脚本和 client 包完成安装。

## 架构

| 组件 | 作用 |
| --- | --- |
| React + Vite | 浏览器 UI、终端、设置、搜索、client 注册 |
| FastAPI | REST API、WebSocket、tmux 编排、鉴权、后台任务 |
| PostgreSQL | 目录、窗口、client、事件、会话、任务 |
| Elasticsearch | 终端块、摘要和 Agent 事件全文检索 |
| Redis | UI 轮询和运行时路径的快速状态/缓存 |
| tmux | 本地与 remote shell 的进程宿主 |
| client-agent | 可选的远程 Python 守护进程 |

## 环境要求

Web Terminal server 需要：

- Docker Engine 与 Docker Compose v2。
- Linux 是主要部署目标。macOS 可用于开发，但宿主 shell 集成会有差异。
- 至少 4 GB 可用内存。
- Linux 上 Elasticsearch 通常需要 `vm.max_map_count >= 262144`。
- 可选：OpenAI 兼容 API，用于生成摘要。
- 可选：在需要运行 Agent 的机器上安装并登录 Claude Code、Codex、Cursor CLI 等工具。

直接注册 remote client 需要：

- `bash`
- `tmux`
- `python3`
- Python venv/ensurepip 支持。Debian/Ubuntu 通常是 `python3-venv`。
- remote host 能访问 Web Terminal backend URL。
- 可选：如果要在 remote client 上启动 Codex / Claude Code / Cursor CLI，需要在那台机器上安装这些 CLI。

直接注册脚本会先检查这些依赖。如果缺包且当前用户没有安装权限，必须让机器 owner/admin 安装；不要绕过依赖检查做半安装。

## Docker 快速开始

```bash
git clone https://github.com/boydfd/web_terminal_acp.git
cd web_terminal_acp
cp .env.example .env
```

启动前编辑 `.env`：

- 如果 UI/API 不是只有你能访问，设置强 `WEB_TERMINAL_AUTH_SECRET`。
- 设置 `WORKSPACE_DIR`，它会作为 `/workspace` 挂进 Web Terminal。
- 如果需要摘要，设置 `OPENAI_COMPAT_BASE_URL`、`OPENAI_COMPAT_API_KEY`、`OPENAI_COMPAT_MODEL`。
- 检查 Agent 配置目录挂载项，例如 `~/.claude`、`~/.codex`、`~/.agents`、`~/.acpx`，这些可能把宿主机凭据暴露给 backend 容器。

构建并启动：

```bash
docker compose --profile build-base build backend-base
docker compose build
docker compose up -d --wait
```

打开：

- UI: http://localhost:5173
- API health: http://localhost:8001/healthz

如果 Linux 上 Elasticsearch 启动失败：

```bash
sudo sysctl -w vm.max_map_count=262144
docker compose up -d --wait elasticsearch
```

## 配置

重要 `.env` 变量：

| 变量 | 用途 | 默认 |
| --- | --- | --- |
| `WEB_TERMINAL_AUTH_SECRET` | 非空时启用 UI/API 内置登录 | 空 |
| `WEB_TERMINAL_AUTH_SESSION_TTL_SECONDS` | 登录态有效期 | `604800` |
| `BACKEND_PUBLISHED_PORT` | backend 宿主机端口 | `8001` |
| `WORKSPACE_DIR` | 挂载到 backend `/workspace` 的宿主机路径 | `~/workspace` |
| `CLAUDE_PROJECTS_DIR` | Claude Code JSONL 采集目录 | `~/.claude/projects` |
| `DEFAULT_SHELL` | 新终端 shell；`auto` 使用运行时用户登录 shell | `auto` |
| `OPENAI_COMPAT_BASE_URL` | OpenAI 兼容 API 地址 | `http://127.0.0.1:11434/v1` |
| `OPENAI_COMPAT_API_KEY` | 摘要生成 API key | `dev-local-key` |
| `OPENAI_COMPAT_MODEL` | 摘要模型 | `local-summarizer` |
| `VITE_API_BASE` | 前端构建时备用 API origin；Docker nginx 代理模式保持为空 | 空 |

不要提交 `.env`。对 localhost 之外开放之前，请设置 `WEB_TERMINAL_AUTH_SECRET`、使用强数据库密码，并在 UI/backend 前放 TLS 反向代理。

## 桌面应用构建

可在 frontend 项目中构建未签名的 Electron 包：

```bash
cd frontend
npm run electron:dist:win:portable
npm run electron:dist:mac:zip
```

桌面应用默认连接 `http://127.0.0.1:8001`。如需连接其他 server，启动后打开 **Settings**，把 **后端地址** 设置为可访问的 backend URL。该运行时设置会保存在桌面应用本地，因此发布包不需要为每个部署环境烘焙不同的 `VITE_API_BASE`。

## 安装 Remote Client

remote client 让另一台机器承载 shell 和 Agent CLI，而 Web Terminal 继续作为控制面。有两种方式。

### 方式 A：直接注册

适合目标机器主动安装自己，不希望 server SSH 登录目标机器的场景。

1. 启动 Web Terminal server。
2. 打开 UI，进入 **Settings** -> **Client registration**。
3. 生成一次性注册 key。
4. 在目标机器运行 Settings 中显示的脚本。

命令形态如下：

```bash
curl -fsSL http://your-server:8001/api/clients/register-script -o register-client-direct.sh
chmod +x register-client-direct.sh
WEB_TERMINAL_SERVER_URL=http://your-server:8001 \
WEB_TERMINAL_REGISTRATION_KEY=wtr_xxx \
./register-client-direct.sh
```

脚本会：

- 检查 `bash`、`tmux`、`python3` 和 Python venv/pip 支持。
- 使用一次性 key 调用 `POST /api/clients/register`。
- 获取生成的 client token、`config.json`、requirements 和最小 Python client 包。
- 默认安装到 `~/.web-terminal-acp`。
- 创建 Python venv 并安装返回的 requirements。
- 在 tmux session 中启动 `python -m app.client_agent`。

可选参数：

```bash
./register-client-direct.sh \
  --server-url http://your-server:8001 \
  --registration-key wtr_xxx \
  --name build-host-1 \
  --install-path ~/.web-terminal-acp
```

如果脚本提示缺依赖，先安装对应包。Debian/Ubuntu 示例：

```bash
sudo apt-get update
sudo apt-get install -y bash tmux python3 python3-venv
```

如果没有安装权限，请让管理员安装后再重新运行注册命令。无法创建 venv 或运行 tmux 的 remote client 不算安装成功。

### 方式 B：SSH Bootstrap

适合允许 Web Terminal server SSH 到目标机器的场景。

1. 在 UI 打开 **Clients** -> **Bootstrap remote client**。
2. 填写 SSH host、user、port、private key、可选 passphrase、安装路径和 server URL。
3. backend 通过 SSH 检查依赖、写入 client config/bundle、安装 requirements，并启动 remote client daemon。

同样需要先满足 `tmux`、`python3` 和 venv 支持。

### 验证 Remote Client

在 remote host 上：

```bash
tmux ls | grep web_terminal_acp_client
tail -n 100 ~/.web-terminal-acp/logs/client.log
```

UI 中该 client 应显示为 **ONLINE**。如果一直 offline，检查 remote host 是否能访问 backend URL，并确认该 URL 不是只在 Docker Compose 网络内可见的 hostname。

## 给 Agent 的安装手册

如果你要让 AI agent 安装或操作这个项目，把 [AGENT_README.md](AGENT_README.md) 发给它。那个文档是交互式 runbook：会要求 agent 先确认 Docker，询问是否要把本机也安装为 remote client，通过 UI/API 生成注册 key，从 server 拉取安装脚本，并在没有权限安装依赖时停下来让人处理。

如果 agent 要修改本仓库代码，请看 [AGENTS.md](AGENTS.md)，里面有项目级开发规则和版本要求。

## 本地开发

启动共享服务：

```bash
make services-up
```

启动 backend：

```bash
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

启动 frontend：

```bash
cd frontend
npm install
npm run dev -- --host 127.0.0.1
```

验证：

```bash
make backend-test
make frontend-build
```

## 项目结构

```text
web_terminal_acp/
├── backend/              # FastAPI app、client-agent、migrations、tests
├── frontend/             # React、Vite、xterm.js、Electron 支持
├── scripts/              # 构建/发布/辅助脚本
├── docker-compose.yml    # Docker 部署栈
├── Makefile              # 本地服务/测试/部署目标
├── AGENT_README.md       # 面向安装 agent 的操作手册
└── AGENTS.md             # 贡献者/开发 agent 规则
```

## 版本管理

client 协议和 UI 版本源保持同步：

- `backend/app/version.py`
- `frontend/package.json`
- `frontend/package-lock.json`

遵循 SemVer：

- `PATCH`：兼容修复和纯文档发布更新。
- `MINOR`：兼容功能或行为新增。
- `MAJOR`：不兼容协议、API、存储或部署变更。

## 许可证

MIT。见 [LICENSE](LICENSE)。
