// The chat terminal's home outside the router (R09.1, R09.6). It mounts the first time `/chat` is
// visited and then stays mounted: other tabs only hide it (`display:none` + `aria-hidden`), so the
// PTY socket and the terminal survive a trip to Logs and back. The router keeps an empty `/chat`
// route for deep links and history.

import { type ComponentType, Suspense, useState } from "react";

export interface ChatHostProps {
  /** True while `/chat` is the current route (the host may pause work while hidden). */
  active: boolean;
}

/**
 * Shown in the slot until P18-26's xterm host lands; the lead swaps it for
 * `lazy(() => import("../chat/ChatHost.tsx"))` in App.tsx.
 */
export function ChatPending({ active }: ChatHostProps) {
  return (
    <div className="p-6 text-sm text-muted" data-active={active ? "true" : "false"}>
      The chat terminal is not part of this build yet.
    </div>
  );
}

export function ChatHostSlot({
  active,
  host: Host,
}: {
  active: boolean;
  host: ComponentType<ChatHostProps>;
}) {
  const [mounted, setMounted] = useState(active);
  if (active && !mounted) setMounted(true);
  if (!mounted) return null;
  return (
    <div
      data-testid="chat-host"
      data-chat-active={active ? "true" : "false"}
      aria-hidden={active ? undefined : true}
      style={active ? undefined : { display: "none" }}
      className="flex min-h-0 flex-1 flex-col"
    >
      <Suspense fallback={active ? <p className="p-6 text-sm text-muted">Loading chat…</p> : null}>
        <Host active={active} />
      </Suspense>
    </div>
  );
}
