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
import { readApiBase, readConfiguredApiBase, writeConfiguredApiBase } from "../apiBase";
import { useEffect, useMemo, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
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
  resetKeyboardShortcutBindings,
  shortcutForCapture,
  type KeyboardShortcut,
  type KeyboardShortcutBindings,
  type KeyboardShortcutId
} from "../keyboardShortcuts";
import { THEME_SKINS } from "../themeSkins";

export type SettingsView = "general" | "theme" | "shortcuts" | "quick-keys" | "clients";
type ShortcutBindingTarget =
  | { type: "builtin"; id: KeyboardShortcutId }
  | { type: "quick-key"; id: string }
  | { type: "quick-key-draft" };

type SettingsModalProps = {
  isOpen: boolean;
  onClose: () => void;
  summaryOutputLanguage: SummaryOutputLanguage;
  terminalGroupingMode: TerminalGroupingMode;
  themeSkin: ThemeSkinId;
  desktopNotificationsEnabled: boolean;
  keyboardShortcutBindings: KeyboardShortcutBindings;
  customQuickKeys: CustomQuickKey[];
  onSummaryOutputLanguageChange: (language: SummaryOutputLanguage) => void;
  onTerminalGroupingModeChange: (mode: TerminalGroupingMode) => void;
  onThemeSkinChange: (themeSkin: ThemeSkinId) => void;
  onDesktopNotificationsEnabledChange: (enabled: boolean) => void;
  onKeyboardShortcutBindingsChange: (bindings: KeyboardShortcutBindings) => void;
  onCustomQuickKeysChange: (quickKeys: CustomQuickKey[]) => void;
  authEnabled: boolean;
  registrationKey: string | null;
  registrationKeyPending: boolean;
  registrationKeyError: string | null;
  initialView?: SettingsView;
  onGenerateRegistrationKey: () => void;
  onboardingEnabled: boolean;
  onStartOnboarding: () => void;
  onLogout: () => void;
};

function apiPath(path: string): string {
  const base = new URL(readApiBase());
  if (!base.pathname.endsWith("/")) {
    base.pathname = `${base.pathname}/`;
  }
  return new URL(path.replace(/^\/+/, ""), base).toString();
}

