// The destination guard (R05.10): every async action notes where it started, and its result is
// applied only if the renderer is still pointed at the same place when the result lands.

import { uiStore } from "./uiStore.ts";

export interface Destination {
  readonly sid: string | null;
  readonly storedSid: string | null;
  readonly revision: number;
}

export function captureDestination(): Destination {
  const { sid, storedSid, revision } = uiStore.getState();
  return { sid, storedSid, revision };
}

export function isCurrent(dest: Destination): boolean {
  const now = uiStore.getState();
  return now.sid === dest.sid && now.storedSid === dest.storedSid && now.revision === dest.revision;
}

/** Point the renderer at a session; results captured before this are no longer current. */
export function focusSession(sid: string | null, storedSid: string | null = sid): void {
  const { revision } = uiStore.getState();
  uiStore.getState().patch({ sid, storedSid, revision: revision + 1 });
}

/** Invalidate every captured destination without changing the session (e.g. a composer reset). */
export function bumpRevision(): void {
  uiStore.getState().patch({ revision: uiStore.getState().revision + 1 });
}
