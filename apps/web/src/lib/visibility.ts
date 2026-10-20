// Page-visibility aware polling: work runs on a fixed period only while the browser tab is visible,
// and once immediately when the tab comes back.

import { useEffect, useRef, useState } from "react";

export function isPageVisible(): boolean {
  return typeof document === "undefined" || !document.hidden;
}

/** True while the tab is visible; follows `visibilitychange`. */
export function usePageVisible(): boolean {
  const [visible, setVisible] = useState(isPageVisible);
  useEffect(() => {
    const update = () => setVisible(isPageVisible());
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  return visible;
}

/** Call `tick` every `periodMs` while the page is visible (and `enabled`). */
export function useVisiblePolling(tick: () => void, periodMs: number, enabled = true): void {
  const latest = useRef(tick);
  latest.current = tick;
  const visible = usePageVisible();
  const wasVisible = useRef(visible);

  useEffect(() => {
    if (!enabled || !visible) {
      wasVisible.current = visible;
      return;
    }
    if (!wasVisible.current) latest.current();
    wasVisible.current = true;
    const timer = setInterval(() => {
      if (isPageVisible()) latest.current();
    }, periodMs);
    return () => clearInterval(timer);
  }, [enabled, visible, periodMs]);
}
