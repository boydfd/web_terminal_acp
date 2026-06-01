import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";

import { isOnboardingEnabled, readOnboardingCompleted, writeOnboardingCompleted } from "../onboarding";
import { useOverlayFocus } from "./useOverlayFocus";

export type OnboardingAction =
  | "remote-bootstrap"
  | "remote-registration-menu"
  | "remote-registration"
  | "new-terminal"
  | "quick-input"
  | "switch-terminal"
  | "details"
  | "settings";

export type OnboardingStep = {
  id: string;
  title: string;
  body: string;
  path?: string[];
  shortcutLabels?: string[];
  targetId?: string;
  action?: OnboardingAction;
};

type OnboardingTourProps = {
  steps: OnboardingStep[];
  onStepAction: (action: OnboardingAction) => void;
};

const TARGET_RETRY_MS = 80;
const TARGET_RETRY_LIMIT = 8;

type TargetRect = {
  top: number;
  left: number;
  width: number;
  height: number;
};

function findTarget(targetId: string): HTMLElement | null {
  return document.querySelector(`[data-onboarding-id="${targetId}"]`);
}

function targetRectFor(targetId: string | undefined): TargetRect | null {
  if (targetId === undefined) {
    return null;
  }

  const target = findTarget(targetId);
  if (target === null) {
    return null;
  }

  const rect = target.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) {
    return null;
  }

  return {
    top: rect.top,
    left: rect.left,
    width: rect.width,
    height: rect.height
  };
}

function cardStyleFor(rect: TargetRect | null): CSSProperties | undefined {
  if (rect === null) {
    return undefined;
  }

  const cardWidth = Math.min(420, Math.max(320, window.innerWidth - 32));
  const cardHeight = Math.min(320, Math.max(220, window.innerHeight - 32));
  const gap = 14;
  const padding = 16;
  const targetRight = rect.left + rect.width;
  const targetBottom = rect.top + rect.height;
  const clamp = (value: number, min: number, max: number) => Math.min(Math.max(value, min), max);
  const candidates = [
    {
      top: clamp(rect.top + rect.height / 2 - cardHeight / 2, padding, window.innerHeight - cardHeight - padding),
      left: targetRight + gap
    },
    {
      top: clamp(rect.top + rect.height / 2 - cardHeight / 2, padding, window.innerHeight - cardHeight - padding),
      left: rect.left - cardWidth - gap
    },
    {
      top: targetBottom + gap,
      left: clamp(rect.left + rect.width / 2 - cardWidth / 2, padding, window.innerWidth - cardWidth - padding)
    },
    {
      top: rect.top - cardHeight - gap,
      left: clamp(rect.left + rect.width / 2 - cardWidth / 2, padding, window.innerWidth - cardWidth - padding)
    }
  ].map((candidate) => ({
    top: clamp(candidate.top, padding, Math.max(padding, window.innerHeight - cardHeight - padding)),
    left: clamp(candidate.left, padding, Math.max(padding, window.innerWidth - cardWidth - padding))
  }));

  const overlapArea = (candidate: { top: number; left: number }) => {
    const overlapWidth = Math.max(0, Math.min(candidate.left + cardWidth, targetRight) - Math.max(candidate.left, rect.left));
    const overlapHeight = Math.max(0, Math.min(candidate.top + cardHeight, targetBottom) - Math.max(candidate.top, rect.top));
    return overlapWidth * overlapHeight;
  };
  const [best] = candidates.sort((first, second) => overlapArea(first) - overlapArea(second));

  return {
    width: cardWidth,
    maxHeight: cardHeight,
    top: best.top,
    left: best.left
  };
}

