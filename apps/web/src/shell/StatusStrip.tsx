// The status strip in the sidebar footer (P18-25): daemon state, version, active sessions and
// connected platforms from the public status probe, refreshed every 5 s while the tab is visible.

import type { StatusResponse } from "../lib/restTypes.ts";
import { useShellStatus } from "./shellStatus.ts";

const DOT: Record<string, string> = {
  running: "bg-ok",
  draining: "bg-warn",
  starting: "bg-warn",
  stopping: "bg-warn",
  degraded: "bg-warn",
  startup_failed: "bg-danger",
};

export function daemonLabel(state: string): string {
  if (state === "startup_failed") return "failed to start";
  return state.replaceAll("_", " ");
}

export function StatusStripView({
  status,
  failed,
}: {
  status: StatusResponse | undefined;
  failed: boolean;
}) {
  if (!status) {
    return (
      <section aria-label="Status" className="px-1 text-xs text-muted">
        {failed ? "Status unavailable" : "Checking status…"}
      </section>
    );
  }
  const state = status.daemon.state;
  const platforms = status.components?.platforms;
  return (
    <section aria-label="Status" className="flex flex-col gap-0.5 px-1 text-xs text-muted">
      <span className="flex items-center gap-1.5 text-fg" data-testid="status-daemon">
        <span
          aria-hidden="true"
          className={`inline-block h-2 w-2 shrink-0 rounded-full ${DOT[state] ?? "bg-muted"}`}
        />
        Daemon {daemonLabel(state)}
      </span>
      {typeof status.active_sessions === "number" ? (
        <span data-testid="status-sessions">
          {status.active_sessions} active session{status.active_sessions === 1 ? "" : "s"}
        </span>
      ) : null}
      {platforms && platforms.configured > 0 ? (
        <span data-testid="status-platforms">
          {platforms.connected}/{platforms.configured} channels connected
        </span>
      ) : null}
      <span>Indus Agent {status.version}</span>
    </section>
  );
}

export function StatusStrip() {
  const status = useShellStatus();
  return <StatusStripView status={status.data} failed={status.isError} />;
}
