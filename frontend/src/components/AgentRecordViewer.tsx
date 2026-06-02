import { useCallback, useEffect, useMemo, useRef, useState, type Ref } from "react";
import ReactMarkdown, { defaultUrlTransform, type Components, type UrlTransform } from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";

import type {
  AgentChatMessage,
  AgentChatRecord,
  AgentChatRoleFilter,
  AgentEventProjection,
  AgentRecord,
  AgentRecordDisplayMode,
  AgentRecordEvent,
  AgentSession
} from "../types";
import { TerminalQuickInput } from "./TerminalQuickInput";
import { useOverlayFocus } from "./useOverlayFocus";

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
  | "subagent-call"
  | "subagent-result"
  | "subagent-context"
  | "lifecycle"
  | "event";
type BodyFormat = "markdown" | "json";
type EventView = {
  tone: EventTone;
  label: string;
  body: string;
  subtype: string | null;
  bodyFormat?: BodyFormat;
  targetSessionId?: string | null;
};
type AgentNode = { id: string; label: string; meta: string; children: AgentNode[] };
type AgentRecordJumpRequest = { sessionId: string; originMessageId?: string };
type AgentRecordPageOffset = { chatOffset?: number; detailOffset?: number };
type AgentRecordViewProps = {
  mode: AgentRecordDisplayMode;
  chatRoleFilter: AgentChatRoleFilter;
  chatRecord: AgentChatRecord | null;
  detailRecord: AgentRecord | null;
  sessions: AgentSession[];
  isLoading?: boolean;
  isError?: boolean;
  isFetching?: boolean;
  onModeChange: (mode: AgentRecordDisplayMode) => void;
  onChatRoleFilterChange: (role: AgentChatRoleFilter) => void;
  jumpRequest?: AgentRecordJumpRequest | null;
  onOpenSubagent?: (sessionId: string, originMessageId?: string) => void;
  onSessionChange?: (sessionId: string | null, pageOffset?: AgentRecordPageOffset) => void;
  onPreviousPage?: () => void;
  onNextPage?: () => void;
};
type AgentRecordViewerProps = AgentRecordViewProps & {
  onExpand: () => void;
  expandShortcutLabel?: string;
};
type AgentRecordModalProps = AgentRecordViewProps & {
  open: boolean;
  terminalStatusLabel?: string;
  terminalStatusTone?: "connected" | "connecting" | "reconnecting" | "unavailable" | "error";
  quickInputDraft?: string;
  canSendQuickInput?: boolean;
  onQuickInputDraftChange?: (draft: string) => void;
  onQuickInputSubmit?: (draft: string) => boolean;
  onClose: () => void;
};
type PageInfo = {
  total: number;
  limit: number;
  offset: number;
  count: number;
  hasMore: boolean;
  totalExact?: boolean;
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
  "subagent-call": "Subagent call",
  "subagent-result": "Subagent result",
  "subagent-context": "Subagent context",
  lifecycle: "Lifecycle",
  event: "Event"
};