export function OnboardingTour({ steps, onStepAction }: OnboardingTourProps) {
  const enabled = isOnboardingEnabled();
  const [visible, setVisible] = useState(() => enabled && !readOnboardingCompleted());
  const [index, setIndex] = useState(0);
  const [targetRect, setTargetRect] = useState<TargetRect | null>(null);
  const cardRef = useRef<HTMLElement | null>(null);
  const step = steps[index] ?? steps[0];
  const isLast = index >= steps.length - 1;

  const complete = useCallback(() => {
    writeOnboardingCompleted(true);
    setVisible(false);
  }, []);

  const restart = useCallback(() => {
    if (!enabled) {
      return;
    }
    writeOnboardingCompleted(false);
    setIndex(0);
    setVisible(true);
  }, [enabled]);

  useEffect(() => {
    window.addEventListener("web-terminal-acp:start-onboarding", restart);
    return () => window.removeEventListener("web-terminal-acp:start-onboarding", restart);
  }, [restart]);

  useEffect(() => {
    if (!enabled || !visible || step === undefined) {
      return;
    }

    if (step.action !== undefined) {
      onStepAction(step.action);
    }
  }, [enabled, onStepAction, step, visible]);

  useEffect(() => {
    if (!enabled || !visible || step === undefined) {
      setTargetRect(null);
      return;
    }

    let retry = 0;
    let timeout: number | null = null;
    let frame: number | null = null;

    const update = () => {
      frame = null;
      const rect = targetRectFor(step.targetId);
      setTargetRect(rect);
      if (rect !== null || step.targetId === undefined || retry >= TARGET_RETRY_LIMIT) {
        return;
      }
      retry += 1;
      timeout = window.setTimeout(() => {
        frame = window.requestAnimationFrame(update);
      }, TARGET_RETRY_MS);
    };

    const schedule = () => {
      if (frame !== null) {
        return;
      }
      frame = window.requestAnimationFrame(update);
    };

    schedule();
    window.addEventListener("resize", schedule);
    window.addEventListener("scroll", schedule, true);
    return () => {
      if (timeout !== null) {
        window.clearTimeout(timeout);
      }
      if (frame !== null) {
        window.cancelAnimationFrame(frame);
      }
      window.removeEventListener("resize", schedule);
      window.removeEventListener("scroll", schedule, true);
    };
  }, [enabled, step, visible]);

  useEffect(() => {
    if (!enabled || !visible || step?.targetId === undefined) {
      return;
    }

    const target = findTarget(step.targetId);
    if (typeof target?.scrollIntoView === "function") {
      target.scrollIntoView({ block: "center", inline: "nearest" });
    }
  }, [enabled, step, visible]);

  useOverlayFocus({
    isOpen: enabled && visible && step !== undefined,
    ref: cardRef,
    onEscape: complete
  });

  const highlightStyle = useMemo<CSSProperties | null>(() => {
    if (targetRect === null) {
      return null;
    }

    return {
      top: Math.max(8, targetRect.top - 6),
      left: Math.max(8, targetRect.left - 6),
      width: targetRect.width + 12,
      height: targetRect.height + 12
    };
  }, [targetRect]);

  if (!enabled) {
    return null;
  }

  if (!visible || step === undefined) {
    return null;
  }

  return (
    <div className="onboarding-layer" aria-live="polite">
      <div className="onboarding-scrim" />
      {highlightStyle !== null && <div className="onboarding-highlight" style={highlightStyle} />}
      <section
        ref={cardRef}
        className="onboarding-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="onboarding-title"
        style={cardStyleFor(targetRect)}
      >
        <div className="onboarding-card-header">
          <span>{index + 1} / {steps.length}</span>
          <button type="button" onClick={complete}>跳过</button>
        </div>
        <h2 id="onboarding-title">{step.title}</h2>
        <p>{step.body}</p>
        {(step.path !== undefined || step.shortcutLabels !== undefined) && (
          <div className="onboarding-step-meta">
            {step.path !== undefined && (
              <div>
                <span>进入路径</span>
                <strong>{step.path.join(" -> ")}</strong>
              </div>
            )}
            {step.shortcutLabels !== undefined && step.shortcutLabels.length > 0 && (
              <div>
                <span>快捷键</span>
                <strong>{step.shortcutLabels.join(" / ")}</strong>
              </div>
            )}
          </div>
        )}
        <div className="onboarding-card-actions">
          <button
            type="button"
            disabled={index === 0}
            onClick={() => setIndex((current) => Math.max(0, current - 1))}
          >
            上一步
          </button>
          <button
            type="button"
            onClick={() => {
              if (isLast) {
                complete();
                return;
              }
              setIndex((current) => Math.min(steps.length - 1, current + 1));
            }}
          >
            {isLast ? "完成" : "下一步"}
          </button>
        </div>
      </section>
    </div>
  );
}
