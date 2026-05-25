---
name: web-terminal-git-worktree
description: Creates and registers an isolated git worktree for Web Terminal agent sessions. Use when developing in Web Terminal (WEB_TERMINAL_WINDOW_ID set), when the user requires git worktree isolation, or before running claude/codex/cursor/acpx in a managed terminal.
---

# Web Terminal Git Worktree

Web Terminal only tracks Git state for **agent-created linked worktrees**. Follow this skill in every agent session inside a Web Terminal shell.

## Prerequisites

- `WEB_TERMINAL_WINDOW_ID` is set (Web Terminal managed shell).
- Current directory is a **git repository** (main checkout to start, or an existing linked worktree to re-register).

## Workflow

Copy and track progress:

```
- [ ] Step 1: init-worktree (create + cd + register)
- [ ] Step 2: do all edits and git operations inside the worktree
- [ ] Step 3: git commit in the worktree when done
- [ ] Step 4: remove-worktree after merge (optional, from main checkout)
```

### Step 1: Create worktree (required)

From the **main repository checkout** (`.git` is a directory), run:

```bash
bash .cursor/skills/web-terminal-git-worktree/scripts/init-worktree.sh
```

Optional branch suffix:

```bash
bash .cursor/skills/web-terminal-git-worktree/scripts/init-worktree.sh my-feature
# branch: agent/my-feature
```

This script:

1. Creates `.web-terminal-acp/worktrees/$WEB_TERMINAL_WINDOW_ID` with branch `agent/<suffix>`.
2. `cd` into the worktree.
3. Registers the worktree with Web Terminal (OSC marker).

**Do not** develop in the main checkout. **Do not** ask the user to run `git worktree add` manually.

### Step 2: Already in a worktree?

If you are already in a linked worktree (`.git` is a file) but Git UI is missing, register only:

```bash
bash .cursor/skills/web-terminal-git-worktree/scripts/register-worktree.sh
```

### Step 3: Develop and commit

- All file edits and `git add` / `git commit` happen **inside the worktree**.
- Web Terminal does not commit for you. Uncommitted changes show a red **G** on the terminal title until you commit.

### Step 4: Cleanup (after task / merge)

From the **main checkout**:

```bash
bash .cursor/skills/web-terminal-git-worktree/scripts/remove-worktree.sh
```

## Rules

| Rule | Detail |
|------|--------|
| One terminal, one worktree | Use the path under `.web-terminal-acp/worktrees/$WEB_TERMINAL_WINDOW_ID` |
| Register after `cd` | `init-worktree.sh` registers automatically; never skip registration |
| No main-checkout edits | `.git` as a **directory** = main checkout — not tracked |
| Read-only platform git | Web Terminal only snapshots; it never runs commit/checkout for you |

## Scripts

| Script | Purpose |
|--------|---------|
| [scripts/init-worktree.sh](scripts/init-worktree.sh) | Create worktree, `cd`, register |
| [scripts/register-worktree.sh](scripts/register-worktree.sh) | Register current linked worktree |
| [scripts/remove-worktree.sh](scripts/remove-worktree.sh) | Remove this terminal's worktree |

Make scripts executable once per clone:

```bash
chmod +x .cursor/skills/web-terminal-git-worktree/scripts/*.sh
```

## Claude EnterWorktree

If using Claude's built-in EnterWorktree instead of `init-worktree.sh`, you **must** still `cd` into the linked worktree and run `register-worktree.sh` so Web Terminal can bind the session.