const CHAT_ROLE_FILTER_LABELS: Record<AgentChatRoleFilter, string> = {
  all: "All",
  user: "User",
  agent: "Agent",
  subagent_call: "Subagent call",
  subagent_result: "Subagent result"
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
    bodyFormat: projection.body_format,
    targetSessionId: projection.target_session_id
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

const markdownComponents: Components = {
  a({ node: _node, href, children, ...props }) {
    if (!href) {
      return <span className="agent-event-markdown-link-text">{children}</span>;
    }
    return <a {...props} href={href} target="_blank" rel="noreferrer">{children}</a>;
  },
  img({ node: _node, alt, ...props }) {
    return <img {...props} alt={alt ?? ""} loading="lazy" decoding="async" />;
  },
  table({ node: _node, children, ...props }) {
    return (
      <div className="agent-event-markdown-table-wrap">
        <table {...props}>{children}</table>
      </div>
    );
  }
};

const markdownUrlTransform: UrlTransform = (url) => defaultUrlTransform(url) || undefined;

function MarkdownText({ text }: { text: string }) {
  return (
    <div className="agent-event-markdown">
      <ReactMarkdown
        components={markdownComponents}
        remarkPlugins={[remarkGfm, remarkBreaks]}
        urlTransform={markdownUrlTransform}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
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

function sessionCreatedTimestamp(session: AgentSession): number {
  const created = Date.parse(session.created_at);
  return Number.isNaN(created) ? 0 : created;
}

export function sortSessionsByCreation(sessions: AgentSession[]): AgentSession[] {
  return [...sessions].sort((left, right) => {
    const delta = sessionCreatedTimestamp(left) - sessionCreatedTimestamp(right);
    if (delta !== 0) {
      return delta;
    }
    return left.id.localeCompare(right.id);
  });
}

export function pickInitialSessionId(sessions: AgentSession[]): string | null {
  return sortSessionsByCreation(sessions)[0]?.id ?? null;
}

function defaultAgentMessageType(message: AgentChatMessage): "user" | "agent" | "subagent_call" | "subagent_result" {
  if (message.agent_message_type === "subagent_call" || message.agent_message_type === "subagent_result") {
    return message.agent_message_type;
  }
  return message.role;
}

function chatSpeakerLabel(message: AgentChatMessage): string {
  switch (defaultAgentMessageType(message)) {
    case "subagent_call":
      return "Main Agent -> Subagent";
    case "subagent_result":
      return "Subagent -> Agent";
    case "agent":
      return "Agent";
    case "user":
      return "User";
  }
}

function chatMessageClass(message: AgentChatMessage): string {
  return `agent-chat-message-${defaultAgentMessageType(message).replace("_", "-")}`;
}

function parentSourceFromPath(sourcePath: string | null): string | null {
  return sourcePath?.match(/\/([^/]+)\/subagents\/agent-[^/]+\.jsonl$/)?.[1] ?? null;
}

function parentSourceFromSession(session: AgentSession): string | null {
  if (session.source_path) {
    return parentSourceFromPath(session.source_path);
  }
  return null;
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
    const parent = sourceToNode.get(parentSourceFromSession(session) ?? "");
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

function AgentChatMessageCard({
  message,
  fullscreen = false,
  onExpand,
  onCollapse,
  onOpenSubagent,
  articleRef
}: {
  message: AgentChatMessage;
  fullscreen?: boolean;
  onExpand?: () => void;
  onCollapse?: () => void;
  onOpenSubagent?: (message: AgentChatMessage) => void;
  articleRef?: Ref<HTMLElement>;
}) {
  const speakerLabel = chatSpeakerLabel(message);
  const actionLabel = fullscreen ? "Collapse" : "Expand";
  const canOpenSubagent = message.target_session_id !== null && onOpenSubagent !== undefined && !fullscreen;
  const titleLabel = speakerLabel.toLowerCase();

  return (
    <article
      ref={articleRef}
      className={[
        "agent-chat-message",
        chatMessageClass(message),
        fullscreen ? "agent-chat-message-fullscreen" : ""
      ].filter(Boolean).join(" ")}
    >
      <header>
        <div className="agent-chat-message-title">
          <span>{speakerLabel}</span>
          <time dateTime={message.created_at}>{formatDateTime(message.created_at)}</time>
        </div>
        <button
          type="button"
          className="agent-chat-message-zoom-button"
          aria-label={`${actionLabel} ${titleLabel} message`}
          title={actionLabel}
          onClick={fullscreen ? onCollapse : onExpand}
        >
          {actionLabel}
        </button>
      </header>
      <small>{message.source_type} · {message.source_id}</small>
      {message.subagent_id && (
        <small>Subagent {message.subagent_id}</small>
      )}
      <div className="agent-chat-message-body">
        {message.body_format === "json"
          ? <pre className="agent-event-json">{message.body}</pre>
          : <MarkdownText text={message.body} />}
      </div>
      {canOpenSubagent && (
        <button
          type="button"
          className="agent-chat-message-subagent-button"
          onClick={() => onOpenSubagent?.(message)}
        >
          Open subagent
        </button>
      )}
    </article>
  );
}

function AgentChatMessageOverlay({
  message,
  onClose
}: {
  message: AgentChatMessage;
  onClose: () => void;
}) {
  const panelRef = useRef<HTMLElement | null>(null);
  const speakerLabel = chatSpeakerLabel(message).toLowerCase();

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      panelRef.current?.querySelector<HTMLButtonElement>(".agent-chat-message-zoom-button")?.focus({ preventScroll: true });
    });

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      onClose();
    };

    window.addEventListener("keydown", handleKeyDown, { capture: true });
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("keydown", handleKeyDown, { capture: true });
    };
  }, [onClose]);

  return (
    <div className="agent-chat-message-overlay" role="dialog" aria-modal="true" aria-label={`Expanded ${speakerLabel} message`}>
      <button type="button" className="agent-chat-message-overlay-backdrop" aria-label="Collapse message" onClick={onClose} />
      <AgentChatMessageCard
        message={message}
        fullscreen
        onCollapse={onClose}
        articleRef={panelRef}
      />
    </div>
  );
}

