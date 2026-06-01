import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import {
  filterCustomQuickKeys,
  normalizeCustomQuickKeys,
  type CustomQuickKey
} from "../terminalQuickKeys";

type TerminalQuickInputProps = {
  value: string;
  canSend: boolean;
  onValueChange: (value: string) => void;
  onSubmit: (value: string) => boolean | void;
  onCancel?: () => void;
  autoFocus?: boolean;
  className?: string;
  placeholder?: string;
  submitLabel?: string;
  submitOnEnter?: boolean;
  customQuickKeys?: CustomQuickKey[];
  onCustomQuickKeySubmit?: (quickKey: CustomQuickKey) => boolean;
};

const QUICK_INPUT_DRAFT_STORAGE_PREFIX = "web-terminal-acp:terminal-quick-input:";

export function quickInputDraftStorageKey(clientId: string, windowId: string): string {
  return `${QUICK_INPUT_DRAFT_STORAGE_PREFIX}${encodeURIComponent(clientId)}:${encodeURIComponent(windowId)}`;
}

export function readQuickInputDraft(storageKey: string): string {
  try {
    return window.localStorage.getItem(storageKey) ?? "";
  } catch {
    return "";
  }
}

export function writeQuickInputDraft(storageKey: string, draft: string): void {
  try {
    if (draft.length === 0) {
      window.localStorage.removeItem(storageKey);
      return;
    }
    window.localStorage.setItem(storageKey, draft);
  } catch {
    return;
  }
}

export function clearQuickInputDraft(storageKey: string): void {
  try {
    window.localStorage.removeItem(storageKey);
  } catch {
    return;
  }
}

function resizeQuickInputTextarea(textarea: HTMLTextAreaElement | null): void {
  if (textarea === null) {
    return;
  }

  textarea.style.height = "auto";
  textarea.style.height = `${textarea.scrollHeight}px`;
}

export function TerminalQuickInput({
  value,
  canSend,
  onValueChange,
  onSubmit,
  onCancel,
  autoFocus = false,
  className = "",
  placeholder = "输入内容，发送时一次性写入终端",
  submitLabel = "Send",
  submitOnEnter = false,
  customQuickKeys = [],
  onCustomQuickKeySubmit
}: TerminalQuickInputProps) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const lastPropValueRef = useRef(value);
  const compositionActiveRef = useRef(false);
  const [quickKeysOpen, setQuickKeysOpen] = useState(false);
  const [quickKeyQuery, setQuickKeyQuery] = useState("");
  const [localValue, setLocalValue] = useState(value);
  const normalizedQuickKeys = useMemo(() => normalizeCustomQuickKeys(customQuickKeys), [customQuickKeys]);
  const visibleQuickKeys = useMemo(
    () => filterCustomQuickKeys(normalizedQuickKeys, quickKeyQuery),
    [normalizedQuickKeys, quickKeyQuery]
  );
  const canUseQuickKeys = onCustomQuickKeySubmit !== undefined;
  const canSubmit = canSend && localValue.length > 0;

  useEffect(() => {
    if (value === lastPropValueRef.current) {
      return;
    }

    lastPropValueRef.current = value;
    setLocalValue(value);
  }, [value]);

  useLayoutEffect(() => {
    resizeQuickInputTextarea(textareaRef.current);
  }, [localValue]);

  const submitValue = (draft: string) => {
    if (compositionActiveRef.current) {
      return;
    }

    const submitted = onSubmit(draft);
    if (submitted === true) {
      setLocalValue("");
      resizeQuickInputTextarea(textareaRef.current);
    }
  };

  useEffect(() => {
    if (!autoFocus) {
      return;
    }

    const frame = window.requestAnimationFrame(() => {
      const textarea = textareaRef.current;
      if (textarea === null) {
        return;
      }

      textarea.focus();
      textarea.setSelectionRange(textarea.value.length, textarea.value.length);
      resizeQuickInputTextarea(textarea);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [autoFocus]);

  return (
    <form
      className={[
        "terminal-quick-input-panel",
        quickKeysOpen ? "terminal-quick-input-panel-expanded" : "",
        className
      ].filter(Boolean).join(" ")}
      data-onboarding-id="quick-input-panel"
      onMouseDown={(event) => event.stopPropagation()}
      onTouchStart={(event) => event.stopPropagation()}
      onSubmit={(event) => {
        event.preventDefault();
        submitValue(localValue);
      }}
    >
      <textarea
        ref={textareaRef}
        aria-label="Quick terminal input"
        value={localValue}
        placeholder={placeholder}
        spellCheck={false}
        rows={1}
        onChange={(event) => {
          const nextValue = event.target.value;
          setLocalValue(nextValue);
          onValueChange(nextValue);
          resizeQuickInputTextarea(event.target);
        }}
        onCompositionStart={() => {
          compositionActiveRef.current = true;
        }}
        onCompositionEnd={(event) => {
          compositionActiveRef.current = false;
          const nextValue = event.currentTarget.value;
          setLocalValue(nextValue);
          onValueChange(nextValue);
          resizeQuickInputTextarea(event.currentTarget);
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape" && onCancel !== undefined) {
            event.preventDefault();
            event.stopPropagation();
            onCancel();
            return;
          }

          if (event.key !== "Enter") {
            return;
          }

          if (compositionActiveRef.current || event.nativeEvent.isComposing) {
            return;
          }

          if (submitOnEnter && !event.shiftKey && !event.ctrlKey && !event.metaKey && !event.altKey) {
            event.preventDefault();
            event.stopPropagation();
            submitValue(event.currentTarget.value);
            return;
          }

          if ((event.metaKey || event.ctrlKey) && !event.shiftKey && !event.altKey) {
            event.preventDefault();
            event.stopPropagation();
            submitValue(event.currentTarget.value);
          }
        }}
      />
      <div className="terminal-quick-input-actions">
        {canUseQuickKeys && (
          <button
            type="button"
            aria-expanded={quickKeysOpen}
            onClick={() => setQuickKeysOpen((open) => !open)}
          >
            快捷按键
          </button>
        )}
        {onCancel !== undefined && (
          <button type="button" onClick={onCancel}>
            Close
          </button>
        )}
        <button type="submit" disabled={!canSubmit}>
          {submitLabel}
        </button>
      </div>
      {quickKeysOpen && canUseQuickKeys && (
        <div className="terminal-quick-key-drawer">
          <input
            type="search"
            aria-label="Search quick keys"
            value={quickKeyQuery}
            placeholder="搜索快捷按键"
            onChange={(event) => setQuickKeyQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                event.stopPropagation();
                return;
              }

              if (event.key === "Escape") {
                event.preventDefault();
                event.stopPropagation();
                setQuickKeysOpen(false);
                textareaRef.current?.focus();
              }
            }}
          />
          <div className="terminal-quick-key-list" role="list">
            {visibleQuickKeys.length === 0 ? (
              <p className="muted terminal-quick-key-empty">没有匹配的快捷按键</p>
            ) : visibleQuickKeys.map((quickKey) => (
              <button
                key={quickKey.id}
                type="button"
                role="listitem"
                className="terminal-quick-key-chip"
                onClick={() => {
                  const sent = onCustomQuickKeySubmit(quickKey);
                  if (sent) {
                    setQuickKeysOpen(false);
                  }
                }}
              >
                <span>{quickKey.label}</span>
                <code>{quickKey.input}</code>
              </button>
            ))}
          </div>
        </div>
      )}
    </form>
  );
}
