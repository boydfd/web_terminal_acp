import { describe, expect, it } from "vitest";

import {
  buildProjectTopicSwitcherTree,
  findPathToSwitcherWindow,
  projectGroupLabel,
  projectPathFromRuntimeTags
} from "../src/terminalGrouping";
import type { TreeFolder } from "../src/types";

const sampleFolders: TreeFolder[] = [
  {
    id: "folder-1",
    name: "主题",
    path: "/主题",
    folders: [],
    windows: [
      {
        id: "w1",
        title: "Terminal A",
        status: "ACTIVE",
        runtime_tags: ["/workspace/project-a"],
        work_status: { state: "WORKING", label: "Working", color: "orange" },
        created_at: "2026-05-24T10:00:00Z"
      }
    ]
  },
  {
    id: "folder-2",
    name: "其他",
    path: "/其他",
    folders: [],
    windows: [
      {
        id: "w2",
        title: "Terminal B",
        status: "ACTIVE",
        runtime_tags: ["/workspace/project-b"],
        work_status: { state: "LONG_IDLE", label: "Idle", color: "gray" },
        created_at: "2026-05-24T11:00:00Z"
      }
    ]
  }
];

describe("terminalGrouping", () => {
  it("extracts absolute project path from runtime tags", () => {
    expect(projectPathFromRuntimeTags(["codex", "/workspace/demo"])).toBe("/workspace/demo");
    expect(projectPathFromRuntimeTags(["codex"])).toBe("/未指定");
  });

  it("uses project path until a summary display name exists", () => {
    const summaries = new Map([
      [
        "/workspace/project-a",
        {
          project_path: "/workspace/project-a",
          display_name: "终端编排",
          status: "SUCCEEDED",
          last_error: null,
          updated_at: "2026-05-24T12:00:00Z"
        }
      ]
    ]);

    expect(projectGroupLabel("/workspace/project-a", summaries)).toBe("终端编排");
    expect(projectGroupLabel("/workspace/project-b", summaries)).toBe("/workspace/project-b");
  });

  it("builds project-topic hierarchy", () => {
    const tree = buildProjectTopicSwitcherTree(sampleFolders, new Map(), "");
    expect(tree).toHaveLength(2);
    expect(tree[0]?.projectPath).toBe("/workspace/project-a");
    expect(tree[0]?.children[0]?.label).toBe("主题");
    expect(tree[0]?.children[0]?.children[0]?.type).toBe("window");
  });

  it("finds path to a window in project-topic tree", () => {
    const tree = buildProjectTopicSwitcherTree(sampleFolders, new Map(), "");
    expect(findPathToSwitcherWindow(tree, "w1")).toEqual([
      "project:/workspace/project-a",
      "project-topic:/workspace/project-a:topic:/主题",
      "window:w1"
    ]);
  });
});