function AgentChatContent({
  record,
  highlightedMessageId,
  onOpenSubagent
}: {
  record: AgentChatRecord;
  highlightedMessageId?: string | null;
  onOpenSubagent?: (message: AgentChatMessage) => void;
}) {
  const [expandedMessageId, setExpandedMessageId] = useState<string | null>(null);
  const expandedMessage = expandedMessageId === null
    ? null
    : record.messages.find((message) => message.id === expandedMessageId) ?? null;

  useEffect(() => {
    if (expandedMessageId !== null && expandedMessage === null) {
      setExpandedMessageId(null);
    }
  }, [expandedMessage, expandedMessageId]);

  if (record.messages_total === 0) {
    return <p className="muted">No agent record captured yet.</p>;
  }
  if (record.messages.length === 0) {
    return <p className="muted">No user input or agent response captured on this page.</p>;
  }

  return (
    <>
      <div className="agent-chat-events">
        {record.messages.map((message) => (
          <AgentChatMessageCard
            key={message.id}
            message={message}
            articleRef={highlightedMessageId === message.id ? (element) => element?.scrollIntoView({ block: "center" }) : undefined}
            onOpenSubagent={onOpenSubagent}
            onExpand={() => setExpandedMessageId(message.id)}
          />
        ))}
      </div>
      {expandedMessage && (
        <AgentChatMessageOverlay
          message={expandedMessage}
          onClose={() => setExpandedMessageId(null)}
        />
      )}
    </>
  );
}

