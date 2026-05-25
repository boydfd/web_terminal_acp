import { useEffect, useState, type ReactNode } from "react";

import type { AgentChatRecord, AgentEventProjection, AgentRecord, AgentRecordEvent, AgentSession } from "../types";

type EventTone =
  | "base-instructions"
  | "system"
  | "developer"
  | "context"
  | "user"
  | "user-input"
  | "agent"
  | "reasoning"
  | "tool-call"
  | "tool-result"
  | "subagent"
  | "lifecycle"
  | "event";
type BodyFormat = "markdown" | "json";
type EventView = { tone: EventTone; label: string; body: string; subtype: string | null; bodyFormat?: BodyFormat };
type AgentNode = { id: string; label: string; meta: string; children: AgentNode[] };
export type AgentRecordDisplayMode = "chat" | "detail";
type Props = {
  mode: AgentRecordDisplayMode;
  chatRecord: AgentChatRecord | null;
  detailRecord: AgentRecord | null;
  sessions: AgentSession[];
  isLoading?: boolean;
  isError?: boolean;
  isFetching?: boolean;
  expandSignal?: number;
  onModeChange: (mode: AgentRecordDisplayMode) => void;
  onExpandedChange?: (expanded: boolean) => void;
  onSessionChange?: (sessionId: string) => void;
  onPreviousPage?: () => void;
  onNextPage?: () => void;
};
type PageInfo = {
  total: number;
  limit: number;
  offset: number;
  count: number;
  hasMore: boolean;
  noun: string;
};

const EVENT_LABELS: Record<EventTone, string> = {
  "base-instructions": "Base instructions",
  system: "System message",
  developer: "Developer instructions",
  context: "Context",
  user: "User message",
  "user-input": "User input",
  agent: "Agent response",
  reasoning: "Agent reasoning",
  "tool-call": "Tool call",
  "tool-result": "Tool response",
  subagent: "Subagent call",
  lifecycle: "Lifecycle",
  event: "Event"
};

function json(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

function jsonMarkdown(value: unknown): string {
  return `\`\`\`json\n${json(value)}\n\`\`\``;
}

function eventToneClass(tone: string): EventTone {
  return EVENT_LABELS[tone as EventTone] ? tone as EventTone : "event";
}

function projectionView(projection: AgentEventProjection): EventView {
  return {
    tone: eventToneClass(projection.tone),
    label: projection.label,
    body: projection.body,
    subtype: projection.subtype,
    bodyFormat: projection.body_format
  };
}

function eventView(event: AgentRecordEvent): EventView {
  if (event.projection) return projectionView(event.projection);
  return {
    tone: "event",
    label: EVENT_LABELS.event,
    body: jsonMarkdown(event.payload_json),
    bodyFormat: "json",
    subtype: event.kind
  };
}

function isBlockStart(line: string): boolean {
  return /^(```|#{1,6}\s|[-*+]\s+|\d+[.)]\s+|>\s?)/.test(line);
}

function safeHref(value: string): string | null {
  return /^(https?:|mailto:)/i.test(value) ? value : null;
}

function inlineMarkdown(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*|\[[^\]]+\]\([^)]+\))/g;
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    const value = match[0];
    const index = match.index ?? 0;
    if (index > cursor) nodes.push(text.slice(cursor, index));
    const key = `${keyPrefix}-${index}`;
    if (value.startsWith("`")) {
      nodes.push(<code key={key}>{value.slice(1, -1)}</code>);
    } else if (value.startsWith("**")) {
      nodes.push(<strong key={key}>{inlineMarkdown(value.slice(2, -2), `${key}-strong`)}</strong>);
    } else if (value.startsWith("*")) {
      nodes.push(<em key={key}>{inlineMarkdown(value.slice(1, -1), `${key}-em`)}</em>);
    } else {
      const link = value.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      const href = link ? safeHref(link[2]) : null;
      nodes.push(href ? <a key={key} href={href} target="_blank" rel="noreferrer">{link?.[1]}</a> : value);
    }
    cursor = index + value.length;
  }
  if (cursor < text.length) nodes.push(text.slice(cursor));
  return nodes;
}

function inlineWithBreaks(text: string, keyPrefix: string): ReactNode[] {
  return text.split("\n").flatMap((line, index) => {
    const nodes = inlineMarkdown(line, `${keyPrefix}-${index}`);
    return index === 0 ? nodes : [<br key={`${keyPrefix}-br-${index}`} />, ...nodes];
  });
}

