import type { TreeFolder, TreeFolderCore, TreeWindow, WindowActivity, WorkStatus } from "./types";

export const DEFAULT_WORK_STATUS: WorkStatus = {
  state: "LONG_IDLE",
  label: "长时间没有工作了",
  color: "gray"
};

export const DEFAULT_WINDOW_ACTIVITY: WindowActivity = {
  work_status: DEFAULT_WORK_STATUS,
  runtime_tags: []
};

export function windowActivityMap(
  activity: { windows: WindowActivityRecord[] } | undefined
): Map<string, WindowActivity> {
  const map = new Map<string, WindowActivity>();
  if (!activity) {
    return map;
  }

  for (const item of activity.windows) {
    map.set(item.window_id, {
      work_status: item.work_status,
      runtime_tags: item.runtime_tags,
      last_agent_task_completed_at: item.last_agent_task_completed_at ?? null,
      last_agent_task_status: item.last_agent_task_status ?? null,
      last_agent_task_status_at: item.last_agent_task_status_at ?? null,
      git_worktree: item.git_worktree ?? null
    });
  }
  return map;
}

export type WindowActivityRecord = {
  window_id: string;
  work_status: WorkStatus;
  runtime_tags: string[];
  last_agent_task_completed_at?: string | null;
  last_agent_task_status?: "FINISHED" | "ABORTED" | null;
  last_agent_task_status_at?: string | null;
  git_worktree?: import("./types").GitWorktreeActivity | null;
};

export function mergeTreeWithActivity(
  folders: TreeFolderCore[] | undefined,
  activityByWindowId: Map<string, WindowActivity>
): TreeFolder[] | undefined {
  if (!folders) {
    return undefined;
  }

  const mergeWindow = (window: TreeFolderCore["windows"][number]): TreeWindow => {
    const activity = activityByWindowId.get(window.id) ?? DEFAULT_WINDOW_ACTIVITY;
    return {
      ...window,
      work_status: activity.work_status,
      runtime_tags: activity.runtime_tags,
      last_agent_task_completed_at: activity.last_agent_task_completed_at ?? null,
      last_agent_task_status: activity.last_agent_task_status ?? null,
      last_agent_task_status_at: activity.last_agent_task_status_at ?? null,
      git_worktree: activity.git_worktree ?? null
    };
  };

  const mergeFolder = (folder: TreeFolderCore): TreeFolder => ({
    ...folder,
    folders: folder.folders.map(mergeFolder),
    windows: folder.windows.map(mergeWindow)
  });

  return folders.map(mergeFolder);
}

export function activityHasWorkingTerminal(
  activity: { windows: WindowActivityRecord[] } | undefined
): boolean {
  if (!activity) {
    return false;
  }

  return activity.windows.some((window) => window.work_status.state === "WORKING");
}
