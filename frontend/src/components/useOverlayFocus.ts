import { useEffect, type RefObject } from "react";

type OverlayFocusOptions<T extends HTMLElement> = {
  isOpen: boolean;
  ref: RefObject<T>;
  onEscape?: (event: KeyboardEvent) => void;
  initialFocusSelector?: string;
  preserveExistingFocus?: boolean;
};

function containsActiveElement(element: HTMLElement): boolean {
  const activeElement = document.activeElement;
  return activeElement instanceof Node && element.contains(activeElement);
}

function focusOverlayElement<T extends HTMLElement>(
  element: T,
  initialFocusSelector: string | undefined
): void {
  const target = initialFocusSelector === undefined
    ? null
    : element.querySelector<HTMLElement>(initialFocusSelector);

  if (!element.hasAttribute("tabindex")) {
    element.tabIndex = -1;
  }
  (target ?? element).focus({ preventScroll: true });
}

export function useOverlayFocus<T extends HTMLElement>({
  isOpen,
  ref,
  onEscape,
  initialFocusSelector,
  preserveExistingFocus = false
}: OverlayFocusOptions<T>): void {
  useEffect(() => {
    if (!isOpen) {
      return;
    }

    const frame = window.requestAnimationFrame(() => {
      const element = ref.current;
      if (element === null) {
        return;
      }
      if (preserveExistingFocus && containsActiveElement(element)) {
        return;
      }
      focusOverlayElement(element, initialFocusSelector);
    });

    return () => window.cancelAnimationFrame(frame);
  }, [initialFocusSelector, isOpen, preserveExistingFocus, ref]);

  useEffect(() => {
    if (!isOpen || onEscape === undefined) {
      return;
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) {
        return;
      }

      event.preventDefault();
      event.stopPropagation();
      onEscape(event);
    };

    window.addEventListener("keydown", handleKeyDown, { capture: true });
    return () => window.removeEventListener("keydown", handleKeyDown, { capture: true });
  }, [isOpen, onEscape]);
}
