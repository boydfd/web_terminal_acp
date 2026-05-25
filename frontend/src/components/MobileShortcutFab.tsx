import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

export type MobileShortcutAction = {
  id: string;
  label: string;
  hint?: string;
  disabled?: boolean;
  badge?: number;
  onPress: () => void;
};

type MobileShortcutFabProps = {
  visible: boolean;
  actions: MobileShortcutAction[];
};

export function MobileShortcutFab({ visible, actions }: MobileShortcutFabProps) {
  const [open, setOpen] = useState(false);
  const [mounted, setMounted] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    if (!visible) {
      setOpen(false);
    }
  }, [visible]);

  useEffect(() => {
    if (!open) {
      return;
    }

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node) || rootRef.current?.contains(target)) {
        return;
      }
      setOpen(false);
    };

    window.addEventListener("pointerdown", handlePointerDown);
    return () => window.removeEventListener("pointerdown", handlePointerDown);
  }, [open]);

  if (!visible || !mounted) {
    return null;
  }

  return createPortal(
    <div ref={rootRef} className={`mobile-shortcut-fab${open ? " open" : ""}`}>
      {open && (
        <div className="mobile-shortcut-fab-menu" role="menu" aria-label="快捷操作">
          {actions.map((action) => (
            <button
              key={action.id}
              type="button"
              role="menuitem"
              className="mobile-shortcut-fab-item"
              disabled={action.disabled}
              onClick={() => {
                if (action.disabled) {
                  return;
                }
                action.onPress();
                setOpen(false);
              }}
            >
              <span className="mobile-shortcut-fab-item-label">{action.label}</span>
              {action.hint ? <span className="mobile-shortcut-fab-item-hint">{action.hint}</span> : null}
              {action.badge !== undefined && action.badge > 0 ? (
                <span className="mobile-shortcut-fab-item-badge" aria-hidden="true">
                  {action.badge > 99 ? "99+" : action.badge}
                </span>
              ) : null}
            </button>
          ))}
        </div>
      )}
      <button
        type="button"
        className="mobile-shortcut-fab-ball"
        aria-expanded={open}
        aria-haspopup="menu"
        aria-label={open ? "关闭快捷菜单" : "打开快捷菜单"}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="mobile-shortcut-fab-ball-icon" aria-hidden="true" />
      </button>
    </div>,
    document.body
  );
}
