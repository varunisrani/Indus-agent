// The running turn's activity line (W52-g): a small dithered accent square and the seconds since
// this window saw the turn start streaming. It replaces the spinner and stays for the whole turn.
// The clock is client-side only; a turn picked up mid-way counts from when it was first seen.

import { useEffect, useState } from "react";
import { useT } from "../i18n/index.tsx";

const started = new Map<string, number>();

/** When the turn under `key` was first seen running in this window. */
function startOf(key: string, now: number): number {
  const known = started.get(key);
  if (known !== undefined) return known;
  started.set(key, now);
  return now;
}

/** Forget the start times (tests). */
export function resetTurnClocks(): void {
  started.clear();
}

export function TurnActivity(props: { turnKey: string; now?: () => number; label?: string }) {
  const t = useT();
  const now = props.now ?? Date.now;
  const since = startOf(props.turnKey, now());
  const [, tick] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(timer);
  }, []);
  const seconds = Math.max(0, Math.floor((now() - since) / 1000));
  return (
    <div className="turn-activity" data-ui="turn-activity">
      <span className="turn-dither" aria-hidden="true" />
      <span className="sr-only">{t("transcript.activity")}</span>
      <span className="turn-elapsed">{t("transcript.elapsed", { seconds })}</span>
      {props.label !== undefined && <span className="turn-activity-label">{props.label}</span>}
    </div>
  );
}
