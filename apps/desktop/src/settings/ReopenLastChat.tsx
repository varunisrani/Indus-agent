// "Reopen last chat on launch" (W55-F3): on a cold start the main window lands on a fresh chat;
// with the preference on it opens the foreground profile's most recent conversation instead —
// the sidebar's newest live row, so a deleted or archived chat is never reopened. It acts once per
// window and only while the person is still on the untouched new chat; any navigation of theirs
// first (or a slow list) cancels it.

import { useEffect, useRef, useSyncExternalStore } from "react";
import { useLocation, useNavigate } from "react-router";
import { launchFacts } from "../app/bridge.ts";
import { NEW_CHAT_PATH, sessionPath } from "../app/routes.ts";
import { useProvidedShellChrome } from "../app/Shell.tsx";
import { useSwitcher, useSwitcherState } from "../profiles/switcherReact.tsx";
import type { SessionListState, SessionListStore } from "../sidebar/sessionList.ts";
import { displayPrefsKnown, useDisplayPrefs } from "./displayPrefs.ts";

/** How long the cold start waits for the preference and the first list before giving up. */
export const REOPEN_WAIT_MS = 15_000;

let settled = false;

/** Allow another cold start (tests). */
export function resetReopenLastChat(): void {
  settled = false;
}

const noStore = {
  subscribe: () => () => undefined,
  getState: () => null,
};

function useList(store: SessionListStore | null): SessionListState | null {
  const source = store ?? noStore;
  return useSyncExternalStore(
    (listener) => source.subscribe(listener),
    () => source.getState(),
  );
}

/** The row a cold start reopens, or null while the list is not known yet ("none" when empty). */
export function lastChatTarget(
  list: SessionListState | null,
  sawLoad: boolean,
): string | "none" | null {
  if (list === null) return null;
  const row = list.rows.find((r) => !r.archived);
  if (row !== undefined) return row.id;
  return sawLoad && !list.loading && list.error === null ? "none" : null;
}

export function ReopenLastChat() {
  const chrome = useProvidedShellChrome();
  const profile = useSwitcherState(useSwitcher())?.foreground ?? null;
  const store = chrome === null || profile === null ? null : chrome.sidebar.sessions(profile);
  const list = useList(store);
  const display = useDisplayPrefs();
  const location = useLocation();
  const navigate = useNavigate();
  const sawLoad = useRef(false);
  if (list?.loading) sawLoad.current = true;

  useEffect(() => {
    if (settled) return;
    const timer = window.setTimeout(() => {
      settled = true;
    }, REOPEN_WAIT_MS);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (settled) return;
    if (launchFacts().windowKind !== "main" || location.pathname !== NEW_CHAT_PATH) {
      settled = true; // a pop-out window, a deep link, or the person moved first
      return;
    }
    if (!displayPrefsKnown()) return;
    if (!display.reopenLastChat) {
      settled = true;
      return;
    }
    const target = lastChatTarget(list, sawLoad.current);
    if (target === null) return;
    settled = true;
    if (target !== "none") navigate(sessionPath(target), { replace: true });
  });
  return null;
}
