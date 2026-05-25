import type { ProjectSummary, TreeFolder, TreeWindow } from "./types";

export type TerminalGroupingMode = "project-topic" | "topic";

export type SwitcherWindowNode = {
  type: "window";
  key: string;
  window: TreeWindow;
  topicPath: string;
};

export type SwitcherGroupNode = {
  type: "group";
  key: string;
  label: string;
  count: number;
  children: SwitcherNode[];
  projectPath?: string;
};

export type SwitcherNode = SwitcherGroupNode | SwitcherWindowNode;

export type ProjectSummaryLookup = Map<string, ProjectSummary>;

const UNASSIGNED_PROJECT_PATH = "/未指定";

export function projectPathFromRuntimeTags(runtimeTags: string[]): string {
  for (const tag of runtimeTags) {
    if (tag.startsWith("/")) {
      return tag;
    }
  }

  return UNASSIGNED_PROJECT_PATH;
}

export function projectGroupLabel(projectPath: string, summaries: ProjectSummaryLookup): string {
  const summary = summaries.get(projectPath);
  if (summary?.display_name) {
    return summary.display_name;
  }

  return projectPath;
}

export function collectProjectPaths(folders: TreeFolder[]): string[] {
  const paths = new Set<string>();

  const visit = (folder: TreeFolder) => {
    for (const window of folder.windows) {
      paths.add(projectPathFromRuntimeTags(window.runtime_tags));
    }

    for (const child of folder.folders) {
      visit(child);
    }
  };

  for (const folder of folders) {
    visit(folder);
  }

  return Array.from(paths).sort((left, right) => left.localeCompare(right));
}

export function buildTerminalSwitcherTree(
  folders: TreeFolder[],
  mode: TerminalGroupingMode,
  summaries: ProjectSummaryLookup,
  query: string
): SwitcherNode[] {
  if (mode === "topic") {
    return buildTopicSwitcherTree(folders, query);
  }

  return buildProjectTopicSwitcherTree(folders, summaries, query);
}

export function buildTopicSwitcherTree(folders: TreeFolder[], query: string): SwitcherNode[] {
  const normalizedQuery = query.trim().toLocaleLowerCase();

  const convert = (folder: TreeFolder): SwitcherGroupNode | null => {
    const childGroups = folder.folders
      .map((child: TreeFolder) => convert(child))
      .filter((node): node is SwitcherGroupNode => node !== null);
    const windowNodes = folder.windows
      .filter((window) => matchesWindow(window, folder.path, normalizedQuery))
      .map((window): SwitcherWindowNode => ({
        type: "window",
        key: `window:${window.id}`,
        window,
        topicPath: folder.path
      }));
    const children: SwitcherNode[] = [...childGroups, ...windowNodes];
    const count = countWindows(children);

    if (count === 0) {
      return null;
    }

    return {
      type: "group",
      key: `topic:${folder.path}`,
      label: folder.name,
      count,
      children
    };
  };

  return folders.map(convert).filter((node): node is SwitcherGroupNode => node !== null);
}

export function buildProjectTopicSwitcherTree(
  folders: TreeFolder[],
  summaries: ProjectSummaryLookup,
  query: string
): SwitcherNode[] {
  const projectPaths = collectProjectPaths(folders);

  const nodes: SwitcherGroupNode[] = [];
  for (const projectPath of projectPaths) {
    const children = buildTopicSwitcherTreeForProject(folders, projectPath, query);
    const count = countWindows(children);
    if (count === 0) {
      continue;
    }

    nodes.push({
      type: "group",
      key: `project:${projectPath}`,
      label: projectGroupLabel(projectPath, summaries),
      projectPath,
      count,
      children
    });
  }

  return nodes;
}

function buildTopicSwitcherTreeForProject(
  folders: TreeFolder[],
  projectPath: string,
  query: string
): SwitcherNode[] {
  const normalizedQuery = query.trim().toLocaleLowerCase();

  const convert = (folder: TreeFolder): SwitcherGroupNode | null => {
    const childGroups = folder.folders
      .map((child: TreeFolder) => convert(child))
      .filter((node): node is SwitcherGroupNode => node !== null);
    const windowNodes = folder.windows
      .filter((window) => projectPathFromRuntimeTags(window.runtime_tags ?? []) === projectPath)
      .filter((window) => matchesWindow(window, folder.path, normalizedQuery))
      .map((window): SwitcherWindowNode => ({
        type: "window",
        key: `window:${window.id}`,
        window,
        topicPath: folder.path
      }));
    const children: SwitcherNode[] = [...childGroups, ...windowNodes];
    const count = countWindows(children);

    if (count === 0) {
      return null;
    }

    return {
      type: "group",
      key: `project-topic:${projectPath}:topic:${folder.path}`,
      label: folder.name,
      count,
      children
    };
  };

  return folders.map(convert).filter((node): node is SwitcherGroupNode => node !== null);
}

function matchesWindow(window: TreeWindow, topicPath: string, normalizedQuery: string): boolean {
  if (!normalizedQuery) {
    return true;
  }

  return [
    window.title,
    window.status,
    window.work_status?.label ?? "",
    topicPath,
    projectPathFromRuntimeTags(window.runtime_tags ?? []),
    ...(window.title_tags ?? []),
    ...(window.runtime_tags ?? [])
  ]
    .join(" ")
    .toLocaleLowerCase()
    .includes(normalizedQuery);
}

function countWindows(nodes: SwitcherNode[]): number {
  return nodes.reduce((total, node) => total + (node.type === "window" ? 1 : node.count), 0);
}

export function findPathToSwitcherWindow(nodes: SwitcherNode[], windowId: string): string[] {
  for (const node of nodes) {
    if (node.type === "window" && node.window.id === windowId) {
      return [node.key];
    }

    if (node.type === "group") {
      const childPath = findPathToSwitcherWindow(node.children, windowId);
      if (childPath.length > 0) {
        return [node.key, ...childPath];
      }
    }
  }

  return [];
}
