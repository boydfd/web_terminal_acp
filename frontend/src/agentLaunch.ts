import { readAgentCommandSettings, type AgentCommandSettings } from "./userPreferences";
import type {
  AgentClient,
  AgentConfig,
  AgentConfigSelection,
  AgentLaunchConfig,
  AgentConfigSelectionSection,
  AgentLaunchKind
} from "./types";

export type AgentLaunchMode = "shell" | AgentLaunchKind;

export const DEFAULT_AGENT_CLIENTS: AgentClient[] = [
  {
    id: "codex",
    provider_id: "codex",
    label: "Codex",
    aliases: [],
    default_command: "codex",
    command_names: ["codex"],
    capabilities: {
      launch: true,
      client_config: true,
      window_config: true,
      profile_config: true,
      agent_records: true,
      runtime_tags: true,
      work_presence: true
    }
  },
  {
    id: "claude",
    provider_id: "claude_code",
    label: "Claude Code",
    aliases: ["claude_code"],
    default_command: "claude",
    command_names: ["claude"],
    capabilities: {
      launch: true,
      client_config: true,
      window_config: true,
      profile_config: true,
      agent_records: true,
      runtime_tags: true,
      work_presence: true
    }
  },
  {
    id: "cursor",
    provider_id: "cursor_cli",
    label: "Cursor",
    aliases: ["cursor_cli", "agent"],
    default_command: "agent",
    command_names: ["agent", "cursor", "cursor-agent"],
    capabilities: {
      launch: true,
      client_config: true,
      window_config: true,
      profile_config: true,
      agent_records: true,
      runtime_tags: true,
      work_presence: true
    }
  },
  {
    id: "antigravity",
    provider_id: "antigravity_cli",
    label: "Antigravity CLI",
    aliases: ["antigravity-cli", "antigravity_cli", "agy"],
    default_command: "agy-p",
    command_names: ["agy-p", "agy"],
    capabilities: {
      launch: true,
      client_config: true,
      window_config: true,
      profile_config: true,
      agent_records: true,
      runtime_tags: false,
      work_presence: false
    }
  }
];

export const AGENT_LAUNCH_LABELS: Record<string, string> = {
  shell: "No Agent",
  codex: "Codex",
  claude: "Claude Code",
  cursor: "Cursor",
  antigravity: "Antigravity CLI"
};

const AGENT_CLIENT_CAPABILITY_DEFAULTS: Required<NonNullable<AgentClient["capabilities"]>> = {
  launch: true,
  client_config: true,
  window_config: true,
  profile_config: true,
  agent_records: false,
  runtime_tags: false,
  work_presence: false
};

export function agentLaunchOptions(agentClients: AgentClient[]): Array<{ id: AgentLaunchMode; label: string }> {
  return [
    { id: "shell", label: AGENT_LAUNCH_LABELS.shell },
    ...agentClientOptions(agentClients, "launch")
  ];
}

export function agentClientOptions(
  agentClients: AgentClient[],
  capability?: keyof NonNullable<AgentClient["capabilities"]>
): Array<{ id: AgentLaunchKind; label: string }> {
  return agentClients
    .filter((agentClient) => capability === undefined || agentClientCapability(agentClient.id, agentClients, capability))
    .map((agentClient) => ({ id: agentClient.id, label: agentClient.label }));
}

export function agentLabel(agent: string, agentClients: AgentClient[]): string {
  return agentClients.find((agentClient) => agentClient.id === agent)?.label ?? AGENT_LAUNCH_LABELS[agent] ?? agent;
}

export function agentDefaultCommand(agent: string, agentClients: AgentClient[]): string {
  return agentClients.find((agentClient) => agentClient.id === agent)?.default_command ?? readDefaultAgentCommands()[agent] ?? agent;
}

export function agentClientCapability(
  agent: string,
  agentClients: AgentClient[],
  capability: keyof NonNullable<AgentClient["capabilities"]>
): boolean {
  const client = agentClients.find((agentClient) => agentClient.id === agent);
  if (!client) {
    return false;
  }
  return client.capabilities?.[capability] ?? AGENT_CLIENT_CAPABILITY_DEFAULTS[capability];
}

export function isAgentLaunchKind(value: AgentLaunchMode): value is AgentLaunchKind {
  return value !== "shell";
}

export function readDefaultAgentCommands(): AgentCommandSettings {
  return readAgentCommandSettings();
}

export function agentLaunchForKind(agent: AgentLaunchKind): AgentLaunchConfig {
  const command = readDefaultAgentCommands()[agent] ?? agent;
  return {
    agent,
    command: command.trim() || agent,
    config: null
  };
}

export function agentLaunchForClient(agent: AgentLaunchKind, agentClients: AgentClient[]): AgentLaunchConfig {
  const command = readDefaultAgentCommands()[agent] ?? agentDefaultCommand(agent, agentClients);
  return {
    agent,
    command: command.trim() || agentDefaultCommand(agent, agentClients),
    config: null
  };
}

export function configToSelection(config: AgentConfig): AgentConfigSelection {
  return {
    agent: config.agent,
    sections: config.sections.map((section) => ({
      id: section.id,
      items: section.items.map((item) => ({
        id: item.id,
        enabled: item.enabled
      }))
    }))
  };
}

export function selectionItemCount(selection: AgentConfigSelection | null): number {
  return selection?.sections.reduce((count, section) => count + section.items.length, 0) ?? 0;
}

export function selectedEnabledCount(selection: AgentConfigSelection | null): number {
  return selection?.sections.reduce(
    (count, section) => count + section.items.filter((item) => item.enabled).length,
    0
  ) ?? 0;
}

export function updateSelectionItem(
  selection: AgentConfigSelection,
  sectionId: AgentConfigSelectionSection["id"],
  itemId: string,
  enabled: boolean
): AgentConfigSelection {
  return {
    ...selection,
    sections: selection.sections.map((section) => {
      if (section.id !== sectionId) {
        return section;
      }
      return {
        ...section,
        items: section.items.map((item) => (item.id === itemId ? { ...item, enabled } : item))
      };
    })
  };
}
