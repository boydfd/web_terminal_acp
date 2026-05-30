import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { createPortal } from "react-dom";

export type MobileShortcutDirection = "up" | "down" | "left" | "right";

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
  onDirectionInput?: (direction: MobileShortcutDirection) => void;
};

type FabPosition = {
  x: number;
  y: number;
};

type DragState = {
  pointerId: number;
  startPointerX: number;
  startPointerY: number;
  latestPointerX: number;
  latestPointerY: number;
  startPosition: FabPosition;
  moved: boolean;
  dragging: boolean;
  longPressTimer: number | null;
};

const FAB_POSITION_STORAGE_KEY = "web-terminal-acp:mobile-shortcut-fab-position";
const FAB_SIZE = 56;
const FAB_MARGIN = 8;
const DEFAULT_BOTTOM_OFFSET = 16;
const TAP_MOVE_TOLERANCE_PX = 6;
const SWIPE_INPUT_THRESHOLD_PX = 28;
const LONG_PRESS_DRAG_DELAY_MS = 420;
const LONG_PRESS_MOVE_TOLERANCE_PX = 10;

function viewportSize(): { width: number; height: number } {
  const viewport = window.visualViewport;
  return {
    width: Math.round(viewport?.width ?? window.innerWidth),
    height: Math.round(viewport?.height ?? window.innerHeight)
  };
}

function defaultFabPosition(): FabPosition {
  const { width, height } = viewportSize();
  return clampFabPosition({
    x: width - FAB_SIZE - Math.max(DEFAULT_BOTTOM_OFFSET, FAB_MARGIN),
    y: height - FAB_SIZE - Math.max(DEFAULT_BOTTOM_OFFSET, FAB_MARGIN)
  });
}

function clampFabPosition(position: FabPosition): FabPosition {
  const { width, height } = viewportSize();
  return {
    x: Math.min(Math.max(position.x, FAB_MARGIN), Math.max(FAB_MARGIN, width - FAB_SIZE - FAB_MARGIN)),
    y: Math.min(Math.max(position.y, FAB_MARGIN), Math.max(FAB_MARGIN, height - FAB_SIZE - FAB_MARGIN))
  };
}

function readFabPosition(): FabPosition {
  if (typeof window === "undefined") {
    return { x: 0, y: 0 };
  }

  try {
    const rawValue = window.localStorage.getItem(FAB_POSITION_STORAGE_KEY);
    if (rawValue === null) {
      return defaultFabPosition();
    }
    const parsed = JSON.parse(rawValue) as Partial<FabPosition>;
    if (typeof parsed.x !== "number" || typeof parsed.y !== "number") {
      return defaultFabPosition();
    }
    return clampFabPosition({ x: parsed.x, y: parsed.y });
  } catch {
    return defaultFabPosition();
  }
}

function writeFabPosition(position: FabPosition): void {
  try {
    window.localStorage.setItem(FAB_POSITION_STORAGE_KEY, JSON.stringify(clampFabPosition(position)));
  } catch {
    return;
  }
}

function clearLongPressTimer(dragState: DragState): void {
  if (dragState.longPressTimer !== null) {
    window.clearTimeout(dragState.longPressTimer);
    dragState.longPressTimer = null;
  }
}

function swipeDirection(deltaX: number, deltaY: number): MobileShortcutDirection | null {
  if (Math.hypot(deltaX, deltaY) < SWIPE_INPUT_THRESHOLD_PX) {
    return null;
  }

  if (Math.abs(deltaX) > Math.abs(deltaY)) {
    return deltaX < 0 ? "left" : "right";
  }

  return deltaY < 0 ? "up" : "down";
}