function AgentRecordContent({
  record,
  onOpenSubagent
}: {
  record: AgentRecord;
  onOpenSubagent?: (sessionId: string) => void;
}) {
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
              {view.targetSessionId && onOpenSubagent && (
                <button
                  type="button"
                  className="agent-event-subagent-button"
                  onClick={() => onOpenSubagent(view.targetSessionId as string)}
                >
                  Open subagent
                </button>
              )}
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

function AgentChatRoleFilterToggle({
  value,
  onChange,
  disabled = false
}: {
  value: AgentChatRoleFilter;
  onChange: (role: AgentChatRoleFilter) => void;
  disabled?: boolean;
}) {
  return (
    <div className="agent-record-role-toggle" role="group" aria-label="Agent chat message type">
      {(Object.keys(CHAT_ROLE_FILTER_LABELS) as AgentChatRoleFilter[]).map((role) => (
        <button
          key={role}
          type="button"
          className={value === role ? "selected" : undefined}
          aria-pressed={value === role}
          disabled={disabled}
          onClick={() => onChange(role)}
        >
          {CHAT_ROLE_FILTER_LABELS[role]}
        </button>
      ))}
    </div>
  );
}

function recordMeta(
  mode: AgentRecordDisplayMode,
  chatRoleFilter: AgentChatRoleFilter,
  chatRecord: AgentChatRecord | null,
  detailRecord: AgentRecord | null,
  isLoading: boolean,
  isError: boolean
): string {
  if (isLoading) return "Loading";
  if (isError) return "Unavailable";
  if (mode === "chat") {
    if (chatRecord === null) return "No data";
    const scope = chatRoleFilter === "all" ? "chat messages" : `${CHAT_ROLE_FILTER_LABELS[chatRoleFilter].toLocaleLowerCase()} messages`;
    return `${chatRecord.messages_total}${chatRecord.messages_total_exact === false ? "+" : ""} ${scope}`;
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
      totalExact: chatRecord.messages_total_exact,
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
  return `${start}-${end} of ${info.total}${info.totalExact === false ? "+" : ""} ${info.noun}`;
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
  const orderedSessions = sortSessionsByCreation(sessions);
  return (
    <div className="agent-record-session-tabs" role="tablist" aria-label="Agent sessions">
      {orderedSessions.map((session, index) => (
        <button
          key={session.id}
          type="button"
          role="tab"
          className={session.id === selectedSessionId ? "selected" : undefined}
          aria-selected={session.id === selectedSessionId}
          title={sessionLabel(session)}
          onClick={() => onSelectSession(session.id)}
        >
          {index === 0 ? "Main" : `Sub ${index}`}
        </button>
      ))}
    </div>
  );
}

function AgentRecordReturnButton({
  originMessageId,
  onReturn
}: {
  originMessageId: string | null;
  onReturn: () => void;
}) {
  if (originMessageId === null) {
    return null;
  }
  return (
    <button type="button" className="agent-record-return-button" onClick={onReturn}>
      Return to call
    </button>
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
  chatRoleFilter,
  chatRecord,
  detailRecord,
  sessions,
  isLoading = false,
  isError = false,
  isFetching = false,
  onModeChange,
  onChatRoleFilterChange,
  onOpenSubagent,
  onSessionChange,
  onPreviousPage,
  onNextPage,
  onExpand,
  expandShortcutLabel = "Expand"
}: AgentRecordViewerProps) {
  const activeRecord = mode === "chat" ? chatRecord : detailRecord;
  const canExpand = activeRecord !== null && !isLoading && !isError;
  const activePageInfo = pageInfo(mode, chatRecord, detailRecord);
  const content = isLoading
    ? <p className="muted">Loading agent record...</p>
    : isError || activeRecord === null
      ? <p className="error" role="alert">Failed to load agent record.</p>
      : mode === "chat"
        ? (
          <AgentChatContent
            record={activeRecord as AgentChatRecord}
            onOpenSubagent={(message) => {
              if (message.target_session_id) {
                onOpenSubagent?.(message.target_session_id, message.id);
              }
            }}
          />
        )
        : (
          <AgentRecordContent
            record={activeRecord as AgentRecord}
            onOpenSubagent={(sessionId) => {
              onOpenSubagent?.(sessionId);
            }}
          />
        );
  const meta = recordMeta(mode, chatRoleFilter, chatRecord, detailRecord, isLoading, isError);

  const openExpanded = () => {
    onExpand();
  };

  return (
    <section className="agent-record-viewer">
      <div className="agent-record-header">
        <div>
          <h3>Agent Record</h3>
          <small>{meta}</small>
        </div>
        <div className="agent-record-actions">
          <AgentRecordModeToggle mode={mode} onModeChange={onModeChange} disabled={isLoading} />
          {mode === "chat" && (
            <AgentChatRoleFilterToggle
              value={chatRoleFilter}
              onChange={onChatRoleFilterChange}
              disabled={isLoading}
            />
          )}
          <button type="button" disabled={!canExpand} onClick={openExpanded} title={expandShortcutLabel}>Expand</button>
        </div>
      </div>
      <AgentRecordPagination info={activePageInfo} isFetching={isFetching && !isLoading} onPreviousPage={onPreviousPage} onNextPage={onNextPage} />
      {content}
    </section>
  );
}

export function AgentRecordModal({
  open,
  mode,
  chatRoleFilter,
  chatRecord,
  detailRecord,
  sessions,
  isLoading = false,
  isError = false,
  isFetching = false,
  terminalStatusLabel = "Terminal unavailable",
  terminalStatusTone = "unavailable",
  quickInputDraft = "",
  canSendQuickInput = false,
  onQuickInputDraftChange,
  onQuickInputSubmit,
  onModeChange,
  onChatRoleFilterChange,
  jumpRequest = null,
  onSessionChange,
  onPreviousPage,
  onNextPage,
  onClose
}: AgentRecordModalProps) {
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [returnTarget, setReturnTarget] = useState<{
    sessionId: string | null;
    messageId: string;
    pageOffset: AgentRecordPageOffset;
  } | null>(null);
  const [highlightedMessageId, setHighlightedMessageId] = useState<string | null>(null);
  const panelRef = useRef<HTMLElement | null>(null);
  const lastJumpKeyRef = useRef<string | null>(null);
  const sortedSessions = useMemo(() => sortSessionsByCreation(sessions), [sessions]);
  const hasMultipleSessions = sortedSessions.length > 1;
  const activeSessionId = selectedSessionId ?? pickInitialSessionId(sortedSessions);
  const activeRecord = mode === "chat" ? chatRecord : detailRecord;
  const canRenderContent = activeRecord !== null && !isLoading && !isError;
  const expandedDisplayRecord = mode === "chat" ? chatRecord : detailRecord;
  const activePageInfo = pageInfo(mode, chatRecord, detailRecord);
  const canUseQuickInput = onQuickInputDraftChange !== undefined && onQuickInputSubmit !== undefined;
  const currentPageOffset = (): AgentRecordPageOffset => {
    if (mode === "chat") {
      return { chatOffset: chatRecord?.messages_offset ?? 0 };
    }
    return { detailOffset: detailRecord?.events_offset ?? 0 };
  };

  const selectSession = (sessionId: string) => {
    setReturnTarget(null);
    setSelectedSessionId(sessionId);
    onSessionChange?.(sessionId);
  };

  const openSubagentFromMessage = (message: AgentChatMessage) => {
    if (!message.target_session_id) {
      return;
    }
    setReturnTarget({ sessionId: activeSessionId, messageId: message.id, pageOffset: currentPageOffset() });
    setSelectedSessionId(message.target_session_id);
    onSessionChange?.(message.target_session_id);
  };

  const openSubagentSession = (sessionId: string) => {
    setReturnTarget(null);
    setSelectedSessionId(sessionId);
    onSessionChange?.(sessionId);
  };

  const returnToCall = () => {
    if (returnTarget === null) {
      return;
    }
    setSelectedSessionId(returnTarget.sessionId);
    setHighlightedMessageId(returnTarget.messageId);
    onSessionChange?.(returnTarget.sessionId, returnTarget.pageOffset);
    setReturnTarget(null);
  };

  useEffect(() => {
    if (highlightedMessageId === null) {
      return;
    }
    const timeout = window.setTimeout(() => setHighlightedMessageId(null), 1600);
    return () => window.clearTimeout(timeout);
  }, [highlightedMessageId]);

  useEffect(() => {
    if (!open || jumpRequest === null) {
      return;
    }
    const jumpKey = `${jumpRequest.sessionId}:${jumpRequest.originMessageId ?? ""}`;
    if (lastJumpKeyRef.current === jumpKey) {
      return;
    }
    lastJumpKeyRef.current = jumpKey;
    const previousSessionId = selectedSessionId ?? pickInitialSessionId(sortedSessions);
    setReturnTarget(jumpRequest.originMessageId
      ? { sessionId: previousSessionId, messageId: jumpRequest.originMessageId, pageOffset: currentPageOffset() }
      : null);
    setSelectedSessionId(jumpRequest.sessionId);
    onSessionChange?.(jumpRequest.sessionId);
  }, [
    chatRecord?.messages_offset,
    detailRecord?.events_offset,
    jumpRequest,
    mode,
    onSessionChange,
    open,
    selectedSessionId,
    sortedSessions
  ]);

  useEffect(() => {
    if (!open) {
      setSelectedSessionId(null);
      setReturnTarget(null);
      setHighlightedMessageId(null);
      lastJumpKeyRef.current = null;
      return;
    }
  }, [open]);

  useEffect(() => {
    if (!open) {
      return;
    }
    if (
      selectedSessionId !== null
      && (
        sortedSessions.some((session) => session.id === selectedSessionId)
        || (jumpRequest !== null && selectedSessionId === jumpRequest.sessionId)
      )
    ) {
      return;
    }
    if (sortedSessions.length === 0) {
      if (jumpRequest === null) {
        setSelectedSessionId(null);
      }
      return;
    }
    setSelectedSessionId(pickInitialSessionId(sortedSessions));
  }, [jumpRequest, open, selectedSessionId, sortedSessions]);

  const handleEscape = useCallback(() => {
    if (panelRef.current?.querySelector(".agent-chat-message-overlay")) {
      return;
    }
    onClose();
  }, [onClose]);

  useOverlayFocus({
    isOpen: open,
    ref: panelRef,
    onEscape: handleEscape,
    preserveExistingFocus: true
  });

  const sessionTabs = hasMultipleSessions && activeSessionId
    ? (
      <AgentRecordSessionTabs
        sessions={sortedSessions}
        selectedSessionId={activeSessionId}
        onSelectSession={selectSession}
      />
    )
    : null;

  if (!open) {
    return null;
  }

  const pendingJumpTarget = jumpRequest !== null && !sortedSessions.some((session) => session.id === jumpRequest.sessionId);

  const submitQuickInput = (draft: string) => {
    if (!canUseQuickInput) {
      return false;
    }
    return onQuickInputSubmit(draft);
  };

  return (
    <div className="agent-record-modal" role="dialog" aria-modal="true" aria-label="Agent record">
      <button type="button" className="agent-record-modal-backdrop" aria-label="Collapse agent record" onClick={onClose} />
      <section ref={panelRef} className="agent-record-modal-panel">
        <div className="agent-record-header">
          <div>
            <h3>Agent Record</h3>
            <small>{recordMeta(mode, chatRoleFilter, chatRecord, detailRecord, isLoading, isError)}</small>
          </div>
          <div className="agent-record-actions">
            <span className={`agent-record-terminal-status ${terminalStatusTone}`} role="status">
              {terminalStatusLabel}
            </span>
            <AgentRecordModeToggle mode={mode} onModeChange={onModeChange} disabled={isLoading} />
            {mode === "chat" && (
              <AgentChatRoleFilterToggle
                value={chatRoleFilter}
                onChange={onChatRoleFilterChange}
                disabled={isLoading}
              />
            )}
            <button type="button" onClick={onClose}>Collapse</button>
          </div>
        </div>
        <div className="agent-record-modal-scroll">
          {sessionTabs}
          <AgentRecordReturnButton originMessageId={returnTarget?.messageId ?? null} onReturn={returnToCall} />
          <AgentRecordPagination info={activePageInfo} isFetching={isFetching} onPreviousPage={onPreviousPage} onNextPage={onNextPage} />
          {isLoading
            ? <p className="muted">Loading agent record...</p>
            : pendingJumpTarget
              ? <p className="muted">Loading subagent record...</p>
            : isError
              ? <p className="error" role="alert">Failed to load agent record.</p>
              : canRenderContent && expandedDisplayRecord
                ? mode === "chat"
                  ? (
                    <AgentChatContent
                      record={expandedDisplayRecord as AgentChatRecord}
                      highlightedMessageId={highlightedMessageId}
                      onOpenSubagent={openSubagentFromMessage}
                    />
                  )
                  : <AgentRecordContent record={expandedDisplayRecord as AgentRecord} onOpenSubagent={openSubagentSession} />
                : <p className="muted">No agent record captured yet.</p>}
        </div>
        {canUseQuickInput && (
          <TerminalQuickInput
            className="agent-record-quick-input"
            value={quickInputDraft}
            canSend={canSendQuickInput}
            onValueChange={onQuickInputDraftChange}
            onSubmit={submitQuickInput}
            autoFocus
            placeholder="输入文字后按 Enter 发送；Shift+Enter 换行"
            submitOnEnter
          />
        )}
      </section>
    </div>
  );
}
