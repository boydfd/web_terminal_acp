import {
  useMutation,
  useQuery,
  useQueryClient
} from "@tanstack/react-query";
import {
  createAgentProfile,
  deleteAgentProfile,
  fetchAgentClients,
  fetchAgentProfileConfig,
  fetchAgentProfiles,
  updateAgentProfile,
  updateAgentProfileConfigItem
} from "../api";
import {
  DEFAULT_AGENT_CLIENTS,
  agentClientCapability,
  agentClientOptions,
  agentLabel
} from "../agentLaunch";
import type { AgentClient, AgentConfig, AgentLaunchKind, AgentProfile } from "../types";
import { AgentConfigViewer } from "./AgentConfigViewer";
import {
  desktopNotificationsSupported,
  readDesktopNotificationsEnabled,
  readAgentCommandSettings,
  readSummaryOutputLanguage,
  readTerminalGroupingMode,
  readThemeSkin,
  type AgentCommandSettings,
  type SummaryOutputLanguage,
  type TerminalGroupingMode,
  type ThemeSkinId,
  writeAgentCommandSettings,
  writeDesktopNotificationsEnabled,
  writeSummaryOutputLanguage,
  writeTerminalGroupingMode,
  writeThemeSkin
} from "../userPreferences";
import { ensureDesktopNotificationPermission } from "../desktopNotifications";
import { normalizeApiBaseInput, readApiBase, readConfiguredApiBase, writeConfiguredApiBase } from "../apiBase";
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import {
  createCustomQuickKey,
  quickKeyToken,
  TERMINAL_SPECIAL_KEYS,
  type CustomQuickKey
} from "../terminalQuickKeys";
import {
  effectiveKeyboardShortcut,
  keyboardShortcutLabel,
  keyboardShortcutsEqual,
  KEYBOARD_SHORTCUT_DEFINITIONS,
  shortcutForCapture,
  type KeyboardShortcut,
  type KeyboardShortcutBindings,
  type KeyboardShortcutId
} from "../keyboardShortcuts";
import { THEME_SKINS } from "../themeSkins";
import { useOverlayFocus } from "./useOverlayFocus";

export type SettingsView = "general" | "theme" | "shortcuts" | "quick-keys";
type SettingsTabId = SettingsView | "agents" | "account";
type ShortcutBindingTarget =
  | { type: "builtin"; id: KeyboardShortcutId }
  | { type: "quick-key"; id: string }
  | { type: "quick-key-draft" };

type SettingsDraft = {
  apiBase: string;
  summaryOutputLanguage: SummaryOutputLanguage;
  terminalGroupingMode: TerminalGroupingMode;
  themeSkin: ThemeSkinId;
  desktopNotificationsEnabled: boolean;
  agentCommandSettings: AgentCommandSettings;
  keyboardShortcutBindings: KeyboardShortcutBindings;
  customQuickKeys: CustomQuickKey[];
};

const SETTINGS_TABS: Array<{ id: SettingsTabId; label: string }> = [
  { id: "general", label: "基础" },
  { id: "theme", label: "界面" },
  { id: "agents", label: "Agent" },
  { id: "shortcuts", label: "快捷键" },
  { id: "quick-keys", label: "快速按键" },
  { id: "account", label: "账号" }
];

type SettingsModalProps = {
  isOpen: boolean;
  onClose: () => void;
  summaryOutputLanguage: SummaryOutputLanguage;
  terminalGroupingMode: TerminalGroupingMode;
  themeSkin: ThemeSkinId;
  desktopNotificationsEnabled: boolean;
  keyboardShortcutBindings: KeyboardShortcutBindings;
  customQuickKeys: CustomQuickKey[];
  selectedClientId: string | null;
  onSummaryOutputLanguageChange: (language: SummaryOutputLanguage) => void;
  onTerminalGroupingModeChange: (mode: TerminalGroupingMode) => void;
  onThemeSkinChange: (themeSkin: ThemeSkinId) => void;
  onDesktopNotificationsEnabledChange: (enabled: boolean) => void;
  onKeyboardShortcutBindingsChange: (bindings: KeyboardShortcutBindings) => void;
  onCustomQuickKeysChange: (quickKeys: CustomQuickKey[]) => void;
  authEnabled: boolean;
  initialView?: SettingsView;
  onboardingEnabled: boolean;
  onStartOnboarding: () => void;
  onLogout: () => void;
};

function shortcutTargetKey(target: ShortcutBindingTarget): string {
  return target.type === "quick-key-draft" ? target.type : `${target.type}:${target.id}`;
}

function copyKeyboardShortcut(shortcut: KeyboardShortcut | null | undefined): KeyboardShortcut | null | undefined {
  return shortcut === undefined || shortcut === null ? shortcut : { ...shortcut };
}

function copyKeyboardShortcutBindings(bindings: KeyboardShortcutBindings): KeyboardShortcutBindings {
  const nextBindings: KeyboardShortcutBindings = {};
  for (const definition of KEYBOARD_SHORTCUT_DEFINITIONS) {
    if (Object.prototype.hasOwnProperty.call(bindings, definition.id)) {
      nextBindings[definition.id] = copyKeyboardShortcut(bindings[definition.id]);
    }
  }
  return nextBindings;
}

function copyCustomQuickKeys(quickKeys: CustomQuickKey[]): CustomQuickKey[] {
  return quickKeys.map((quickKey) => ({
    ...quickKey,
    ...(quickKey.shortcut !== undefined ? { shortcut: copyKeyboardShortcut(quickKey.shortcut) } : {})
  }));
}

function copyAgentCommandSettings(settings: AgentCommandSettings): AgentCommandSettings {
  return { ...settings };
}

function shortcutSignature(shortcut: KeyboardShortcut | null | undefined): string {
  if (shortcut === undefined) {
    return "undefined";
  }
  if (shortcut === null) {
    return "null";
  }
  return [
    shortcut.key,
    shortcut.alt === true ? "1" : "0",
    shortcut.ctrl === true ? "1" : "0",
    shortcut.meta === true ? "1" : "0",
    shortcut.shift === true ? "1" : "0"
  ].join(":");
}