function MarkdownText({ text }: { text: string }) {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const blocksOut: ReactNode[] = [];
  let index = 0;
  let key = 0;

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }

    const fence = line.match(/^```(\w+)?\s*$/);
    if (fence) {
      const lang = fence[1];
      const code: string[] = [];
      index += 1;
      while (index < lines.length && !lines[index].startsWith("```")) {
        code.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocksOut.push(<pre key={key++}><code className={lang ? `language-${lang}` : undefined}>{code.join("\n")}</code></pre>);
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length;
      const children = inlineMarkdown(heading[2], `h-${key}`);
      const Tag = `h${level}` as keyof JSX.IntrinsicElements;
      blocksOut.push(<Tag key={key++}>{children}</Tag>);
      index += 1;
      continue;
    }

    const list = line.match(/^([-*+]|\d+[.)])\s+(.+)$/);
    if (list) {
      const ordered = /^\d/.test(list[1]);
      const items: string[] = [];
      while (index < lines.length) {
        const item = lines[index].match(/^([-*+]|\d+[.)])\s+(.+)$/);
        if (!item || /^\d/.test(item[1]) !== ordered) break;
        items.push(item[2]);
        index += 1;
      }
      const children = items.map((item, itemIndex) => <li key={itemIndex}>{inlineWithBreaks(item, `li-${key}-${itemIndex}`)}</li>);
      blocksOut.push(ordered ? <ol key={key++}>{children}</ol> : <ul key={key++}>{children}</ul>);
      continue;
    }

    if (/^>\s?/.test(line)) {
      const quote: string[] = [];
      while (index < lines.length && /^>\s?/.test(lines[index])) {
        quote.push(lines[index].replace(/^>\s?/, ""));
        index += 1;
      }
      blocksOut.push(<blockquote key={key++}>{inlineWithBreaks(quote.join("\n"), `quote-${key}`)}</blockquote>);
      continue;
    }

    const paragraph: string[] = [line];
    index += 1;
    while (index < lines.length && lines[index].trim() && !isBlockStart(lines[index])) {
      paragraph.push(lines[index]);
      index += 1;
    }
    blocksOut.push(<p key={key++}>{inlineWithBreaks(paragraph.join("\n"), `p-${key}`)}</p>);
  }

  return <div className="agent-event-markdown">{blocksOut}</div>;
}

function formatDateTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function tail(value: string | null): string | null {
  if (value === null) return null;
  const parts = value.split("/").filter(Boolean);
  return parts.length > 0 ? parts[parts.length - 1] : value;
}

function sessionLabel(session: AgentSession): string {
  return session.title ?? tail(session.source_path) ?? session.source_id;
}

function sessionTimestamp(session: AgentSession): number {
  const updated = Date.parse(session.updated_at);
  if (!Number.isNaN(updated)) {
    return updated;
  }
  const created = Date.parse(session.created_at);
  return Number.isNaN(created) ? 0 : created;
}

export function sortSessionsNewestFirst(sessions: AgentSession[]): AgentSession[] {
  return [...sessions].sort((left, right) => {
    const delta = sessionTimestamp(right) - sessionTimestamp(left);
    if (delta !== 0) {
      return delta;
    }
    return right.id.localeCompare(left.id);
  });
}

export function pickLatestSessionId(sessions: AgentSession[]): string | null {
  return sortSessionsNewestFirst(sessions)[0]?.id ?? null;
}

function filterDetailRecordBySession(record: AgentRecord, sessionId: string | null): AgentRecord {
  if (sessionId === null) {
    return record;
  }
  return {
    ...record,
    sessions: record.sessions.filter((session) => session.id === sessionId),
    events: record.events.filter((event) => event.ai_session_id === sessionId)
  };
}

function filterChatRecordBySession(record: AgentChatRecord, sessionId: string | null): AgentChatRecord {
  if (sessionId === null) {
    return record;
  }
  const messages = record.messages.filter((message) => message.ai_session_id === sessionId);
  return {
    ...record,
    messages,
    messages_total: messages.length,
    messages_offset: 0,
    messages_has_more: false,
    messages_limit: Math.max(record.messages_limit, messages.length)
  };
}

function parentSourceFromPath(sourcePath: string | null): string | null {
  return sourcePath?.match(/\/([^/]+)\/subagents\/[^/]+\.jsonl$/)?.[1] ?? null;
}

