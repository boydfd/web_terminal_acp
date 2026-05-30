import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { fetchClientAgentConfig } from "../api";
import {
  AGENT_LAUNCH_OPTIONS,
  configToSelection,
  isAgentLaunchKind,
  readDefaultAgentCommands,
  selectedEnabledCount,
  selectionItemCount
} from "../agentLaunch";
import type { AgentLaunchMode } from "../agentLaunch";
import type { AgentConfigSelection, AgentLaunchConfig, AgentLaunchKind } from "../types";
import { AgentConfigPicker } from "./AgentConfigPicker";

export type TerminalCreateContext = {
  title: string;
  description?: string;
  cwd?: string | null;
  folder_path?: string | null;
  initialAgent?: AgentLaunchKind;
  showConfigInitially?: boolean;
  afterCreate?: () => void;
};

export type TerminalCreateSubmit = {
  cwd?: string | null;
  folder_path?: string | null;
  agent_launch?: AgentLaunchConfig | null;
};

type TerminalCreateModalProps = {
  isOpen: boolean;
  clientId: string | null;
  context: TerminalCreateContext | null;
  creatingTerminal?: boolean;
  createTerminalDisabled?: boolean;
  onClose: () => void;
  onSubmit: (payload: TerminalCreateSubmit) => void;
};

function selectionForConfig(configAgent: AgentLaunchKind, current: AgentConfigSelection | null) {
  if (current !== null && current.agent === configAgent) {
    return current;
  }
  return null;
}

export function TerminalCreateModal({
  isOpen,
  clientId,
  context,
  creatingTerminal = false,
  createTerminalDisabled = false,
  onClose,
  onSubmit
}: TerminalCreateModalProps) {
  const [mode, setMode] = useState<AgentLaunchMode>("shell");
  const [commands, setCommands] = useState<Record<AgentLaunchKind, string>>(() => readDefaultAgentCommands());
  const [configPanelOpen, setConfigPanelOpen] = useState(false);
  const [selection, setSelection] = useState<AgentConfigSelection | null>(null);
  const configQuery = useQuery({
    queryKey: ["client-agent-config", clientId, mode],
    queryFn: () => fetchClientAgentConfig(clientId as string, mode as AgentLaunchKind),
    enabled: isOpen && clientId !== null && isAgentLaunchKind(mode) && configPanelOpen,
    staleTime: 10000
  });
  const activeSelection = isAgentLaunchKind(mode) ? selectionForConfig(mode, selection) : null;

  useEffect(() => {
    if (isOpen && context !== null) {
      setMode(context.initialAgent ?? "shell");
      setCommands(readDefaultAgentCommands());
      setConfigPanelOpen(context.showConfigInitially === true);
      setSelection(null);
      return;
    }

    if (!isOpen || context === null) {
      setMode("shell");
      setCommands(readDefaultAgentCommands());
      setConfigPanelOpen(false);
      setSelection(null);
    }
  }, [context, isOpen]);

  useEffect(() => {
    if (!isAgentLaunchKind(mode) || configQuery.data === undefined) {
      return;
    }
    setSelection((current) => {
      if (current !== null && current.agent === mode) {
        return current;
      }
      return configToSelection(configQuery.data);
    });
  }, [configQuery.data, mode]);

  const command = isAgentLaunchKind(mode) ? commands[mode] : "";
  const configSummary = useMemo(() => {
    if (!isAgentLaunchKind(mode)) {
      return "未配置";
    }
    if (activeSelection === null) {
      return "使用当前配置";
    }
    const total = selectionItemCount(activeSelection);
    const enabled = selectedEnabledCount(activeSelection);
    return total > 0 ? `${enabled}/${total} enabled` : "空配置";
  }, [activeSelection, mode]);

  if (!isOpen || context === null) {
    return null;
  }

  const canSubmit = clientId !== null && !creatingTerminal && !createTerminalDisabled;
  const submit = () => {
    if (!canSubmit) {
      return;
    }
    onSubmit({
      cwd: context.cwd ?? null,
      folder_path: context.folder_path ?? null,
      agent_launch: isAgentLaunchKind(mode)
        ? {
            agent: mode,
            command: command.trim() || readDefaultAgentCommands()[mode],
            config: activeSelection
          }
        : null
    });
  };

  return (
    <div
      className="terminal-create-modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <section
        className="terminal-create-modal"
        data-onboarding-id="terminal-create-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Create terminal"
      >
        <div className="terminal-create-header">
          <div>
            <h2>{context.title}</h2>
            {context.description && <p className="muted">{context.description}</p>}
          </div>
          <button type="button" onClick={onClose}>关闭</button>
        </div>

        <div className="terminal-create-agent-tabs" role="tablist" aria-label="Agent">
          {AGENT_LAUNCH_OPTIONS.map((option) => (
            <button
              key={option.id}
              type="button"
              className={mode === option.id ? "active" : undefined}
              aria-selected={mode === option.id}
              onClick={() => {
                setMode(option.id);
                setConfigPanelOpen(false);
                setSelection(null);
              }}
            >
              {option.label}
            </button>
          ))}
        </div>

        {isAgentLaunchKind(mode) && (
          <>
            <label className="settings-field">
              <span>启动命令</span>
              <input
                value={command}
                onChange={(event) => setCommands((current) => ({ ...current, [mode]: event.target.value }))}
                placeholder={readDefaultAgentCommands()[mode]}
              />
            </label>
            <button
              type="button"
              className="settings-nav-row terminal-create-config-row"
              onClick={() => setConfigPanelOpen((open) => !open)}
            >
              <span>配置</span>
              <strong>{configSummary}</strong>
            </button>
          </>
        )}

        {isAgentLaunchKind(mode) && configPanelOpen && (
          <div className="terminal-create-config-panel">
            <AgentConfigPicker
              config={configQuery.data ?? null}
              selection={activeSelection}
              isLoading={configQuery.isLoading}
              isError={configQuery.isError}
              isFetching={configQuery.isFetching}
              onSelectionChange={setSelection}
            />
          </div>
        )}

        <div className="terminal-create-actions">
          <button type="button" onClick={onClose}>取消</button>
          <button type="button" disabled={!canSubmit} onClick={submit}>
            {creatingTerminal ? "创建中..." : "创建"}
          </button>
        </div>
      </section>
    </div>
  );
}
