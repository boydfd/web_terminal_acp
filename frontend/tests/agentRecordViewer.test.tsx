import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentRecordViewer } from "../src/components/AgentRecordViewer";
import type { AgentChatRecord } from "../src/types";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

const baseMessage = {
  id: "message-1",
  ai_session_id: "session-1",
  source_type: "codex",
  source_id: "session.jsonl",
  role: "agent" as const,
  body: "Expanded message body with **markdown**.",
  body_format: "markdown" as const,
  agent_message_type: "agent" as const,
  subagent_id: null,
  subagent_tool_use_id: null,
  target_session_id: null,
  target_session_source_id: null,
  created_at: "2026-06-01T00:00:00Z"
};

const chatRecord: AgentChatRecord = {
  window_id: "window-1",
  messages: [baseMessage],
  messages_total: 1,
  messages_limit: 30,
  messages_offset: 0,
  messages_has_more: false
};

function renderViewer(record: AgentChatRecord = chatRecord, onOpenSubagent?: (sessionId: string, originMessageId?: string) => void) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);

  act(() => {
    root?.render(
      <AgentRecordViewer
        mode="chat"
        chatRoleFilter="all"
        chatRecord={record}
        detailRecord={null}
        sessions={[]}
        onModeChange={vi.fn()}
        onChatRoleFilterChange={vi.fn()}
        onOpenSubagent={onOpenSubagent}
        onExpand={vi.fn()}
      />
    );
  });
}

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  container?.remove();
  root = null;
  container = null;
  vi.restoreAllMocks();
});

describe("AgentRecordViewer", () => {
  it("expands and collapses a single chat message", () => {
    renderViewer();

    const expandButton = container?.querySelector<HTMLButtonElement>('button[aria-label="Expand agent message"]');
    expect(expandButton).toBeInstanceOf(HTMLButtonElement);

    act(() => {
      expandButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    const overlay = document.body.querySelector<HTMLElement>(".agent-chat-message-overlay");
    expect(overlay).not.toBeNull();
    expect(overlay?.getAttribute("role")).toBe("dialog");
    expect(overlay?.textContent).toContain("Expanded message body with markdown.");

    const collapseButton = overlay?.querySelector<HTMLButtonElement>('button[aria-label="Collapse agent message"]');
    expect(collapseButton).toBeInstanceOf(HTMLButtonElement);

    act(() => {
      collapseButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(document.body.querySelector(".agent-chat-message-overlay")).toBeNull();
  });

  it("renders GFM markdown structures in agent messages", () => {
    renderViewer({
      ...chatRecord,
      messages: [
        {
          ...baseMessage,
          body: [
            "# Plan",
            "",
            "- [x] render tables",
            "- [ ] keep task state visible",
            "",
            "| Area | Status |",
            "| --- | --- |",
            "| Preview | **better** |",
            "",
            "~~old~~ new",
            "line one",
            "line two"
          ].join("\n")
        }
      ]
    });

    expect(container?.querySelector(".agent-event-markdown h1")?.textContent).toBe("Plan");
    expect(container?.querySelectorAll(".agent-event-markdown input[type=\"checkbox\"]")).toHaveLength(2);
    expect(container?.querySelector(".agent-event-markdown table")).not.toBeNull();
    expect(container?.querySelector(".agent-event-markdown del")?.textContent).toBe("old");
    expect(container?.querySelector(".agent-event-markdown p")?.innerHTML).toContain("<br>");
  });

  it("does not execute raw HTML or unsafe markdown links", () => {
    renderViewer({
      ...chatRecord,
      messages: [
        {
          ...baseMessage,
          body: '<img src=x onerror="window.__agentRecordInjected = true"> [bad](javascript:alert(1)) [ok](https://example.com)'
        }
      ]
    });

    expect((window as unknown as { __agentRecordInjected?: boolean }).__agentRecordInjected).toBeUndefined();
    expect(container?.querySelector(".agent-event-markdown img")).toBeNull();

    expect(container?.querySelector(".agent-event-markdown-link-text")?.textContent).toBe("bad");

    const links = [...(container?.querySelectorAll<HTMLAnchorElement>(".agent-event-markdown a") ?? [])];
    expect(links).toHaveLength(1);
    expect(links[0].getAttribute("href")).toBe("https://example.com");
    expect(links[0].getAttribute("target")).toBe("_blank");
    expect(links[0].getAttribute("rel")).toBe("noreferrer");
  });

  it("renders subagent call and result as distinct agent message types", () => {
    renderViewer(
      {
        ...chatRecord,
        messages: [
          {
            ...baseMessage,
            id: "message-call",
            body: "Return exactly: 1",
            agent_message_type: "subagent_call",
            subagent_id: "subagent-1",
            subagent_tool_use_id: "call-subagent-1",
            target_session_id: "sub-session-1",
            target_session_source_id: "agent-subagent-1"
          },
          {
            ...baseMessage,
            id: "message-result",
            body: "1",
            agent_message_type: "subagent_result",
            subagent_id: "subagent-1",
            subagent_tool_use_id: "call-subagent-1",
            target_session_id: "sub-session-1",
            target_session_source_id: "agent-subagent-1"
          }
        ],
        messages_total: 2
      },
      vi.fn()
    );

    expect(container?.querySelector(".agent-chat-message-subagent-call")?.textContent).toContain("Main Agent -> Subagent");
    expect(container?.querySelector(".agent-chat-message-subagent-result")?.textContent).toContain("Subagent -> Agent");
    expect(container?.querySelectorAll(".agent-chat-message-subagent-button")).toHaveLength(2);
  });

  it("marks chat totals as approximate when the backend has more pages without exact count", () => {
    renderViewer({
      ...chatRecord,
      messages_total: 31,
      messages_total_exact: false,
      messages_has_more: true
    });

    expect(container?.textContent).toContain("31+ chat messages");
    expect(container?.textContent).toContain("1-1 of 31+ messages");
  });
});
