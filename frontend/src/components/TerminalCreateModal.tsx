import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { fetchAgentClients, fetchAgentProfileConfig, fetchAgentProfiles, fetchClientAgentConfig } from "../api";
import {
  DEFAULT_AGENT_CLIENTS,
  agentClientCapability,
  agentDefaultCommand,
  agentLaunchOptions,
  configToSelection,
  isAgentLaunchKind,
  readDefaultAgentCommands,
  selectedEnabledCount,
  selectionItemCount
} from "../agentLaunch";
import type { AgentLaunchMode } from "../agentLaunch";
import type { AgentConfigSelection, AgentLaunchConfig, AgentLaunchKind } from "../types";
import { AgentConfigPicker } from "./AgentConfigPicker";
import { useOverlayFocus } from "./useOverlayFocus";

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
  const [selectedProfileId, setSelectedProfileId] = useState<string>("");
  const panelRef = useRef<HTMLElement | null>(null);
  const agentClientsQuery = useQuery({
    queryKey: ["agent-clients", clientId],
    queryFn: () => fetchAgentClients(clientId as string),
    enabled: isOpen && clientId !== null,
    staleTime: 60000
  });
  const agentClients = agentClientsQuery.data?.agent_clients ?? DEFAULT_AGENT_CLIENTS;
  const launchOptions = agentLaunchOptions(agentClients);
  const profilesQuery = useQuery({
    queryKey: ["agent-profiles", clientId],
    queryFn: () => fetchAgentProfiles(clientId as string),
    enabled: isOpen && clientId !== null,
    staleTime: 10000
  });
  const profiles = profilesQuery.data?.profiles ?? [];
  const selectedProfile = profiles.find((profile) => profile.id === selectedProfileId) ?? null;
  const configSupported = isAgentLaunchKind(mode)
    ? agentClientCapability(mode, agentClients, "client_config")
    : false;
  const configQuery = useQuery({
    queryKey: ["client-agent-config", clientId, mode, selectedProfileId],
    queryFn: () => selectedProfile !== null
      ? fetchAgentProfileConfig(clientId as string, selectedProfile.id, mode as AgentLaunchKind)
      : fetchClientAgentConfig(clientId as string, mode as AgentLaunchKind),
    enabled: isOpen && clientId !== null && isAgentLaunchKind(mode) && configPanelOpen && configSupported,
    staleTime: 10000
  });
  const activeSelection = isAgentLaunchKind(mode) ? selectionForConfig(mode, selection) : null;

  useEffect(() => {
    if (isOpen && context !== null) {
      setMode(context.initialAgent ?? "shell");
      setCommands(readDefaultAgentCommands());
      setConfigPanelOpen(context.showConfigInitially === true);
      setSelection(null);
      setSelectedProfileId("");
      return;
    }

    if (!isOpen || context === null) {
      setMode("shell");
      setCommands(readDefaultAgentCommands());
      setConfigPanelOpen(false);
      setSelection(null);
      setSelectedProfileId("");
    }
  }, [context, isOpen]);

  useEffect(() => {
    if (!isAgentLaunchKind(mode) || configQuery.data === undefined) {
      return;
    }
    if (selectedProfile !== null) {
      setSelection(configToSelection(configQuery.data));
      return;
    }
    setSelection((current) => {
      if (current !== null && current.agent === mode) {
        return current;
      }
      return configToSelection(configQuery.data);
    });
  }, [configQuery.data, mode, selectedProfile]);

  useEffect(() => {
    if (!isAgentLaunchKind(mode) || selectedProfile !== null || selectedProfileId !== "") {
      return;
    }
    setSelection(null);
  }, [mode, selectedProfile, selectedProfileId]);

  useEffect(() => {
    if (selectedProfileId === "" || selectedProfile !== null || profilesQuery.isLoading) {
      return;
    }
    setSelectedProfileId("");
  }, [profilesQuery.isLoading, selectedProfile, selectedProfileId]);

  const command = isAgentLaunchKind(mode) ? commands[mode] : "";
  const handleEscape = useCallback(() => {
    onClose();
  }, [onClose]);
  useOverlayFocus({
    isOpen: isOpen && context !== null,
    ref: panelRef,
    onEscape: handleEscape
  });
  const configSummary = useMemo(() => {
    if (!isAgentLaunchKind(mode)) {
      return "未配置";
    }
    if (selectedProfile !== null) {
      return selectedProfile.name;
    }
    if (activeSelection === null) {
      return "使用当前配置";
    }
    const total = selectionItemCount(activeSelection);
    const enabled = selectedEnabledCount(activeSelection);
    return total > 0 ? `${enabled}/${total} enabled` : "空配置";
  }, [activeSelection, mode, selectedProfile]);

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
            command: command.trim() || agentDefaultCommand(mode, agentClients),
            config: selectedProfile !== null ? null : activeSelection,
            profile_id: selectedProfile?.id ?? null
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
        ref={panelRef}
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
          {launchOptions.map((option) => (
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
              <span>Agent</span>
              <select
                value={selectedProfileId}
                onChange={(event) => {
                  const nextProfileId = event.target.value;
                  const nextProfile = profiles.find((profile) => profile.id === nextProfileId) ?? null;
                  setSelectedProfileId(nextProfileId);
                  if (nextProfile !== null) {
                    setMode(nextProfile.default_agent_client);
                    setConfigPanelOpen(false);
                  }
                  setSelection(null);
                }}
                disabled={profilesQuery.isLoading}
              >
                <option value="">直接配置 agent-client</option>
                {profiles.map((profile) => (
                  <option key={profile.id} value={profile.id}>
                    {profile.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="settings-field">
              <span>启动命令</span>
              <input
                value={command}
                onChange={(event) => setCommands((current) => ({ ...current, [mode]: event.target.value }))}
                placeholder={agentDefaultCommand(mode, agentClients)}
              />
            </label>
            {configSupported && (
              <button
                type="button"
                className="settings-nav-row terminal-create-config-row"
                onClick={() => setConfigPanelOpen((open) => !open)}
              >
                <span>配置</span>
                <strong>{configSummary}</strong>
              </button>
            )}
          </>
        )}

        {isAgentLaunchKind(mode) && configPanelOpen && configSupported && (
          <div className="terminal-create-config-panel">
            <AgentConfigPicker
              config={configQuery.data ?? null}
              selection={activeSelection}
              isLoading={configQuery.isLoading}
              isError={configQuery.isError}
              isFetching={configQuery.isFetching}
              readOnly={selectedProfile !== null}
              onSelectionChange={selectedProfile === null ? setSelection : () => {}}
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