function keyboardShortcutBindingsEqual(left: KeyboardShortcutBindings, right: KeyboardShortcutBindings): boolean {
  return KEYBOARD_SHORTCUT_DEFINITIONS.every((definition) => {
    const leftHasKey = Object.prototype.hasOwnProperty.call(left, definition.id);
    const rightHasKey = Object.prototype.hasOwnProperty.call(right, definition.id);
    if (leftHasKey !== rightHasKey) {
      return false;
    }
    return shortcutSignature(left[definition.id]) === shortcutSignature(right[definition.id]);
  });
}

function customQuickKeysEqual(left: CustomQuickKey[], right: CustomQuickKey[]): boolean {
  if (left.length !== right.length) {
    return false;
  }
  return left.every((quickKey, index) => {
    const other = right[index];
    return quickKey.id === other.id
      && quickKey.label === other.label
      && quickKey.input === other.input
      && shortcutSignature(quickKey.shortcut) === shortcutSignature(other.shortcut);
  });
}

function agentCommandSettingsEqual(left: AgentCommandSettings, right: AgentCommandSettings): boolean {
  const keys = new Set([...Object.keys(left), ...Object.keys(right)]);
  for (const key of keys) {
    if ((left[key] ?? "") !== (right[key] ?? "")) {
      return false;
    }
  }
  return true;
}

function createSettingsDraft(input: {
  apiBase: string;
  summaryOutputLanguage: SummaryOutputLanguage;
  terminalGroupingMode: TerminalGroupingMode;
  themeSkin: ThemeSkinId;
  desktopNotificationsEnabled: boolean;
  agentCommandSettings: AgentCommandSettings;
  keyboardShortcutBindings: KeyboardShortcutBindings;
  customQuickKeys: CustomQuickKey[];
}): SettingsDraft {
  return {
    apiBase: input.apiBase,
    summaryOutputLanguage: input.summaryOutputLanguage,
    terminalGroupingMode: input.terminalGroupingMode,
    themeSkin: input.themeSkin,
    desktopNotificationsEnabled: input.desktopNotificationsEnabled,
    agentCommandSettings: copyAgentCommandSettings(input.agentCommandSettings),
    keyboardShortcutBindings: copyKeyboardShortcutBindings(input.keyboardShortcutBindings),
    customQuickKeys: copyCustomQuickKeys(input.customQuickKeys)
  };
}

function settingsDraftsEqual(left: SettingsDraft, right: SettingsDraft): boolean {
  return left.apiBase === right.apiBase
    && left.summaryOutputLanguage === right.summaryOutputLanguage
    && left.terminalGroupingMode === right.terminalGroupingMode
    && left.themeSkin === right.themeSkin
    && left.desktopNotificationsEnabled === right.desktopNotificationsEnabled
    && agentCommandSettingsEqual(left.agentCommandSettings, right.agentCommandSettings)
    && keyboardShortcutBindingsEqual(left.keyboardShortcutBindings, right.keyboardShortcutBindings)
    && customQuickKeysEqual(left.customQuickKeys, right.customQuickKeys);
}

function shortcutConflictLabel(
  shortcut: KeyboardShortcut | null,
  target: ShortcutBindingTarget,
  keyboardShortcutBindings: KeyboardShortcutBindings,
  customQuickKeys: CustomQuickKey[]
): string | null {
  if (shortcut === null) {
    return null;
  }

  const targetKey = shortcutTargetKey(target);
  for (const definition of KEYBOARD_SHORTCUT_DEFINITIONS) {
    if (targetKey === shortcutTargetKey({ type: "builtin", id: definition.id })) {
      continue;
    }
    if (keyboardShortcutsEqual(shortcut, effectiveKeyboardShortcut(definition.id, keyboardShortcutBindings))) {
      return definition.label;
    }
  }

  for (const quickKey of customQuickKeys) {
    if (targetKey === shortcutTargetKey({ type: "quick-key", id: quickKey.id })) {
      continue;
    }
    if (keyboardShortcutsEqual(shortcut, quickKey.shortcut ?? null)) {
      return quickKey.label;
    }
  }

  return null;
}

function ShortcutRecorder({
  label,
  shortcut,
  target,
  recordingTarget,
  conflictLabel,
  onStartRecording,
  onCapture,
  onClear
}: {
  label: string;
  shortcut: KeyboardShortcut | null;
  target: ShortcutBindingTarget;
  recordingTarget: ShortcutBindingTarget | null;
  conflictLabel: string | null;
  onStartRecording: (target: ShortcutBindingTarget) => void;
  onCapture: (target: ShortcutBindingTarget, event: ReactKeyboardEvent<HTMLButtonElement>) => void;
  onClear: (target: ShortcutBindingTarget) => void;
}) {
  const recording = recordingTarget !== null && shortcutTargetKey(recordingTarget) === shortcutTargetKey(target);

  return (
    <div className="shortcut-recorder">
      <button
        type="button"
        className={recording ? "shortcut-recorder-button recording" : "shortcut-recorder-button"}
        aria-label={`绑定${label}`}
        onClick={() => {
          if (recording) {
            return;
          }
          onStartRecording(target);
        }}
        onKeyDown={(event) => {
          if (recording) {
            onCapture(target, event);
          }
        }}
      >
        {recording ? "按下快捷键" : keyboardShortcutLabel(shortcut)}
      </button>
      <button type="button" onClick={() => onClear(target)}>
        清除
      </button>
      {conflictLabel !== null && (
        <span className="shortcut-conflict">冲突：{conflictLabel}</span>
      )}
    </div>
  );
}

