import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  AGENT_LAUNCH_OPTIONS,
  agentLaunchForKind,
  isAgentLaunchKind,
} from "../agentLaunch";
import type { AgentLaunchMode } from "../agentLaunch";
import type { AgentLaunchKind, AgentLaunchConfig, ProjectSummary } from "../types";
import { projectGroupLabel } from "../terminalGrouping";
import { useOverlayFocus } from "./useOverlayFocus";

type ProjectTerminalPickerProps = {
  isOpen: boolean;
  projectPaths: string[];
  projectSummaries: ProjectSummary[];
  loadingProjects?: boolean;
  creatingTerminal?: boolean;
  createTerminalDisabled?: boolean;
  onClose: () => void;
  onCreateTerminal: (projectPath: string, agentLaunch: AgentLaunchConfig | null) => void;
  onConfigureTerminal?: (projectPath: string, agent: AgentLaunchKind) => void;
};

type ProjectOption = {
  path: string;
  label: string;
};

export function ProjectTerminalPicker({
  isOpen,
  projectPaths,
  projectSummaries,
  loadingProjects,
  creatingTerminal,
  createTerminalDisabled,
  onClose,
  onCreateTerminal,
  onConfigureTerminal
}: ProjectTerminalPickerProps) {
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const [selectedMode, setSelectedMode] = useState<AgentLaunchMode>("shell");
  const panelRef = useRef<HTMLDivElement | null>(null);
  const projectSummaryLookup = useMemo(() => {
    const lookup = new Map<string, ProjectSummary>();
    for (const summary of projectSummaries) {
      lookup.set(summary.project_path, summary);
    }
    return lookup;
  }, [projectSummaries]);
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const options = useMemo<ProjectOption[]>(
    () => projectPaths.map((path) => ({
      path,
      label: projectGroupLabel(path, projectSummaryLookup)
    })),
    [projectPaths, projectSummaryLookup]
  );
  const filteredOptions = useMemo(
    () => options.filter((option) => {
      if (!normalizedQuery) {
        return true;
      }

      return `${option.label} ${option.path}`.toLocaleLowerCase().includes(normalizedQuery);
    }),
    [normalizedQuery, options]
  );

  useEffect(() => {
    if (!isOpen) {
      setQuery("");
      setActiveIndex(0);
      setSelectedMode("shell");
    }
  }, [isOpen]);

  useEffect(() => {
    setActiveIndex((currentIndex) => {
      if (filteredOptions.length === 0) {
        return 0;
      }

      return Math.min(currentIndex, filteredOptions.length - 1);
    });
  }, [filteredOptions.length]);

  const activeOption = filteredOptions[activeIndex] ?? null;
  const createTerminalForProject = useCallback((projectPath: string) => {
    onCreateTerminal(projectPath, isAgentLaunchKind(selectedMode) ? agentLaunchForKind(selectedMode) : null);
  }, [onCreateTerminal, selectedMode]);

  useEffect(() => {
    if (!isOpen) {
      return;
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (filteredOptions.length === 0) {
        return;
      }

      if (event.key === "Tab") {
        event.preventDefault();
        setSelectedMode((currentMode) => {
          const currentIndex = AGENT_LAUNCH_OPTIONS.findIndex((option) => option.id === currentMode);
          const offset = event.shiftKey ? -1 : 1;
          const nextIndex = (currentIndex + offset + AGENT_LAUNCH_OPTIONS.length) % AGENT_LAUNCH_OPTIONS.length;
          return AGENT_LAUNCH_OPTIONS[nextIndex].id;
        });
        return;
      }

      if (event.key === "ArrowDown") {
        event.preventDefault();
        setActiveIndex((currentIndex) => (currentIndex + 1) % filteredOptions.length);
        return;
      }

      if (event.key === "ArrowUp") {
        event.preventDefault();
        setActiveIndex((currentIndex) => (currentIndex - 1 + filteredOptions.length) % filteredOptions.length);
        return;
      }

      if (event.key === "Enter") {
        const option = filteredOptions[activeIndex];
        if (!option || creatingTerminal || createTerminalDisabled) {
          return;
        }

        event.preventDefault();
        createTerminalForProject(option.path);
      }
    };

    window.addEventListener("keydown", handleKeyDown, { capture: true });
    return () => window.removeEventListener("keydown", handleKeyDown, { capture: true });
  }, [
    activeIndex,
    createTerminalDisabled,
    creatingTerminal,
    filteredOptions,
    isOpen,
    createTerminalForProject
  ]);

  const handleEscape = useCallback(() => {
    onClose();
  }, [onClose]);

  useOverlayFocus({
    isOpen,
    ref: panelRef,
    onEscape: handleEscape,
    initialFocusSelector: "input"
  });

  if (!isOpen) {
    return null;
  }

  return (
    <div
      className="project-terminal-picker-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <div ref={panelRef} aria-modal="true" className="project-terminal-picker" role="dialog">
        <div className="project-terminal-picker-header">
          <div>
            <h2>New terminal by project path</h2>
            <p className="muted">选择现有项目路径新建 terminal</p>
          </div>
          <div className="project-terminal-picker-actions">
            {onConfigureTerminal && (
              <button
                type="button"
                disabled={
                  activeOption === null
                  || !isAgentLaunchKind(selectedMode)
                  || creatingTerminal
                  || createTerminalDisabled
                }
                onClick={() => {
                  if (activeOption !== null && isAgentLaunchKind(selectedMode)) {
                    onConfigureTerminal(activeOption.path, selectedMode);
                  }
                }}
              >
                配置
              </button>
            )}
            <button type="button" onClick={onClose}>
              Close
            </button>
          </div>
        </div>

        <div className="project-terminal-picker-agent-tabs" role="tablist" aria-label="Agent">
          {AGENT_LAUNCH_OPTIONS.map((option) => (
            <button
              key={option.id}
              type="button"
              className={selectedMode === option.id ? "active" : undefined}
              aria-selected={selectedMode === option.id}
              onClick={() => setSelectedMode(option.id)}
            >
              {option.label}
            </button>
          ))}
        </div>

        <input
          aria-label="Search project paths"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setActiveIndex(0);
          }}
          placeholder="Search project path..."
        />

        {loadingProjects && projectPaths.length === 0 && (
          <p className="project-terminal-picker-empty">Loading project paths...</p>
        )}
        {!loadingProjects && projectPaths.length === 0 && (
          <p className="project-terminal-picker-empty">No project paths found for this client.</p>
        )}
        {!loadingProjects && projectPaths.length > 0 && filteredOptions.length === 0 && (
          <p className="project-terminal-picker-empty">No matching project paths.</p>
        )}
        {filteredOptions.length > 0 && (
          <ul className="project-terminal-picker-results" role="listbox" aria-label="Project paths">
            {filteredOptions.map((option, index) => {
              const isActive = index === activeIndex;

              return (
                <li key={option.path} className="project-terminal-picker-result">
                  <button
                    type="button"
                    aria-selected={isActive}
                    className={isActive ? "project-terminal-picker-option active" : "project-terminal-picker-option"}
                    disabled={creatingTerminal || createTerminalDisabled}
                    onClick={() => {
                      if (creatingTerminal || createTerminalDisabled) {
                        return;
                      }

                      createTerminalForProject(option.path);
                    }}
                    role="option"
                    title={option.path}
                  >
                    <span className="project-terminal-picker-label">{option.label}</span>
                    <span className="project-terminal-picker-path">{option.path}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
