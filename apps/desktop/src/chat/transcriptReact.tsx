// React access to the transcript store (one per connection, provided at the root).

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useSyncExternalStore,
} from "react";
import type { SessionTranscript } from "./messageStream.ts";
import type { TranscriptStore } from "./transcriptStore.ts";

const StoreContext = createContext<TranscriptStore | null>(null);

export function TranscriptStoreProvider(props: { store: TranscriptStore; children: ReactNode }) {
  return <StoreContext.Provider value={props.store}>{props.children}</StoreContext.Provider>;
}

export function useTranscriptStore(): TranscriptStore {
  const store = useContext(StoreContext);
  if (store === null) throw new Error("useTranscriptStore outside TranscriptStoreProvider");
  return store;
}

export function useTranscript(sessionId: string): SessionTranscript {
  const store = useTranscriptStore();
  return useSyncExternalStore(
    (listener) => store.subscribe(sessionId, listener),
    () => store.get(sessionId),
  );
}

export function useShowReasoning(): boolean {
  const store = useTranscriptStore();
  return useSyncExternalStore(
    (listener) => store.subscribeSettings(listener),
    () => store.showReasoning,
  );
}

const noSubscribe = () => () => {};

/**
 * The first prompt of a conversation as this window knows it, or null (no store here, no
 * session, nothing asked yet). The chat tab shows it until the list has the chat's title.
 */
export function useFirstPrompt(sessionId: string | null): string | null {
  const store = useContext(StoreContext);
  const subscribe = useCallback(
    (listener: () => void) =>
      store === null || sessionId === null ? noSubscribe() : store.subscribe(sessionId, listener),
    [store, sessionId],
  );
  const read = useCallback(() => {
    if (store === null || sessionId === null) return null;
    const first = store.get(sessionId).items.find((item) => item.kind === "user");
    const text = first?.kind === "user" ? first.text.trim().split("\n")[0]?.trim() : "";
    return text === undefined || text === "" ? null : text;
  }, [store, sessionId]);
  return useSyncExternalStore(subscribe, read, read);
}