function AgentProfilesSettings({
  selectedClientId,
  agentClients,
  profiles,
  selectedProfile,
  profileConfig,
  profileConfigAgentClient,
  profileDraftName,
  profileDraftDescription,
  profileDraftAgentClient,
  profileAgentMdDraft,
  pendingProfileConfigItem,
  profilesLoading,
  profilesError,
  configLoading,
  configError,
  configFetching,
  creatingProfile,
  updatingProfile,
  deletingProfile,
  updatingConfig,
  onSelectProfile,
  onProfileDraftNameChange,
  onProfileDraftDescriptionChange,
  onProfileDraftAgentClientChange,
  onCreateProfile,
  onDeleteProfile,
  onProfileConfigAgentClientChange,
  onSaveProfileBasics,
  onAgentMdDraftChange,
  onSaveAgentMd,
  onToggleConfigItem
}: {
  selectedClientId: string | null;
  agentClients: AgentClient[];
  profiles: AgentProfile[];
  selectedProfile: AgentProfile | null;
  profileConfig: AgentConfig | null;
  profileConfigAgentClient: AgentLaunchKind;
  profileDraftName: string;
  profileDraftDescription: string;
  profileDraftAgentClient: AgentLaunchKind;
  profileAgentMdDraft: string;
  pendingProfileConfigItem: string | null;
  profilesLoading: boolean;
  profilesError: boolean;
  configLoading: boolean;
  configError: boolean;
  configFetching: boolean;
  creatingProfile: boolean;
  updatingProfile: boolean;
  deletingProfile: boolean;
  updatingConfig: boolean;
  onSelectProfile: (profileId: string) => void;
  onProfileDraftNameChange: (value: string) => void;
  onProfileDraftDescriptionChange: (value: string) => void;
  onProfileDraftAgentClientChange: (value: AgentLaunchKind) => void;
  onCreateProfile: () => void;
  onDeleteProfile: (profile: AgentProfile) => void;
  onProfileConfigAgentClientChange: (value: AgentLaunchKind) => void;
  onSaveProfileBasics: (profile: AgentProfile) => void;
  onAgentMdDraftChange: (value: string) => void;
  onSaveAgentMd: (profile: AgentProfile) => void;
  onToggleConfigItem: (sectionId: string, itemId: string, enabled: boolean) => void;
}) {
  const canCreate = selectedClientId !== null && profileDraftName.trim().length > 0 && !creatingProfile;
  const agentOptions = agentClientOptions(agentClients, "launch");
  const configAgentOptions = agentClientOptions(agentClients, "profile_config");
  const configAgentSupported = agentClientCapability(profileConfigAgentClient, agentClients, "profile_config");

  return (
    <section className="agent-profile-settings">
      {selectedClientId === null ? (
        <p className="muted">先选择一个 client。</p>
      ) : profilesError ? (
        <p className="error" role="alert">加载 agent 配置失败。</p>
      ) : (
        <>
          <div className="agent-profile-grid">
            <div className="agent-profile-list">
              <div className="quick-key-list-header">
                <h3>Agents</h3>
                <span>{profilesLoading ? "..." : profiles.length}</span>
              </div>
              {profiles.length === 0 ? (
                <p className="muted quick-key-empty-state">还没有 agent。</p>
              ) : (
                profiles.map((profile) => (
                  <button
                    key={profile.id}
                    type="button"
                    className={selectedProfile?.id === profile.id ? "agent-profile-row selected" : "agent-profile-row"}
                    onClick={() => onSelectProfile(profile.id)}
                  >
                    <strong>{profile.name}</strong>
                    <span>{agentLabel(profile.default_agent_client, agentClients)}</span>
                  </button>
                ))
              )}
            </div>
            <div className="agent-profile-editor">
              <div className="agent-profile-create-row">
                <label className="settings-field">
                  <span>名称</span>
                  <input
                    value={profileDraftName}
                    onChange={(event) => onProfileDraftNameChange(event.target.value)}
                    placeholder="例如：Review Agent"
                  />
                </label>
                <label className="settings-field">
                  <span>默认 agent-client</span>
                  <select
                    value={profileDraftAgentClient}
                    onChange={(event) => onProfileDraftAgentClientChange(event.target.value as AgentLaunchKind)}
                  >
                    {agentOptions.map((option) => (
                      <option key={option.id} value={option.id}>{option.label}</option>
                    ))}
                  </select>
                </label>
                <label className="settings-field">
                  <span>描述</span>
                  <input
                    value={profileDraftDescription}
                    onChange={(event) => onProfileDraftDescriptionChange(event.target.value)}
                    placeholder="可选"
                  />
                </label>
                <div className="settings-actions">
                  <button type="button" disabled={!canCreate} onClick={onCreateProfile}>
                    {creatingProfile ? "创建中..." : "创建 agent"}
                  </button>
                </div>
              </div>

              {selectedProfile !== null && (
                <div className="agent-profile-detail">
                  <div className="quick-key-list-header">
                    <h3>{selectedProfile.name}</h3>
                    <button
                      type="button"
                      disabled={deletingProfile}
                      onClick={() => onDeleteProfile(selectedProfile)}
                    >
                      删除
                    </button>
                  </div>
                  <div className="settings-agent-command-grid">
                    <label className="settings-field">
                      <span>名称</span>
                      <input
                        defaultValue={selectedProfile.name}
                        onBlur={(event) => {
                          const nextName = event.target.value.trim();
                          if (nextName && nextName !== selectedProfile.name) {
                            onSaveProfileBasics({ ...selectedProfile, name: nextName });
                          }
                        }}
                      />
                    </label>
                    <label className="settings-field">
                      <span>默认 agent-client</span>
                      <select
                        value={selectedProfile.default_agent_client}
                        onChange={(event) => onSaveProfileBasics({
                          ...selectedProfile,
                          default_agent_client: event.target.value as AgentLaunchKind
                        })}
                      >
                        {agentOptions.map((option) => (
                          <option key={option.id} value={option.id}>{option.label}</option>
                        ))}
                      </select>
                    </label>
                  </div>
                  <label className="settings-field">
                    <span>描述</span>
                    <input
                      defaultValue={selectedProfile.description ?? ""}
                      onBlur={(event) => {
                        const nextDescription = event.target.value.trim();
                        if (nextDescription !== (selectedProfile.description ?? "")) {
                          onSaveProfileBasics({ ...selectedProfile, description: nextDescription || null });
                        }
                      }}
                    />
                  </label>
                  <label className="settings-field agent-profile-agent-md">
                    <span>AGENT.md</span>
                    <textarea
                      rows={8}
                      value={profileAgentMdDraft}
                      onChange={(event) => onAgentMdDraftChange(event.target.value)}
                    />
                  </label>
                  <div className="settings-actions">
                    <button
                      type="button"
                      disabled={updatingProfile || profileAgentMdDraft === selectedProfile.agent_md}
                      onClick={() => onSaveAgentMd(selectedProfile)}
                    >
                      保存 AGENT.md
                    </button>
                  </div>
                  {configAgentOptions.length > 0 && (
                    <label className="settings-field">
                      <span>配置目标 agent-client</span>
                      <select
                        value={profileConfigAgentClient}
                        onChange={(event) => onProfileConfigAgentClientChange(event.target.value as AgentLaunchKind)}
                      >
                        {configAgentOptions.map((option) => (
                          <option key={option.id} value={option.id}>{option.label}</option>
                        ))}
                      </select>
                    </label>
                  )}
                  {configAgentSupported && (
                    <AgentConfigViewer
                      config={profileConfig}
                      isLoading={configLoading}
                      isError={configError}
                      isFetching={configFetching}
                      pendingItemId={pendingProfileConfigItem}
                      isToggling={updatingConfig}
                      title="Agent 配置"
                      metaPrefix="profile config"
                      emptyMessage="Failed to load profile config."
                      onToggleItem={onToggleConfigItem}
                    />
                  )}
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </section>
  );
}

export function SettingsModal({
  isOpen,
  onClose,
  summaryOutputLanguage,
  terminalGroupingMode,
  themeSkin,
  desktopNotificationsEnabled,
  keyboardShortcutBindings,
  customQuickKeys,
  selectedClientId,
  onSummaryOutputLanguageChange,
  onTerminalGroupingModeChange,
  onThemeSkinChange,
  onDesktopNotificationsEnabledChange,
  onKeyboardShortcutBindingsChange,
  onCustomQuickKeysChange,
  authEnabled,
  initialView = "general",
  onboardingEnabled,
  onStartOnboarding,
  onLogout
}: SettingsModalProps) {
  const currentPersistedDraft = useMemo(() => createSettingsDraft({
    apiBase: readConfiguredApiBase(),
    summaryOutputLanguage,
    terminalGroupingMode,
    themeSkin,
    desktopNotificationsEnabled,
    agentCommandSettings: readAgentCommandSettings(),
    keyboardShortcutBindings,
    customQuickKeys
  }), [
    summaryOutputLanguage,
    terminalGroupingMode,
    themeSkin,
    desktopNotificationsEnabled,
    keyboardShortcutBindings,
    customQuickKeys
  ]);
  const [draft, setDraft] = useState<SettingsDraft>(currentPersistedDraft);
  const [savedDraft, setSavedDraft] = useState<SettingsDraft>(currentPersistedDraft);
  const [apiBaseError, setApiBaseError] = useState<string | null>(null);
  const [saveStatus, setSaveStatus] = useState<string | null>(null);
  const [view, setView] = useState<SettingsTabId>("general");
  const [selectedAgentProfileId, setSelectedAgentProfileId] = useState<string | null>(null);
  const [profileDraftName, setProfileDraftName] = useState("");
  const [profileDraftDescription, setProfileDraftDescription] = useState("");
  const [profileDraftAgentClient, setProfileDraftAgentClient] = useState<AgentLaunchKind>("codex");
  const [profileConfigAgentClient, setProfileConfigAgentClient] = useState<AgentLaunchKind>("codex");
  const [profileAgentMdDraft, setProfileAgentMdDraft] = useState("");
  const [pendingProfileConfigItem, setPendingProfileConfigItem] = useState<string | null>(null);
  const [quickKeyDraft, setQuickKeyDraft] = useState<CustomQuickKey>(() => createCustomQuickKey());
  const [recordingShortcutTarget, setRecordingShortcutTarget] = useState<ShortcutBindingTarget | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const wasOpenRef = useRef(false);
  const queryClient = useQueryClient();
  const agentProfilesQuery = useQuery({
    queryKey: ["agent-profiles", selectedClientId],
    queryFn: () => fetchAgentProfiles(selectedClientId as string),
    enabled: isOpen && view === "agents" && selectedClientId !== null
  });
  const agentClientsQuery = useQuery({
    queryKey: ["agent-clients", selectedClientId],
    queryFn: () => fetchAgentClients(selectedClientId as string),
    enabled: isOpen && view === "agents" && selectedClientId !== null,
    staleTime: 60000
  });
  const agentClients = agentClientsQuery.data?.agent_clients ?? DEFAULT_AGENT_CLIENTS;
  const agentProfiles = agentProfilesQuery.data?.profiles ?? [];
  const selectedAgentProfile = agentProfiles.find((profile) => profile.id === selectedAgentProfileId) ?? agentProfiles[0] ?? null;
  const profileConfigSupported = agentClientCapability(profileConfigAgentClient, agentClients, "profile_config");
  const profileConfigQuery = useQuery({
    queryKey: ["agent-profile-config", selectedClientId, selectedAgentProfile?.id ?? null, profileConfigAgentClient],
    queryFn: () => fetchAgentProfileConfig(selectedClientId as string, selectedAgentProfile?.id as string, profileConfigAgentClient),
    enabled: isOpen && view === "agents" && selectedClientId !== null && selectedAgentProfile !== null && profileConfigSupported
  });
  const createProfileMutation = useMutation({
    mutationFn: () => createAgentProfile(selectedClientId as string, {
      name: profileDraftName,
      description: profileDraftDescription || null,
      default_agent_client: profileDraftAgentClient,
      source_agent_client: profileDraftAgentClient
    }),
    onSuccess: (profile) => {
      queryClient.invalidateQueries({ queryKey: ["agent-profiles", selectedClientId] });
      setSelectedAgentProfileId(profile.id);
      setProfileConfigAgentClient(profile.default_agent_client);
      setProfileDraftName("");
      setProfileDraftDescription("");
    }
  });
  const updateProfileMutation = useMutation({
    mutationFn: (input: { profile: AgentProfile; patch: Partial<Pick<AgentProfile, "name" | "description" | "default_agent_client" | "agent_md">> }) =>
      updateAgentProfile(selectedClientId as string, input.profile.id, input.patch),
    onSuccess: (profile) => {
      queryClient.invalidateQueries({ queryKey: ["agent-profiles", selectedClientId] });
      queryClient.setQueryData(["agent-profiles", selectedClientId], (current: { profiles?: AgentProfile[] } | undefined) => ({
        profiles: (current?.profiles ?? []).map((candidate) => candidate.id === profile.id ? profile : candidate)
      }));
    }
  });
  const deleteProfileMutation = useMutation({
    mutationFn: (profile: AgentProfile) => deleteAgentProfile(selectedClientId as string, profile.id),
    onSuccess: (_result, profile) => {
      queryClient.invalidateQueries({ queryKey: ["agent-profiles", selectedClientId] });
      if (selectedAgentProfileId === profile.id) {
        setSelectedAgentProfileId(null);
      }
    }
  });
  const updateProfileConfigMutation = useMutation({
    mutationFn: (input: { sectionId: string; itemId: string; enabled: boolean }) =>
      updateAgentProfileConfigItem(
        selectedClientId as string,
        selectedAgentProfile?.id as string,
        profileConfigAgentClient,
        input.sectionId,
        input.itemId,
        input.enabled
      ),
    onMutate: (input) => setPendingProfileConfigItem(`${input.sectionId}:${input.itemId}`),
    onSuccess: (config) => {
      queryClient.setQueryData(
        ["agent-profile-config", selectedClientId, selectedAgentProfile?.id ?? null, profileConfigAgentClient],
        config
      );
      queryClient.invalidateQueries({ queryKey: ["agent-profiles", selectedClientId] });
    },
    onSettled: () => setPendingProfileConfigItem(null)
  });
  const hasUnsavedChanges = !settingsDraftsEqual(draft, savedDraft);
  const canShowAccountTab = authEnabled || onboardingEnabled;
  const visibleTabs = useMemo(
    () => SETTINGS_TABS.filter((tab) => tab.id !== "account" || canShowAccountTab),
    [canShowAccountTab]
  );
  const quickKeyDraftValid = quickKeyDraft.label.trim().length > 0 && quickKeyDraft.input.length > 0;
  const shortcutRows: Array<{
    target: ShortcutBindingTarget;
    label: string;
    shortcut: KeyboardShortcut | null;
    defaultShortcut?: KeyboardShortcut;
  }> = useMemo(() => [
    ...KEYBOARD_SHORTCUT_DEFINITIONS.map((definition) => ({
      target: { type: "builtin", id: definition.id } as ShortcutBindingTarget,
      label: definition.label,
      shortcut: effectiveKeyboardShortcut(definition.id, draft.keyboardShortcutBindings),
      defaultShortcut: definition.defaultShortcut
    })),
    ...draft.customQuickKeys.map((quickKey) => ({
      target: { type: "quick-key", id: quickKey.id } as ShortcutBindingTarget,
      label: `快捷按键：${quickKey.label}`,
      shortcut: quickKey.shortcut ?? null
    }))
  ], [draft.customQuickKeys, draft.keyboardShortcutBindings]);

  useEffect(() => {
    if (!isOpen) {
      wasOpenRef.current = false;
      return;
    }
    if (wasOpenRef.current) {
      return;
    }

    wasOpenRef.current = true;
    const nextDraft = createSettingsDraft({
      apiBase: readConfiguredApiBase(),
      summaryOutputLanguage,
      terminalGroupingMode,
      themeSkin,
      desktopNotificationsEnabled,
      agentCommandSettings: readAgentCommandSettings(),
      keyboardShortcutBindings,
      customQuickKeys
    });
    setDraft(nextDraft);
    setSavedDraft(nextDraft);
    setApiBaseError(null);
    setSaveStatus(null);
    setRecordingShortcutTarget(null);
    setView(initialView);
  }, [
    customQuickKeys,
    desktopNotificationsEnabled,
    initialView,
    isOpen,
    keyboardShortcutBindings,
    summaryOutputLanguage,
    terminalGroupingMode,
    themeSkin
  ]);

  useEffect(() => {
    if (selectedAgentProfile === null) {
      setProfileAgentMdDraft("");
      return;
    }
    setProfileAgentMdDraft(selectedAgentProfile.agent_md);
    setProfileConfigAgentClient(selectedAgentProfile.default_agent_client);
  }, [selectedAgentProfile?.id, selectedAgentProfile?.agent_md, selectedAgentProfile?.default_agent_client]);

  useEffect(() => {
    if (agentClientCapability(profileConfigAgentClient, agentClients, "profile_config")) {
      return;
    }
    const fallback = agentClients.find((agentClient) =>
      agentClientCapability(agentClient.id, agentClients, "profile_config")
    );
    if (fallback !== undefined) {
      setProfileConfigAgentClient(fallback.id);
    }
  }, [agentClients, profileConfigAgentClient]);

  useEffect(() => {
    if (selectedAgentProfileId !== null || agentProfiles.length === 0) {
      return;
    }
    setSelectedAgentProfileId(agentProfiles[0].id);
  }, [agentProfiles, selectedAgentProfileId]);

  const changeDraft = (patch: Partial<SettingsDraft>) => {
    setDraft((current) => ({ ...current, ...patch }));
    setSaveStatus(null);
  };

  const requestClose = useCallback(() => {
    if (recordingShortcutTarget !== null) {
      setRecordingShortcutTarget(null);
      return;
    }

    if (hasUnsavedChanges && !window.confirm("设置有未保存的修改，退出后将丢弃这些修改。是否退出？")) {
      return;
    }
    onClose();
  }, [hasUnsavedChanges, onClose, recordingShortcutTarget]);

  const saveSettings = async () => {
    try {
      const normalizedApiBase = normalizeApiBaseInput(draft.apiBase);
      const previousApiBase = savedDraft.apiBase;
      if (
        draft.desktopNotificationsEnabled
        && !savedDraft.desktopNotificationsEnabled
        && desktopNotificationsSupported()
      ) {
        const permission = await ensureDesktopNotificationPermission();
        if (permission !== "granted") {
          setDraft((current) => ({ ...current, desktopNotificationsEnabled: false }));
          setApiBaseError(null);
          setSaveStatus("系统未授予通知权限，已保持关闭。");
          return;
        }
      }

      writeSummaryOutputLanguage(draft.summaryOutputLanguage);
      writeTerminalGroupingMode(draft.terminalGroupingMode);
      writeThemeSkin(draft.themeSkin);
      writeDesktopNotificationsEnabled(draft.desktopNotificationsEnabled);
      writeAgentCommandSettings(draft.agentCommandSettings);
      writeConfiguredApiBase(normalizedApiBase);

      onSummaryOutputLanguageChange(draft.summaryOutputLanguage);
      onTerminalGroupingModeChange(draft.terminalGroupingMode);
      onThemeSkinChange(draft.themeSkin);
      onDesktopNotificationsEnabledChange(draft.desktopNotificationsEnabled);
      onKeyboardShortcutBindingsChange(copyKeyboardShortcutBindings(draft.keyboardShortcutBindings));
      onCustomQuickKeysChange(copyCustomQuickKeys(draft.customQuickKeys));

      const nextSavedDraft = createSettingsDraft({ ...draft, apiBase: normalizedApiBase });
      setDraft(nextSavedDraft);
      setSavedDraft(nextSavedDraft);
      setApiBaseError(null);
      setSaveStatus("已保存");

      if (normalizedApiBase !== previousApiBase) {
        window.location.reload();
      }
    } catch {
      setApiBaseError("请输入有效的 HTTP/HTTPS 地址");
      setSaveStatus(null);
    }
  };

  const updateAgentCommand = (agent: string, value: string) => {
    changeDraft({
      agentCommandSettings: { ...draft.agentCommandSettings, [agent]: value }
    });
  };

  useOverlayFocus({
    isOpen,
    ref: panelRef,
    onEscape: requestClose
  });

  if (!isOpen) {
    return null;
  }

  const createProfile = () => {
    if (selectedClientId === null || profileDraftName.trim().length === 0) {
      return;
    }
    createProfileMutation.mutate();
  };

  const saveProfileBasics = (profile: AgentProfile) => {
    updateProfileMutation.mutate({
      profile,
      patch: {
        name: profile.name,
        description: profile.description,
        default_agent_client: profile.default_agent_client
      }
    });
  };

  const saveProfileAgentMd = (profile: AgentProfile) => {
    updateProfileMutation.mutate({
      profile,
      patch: { agent_md: profileAgentMdDraft }
    });
  };

  const updateQuickKeyDraft = (patch: Partial<CustomQuickKey>) => {
    setQuickKeyDraft((current) => ({ ...current, ...patch }));
  };

  const resetQuickKeyDraft = () => {
    setQuickKeyDraft(createCustomQuickKey());
  };

  const addQuickKey = () => {
    if (!quickKeyDraftValid) {
      return;
    }

    changeDraft({
      customQuickKeys: [
        ...draft.customQuickKeys,
        {
          ...quickKeyDraft,
          label: quickKeyDraft.label.trim()
        }
      ]
    });
    resetQuickKeyDraft();
  };

  const updateQuickKey = (id: string, patch: Partial<CustomQuickKey>) => {
    changeDraft({
      customQuickKeys: draft.customQuickKeys.map((quickKey) => (
        quickKey.id === id ? { ...quickKey, ...patch } : quickKey
      ))
    });
  };

  const removeQuickKey = (id: string) => {
    changeDraft({
      customQuickKeys: draft.customQuickKeys.filter((quickKey) => quickKey.id !== id)
    });
  };

  const appendSpecialKeyToDraft = (token: string) => {
    updateQuickKeyDraft({ input: `${quickKeyDraft.input}${quickKeyToken(token)}` });
  };

  const bindShortcut = (target: ShortcutBindingTarget, shortcut: KeyboardShortcut | null) => {
    if (target.type === "builtin") {
      changeDraft({
        keyboardShortcutBindings: {
          ...draft.keyboardShortcutBindings,
          [target.id]: shortcut
        }
      });
      return;
    }

    if (target.type === "quick-key-draft") {
      updateQuickKeyDraft({ shortcut });
      return;
    }

    updateQuickKey(target.id, { shortcut });
  };

  const bindDefaultShortcut = (id: KeyboardShortcutId) => {
    const nextBindings = { ...draft.keyboardShortcutBindings };
    delete nextBindings[id];
    changeDraft({ keyboardShortcutBindings: nextBindings });
  };

  const resetAllBuiltInShortcuts = () => {
    changeDraft({ keyboardShortcutBindings: {} });
  };

  const captureShortcut = (target: ShortcutBindingTarget, event: ReactKeyboardEvent<HTMLButtonElement>) => {
    event.preventDefault();
    event.stopPropagation();

    if (event.key === "Escape") {
      setRecordingShortcutTarget(null);
      return;
    }

    const shortcut = shortcutForCapture(event.nativeEvent);
    if (shortcut === null) {
      return;
    }

    bindShortcut(target, shortcut);
    setRecordingShortcutTarget(null);
  };

  return (
    <div
      className="settings-modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          requestClose();
        }
      }}
    >
      <div
        ref={panelRef}
        aria-modal="true"
        className="settings-modal settings-modal-wide"
        data-onboarding-id="settings-modal"
        role="dialog"
        aria-label="Settings"
      >
        <div className="settings-modal-header">
          <div className="settings-modal-title">
            <h2>设置</h2>
            {hasUnsavedChanges && <span className="settings-dirty-badge">未保存</span>}
          </div>
          <button type="button" onClick={requestClose}>关闭</button>
        </div>
        <div className="settings-tabs" role="tablist" aria-label="设置分组">
          {visibleTabs.map((tab) => (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={view === tab.id}
              className={view === tab.id ? "active" : ""}
              onClick={() => setView(tab.id)}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {view === "general" ? (
          <>
            <label className="settings-field">
              <span>后端地址</span>
              <input
                value={draft.apiBase}
                onChange={(event) => {
                  changeDraft({ apiBase: event.target.value });
                  setApiBaseError(null);
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    void saveSettings();
                  }
                }}
                placeholder={readApiBase()}
              />
            </label>
            <div className="settings-actions">
              <button
                type="button"
                onClick={() => changeDraft({ apiBase: "" })}
              >
                使用默认后端地址
              </button>
            </div>
            {apiBaseError && (
              <p className="error settings-error" role="alert">
                {apiBaseError}
              </p>
            )}

            <label className="settings-field">
              <span>项目名显示语言</span>
              <select
                value={draft.summaryOutputLanguage}
                onChange={(event) => {
                  changeDraft({ summaryOutputLanguage: event.target.value as SummaryOutputLanguage });
                }}
              >
                <option value="中文">中文</option>
                <option value="English">English</option>
              </select>
            </label>

            <label className="settings-field">
              <span>终端列表分组方式</span>
              <select
                value={draft.terminalGroupingMode}
                onChange={(event) => {
                  changeDraft({ terminalGroupingMode: event.target.value as TerminalGroupingMode });
                }}
              >
                <option value="project-topic">项目 / 主题</option>
                <option value="topic">主题</option>
                <option value="time-topic">时间 / 主题 / 子主题</option>
                <option value="project-time-topic">项目 / 时间 / 主题 / 子主题</option>
              </select>
            </label>

            {desktopNotificationsSupported() && (
              <label className="settings-field settings-field-checkbox">
                <span>系统桌面通知</span>
                <input
                  type="checkbox"
                  checked={draft.desktopNotificationsEnabled}
                  onChange={(event) => changeDraft({ desktopNotificationsEnabled: event.target.checked })}
                />
              </label>
            )}

            <p className="muted settings-hint">
              快捷键：{keyboardShortcutLabel(effectiveKeyboardShortcut("settings", draft.keyboardShortcutBindings))} 打开设置
            </p>
          </>
        ) : view === "theme" ? (
          <section className="settings-theme-page">
            <label className="settings-field">
              <span>当前皮肤</span>
              <select
                value={draft.themeSkin}
                onChange={(event) => {
                  changeDraft({ themeSkin: event.target.value as ThemeSkinId });
                }}
              >
                {THEME_SKINS.map((skin) => (
                  <option key={skin.id} value={skin.id}>
                    {skin.label}
                  </option>
                ))}
              </select>
            </label>
            <div className="settings-skin-preview-grid" aria-label="皮肤预览">
              {THEME_SKINS.map((skin) => (
                <button
                  key={skin.id}
                  type="button"
                  className={[
                    "settings-skin-preview",
                    skin.cssClass,
                    skin.id === draft.themeSkin ? "selected" : ""
                  ].filter(Boolean).join(" ")}
                  aria-pressed={skin.id === draft.themeSkin}
                  onClick={() => {
                    changeDraft({ themeSkin: skin.id });
                  }}
                >
                  <span className="settings-skin-preview-header">
                    <strong>{skin.label}</strong>
                    <span>{skin.source}</span>
                  </span>
                  <span className="settings-skin-preview-swatch" aria-hidden="true">
                    <i />
                    <i />
                    <i />
                  </span>
                  <span className="settings-skin-preview-summary">{skin.summary}</span>
                </button>
              ))}
            </div>
          </section>
        ) : view === "agents" ? (
          <section className="settings-agent-page">
            <div className="settings-agent-command-grid">
              {agentClients.map((agentClient) => (
                <label key={agentClient.id} className="settings-field">
                  <span>{agentClient.label} 启动命令</span>
                  <input
                    value={draft.agentCommandSettings[agentClient.id] ?? agentClient.default_command}
                    onChange={(event) => updateAgentCommand(agentClient.id, event.target.value)}
                    placeholder={agentClient.default_command}
                  />
                </label>
              ))}
            </div>
            <AgentProfilesSettings
              selectedClientId={selectedClientId}
              agentClients={agentClients}
              profiles={agentProfiles}
              selectedProfile={selectedAgentProfile}
              profileConfig={profileConfigQuery.data ?? null}
              profileConfigAgentClient={profileConfigAgentClient}
              profileDraftName={profileDraftName}
              profileDraftDescription={profileDraftDescription}
              profileDraftAgentClient={profileDraftAgentClient}
              profileAgentMdDraft={profileAgentMdDraft}
              pendingProfileConfigItem={pendingProfileConfigItem}
              profilesLoading={agentProfilesQuery.isLoading}
              profilesError={agentProfilesQuery.isError}
              configLoading={profileConfigQuery.isLoading}
              configError={profileConfigQuery.isError}
              configFetching={profileConfigQuery.isFetching}
              creatingProfile={createProfileMutation.isPending}
              updatingProfile={updateProfileMutation.isPending}
              deletingProfile={deleteProfileMutation.isPending}
              updatingConfig={updateProfileConfigMutation.isPending}
              onSelectProfile={setSelectedAgentProfileId}
              onProfileDraftNameChange={setProfileDraftName}
              onProfileDraftDescriptionChange={setProfileDraftDescription}
              onProfileDraftAgentClientChange={setProfileDraftAgentClient}
              onCreateProfile={createProfile}
              onDeleteProfile={(profile) => deleteProfileMutation.mutate(profile)}
              onProfileConfigAgentClientChange={setProfileConfigAgentClient}
              onSaveProfileBasics={saveProfileBasics}
              onAgentMdDraftChange={setProfileAgentMdDraft}
              onSaveAgentMd={saveProfileAgentMd}
              onToggleConfigItem={(sectionId, itemId, enabled) => updateProfileConfigMutation.mutate({ sectionId, itemId, enabled })}
            />
          </section>
        ) : view === "shortcuts" ? (
          <section className="shortcut-binding-page">
            <div className="shortcut-binding-toolbar">
              <button type="button" onClick={resetAllBuiltInShortcuts}>
                恢复内置默认
              </button>
            </div>
            <div className="shortcut-binding-list">
              {shortcutRows.map((row) => {
                const builtInShortcutId = row.target.type === "builtin" ? row.target.id : null;
                return (
                  <article key={shortcutTargetKey(row.target)} className="shortcut-binding-item">
                    <div className="shortcut-binding-label">
                      <strong>{row.label}</strong>
                      {builtInShortcutId !== null && (
                        <span>默认 {keyboardShortcutLabel(row.defaultShortcut ?? null)}</span>
                      )}
                    </div>
                    <ShortcutRecorder
                      label={row.label}
                      shortcut={row.shortcut}
                      target={row.target}
                      recordingTarget={recordingShortcutTarget}
                      conflictLabel={shortcutConflictLabel(
                        row.shortcut,
                        row.target,
                        draft.keyboardShortcutBindings,
                        draft.customQuickKeys
                      )}
                      onStartRecording={setRecordingShortcutTarget}
                      onCapture={captureShortcut}
                      onClear={(target) => bindShortcut(target, null)}
                    />
                    {builtInShortcutId !== null ? (
                      <button type="button" onClick={() => bindDefaultShortcut(builtInShortcutId)}>
                        默认
                      </button>
                    ) : null}
                  </article>
                );
              })}
            </div>
          </section>
        ) : view === "quick-keys" ? (
          <section className="quick-key-page">
            <div className="quick-key-editor">
              <div className="quick-key-editor-grid">
                <label className="settings-field">
                  <span>名称</span>
                  <input
                    value={quickKeyDraft.label}
                    onChange={(event) => updateQuickKeyDraft({ label: event.target.value })}
                    placeholder="例如：Git status"
                  />
                </label>
                <label className="settings-field quick-key-input-field">
                  <span>输入内容</span>
                  <textarea
                    value={quickKeyDraft.input}
                    onChange={(event) => updateQuickKeyDraft({ input: event.target.value })}
                    placeholder="例如：git status{Enter} 或 {Ctrl-F}"
                    rows={3}
                  />
                </label>
              </div>
              <div className="quick-key-draft-shortcut">
                <span>快捷键</span>
                <ShortcutRecorder
                  label="新快捷按键"
                  shortcut={quickKeyDraft.shortcut ?? null}
                  target={{ type: "quick-key-draft" }}
                  recordingTarget={recordingShortcutTarget}
                  conflictLabel={shortcutConflictLabel(
                    quickKeyDraft.shortcut ?? null,
                    { type: "quick-key-draft" },
                    draft.keyboardShortcutBindings,
                    draft.customQuickKeys
                  )}
                  onStartRecording={setRecordingShortcutTarget}
                  onCapture={captureShortcut}
                  onClear={(target) => bindShortcut(target, null)}
                />
              </div>
              <div className="quick-key-special-picker" aria-label="插入常用特殊按键">
                {TERMINAL_SPECIAL_KEYS.map((key) => (
                  <button
                    key={key.token}
                    type="button"
                    onClick={() => appendSpecialKeyToDraft(key.token)}
                  >
                    {key.label}
                  </button>
                ))}
              </div>
              <div className="settings-actions">
                <button type="button" disabled={!quickKeyDraftValid} onClick={addQuickKey}>
                  添加快速按键
                </button>
                <button type="button" onClick={resetQuickKeyDraft}>
                  清空
                </button>
              </div>
            </div>

            <div className="quick-key-list-header">
              <h3>已有快捷按钮</h3>
              <span>{draft.customQuickKeys.length}</span>
            </div>
            {draft.customQuickKeys.length === 0 ? (
              <p className="muted quick-key-empty-state">还没有快捷按钮</p>
            ) : (
              <div className="quick-key-settings-list">
                {draft.customQuickKeys.map((quickKey) => (
                  <article key={quickKey.id} className="quick-key-settings-item">
                    <label className="settings-field">
                      <span>名称</span>
                      <input
                        value={quickKey.label}
                        onChange={(event) => updateQuickKey(quickKey.id, { label: event.target.value })}
                      />
                    </label>
                    <label className="settings-field">
                      <span>输入内容</span>
                      <textarea
                        value={quickKey.input}
                        rows={2}
                        onChange={(event) => updateQuickKey(quickKey.id, { input: event.target.value })}
                      />
                    </label>
                    <div className="quick-key-item-shortcut">
                      <span>快捷键</span>
                      <ShortcutRecorder
                        label={quickKey.label}
                        shortcut={quickKey.shortcut ?? null}
                        target={{ type: "quick-key", id: quickKey.id }}
                        recordingTarget={recordingShortcutTarget}
                        conflictLabel={shortcutConflictLabel(
                          quickKey.shortcut ?? null,
                          { type: "quick-key", id: quickKey.id },
                          draft.keyboardShortcutBindings,
                          draft.customQuickKeys
                        )}
                        onStartRecording={setRecordingShortcutTarget}
                        onCapture={captureShortcut}
                        onClear={(target) => bindShortcut(target, null)}
                      />
                    </div>
                    <button type="button" onClick={() => removeQuickKey(quickKey.id)}>
                      删除
                    </button>
                  </article>
                ))}
              </div>
            )}
          </section>
        ) : (
          <section className="settings-account-page">
            {onboardingEnabled && (
              <button
                type="button"
                className="settings-nav-row"
                onClick={onStartOnboarding}
              >
                <span>新手引导</span>
                <strong>Start</strong>
              </button>
            )}
            {authEnabled && (
              <div className="settings-actions">
                <button type="button" onClick={onLogout}>
                  退出登录
                </button>
              </div>
            )}
          </section>
        )}
        <div className="settings-save-bar">
          <span className={hasUnsavedChanges ? "settings-save-state dirty" : "settings-save-state"}>
            {apiBaseError ?? saveStatus ?? (hasUnsavedChanges ? "有未保存的修改" : "所有修改已保存")}
          </span>
          <div className="settings-save-actions">
            <button type="button" onClick={requestClose}>
              取消
            </button>
            <button type="button" className="settings-save-button" disabled={!hasUnsavedChanges} onClick={() => void saveSettings()}>
              保存
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export function readInitialSettings() {
  return {
    summaryOutputLanguage: readSummaryOutputLanguage(),
    terminalGroupingMode: readTerminalGroupingMode(),
    themeSkin: readThemeSkin(),
    desktopNotificationsEnabled: readDesktopNotificationsEnabled()
  };
}
