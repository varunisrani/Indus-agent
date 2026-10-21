// React access to the connection store: one store per renderer, provided at the root.

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useMemo,
  useSyncExternalStore,
} from "react";
import type {
  ChatConnection,
  ConnectionState,
  ConnectionStore,
  GatewayConnection,
} from "./connectionStore.ts";
import {
  bindAnswers,
  type OpenServerRequest,
  type PendingServerRequest,
  type ServerRequestStore,
} from "./serverRequests.ts";

const StoreContext = createContext<ConnectionStore | null>(null);

export function ConnectionStoreProvider(props: { store: ConnectionStore; children: ReactNode }) {
  return <StoreContext.Provider value={props.store}>{props.children}</StoreContext.Provider>;
}

export function useConnectionStore(): ConnectionStore {
  const store = useContext(StoreContext);
  if (store === null) throw new Error("useConnectionStore outside ConnectionStoreProvider");
  return store;
}

export function usePrimaryConnection(): GatewayConnection {
  return useConnectionStore().primary();
}

export function useConnectionState(connection: GatewayConnection): ConnectionState {
  return useSyncExternalStore(
    (listener) => connection.subscribe(listener),
    () => connection.getState(),
  );
}

export function useServerRequests(
  store: ServerRequestStore,
  sessionId?: string,
): readonly PendingServerRequest[] {
  return useSyncExternalStore(
    (listener) => store.subscribe(listener),
    () => store.list(sessionId),
  );
}

/** The store when one is provided (the shell renders without one in previews and tests). */
export function useMaybeConnectionStore(): ConnectionStore | null {
  return useContext(StoreContext);
}

// -- the chat surfaces' hooks (slice C) -----------------------------------------------------------

const ConnectionOverride = createContext<ChatConnection | null>(null);
const RequestStoreOverride = createContext<ServerRequestStore | null>(null);

/** Puts an explicit connection under the chat surfaces (tests, previews). */
export function GatewayConnectionProvider(props: {
  value: ChatConnection | null;
  children?: ReactNode;
}) {
  return (
    <ConnectionOverride.Provider value={props.value}>{props.children}</ConnectionOverride.Provider>
  );
}

/** Puts an explicit server-request store under the cards and status bar (tests, previews). */
export function ServerRequestStoreProvider(props: {
  value: ServerRequestStore;
  children?: ReactNode;
}) {
  return (
    <RequestStoreOverride.Provider value={props.value}>
      {props.children}
    </RequestStoreOverride.Provider>
  );
}

/** The chat surfaces' connection: an explicit one, else the window's primary, else null. */
export function useGatewayConnection(): ChatConnection | null {
  const override = useContext(ConnectionOverride);
  const store = useContext(StoreContext);
  return override ?? store?.primary() ?? null;
}

/** The server-request store: an explicit one, else the primary connection's, else null. */
export function useServerRequestStore(): ServerRequestStore | null {
  const override = useContext(RequestStoreOverride);
  const store = useContext(StoreContext);
  return override ?? store?.primary().serverRequests ?? null;
}

const NO_REQUESTS: readonly PendingServerRequest[] = [];
const NO_OPEN: readonly OpenServerRequest[] = [];
const noSubscribe = () => () => {};

/**
 * Open server requests with their answers bound: one session's, all of them (`undefined`), or
 * none (`null`). Re-renders on change.
 */
export function useOpenServerRequests(sessionId?: string | null): readonly OpenServerRequest[] {
  const store = useServerRequestStore();
  const subscribe = useCallback(
    (listener: () => void) => (store === null ? noSubscribe() : store.subscribe(listener)),
    [store],
  );
  const read = useCallback(
    () => (store === null || sessionId === null ? NO_REQUESTS : store.list(sessionId)),
    [store, sessionId],
  );
  const pending = useSyncExternalStore(subscribe, read, read);
  return useMemo(() => (store === null ? NO_OPEN : bindAnswers(store, pending)), [store, pending]);
}