function buildAgentTree(record: AgentRecord): AgentNode[] {
  const nodes = new Map<string, AgentNode>();
  const sourceToNode = new Map<string, AgentNode>();
  const roots: AgentNode[] = [];
  for (const session of record.sessions) {
    const node = { id: session.id, label: sessionLabel(session), meta: `${session.provider} · ${session.source_id}`, children: [] };
    nodes.set(session.id, node);
    sourceToNode.set(session.source_id, node);
  }
  for (const session of record.sessions) {
    const node = nodes.get(session.id);
    const parent = sourceToNode.get(parentSourceFromPath(session.source_path) ?? "");
    if (!node) continue;
    if (parent && parent.id !== node.id) parent.children.push(node);
    else roots.push(node);
  }
  for (const session of record.sessions) {
    const hasSidechain = record.events.some((event) => event.ai_session_id === session.id && event.payload_json.isSidechain === true);
    const node = nodes.get(session.id);
    if (hasSidechain && node) node.children.push({ id: `${session.id}:sidechain`, label: "Sidechain / subagent", meta: "Claude sidechain events", children: [] });
  }
  if (roots.length === 0 && record.events.length > 0) roots.push({ id: "events", label: "Unlinked agent events", meta: `${record.events.length} events`, children: [] });
  return roots;
}

function AgentTree({ nodes }: { nodes: AgentNode[] }) {
  return (
    <ul className="agent-tree">
      {nodes.map((node) => (
        <li key={node.id}>
          <div><strong>{node.label}</strong><small>{node.meta}</small></div>
          {node.children.length > 0 && <AgentTree nodes={node.children} />}
        </li>
      ))}
    </ul>
  );
}

function AgentChatContent({ record }: { record: AgentChatRecord }) {
  if (record.messages_total === 0) {
    return <p className="muted">No agent record captured yet.</p>;
  }
  if (record.messages.length === 0) {
    return <p className="muted">No user input or agent response captured on this page.</p>;
  }

  return (
    <div className="agent-chat-events">
      {record.messages.map((message) => {
        const speaker = message.role;
        return (
          <article key={message.id} className={`agent-chat-message agent-chat-message-${speaker}`}>
            <header>
              <span>{speaker === "agent" ? "Agent" : "User"}</span>
              <time dateTime={message.created_at}>{formatDateTime(message.created_at)}</time>
            </header>
            <small>{message.source_type} · {message.source_id}</small>
            {message.body_format === "json"
              ? <pre className="agent-event-json">{message.body}</pre>
              : <MarkdownText text={message.body} />}
          </article>
        );
      })}
    </div>
  );
}

function AgentRecordContent({ record }: { record: AgentRecord }) {
  if (record.sessions.length === 0 && record.events.length === 0) {
    return <p className="muted">No agent record captured yet.</p>;
  }
  const sessions = new Map(record.sessions.map((session) => [session.id, session]));

  return (
    <>
      <h4>Agents</h4>
      <AgentTree nodes={buildAgentTree(record)} />
      <h4>Record Events</h4>
      <div className="agent-events">
        {record.events.map((event) => {
          const view = eventView(event);
          const session = event.ai_session_id ? sessions.get(event.ai_session_id) : undefined;
          return (
            <article key={event.id} className={`agent-event agent-event-${view.tone}`}>
              <header>
                <div className="agent-event-title">
                  <span>{view.label}</span>
                  {view.subtype && <code>{view.subtype}</code>}
                </div>
                <time dateTime={event.created_at}>{formatDateTime(event.created_at)}</time>
              </header>
              <small>{session ? `${session.provider} · ${sessionLabel(session)}` : `${event.source_type} · ${event.source_id}`}</small>
              {view.bodyFormat === "json"
                ? <pre className="agent-event-json">{view.body}</pre>
                : <MarkdownText text={view.body} />}
              <details className="agent-event-raw"><summary>Raw event</summary><pre>{json(event)}</pre></details>
            </article>
          );
        })}
      </div>
    </>
  );
}

