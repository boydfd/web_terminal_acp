import type { AgentConfig, AgentConfigItem, AgentConfigSection } from "../types";

type AgentConfigViewerProps = {
  config: AgentConfig | null;
  isLoading?: boolean;
  isError?: boolean;
  isFetching?: boolean;
  pendingItemId?: string | null;
  isToggling?: boolean;
  toggleError?: boolean;
  title?: string;
  metaPrefix?: string;
  emptyMessage?: string;
  onToggleItem: (sectionId: string, itemId: string, nextEnabled: boolean) => void;
};

const AGENT_LABELS: Record<AgentConfig["agent"], string> = {
  codex: "Codex",
  claude: "Claude",
  cursor: "Cursor"
};

function AgentConfigItemRow({
  section,
  item,
  disabled,
  onToggleItem
}: {
  section: AgentConfigSection;
  item: AgentConfigItem;
  disabled: boolean;
  onToggleItem: (sectionId: string, itemId: string, nextEnabled: boolean) => void;
}) {
  const action = item.enabled ? "Disable" : "Enable";
  return (
    <li className="agent-config-item">
      <div>
        <strong>{item.name}</strong>
        <small>{item.id}</small>
      </div>
      <label className="agent-config-switch">
        <input
          type="checkbox"
          checked={item.enabled}
          disabled={disabled}
          aria-label={`${action} ${item.name}`}
          onChange={(event) => onToggleItem(section.id, item.id, event.target.checked)}
        />
        <span>{item.enabled ? "Enabled" : "Disabled"}</span>
      </label>
    </li>
  );
}

function AgentConfigSectionView({
  section,
  pendingItemId,
  isToggling,
  onToggleItem
}: {
  section: AgentConfigSection;
  pendingItemId: string | null;
  isToggling: boolean;
  onToggleItem: (sectionId: string, itemId: string, nextEnabled: boolean) => void;
}) {
  return (
    <section className="agent-config-section">
      <header>
        <h4>{section.name}</h4>
        <small>{section.items.length} items</small>
      </header>
      {section.items.length === 0 ? (
        <p className="muted">No {section.name.toLocaleLowerCase()} found.</p>
      ) : (
        <ul>
          {section.items.map((item) => (
            <AgentConfigItemRow
              key={item.id}
              section={section}
              item={item}
              disabled={isToggling && pendingItemId === item.id}
              onToggleItem={onToggleItem}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

export function AgentConfigViewer({
  config,
  isLoading = false,
  isError = false,
  isFetching = false,
  pendingItemId = null,
  isToggling = false,
  toggleError = false,
  title = "Agent Config",
  metaPrefix = "user config",
  emptyMessage,
  onToggleItem
}: AgentConfigViewerProps) {
  const meta = config
    ? `${AGENT_LABELS[config.agent]} ${metaPrefix}${isFetching && !isLoading ? " · refreshing" : ""}`
    : isLoading
      ? "Loading"
      : "Unavailable";

  return (
    <section className="agent-config-viewer">
      <div className="agent-record-header">
        <div>
          <h3>{title}</h3>
          <small>{meta}</small>
        </div>
      </div>
      {isLoading ? (
        <p className="muted">Loading agent config...</p>
      ) : isError || config === null ? (
        <p className="error" role="alert">{emptyMessage ?? "Failed to load agent config."}</p>
      ) : (
        <div className="agent-config-sections">
          {config.sections.map((section) => (
            <AgentConfigSectionView
              key={section.id}
              section={section}
              pendingItemId={pendingItemId}
              isToggling={isToggling}
              onToggleItem={onToggleItem}
            />
          ))}
        </div>
      )}
      {toggleError && <p className="error" role="alert">Failed to update agent config.</p>}
    </section>
  );
}
