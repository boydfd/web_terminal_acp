import { useEffect, useState } from "react";

function hasTouchInput(): boolean {
  if (typeof window === "undefined") {
    return false;
  }

  return "ontouchstart" in window || navigator.maxTouchPoints > 0;
}

function isMobileUserAgent(): boolean {
  if (typeof navigator === "undefined") {
    return false;
  }

  return /Android|iPhone|iPad|iPod|Mobile|webOS|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);
}

export function readMobileLayout(): boolean {
  if (typeof window === "undefined") {
    return false;
  }

  const narrowViewport = window.matchMedia("(max-width: 1024px)").matches;
  const coarsePointer = window.matchMedia("(hover: none) and (pointer: coarse)").matches;
  const touchPhone = hasTouchInput() && (narrowViewport || isMobileUserAgent());

  return narrowViewport || coarsePointer || touchPhone;
}

export function useMobileLayout(): boolean {
  const [isMobileLayout, setIsMobileLayout] = useState(readMobileLayout);

  useEffect(() => {
    const narrowQuery = window.matchMedia("(max-width: 1024px)");
    const coarseQuery = window.matchMedia("(hover: none) and (pointer: coarse)");

    const sync = () => {
      setIsMobileLayout(readMobileLayout());
    };

    sync();
    narrowQuery.addEventListener("change", sync);
    coarseQuery.addEventListener("change", sync);
    window.addEventListener("resize", sync);
    window.addEventListener("orientationchange", sync);
    return () => {
      narrowQuery.removeEventListener("change", sync);
      coarseQuery.removeEventListener("change", sync);
      window.removeEventListener("resize", sync);
      window.removeEventListener("orientationchange", sync);
    };
  }, []);

  return isMobileLayout;
}
