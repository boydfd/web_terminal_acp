export type DetailPanelTab = "overview" | "agent" | "git";

type DetailPanelTabsProps = {
  activeTab: DetailPanelTab;
  showGitTab: boolean;
  onTabChange: (tab: DetailPanelTab) => void;
};

export function DetailPanelTabs({ activeTab, showGitTab, onTabChange }: DetailPanelTabsProps) {
  return (
    <div className="detail-panel-tabs" role="tablist" aria-label="Window details">
      <button
        type="button"
        role="tab"
        aria-selected={activeTab === "overview"}
        className={activeTab === "overview" ? "selected" : undefined}
        onClick={() => onTabChange("overview")}
      >
        Overview
      </button>
      <button
        type="button"
        role="tab"
        aria-selected={activeTab === "agent"}
        className={activeTab === "agent" ? "selected" : undefined}
        onClick={() => onTabChange("agent")}
      >
        Agent
      </button>
      {showGitTab && (
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "git"}
          className={activeTab === "git" ? "selected" : undefined}
          onClick={() => onTabChange("git")}
        >
          Git
        </button>
      )}
    </div>
  );
}
