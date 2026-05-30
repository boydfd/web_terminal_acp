import { describe, expect, it } from "vitest";

import { DEFAULT_WORK_STATUS, mergeTreeWithActivity, windowActivityMap } from "../src/terminalTree";
import type { TreeFolderCore } from "../src/types";

const sampleTree: TreeFolderCore[] = [
  {
    id: "folder-1",
    name: "Project",
    path: "/project",
    folders: [],
    windows: [
      {
        id: "window-1",
        title: "Terminal",
        status: "ACTIVE",
        created_at: "2026-05-24T00:00:00Z"
      }
    ]
  }
];

describe("terminalTree", () => {
  it("merges activity into tree windows", () => {
    const gitWorktree = {
      worktree_root: "/repo/.worktrees/feature",
      main_repo_root: "/repo",
      branch: "agent/feature",
      pending_commit: true
    };
    const activity = windowActivityMap({
      windows: [
        {
          window_id: "window-1",
          work_status: { state: "WORKING", label: "Working", color: "orange" },
          runtime_tags: ["/workspace/project"],
          last_agent_task_completed_at: "2026-05-24T01:00:00Z",
          last_agent_task_status: "FINISHED",
          last_agent_task_status_at: "2026-05-24T01:00:00Z",
          git_worktree: gitWorktree
        }
      ]
    });

    const merged = mergeTreeWithActivity(sampleTree, activity);
    expect(merged?.[0]?.windows[0]?.work_status.state).toBe("WORKING");
    expect(merged?.[0]?.windows[0]?.runtime_tags).toEqual(["/workspace/project"]);
    expect(merged?.[0]?.windows[0]?.last_agent_task_completed_at).toBe("2026-05-24T01:00:00Z");
    expect(merged?.[0]?.windows[0]?.last_agent_task_status).toBe("FINISHED");
    expect(merged?.[0]?.windows[0]?.last_agent_task_status_at).toBe("2026-05-24T01:00:00Z");
    expect(merged?.[0]?.windows[0]?.git_worktree).toEqual(gitWorktree);
  });

  it("uses defaults when activity is missing", () => {
    const merged = mergeTreeWithActivity(sampleTree, new Map());
    expect(merged?.[0]?.windows[0]?.work_status).toEqual(DEFAULT_WORK_STATUS);
    expect(merged?.[0]?.windows[0]?.runtime_tags).toEqual([]);
    expect(merged?.[0]?.windows[0]?.git_worktree).toBeNull();
  });
});
