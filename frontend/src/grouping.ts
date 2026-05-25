import type { TreeFolder, TreeWindow } from "./types";

export type GroupingMode = "topic" | "time" | "time-topic" | "topic-time";

export type DisplayWindow = {
  type: "window";
  key: string;
  window: TreeWindow;
  topicPath: string;
};

export type DisplayGroup = {
  type: "group";
  key: string;
  label: string;
  count: number;
  children: DisplayNode[];
};

export type DisplayNode = DisplayGroup | DisplayWindow;

export type TreeWindowEntry = {
  window: TreeWindow;
  topicPath: string;
};

type TimeParts = {
  month: string;
  day: string;
};

export function buildDisplayTree(folders: TreeFolder[], mode: GroupingMode): DisplayNode[] {
  if (mode === "topic") {
    return buildTopicDisplayTree(folders);
  }

  if (mode === "time") {
    return buildTimeDisplayTree(flattenTreeWindows(folders));
  }

  if (mode === "time-topic") {
    return buildTimeTopicDisplayTree(folders);
  }

  return buildTopicTimeDisplayTree(folders);
}

export function flattenTreeWindows(folders: TreeFolder[]): TreeWindowEntry[] {
  const entries: TreeWindowEntry[] = [];

  const visit = (folder: TreeFolder) => {
    for (const window of folder.windows) {
      entries.push({ window, topicPath: folder.path });
    }

    for (const child of folder.folders) {
      visit(child);
    }
  };

  for (const folder of folders) {
    visit(folder);
  }

  return entries;
}

export function buildTopicDisplayTree(folders: TreeFolder[]): DisplayNode[] {
  return folders.map((folder) => topicFolderToDisplayNode(folder, "topic")).filter((node) => node.count > 0);
}

export function buildTimeDisplayTree(entries: TreeWindowEntry[]): DisplayNode[] {
  return groupEntriesByTime(entries, "time");
}

export function buildTimeTopicDisplayTree(folders: TreeFolder[]): DisplayNode[] {
  const entries = flattenTreeWindows(folders);
  const byMonth = new Map<string, Map<string, TreeWindowEntry[]>>();

  for (const entry of entries) {
    const { month, day } = timeParts(entry.window.created_at);
    const monthGroup = byMonth.get(month) ?? new Map<string, TreeWindowEntry[]>();
    const dayEntries = monthGroup.get(day) ?? [];
    dayEntries.push(entry);
    monthGroup.set(day, dayEntries);
    byMonth.set(month, monthGroup);
  }

  return sortedMapEntries(byMonth).map(([month, days]) => ({
    type: "group",
    key: `time-topic:${month}`,
    label: month,
    count: countEntriesInDayMap(days),
    children: sortedMapEntries(days).map(([day, dayEntries]) => ({
      type: "group",
      key: `time-topic:${month}:${day}`,
      label: day,
      count: dayEntries.length,
      children: buildTopicTreeForEntries(
        folders,
        new Set(dayEntries.map((entry) => entry.window.id)),
        `time-topic:${month}:${day}`
      )
    }))
  }));
}

export function buildTopicTimeDisplayTree(folders: TreeFolder[]): DisplayNode[] {
  const convert = (folder: TreeFolder): DisplayGroup | null => {
    const childGroups = folder.folders
      .map((child: TreeFolder) => convert(child))
      .filter((child): child is DisplayGroup => child !== null);
    const ownTimeGroups = groupEntriesByTime(
      folder.windows.map((window) => ({ window, topicPath: folder.path })),
      `topic-time:${folder.path}`
    );
    const children: DisplayNode[] = [...childGroups, ...ownTimeGroups];
    const count = countWindows(children);

    if (count === 0) {
      return null;
    }

    return {
      type: "group",
      key: `topic-time:${folder.path}`,
      label: folder.name,
      count,
      children
    };
  };

  return folders.map(convert).filter((node): node is DisplayGroup => node !== null);
}

export function findPathToWindow(nodes: DisplayNode[], windowId: string): string[] {
  for (const node of nodes) {
    if (node.type === "window" && node.window.id === windowId) {
      return [node.key];
    }

    if (node.type === "group") {
      const childPath = findPathToWindow(node.children, windowId);
      if (childPath.length > 0) {
        return [node.key, ...childPath];
      }
    }
  }

  return [];
}