function AgentRecordModeToggle({
  mode,
  onModeChange,
  disabled = false
}: {
  mode: AgentRecordDisplayMode;
  onModeChange: (mode: AgentRecordDisplayMode) => void;
  disabled?: boolean;
}) {
  return (
    <div className="agent-record-mode-toggle" role="group" aria-label="Agent record display mode">
      <button
        type="button"
        className={mode === "chat" ? "selected" : undefined}
        aria-pressed={mode === "chat"}
        disabled={disabled}
        onClick={() => onModeChange("chat")}
      >
        Chat
      </button>
      <button
        type="button"
        className={mode === "detail" ? "selected" : undefined}
        aria-pressed={mode === "detail"}
        disabled={disabled}
        onClick={() => onModeChange("detail")}
      >
        Detail
      </button>
    </div>
  );
}

function recordMeta(
  mode: AgentRecordDisplayMode,
  chatRecord: AgentChatRecord | null,
  detailRecord: AgentRecord | null,
  isLoading: boolean,
  isError: boolean
): string {
  if (isLoading) return "Loading";
  if (isError) return "Unavailable";
  if (mode === "chat") {
    if (chatRecord === null) return "No data";
    return `${chatRecord.messages_total} chat messages`;
  }
  if (detailRecord === null) return "No data";
  return `${detailRecord.sessions.length} agents · ${detailRecord.events_total} events`;
}

function pageInfo(mode: AgentRecordDisplayMode, chatRecord: AgentChatRecord | null, detailRecord: AgentRecord | null): PageInfo | null {
  if (mode === "chat") {
    if (chatRecord === null) return null;
    return {
      total: chatRecord.messages_total,
      limit: chatRecord.messages_limit,
      offset: chatRecord.messages_offset,
      count: chatRecord.messages.length,
      hasMore: chatRecord.messages_has_more,
      noun: "messages"
    };
  }
  if (detailRecord === null) return null;
  return {
    total: detailRecord.events_total,
    limit: detailRecord.events_limit,
    offset: detailRecord.events_offset,
    count: detailRecord.events.length,
    hasMore: detailRecord.events_has_more,
    noun: "events"
  };
}

function pageRange(info: PageInfo): string {
  if (info.total === 0) return `0 of 0 ${info.noun}`;
  const start = info.offset + 1;
  const end = info.offset + info.count;
  return `${start}-${end} of ${info.total} ${info.noun}`;
}

function AgentRecordSessionTabs({
  sessions,
  selectedSessionId,
  onSelectSession
}: {
  sessions: AgentSession[];
  selectedSessionId: string;
  onSelectSession: (sessionId: string) => void;
}) {
  const orderedSessions = sortSessionsNewestFirst(sessions);
  return (
    <div className="agent-record-session-tabs" role="tablist" aria-label="Agent sessions">
      {orderedSessions.map((session) => (
        <button
          key={session.id}
          type="button"
          role="tab"
          className={session.id === selectedSessionId ? "selected" : undefined}
          aria-selected={session.id === selectedSessionId}
          onClick={() => onSelectSession(session.id)}
        >
          {sessionLabel(session)}
        </button>
      ))}
    </div>
  );
}

function AgentRecordPagination({
  info,
  isFetching,
  onPreviousPage,
  onNextPage
}: {
  info: PageInfo | null;
  isFetching: boolean;
  onPreviousPage?: () => void;
  onNextPage?: () => void;
}) {
  if (info === null) return null;
  const canPrevious = info.offset > 0;
  const canNext = info.hasMore;
  return (
    <div className="agent-record-pagination">
      <span>{pageRange(info)}{isFetching ? " · refreshing" : ""}</span>
      <div>
        <button type="button" disabled={!canPrevious || !onPreviousPage} onClick={onPreviousPage}>Previous</button>
        <button type="button" disabled={!canNext || !onNextPage} onClick={onNextPage}>Next</button>
      </div>
    </div>
  );
}

