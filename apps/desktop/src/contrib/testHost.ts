// A scripted plugin host for the contrib tests (P24-32).

import type { PluginHost } from "./sdk.ts";

export function fakeHost(overrides: Partial<PluginHost> = {}): PluginHost & {
  toasts: string[];
  navigated: string[];
  rpcCalls: string[];
  restCalls: string[];
} {
  const toasts: string[] = [];
  const navigated: string[] = [];
  const rpcCalls: string[] = [];
  const restCalls: string[] = [];
  const store = new Map<string, string>();
  return {
    toasts,
    navigated,
    rpcCalls,
    restCalls,
    state: {
      connection: () => ({ phase: "open", mode: "local", codeVersion: "1.0.0" }),
      profile: () => "default",
      route: () => "/",
      theme: () => "dark",
      subscribe: () => () => undefined,
    },
    toast: (text) => toasts.push(text),
    navigate: (path) => navigated.push(path),
    log: () => undefined,
    restartGateway: async () => true,
    onEvent: () => () => undefined,
    rpc: async (method) => {
      rpcCalls.push(method);
      return {};
    },
    rest: async (path) => {
      restCalls.push(path);
      return {};
    },
    socket: () => () => undefined,
    storage: () => ({
      getItem: (k) => store.get(k) ?? null,
      setItem: (k, v) => void store.set(k, v),
      removeItem: (k) => void store.delete(k),
    }),
    ...overrides,
  };
}