function topicFolderToDisplayNode(folder: TreeFolder, keyPrefix: string): DisplayGroup {
  const childGroups = folder.folders
    .map((child: TreeFolder) => topicFolderToDisplayNode(child, keyPrefix))
    .filter((node) => node.count > 0);
  const windowNodes = windowsToDisplayNodes(folder.windows, folder.path, keyPrefix);
  const children: DisplayNode[] = [...childGroups, ...windowNodes];

  return {
    type: "group",
    key: `${keyPrefix}:topic:${folder.path}`,
    label: folder.name,
    count: countWindows(children),
    children
  };
}

function buildTopicTreeForEntries(folders: TreeFolder[], allowedWindowIds: Set<string>, keyPrefix: string): DisplayNode[] {
  const convert = (folder: TreeFolder): DisplayGroup | null => {
    const childGroups = folder.folders
      .map((child: TreeFolder) => convert(child))
      .filter((child): child is DisplayGroup => child !== null);
    const windowNodes = windowsToDisplayNodes(
      folder.windows.filter((window) => allowedWindowIds.has(window.id)),
      folder.path,
      keyPrefix
    );
    const children: DisplayNode[] = [...childGroups, ...windowNodes];
    const count = countWindows(children);

    if (count === 0) {
      return null;
    }

    return {
      type: "group",
      key: `${keyPrefix}:topic:${folder.path}`,
      label: folder.name,
      count,
      children
    };
  };

  return folders.map(convert).filter((node): node is DisplayGroup => node !== null);
}

function groupEntriesByTime(entries: TreeWindowEntry[], keyPrefix: string): DisplayNode[] {
  const byMonth = new Map<string, Map<string, TreeWindowEntry[]>>();

  for (const entry of entries) {
    const { month, day } = timeParts(entry.window.created_at);
    const monthGroup = byMonth.get(month) ?? new Map<string, TreeWindowEntry[]>();
    const dayEntries = monthGroup.get(day) ?? [];
    dayEntries.push(entry);
    monthGroup.set(day, dayEntries);
    byMonth.set(month, monthGroup);
  }

  return sortedMapEntries(byMonth).map(([month, days]) => ({
    type: "group",
    key: `${keyPrefix}:${month}`,
    label: month,
    count: countEntriesInDayMap(days),
    children: sortedMapEntries(days).map(([day, dayEntries]) => ({
      type: "group",
      key: `${keyPrefix}:${month}:${day}`,
      label: day,
      count: dayEntries.length,
      children: dayEntries.map((entry) => ({
        type: "window",
        key: `${keyPrefix}:${month}:${day}:window:${entry.window.id}`,
        window: entry.window,
        topicPath: entry.topicPath
      }))
    }))
  }));
}

function windowsToDisplayNodes(windows: TreeWindow[], topicPath: string, keyPrefix: string): DisplayWindow[] {
  return windows.map((window) => ({
    type: "window",
    key: `${keyPrefix}:window:${window.id}`,
    window,
    topicPath
  }));
}

function timeParts(value: string): TimeParts {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return { month: "unknown", day: "unknown" };
  }

  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  if (match) {
    const [, inputYear, inputMonth, inputDay] = match;
    if (!isValidDatePrefix(inputYear, inputMonth, inputDay)) {
      return { month: "unknown", day: "unknown" };
    }

    return { month: `${inputYear}-${inputMonth}`, day: `${inputMonth}-${inputDay}` };
  }

  const localYear = String(date.getFullYear()).padStart(4, "0");
  const localMonth = String(date.getMonth() + 1).padStart(2, "0");
  const localDay = String(date.getDate()).padStart(2, "0");

  return { month: `${localYear}-${localMonth}`, day: `${localMonth}-${localDay}` };
}

function isValidDatePrefix(year: string, month: string, day: string): boolean {
  const numericYear = Number(year);
  const numericMonth = Number(month);
  const numericDay = Number(day);

  if (numericMonth < 1 || numericMonth > 12 || numericDay < 1) {
    return false;
  }

  const daysInMonth = new Date(Date.UTC(numericYear, numericMonth, 0)).getUTCDate();
  return numericDay <= daysInMonth;
}

function countWindows(nodes: DisplayNode[]): number {
  return nodes.reduce((total, node) => total + (node.type === "window" ? 1 : node.count), 0);
}

function countEntriesInDayMap(days: Map<string, TreeWindowEntry[]>): number {
  let count = 0;

  for (const entries of days.values()) {
    count += entries.length;
  }

  return count;
}

function sortedMapEntries<T>(map: Map<string, T>): Array<[string, T]> {
  return Array.from(map.entries()).sort(([left], [right]) => left.localeCompare(right));
}
