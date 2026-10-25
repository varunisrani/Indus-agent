// The status block in the sidebar footer (P18-25, restyled W51-a): daemon state, active sessions
// and connected channels from the public status probe, refreshed every 5 s while the tab is
// visible. The version sits in the sidebar's bottom row (`VersionLine`). Collapsed, the block is a
// single coloured dot whose tooltip carries the same facts.

import { BRAND_NAME } from "@indus/ui";
import type { StatusResponse } from "../lib/restTypes.ts";
import { cx } from "../ui/cx.ts";
import { useShellStatus } from "./shellStatus.ts";

const DOT: Record<string, string> = {
  running: "bg-success",
  draining: "bg-warning",
  starting: "bg-warning",
  stopping: "bg-warning",
  degraded: "bg-warning",
  startup_failed: "bg-danger",
};

export function daemonLabel(state: string): string {
  if (state === "startup_failed") return "failed to start";
  return state.replaceAll("_", " ");
}

function capitalised(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function StatusStripView({
  status,
  failed,
  compact = false,
}: {
  status: StatusResponse | undefined;
  failed: boolean;
  compact?: boolean;
}) {
  if (!status) {
    const text = failed ? "Status unavailable" : "Checking status…";
    return (
      <section
        aria-label="Status"
        className={cx("text-[0.75rem] text-ink-soft", compact ? "sr-only" : "px-5")}
      >
        {text}
      </section>
    );
  }
  const state = status.daemon.state;
  const platforms = status.components?.platforms;
  const daemon = capitalised(daemonLabel(state));
  if (compact) {
    const summary = `Daemon status: ${daemon}`;
    return (
      <section aria-label="Status" className="flex justify-center py-1">
        <span
          title={summary}
          className={cx("inline-block size-1.5 rounded-full", DOT[state] ?? "bg-ink-soft")}
        />
        <span className="sr-only" data-testid="status-daemon">
          {summary}
        </span>
      </section>
    );
  }
  return (
    <section
      aria-label="Status"
      className="flex flex-col gap-0.5 px-5 text-[0.75rem] tracking-[0.06em] text-ink-muted"
    >
      <span className="text-ink-soft">System</span>
      <span data-testid="status-daemon">
        Daemon status: <strong className="font-semibold text-ink">{daemon}</strong>
      </span>
      {typeof status.active_sessions === "number" ? (
        <span data-testid="status-sessions">
          Active sessions: <span className="text-ink">{status.active_sessions}</span>
        </span>
      ) : null}
      {platforms && platforms.configured > 0 ? (
        <span data-testid="status-platforms">
          Channels:{" "}
          <span className="text-ink">
            {platforms.connected}/{platforms.configured}
          </span>{" "}
          connected
        </span>
      ) : null}
    </section>
  );
}

export function StatusStrip({ compact = false }: { compact?: boolean }) {
  const status = useShellStatus();
  return <StatusStripView status={status.data} failed={status.isError} compact={compact} />;
}

/** "v0.1.0 · Indus AGI Agent" in the sidebar's last row. */
export function VersionLine() {
  const status = useShellStatus();
  return (
    <p
      data-testid="sidebar-version"
      className="m-0 flex items-center justify-between gap-2 px-5 text-[0.75rem] tracking-[0.08em] text-ink-soft"
    >
      <span>{status.data ? `v${status.data.version}` : "v…"}</span>
      <span className="text-ink-muted">{BRAND_NAME}</span>
    </p>
  );
}
