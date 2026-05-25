# AGENTS.md

## Client Versioning

- Any client-side change must include a version bump in the same change set.
- Use Semantic Versioning (`MAJOR.MINOR.PATCH`) as the default convention.
- Increment `MAJOR` for incompatible protocol, API, storage, or deployment changes that require coordinated upgrades.
- Increment `MINOR` for backward-compatible client features or behavior additions.
- Increment `PATCH` for backward-compatible bug fixes, small UI adjustments, refactors, or internal-only client changes.
- Keep all project version sources that represent the client in sync, such as `backend/app/version.py` and `frontend/package.json` when applicable.

## Web Terminal Agent 开发

在 Web Terminal 管理 shell（已设置 `WEB_TERMINAL_WINDOW_ID`）中跑 agent 时，**必须先读取并严格遵循**项目 skill：

**`.cursor/skills/web-terminal-git-worktree`**（`web-terminal-git-worktree`）

禁止在主仓库 checkout 直接改代码；禁止让用户代替 agent 手工 `git worktree add`。worktree 创建、进入、注册与清理均按该 skill 中的脚本与流程执行。