export function MobileShortcutFab({ visible, actions, onDirectionInput }: MobileShortcutFabProps) {
  const [open, setOpen] = useState(false);
  const [mounted, setMounted] = useState(false);
  const [position, setPosition] = useState<FabPosition | null>(null);
  const [draggingPosition, setDraggingPosition] = useState(false);
  const dragStateRef = useRef<DragState | null>(null);
  const suppressClickRef = useRef(false);

  useEffect(() => {
    setMounted(true);
    setPosition(readFabPosition());
  }, []);

  useEffect(() => {
    if (!visible) {
      setOpen(false);
      setDraggingPosition(false);
    }
  }, [visible]);

  useEffect(() => {
    if (!mounted) {
      return;
    }

    const handleResize = () => {
      setPosition((currentPosition) => {
        const clamped = clampFabPosition(currentPosition ?? readFabPosition());
        writeFabPosition(clamped);
        return clamped;
      });
    };

    window.addEventListener("resize", handleResize);
    window.addEventListener("orientationchange", handleResize);
    window.visualViewport?.addEventListener("resize", handleResize);
    return () => {
      window.removeEventListener("resize", handleResize);
      window.removeEventListener("orientationchange", handleResize);
      window.visualViewport?.removeEventListener("resize", handleResize);
    };
  }, [mounted]);

  useEffect(() => () => {
    const dragState = dragStateRef.current;
    if (dragState !== null) {
      clearLongPressTimer(dragState);
    }
  }, []);

  const handleBallPointerDown = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (position === null || event.button !== 0) {
      return;
    }

    dragStateRef.current = {
      pointerId: event.pointerId,
      startPointerX: event.clientX,
      startPointerY: event.clientY,
      latestPointerX: event.clientX,
      latestPointerY: event.clientY,
      startPosition: position,
      moved: false,
      dragging: false,
      longPressTimer: window.setTimeout(() => {
        const currentDragState = dragStateRef.current;
        if (currentDragState === null || currentDragState.pointerId !== event.pointerId) {
          return;
        }
        currentDragState.longPressTimer = null;
        currentDragState.dragging = true;
        currentDragState.moved = true;
        suppressClickRef.current = true;
        setDraggingPosition(true);
        setOpen(false);
        setPosition(clampFabPosition({
          x: currentDragState.startPosition.x + currentDragState.latestPointerX - currentDragState.startPointerX,
          y: currentDragState.startPosition.y + currentDragState.latestPointerY - currentDragState.startPointerY
        }));
      }, LONG_PRESS_DRAG_DELAY_MS)
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const handleBallPointerMove = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const dragState = dragStateRef.current;
    if (dragState === null || dragState.pointerId !== event.pointerId) {
      return;
    }

    const deltaX = event.clientX - dragState.startPointerX;
    const deltaY = event.clientY - dragState.startPointerY;
    dragState.latestPointerX = event.clientX;
    dragState.latestPointerY = event.clientY;

    if (!dragState.dragging && Math.hypot(deltaX, deltaY) > LONG_PRESS_MOVE_TOLERANCE_PX) {
      clearLongPressTimer(dragState);
    }

    if (!dragState.dragging) {
      return;
    }

    if (!dragState.moved && Math.hypot(deltaX, deltaY) < TAP_MOVE_TOLERANCE_PX) {
      return;
    }
    dragState.moved = true;
    setOpen(false);
    setPosition(clampFabPosition({
      x: dragState.startPosition.x + deltaX,
      y: dragState.startPosition.y + deltaY
    }));
  };

  const finishBallDrag = (event: ReactPointerEvent<HTMLButtonElement>, canceled = false) => {
    const dragState = dragStateRef.current;
    if (dragState === null || dragState.pointerId !== event.pointerId) {
      return;
    }

    dragStateRef.current = null;
    clearLongPressTimer(dragState);
    setDraggingPosition(false);

    const deltaX = event.clientX - dragState.startPointerX;
    const deltaY = event.clientY - dragState.startPointerY;
    if (canceled) {
      suppressClickRef.current = true;
    } else if (!dragState.dragging) {
      const direction = swipeDirection(deltaX, deltaY);
      if (direction !== null) {
        onDirectionInput?.(direction);
        suppressClickRef.current = true;
      } else {
        suppressClickRef.current = false;
      }
    } else {
      suppressClickRef.current = dragState.moved;
    }

    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    if (dragState.dragging && !canceled) {
      setPosition((currentPosition) => {
        const nextPosition = clampFabPosition(currentPosition ?? dragState.startPosition);
        writeFabPosition(nextPosition);
        return nextPosition;
      });
    }
  };

  if (!visible || !mounted || position === null) {
    return null;
  }

  return createPortal(
    <>
      {open && (
        <div className="mobile-shortcut-fab-drawer-layer">
          <button
            type="button"
            className="mobile-shortcut-fab-scrim"
            aria-label="关闭快捷操作"
            onClick={() => setOpen(false)}
          />
          <aside className="mobile-shortcut-fab-drawer" role="menu" aria-label="快捷操作">
            <div className="mobile-shortcut-fab-drawer-header">
              <span>快捷操作</span>
              <button type="button" aria-label="关闭快捷操作" onClick={() => setOpen(false)}>
                <span aria-hidden="true">×</span>
              </button>
            </div>
            <div className="mobile-shortcut-fab-drawer-actions">
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
          </aside>
        </div>
      )}
      {!open && (
        <div
          className={`mobile-shortcut-fab${draggingPosition ? " dragging" : ""}`}
          style={{ left: `${position.x}px`, top: `${position.y}px` }}
        >
          <button
            type="button"
            className="mobile-shortcut-fab-ball"
            aria-expanded={open}
            aria-haspopup="menu"
            aria-label="打开快捷操作"
            onPointerDown={handleBallPointerDown}
            onPointerMove={handleBallPointerMove}
            onPointerUp={finishBallDrag}
            onPointerCancel={(event) => finishBallDrag(event, true)}
            onClick={() => {
              if (suppressClickRef.current) {
                suppressClickRef.current = false;
                return;
              }
              setOpen(true);
            }}
          >
            <span className="mobile-shortcut-fab-ball-icon" aria-hidden="true" />
          </button>
        </div>
      )}
    </>,
    document.body
  );
}
