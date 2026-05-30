# CLAUDE.md

## Client Versioning

- Any client-side change must include a version bump in the same change set.
- Use Semantic Versioning (`MAJOR.MINOR.PATCH`) as the default convention.
- Increment `MAJOR` for incompatible protocol, API, storage, or deployment changes that require coordinated upgrades.
- Increment `MINOR` for backward-compatible client features or behavior additions.
- Increment `PATCH` for backward-compatible bug fixes, small UI adjustments, refactors, or internal-only client changes.
- Keep all project version sources that represent the client in sync, such as `backend/app/version.py` and `frontend/package.json` when applicable.

## Web Terminal Agent 开发

在 Web Terminal 中开发时，必须使用 skill **`web-terminal-git-worktree`**（见 [AGENTS.md](./AGENTS.md)）。

## Remote Client Bundle

- Remote client bootstrap/self-update 只上传 `backend/app/services/bootstrap/installer.py::client_app_file_contents()` 中列出的精简包，不会自动包含整个 backend。
- `backend/app/client_agent/**` 新增任何启动或运行时 `app.*` import 时，同步更新精简包清单；remote client 在 WebSocket hello 前退出时，优先查远端 `~/.web-terminal-acp/logs/client.log` 的 `ImportError` / `ModuleNotFoundError`。
- 包清单变更必须有隔离包导入测试覆盖，例如确认精简包可单独 import `app.client_agent.runner`。
- 已离线且启动不起来的 remote client 无法 self-update，需要重新 Bootstrap remote client。

## Web Terminal 性能优先级

Web Terminal 的性能优化和回归判断必须按以下优先级排序：

1. 用户针对 terminal 的输入输出显示是最高优先级。用户输入必须瞬间反应，屏幕显示也必须瞬间反应；这是最终最核心的体验部分，必须有足够的自动化测试和回归覆盖。
2. 各种状态展示是第二优先级。
3. Agent record 和命令历史是第三优先级。
4. Git worktree 状态是第四优先级。

当这些目标发生冲突时，优先保护第一优先级的 terminal 输入、输出和屏幕显示延迟，不允许为了状态、agent record、命令历史或 git worktree 状态牺牲第一优先级体验。
