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

type SettingsModalProps = {
  isOpen: boolean;
  onClose: () => void;
  summaryOutputLanguage: SummaryOutputLanguage;
  terminalGroupingMode: TerminalGroupingMode;
  desktopNotificationsEnabled: boolean;
  onSummaryOutputLanguageChange: (language: SummaryOutputLanguage) => void;
  onTerminalGroupingModeChange: (mode: TerminalGroupingMode) => void;
  onDesktopNotificationsEnabledChange: (enabled: boolean) => void;
};

export function SettingsModal({
  isOpen,
  onClose,
  summaryOutputLanguage,
  terminalGroupingMode,
  desktopNotificationsEnabled,
  onSummaryOutputLanguageChange,
  onTerminalGroupingModeChange,
  onDesktopNotificationsEnabledChange
}: SettingsModalProps) {
  if (!isOpen) {
    return null;
  }

  return (
    <div
      className="settings-modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <div aria-modal="true" className="settings-modal" role="dialog" aria-label="Settings">
        <div className="settings-modal-header">
          <h2>设置</h2>
          <button type="button" onClick={onClose}>
            关闭
          </button>
        </div>

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

        <p className="muted settings-hint">快捷键：Alt+, 打开设置</p>
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
