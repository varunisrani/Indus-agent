// React access to the transcript store (one per connection, provided at the root).

import { createContext, type ReactNode, useContext, useSyncExternalStore } from "react";
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
