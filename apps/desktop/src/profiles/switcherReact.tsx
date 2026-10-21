// React access to the profile switcher (P20-26): one switcher per window, provided at the root.

import { createContext, type ReactNode, useContext, useSyncExternalStore } from "react";
import type { ProfileSwitcher, SwitcherState } from "./switching.ts";
import type { ChatWorkspace } from "./workspace.ts";

export type WindowSwitcher = ProfileSwitcher<ChatWorkspace>;

const SwitcherContext = createContext<WindowSwitcher | null>(null);

export function SwitcherProvider(props: { switcher: WindowSwitcher | null; children: ReactNode }) {
  return (
    <SwitcherContext.Provider value={props.switcher}>{props.children}</SwitcherContext.Provider>
  );
}

export function useSwitcher(): WindowSwitcher | null {
  return useContext(SwitcherContext);
}

const noSubscribe = () => () => undefined;
const noState = () => null;

export function useSwitcherState(switcher: WindowSwitcher | null): SwitcherState | null {
  return useSyncExternalStore(
    switcher === null ? noSubscribe : (listener) => switcher.subscribe(listener),
    switcher === null ? noState : () => switcher.getState(),
  );
}

/** The foreground profile's workspace; re-renders on a swap and after a wipe. */
export function useForeground(switcher: WindowSwitcher | null): ChatWorkspace | null {
  const state = useSwitcherState(switcher);
  const workspace =
    switcher === null || state === null ? null : switcher.workspace(state.foreground);
  useSyncExternalStore(
    workspace === null ? noSubscribe : (listener) => workspace.subscribe(listener),
    workspace === null ? () => 0 : () => workspace.getVersion(),
  );
  return workspace;
}