function shellSingleQuote(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

function shortcutTargetKey(target: ShortcutBindingTarget): string {
  return target.type === "quick-key-draft" ? target.type : `${target.type}:${target.id}`;
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

export function SettingsModal({
  isOpen,
  onClose,
  summaryOutputLanguage,
  terminalGroupingMode,
  themeSkin,
  desktopNotificationsEnabled,
  keyboardShortcutBindings,
  customQuickKeys,
  onSummaryOutputLanguageChange,
  onTerminalGroupingModeChange,
  onThemeSkinChange,
  onDesktopNotificationsEnabledChange,
  onKeyboardShortcutBindingsChange,
  onCustomQuickKeysChange,
  authEnabled,
  registrationKey,
  registrationKeyPending,
  registrationKeyError,
  initialView = "general",
  onGenerateRegistrationKey,
  onboardingEnabled,
  onStartOnboarding,
  onLogout
}: SettingsModalProps) {
  const [apiBaseDraft, setApiBaseDraft] = useState("");
  const [apiBaseError, setApiBaseError] = useState<string | null>(null);
  const [agentCommandDraft, setAgentCommandDraft] = useState<AgentCommandSettings>(() => readAgentCommandSettings());
  const [view, setView] = useState<SettingsView>("general");
  const [quickKeyDraft, setQuickKeyDraft] = useState<CustomQuickKey>(() => createCustomQuickKey());
  const [recordingShortcutTarget, setRecordingShortcutTarget] = useState<ShortcutBindingTarget | null>(null);
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
      shortcut: effectiveKeyboardShortcut(definition.id, keyboardShortcutBindings),
      defaultShortcut: definition.defaultShortcut
    })),
    ...customQuickKeys.map((quickKey) => ({
      target: { type: "quick-key", id: quickKey.id } as ShortcutBindingTarget,
      label: `快捷按键：${quickKey.label}`,
      shortcut: quickKey.shortcut ?? null
    }))
  ], [customQuickKeys, keyboardShortcutBindings]);

  useEffect(() => {
    if (isOpen) {
      setApiBaseDraft(readConfiguredApiBase());
      setApiBaseError(null);
      setAgentCommandDraft(readAgentCommandSettings());
      setRecordingShortcutTarget(null);
      setView(initialView);
    }
  }, [initialView, isOpen]);

  useEffect(() => {
    if (!isOpen) {
      return;
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) {
        return;
      }

      if (recordingShortcutTarget !== null) {
        event.preventDefault();
        setRecordingShortcutTarget(null);
        return;
      }

      event.preventDefault();
      onClose();
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isOpen, onClose, recordingShortcutTarget]);

  if (!isOpen) {
    return null;
  }

  const saveApiBase = (value: string) => {
    try {
      writeConfiguredApiBase(value);
      window.location.reload();
    } catch {
      setApiBaseError("请输入有效的 HTTP/HTTPS 地址");
    }
  };

  const updateAgentCommand = (agent: keyof AgentCommandSettings, value: string) => {
    const next = { ...agentCommandDraft, [agent]: value };
    setAgentCommandDraft(next);
    writeAgentCommandSettings(next);
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

    onCustomQuickKeysChange([
      ...customQuickKeys,
      {
        ...quickKeyDraft,
        label: quickKeyDraft.label.trim()
      }
    ]);
    resetQuickKeyDraft();
  };

  const updateQuickKey = (id: string, patch: Partial<CustomQuickKey>) => {
    onCustomQuickKeysChange(customQuickKeys.map((quickKey) => (
      quickKey.id === id ? { ...quickKey, ...patch } : quickKey
    )));
  };

  const removeQuickKey = (id: string) => {
    onCustomQuickKeysChange(customQuickKeys.filter((quickKey) => quickKey.id !== id));
  };

  const appendSpecialKeyToDraft = (token: string) => {
    updateQuickKeyDraft({ input: `${quickKeyDraft.input}${quickKeyToken(token)}` });
  };

  const bindShortcut = (target: ShortcutBindingTarget, shortcut: KeyboardShortcut | null) => {
    if (target.type === "builtin") {
      onKeyboardShortcutBindingsChange({
        ...keyboardShortcutBindings,
        [target.id]: shortcut
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
    const nextBindings = { ...keyboardShortcutBindings };
    delete nextBindings[id];
    onKeyboardShortcutBindingsChange(nextBindings);
  };

  const resetAllBuiltInShortcuts = () => {
    onKeyboardShortcutBindingsChange(resetKeyboardShortcutBindings());
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

  const settingsTitle = view === "theme"
    ? "界面皮肤"
    : view === "shortcuts"
      ? "快捷键绑定"
      : view === "quick-keys"
        ? "快速按键"
        : view === "clients"
          ? "Client 注册"
          : "设置";
  const selectedThemeSkin = THEME_SKINS.find((skin) => skin.id === themeSkin) ?? THEME_SKINS[0];
  const registrationScript = [
    `curl -fsSL ${shellSingleQuote(apiPath("/api/clients/register-script"))} -o register-client-direct.sh`,
    "chmod +x register-client-direct.sh",
    `WEB_TERMINAL_SERVER_URL=${shellSingleQuote(readApiBase())} \\`,
    `WEB_TERMINAL_REGISTRATION_KEY=${shellSingleQuote(registrationKey ?? "<先生成 key>")} \\`,
    "./register-client-direct.sh"
  ].join("\n");

  return (
    <div
      className="settings-modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <div
        aria-modal="true"
        className={["settings-modal", view !== "general" ? "settings-modal-wide" : ""].filter(Boolean).join(" ")}
        data-onboarding-id="settings-modal"
        role="dialog"
        aria-label="Settings"
      >
        <div className="settings-modal-header">
          <div className="settings-modal-title">
            {view !== "general" && (
              <button type="button" onClick={() => setView("general")}>
                返回
              </button>
            )}
            <h2>{settingsTitle}</h2>
          </div>
          <button type="button" onClick={onClose}>关闭</button>
        </div>

        {view === "general" ? (
          <>
            <label className="settings-field">
              <span>后端地址</span>
              <input
                value={apiBaseDraft}
                onChange={(event) => {
                  setApiBaseDraft(event.target.value);
                  setApiBaseError(null);
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    saveApiBase(apiBaseDraft);
                  }
                }}
                placeholder={readApiBase()}
              />
            </label>
            <div className="settings-actions">
              <button type="button" onClick={() => saveApiBase(apiBaseDraft)}>
                保存后端地址
              </button>
              <button
                type="button"
                onClick={() => {
                  setApiBaseDraft("");
                  saveApiBase("");
                }}
              >
                恢复默认
              </button>
            </div>
            {apiBaseError && (
              <p className="error settings-error" role="alert">
                {apiBaseError}
              </p>
            )}

            <button
              type="button"
              className="settings-nav-row"
              onClick={() => setView("theme")}
            >
              <span>界面皮肤</span>
              <strong>{selectedThemeSkin.label}</strong>
            </button>

            <label className="settings-field">
              <span>项目名显示语言</span>
              <select
                value={summaryOutputLanguage}
                onChange={(event) => {
                  const language = event.target.value as SummaryOutputLanguage;
                  writeSummaryOutputLanguage(language);
                  onSummaryOutputLanguageChange(language);
                }}
              >
                <option value="中文">中文</option>
                <option value="English">English</option>
              </select>
            </label>

            <label className="settings-field">
              <span>终端列表分组方式</span>
              <select
                value={terminalGroupingMode}
                onChange={(event) => {
                  const mode = event.target.value as TerminalGroupingMode;
                  writeTerminalGroupingMode(mode);
                  onTerminalGroupingModeChange(mode);
                }}
              >
                <option value="project-topic">项目 / 主题</option>
                <option value="topic">主题</option>
                <option value="time-topic">时间 / 主题 / 子主题</option>
                <option value="project-time-topic">项目 / 时间 / 主题 / 子主题</option>
              </select>
            </label>

            <div className="settings-agent-command-grid">
              <label className="settings-field">
                <span>Codex 启动命令</span>
                <input
                  value={agentCommandDraft.codex}
                  onChange={(event) => updateAgentCommand("codex", event.target.value)}
                  placeholder="codex"
                />
              </label>
              <label className="settings-field">
                <span>Claude Code 启动命令</span>
                <input
                  value={agentCommandDraft.claude}
                  onChange={(event) => updateAgentCommand("claude", event.target.value)}
                  placeholder="claude"
                />
              </label>
              <label className="settings-field">
                <span>Cursor 启动命令</span>
                <input
                  value={agentCommandDraft.cursor}
                  onChange={(event) => updateAgentCommand("cursor", event.target.value)}
                  placeholder="agent"
                />
              </label>
            </div>

            {desktopNotificationsSupported() && (
              <label className="settings-field settings-field-checkbox">
                <span>系统桌面通知</span>
                <input
                  type="checkbox"
                  checked={desktopNotificationsEnabled}
                  onChange={(event) => {
                    const enabled = event.target.checked;
                    void (async () => {
                      if (enabled) {
                        const permission = await ensureDesktopNotificationPermission();
                        if (permission !== "granted") {
                          writeDesktopNotificationsEnabled(false);
                          onDesktopNotificationsEnabledChange(false);
                          return;
                        }
                      }
                      writeDesktopNotificationsEnabled(enabled);
                      onDesktopNotificationsEnabledChange(enabled);
                    })();
                  }}
                />
              </label>
            )}

            <button
              type="button"
              className="settings-nav-row"
              onClick={() => setView("shortcuts")}
            >
              <span>快捷键绑定</span>
              <strong>{shortcutRows.length}</strong>
            </button>

            <button
              type="button"
              className="settings-nav-row"
              onClick={() => setView("quick-keys")}
            >
              <span>快速按键管理</span>
              <strong>{customQuickKeys.length}</strong>
            </button>

            <button
              type="button"
              className="settings-nav-row"
              data-onboarding-id="settings-client-registration-nav"
              onClick={() => setView("clients")}
            >
              <span>Client 注册</span>
              <strong>Key</strong>
            </button>

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

            <p className="muted settings-hint">
              快捷键：{keyboardShortcutLabel(effectiveKeyboardShortcut("settings", keyboardShortcutBindings))} 打开设置
            </p>
          </>
        ) : view === "theme" ? (
          <section className="settings-theme-page">
            <label className="settings-field">
              <span>当前皮肤</span>
              <select
                value={themeSkin}
                onChange={(event) => {
                  const nextThemeSkin = event.target.value as ThemeSkinId;
                  writeThemeSkin(nextThemeSkin);
                  onThemeSkinChange(nextThemeSkin);
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
                    skin.id === themeSkin ? "selected" : ""
                  ].filter(Boolean).join(" ")}
                  aria-pressed={skin.id === themeSkin}
                  onClick={() => {
                    writeThemeSkin(skin.id);
                    onThemeSkinChange(skin.id);
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
                        keyboardShortcutBindings,
                        customQuickKeys
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
        ) : view === "clients" ? (
          <section className="settings-client-registration-page" data-onboarding-id="remote-registration-panel">
            <div className="settings-registration-panel">
              <p className="muted">
                一次性注册 Key 适合在目标机器上主动运行脚本接入 remote client；不用从本机 SSH 登录目标机器。
              </p>
              <button
                type="button"
                disabled={registrationKeyPending}
                onClick={onGenerateRegistrationKey}
              >
                生成一次性注册 Key
              </button>
              {registrationKeyError && (
                <p className="error settings-error" role="alert">
                  {registrationKeyError}
                </p>
              )}
              {registrationKey && (
                <label className="settings-field">
                  <span>一次性注册 Key</span>
                  <textarea readOnly rows={3} value={registrationKey} />
                </label>
              )}
              <label className="settings-field">
                <span>注册脚本</span>
                <textarea
                  readOnly
                  rows={7}
                  value={registrationScript}
                />
              </label>
            </div>
          </section>
        ) : (
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
                    keyboardShortcutBindings,
                    customQuickKeys
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
              <span>{customQuickKeys.length}</span>
            </div>
            {customQuickKeys.length === 0 ? (
              <p className="muted quick-key-empty-state">还没有快捷按钮</p>
            ) : (
              <div className="quick-key-settings-list">
                {customQuickKeys.map((quickKey) => (
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
                          keyboardShortcutBindings,
                          customQuickKeys
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
        )}
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