export function AgentRecordViewer({
  mode,
  chatRecord,
  detailRecord,
  sessions,
  isLoading = false,
  isError = false,
  isFetching = false,
  expandSignal = 0,
  onModeChange,
  onExpandedChange,
  onSessionChange,
  onPreviousPage,
  onNextPage
}: Props) {
  const [expanded, setExpanded] = useState(false);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const sortedSessions = sortSessionsNewestFirst(sessions);
  const hasMultipleSessions = sortedSessions.length > 1;
  const activeSessionId = selectedSessionId ?? pickLatestSessionId(sortedSessions);
  const activeRecord = mode === "chat" ? chatRecord : detailRecord;
  const canExpand = activeRecord !== null && !isLoading && !isError;
  const expandedChatRecord = chatRecord && activeSessionId
    ? filterChatRecordBySession(chatRecord, activeSessionId)
    : chatRecord;
  const expandedDetailRecord = detailRecord && activeSessionId
    ? filterDetailRecordBySession(detailRecord, activeSessionId)
    : detailRecord;
  const expandedDisplayRecord = mode === "chat" ? expandedChatRecord : expandedDetailRecord;
  const activePageInfo = pageInfo(mode, chatRecord, detailRecord);
  const content = isLoading
    ? <p className="muted">Loading agent record...</p>
    : isError || activeRecord === null
      ? <p className="error" role="alert">Failed to load agent record.</p>
      : mode === "chat"
        ? <AgentChatContent record={activeRecord as AgentChatRecord} />
        : <AgentRecordContent record={activeRecord as AgentRecord} />;
  const meta = recordMeta(mode, chatRecord, detailRecord, isLoading, isError);

  const setExpandedState = (nextExpanded: boolean) => {
    setExpanded(nextExpanded);
    onExpandedChange?.(nextExpanded);
  };

  const openExpanded = () => {
    setSelectedSessionId(pickLatestSessionId(sortedSessions));
    setExpandedState(true);
  };

  const closeExpanded = () => {
    setExpandedState(false);
  };

  const selectSession = (sessionId: string) => {
    setSelectedSessionId(sessionId);
    onSessionChange?.(sessionId);
  };

  useEffect(() => {
    if (expandSignal === 0 || !canExpand) {
      return;
    }
    setSelectedSessionId(pickLatestSessionId(sessions));
    setExpandedState(true);
  }, [canExpand, expandSignal, sessions]);

  useEffect(() => {
    if (!expanded) {
      return;
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      closeExpanded();
    };

    window.addEventListener("keydown", handleKeyDown, { capture: true });
    return () => window.removeEventListener("keydown", handleKeyDown, { capture: true });
  }, [expanded]);

  useEffect(() => {
    if (sortedSessions.length === 0) {
      setSelectedSessionId(null);
      return;
    }
    if (selectedSessionId !== null && sortedSessions.some((session) => session.id === selectedSessionId)) {
      return;
    }
    setSelectedSessionId(pickLatestSessionId(sortedSessions));
  }, [selectedSessionId, sortedSessions]);

  const sessionTabs = hasMultipleSessions && activeSessionId
    ? (
      <AgentRecordSessionTabs
        sessions={sortedSessions}
        selectedSessionId={activeSessionId}
        onSelectSession={selectSession}
      />
    )
    : null;

  return (
    <>
      <section className="agent-record-viewer">
        <div className="agent-record-header">
          <div>
            <h3>Agent Record</h3>
            <small>{meta}</small>
          </div>
          <div className="agent-record-actions">
            <AgentRecordModeToggle mode={mode} onModeChange={onModeChange} disabled={isLoading} />
            <button type="button" disabled={!canExpand} onClick={openExpanded} title="Alt+R">Expand</button>
          </div>
        </div>
        <AgentRecordPagination info={activePageInfo} isFetching={isFetching && !isLoading} onPreviousPage={onPreviousPage} onNextPage={onNextPage} />
        {content}
      </section>
      {expanded && canExpand && expandedDisplayRecord && (
        <div className="agent-record-modal" role="dialog" aria-modal="true" aria-label="Agent record">
          <button type="button" className="agent-record-modal-backdrop" aria-label="Collapse agent record" onClick={closeExpanded} />
          <section className="agent-record-modal-panel">
            <div className="agent-record-header">
              <div>
                <h3>Agent Record</h3>
                <small>{recordMeta(mode, chatRecord, detailRecord, false, false)}</small>
              </div>
              <div className="agent-record-actions">
                <AgentRecordModeToggle mode={mode} onModeChange={onModeChange} />
                <button type="button" onClick={closeExpanded}>Collapse</button>
              </div>
            </div>
            {sessionTabs}
            <AgentRecordPagination info={activePageInfo} isFetching={isFetching} onPreviousPage={onPreviousPage} onNextPage={onNextPage} />
            {mode === "chat"
              ? <AgentChatContent record={expandedDisplayRecord as AgentChatRecord} />
              : <AgentRecordContent record={expandedDisplayRecord as AgentRecord} />}
          </section>
        </div>
      )}
    </>
  );
}
