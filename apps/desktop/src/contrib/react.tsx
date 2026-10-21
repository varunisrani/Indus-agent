// The shell's side of the contribution registry (P24-32): read an area and render it, each
// contribution inside its own error boundary, so a plugin that throws while rendering removes
// itself from the screen and nothing else.

import { Component, type ReactNode, useSyncExternalStore } from "react";
import {
  type Area,
  type Contribution,
  type ContributionRegistry,
  contributions,
} from "./registry.ts";

/** The live list of `area` (re-renders on every registry change). */
export function useContributions(
  area: Area,
  registry: ContributionRegistry = contributions,
): Contribution[] {
  useSyncExternalStore(registry.subscribe, registry.snapshot, registry.snapshot);
  return registry.list(area);
}

class ContributionBoundary extends Component<
  { id: string; children: ReactNode },
  { failed: boolean }
> {
  override state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  override componentDidCatch(error: unknown): void {
    // biome-ignore lint/suspicious/noConsole: a contribution's render failure only warns
    console.warn(`[plugins] ${this.props.id} failed to render`, error);
  }

  override render(): ReactNode {
    return this.state.failed ? null : this.props.children;
  }
}

/** Every `render()` contribution of `area`, in order. */
export function ContributionArea(props: {
  area: Area;
  registry?: ContributionRegistry;
  wrap?: (c: Contribution, node: ReactNode) => ReactNode;
}) {
  const list = useContributions(props.area, props.registry);
  return (
    <>
      {list.map((c) => {
        const render = c.render as (() => ReactNode) | undefined;
        if (typeof render !== "function") return null;
        const node = (
          <ContributionBoundary key={c.id} id={c.id}>
            {<Rendered render={render} />}
          </ContributionBoundary>
        );
        return props.wrap ? <span key={c.id}>{props.wrap(c, node)}</span> : node;
      })}
    </>
  );
}

function Rendered(props: { render: () => ReactNode }) {
  return <>{props.render()}</>;
}
