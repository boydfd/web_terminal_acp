import {
  desktopNotificationsSupported,
  readDesktopNotificationsEnabled,
  readSummaryOutputLanguage,
  readTerminalGroupingMode,
  type SummaryOutputLanguage,
  type TerminalGroupingMode,
  writeDesktopNotificationsEnabled,
  writeSummaryOutputLanguage,
  writeTerminalGroupingMode
} from "../userPreferences";
import { ensureDesktopNotificationPermission } from "../desktopNotifications";
import { readApiBase, readConfiguredApiBase, writeConfiguredApiBase } from "../apiBase";
import { useEffect, useState } from "react";
import {
  createCustomQuickKey,
  quickKeyToken,
  TERMINAL_SPECIAL_KEYS,
  type CustomQuickKey
} from "../terminalQuickKeys";

type SettingsView = "general" | "quick-keys";

type SettingsModalProps = {
  isOpen: boolean;
  onClose: () => void;
  summaryOutputLanguage: SummaryOutputLanguage;
  terminalGroupingMode: TerminalGroupingMode;
  desktopNotificationsEnabled: boolean;
  customQuickKeys: CustomQuickKey[];
  onSummaryOutputLanguageChange: (language: SummaryOutputLanguage) => void;
  onTerminalGroupingModeChange: (mode: TerminalGroupingMode) => void;
  onDesktopNotificationsEnabledChange: (enabled: boolean) => void;
  onCustomQuickKeysChange: (quickKeys: CustomQuickKey[]) => void;
};

export function SettingsModal({
  isOpen,
  onClose,
  summaryOutputLanguage,
  terminalGroupingMode,
  desktopNotificationsEnabled,
  customQuickKeys,
  onSummaryOutputLanguageChange,
  onTerminalGroupingModeChange,
  onDesktopNotificationsEnabledChange,
  onCustomQuickKeysChange
}: SettingsModalProps) {
  const [apiBaseDraft, setApiBaseDraft] = useState("");
  const [apiBaseError, setApiBaseError] = useState<string | null>(null);
  const [view, setView] = useState<SettingsView>("general");
  const [quickKeyDraft, setQuickKeyDraft] = useState<CustomQuickKey>(() => createCustomQuickKey());
  const quickKeyDraftValid = quickKeyDraft.label.trim().length > 0 && quickKeyDraft.input.length > 0;

  useEffect(() => {
    if (isOpen) {
      setApiBaseDraft(readConfiguredApiBase());
      setApiBaseError(null);
      setView("general");
    }
  }, [isOpen]);

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

  const settingsTitle = view === "quick-keys" ? "快速按键" : "设置";

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
        className={["settings-modal", view === "quick-keys" ? "settings-modal-wide" : ""].filter(Boolean).join(" ")}
        role="dialog"
        aria-label="Settings"
      >
        <div className="settings-modal-header">
          <div className="settings-modal-title">
            {view === "quick-keys" && (
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
              onClick={() => setView("quick-keys")}
            >
              <span>快速按键管理</span>
              <strong>{customQuickKeys.length}</strong>
            </button>

            <p className="muted settings-hint">快捷键：Alt+, 打开设置</p>
          </>
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
    desktopNotificationsEnabled: readDesktopNotificationsEnabled()
  };
}
