// The shell's consumers of the contribution registry (P24-32 wiring, D-16.799): `route` pages, the
// sidebar's `sidebar-nav` entries and `pane` tool panes. `status-bar` is read by the status bar.
//
// - Routes: `useContributedPaths()` is the set of route paths `classifyPath` is given; the route
//   view renders the matching contribution's `render()` inside its own error boundary. A route
//   never shadows a built-in page or overlay (`routes.ts`).
// - Nav: each `sidebar-nav` entry is a link under the built-in pages; its icon is its own
//   `icon()` (inside a boundary), the built-in page's icon when it names one (`navId`), else a
//   puzzle piece.
// - Panes: `mirrorPaneContributions` keeps the window's pane registry in step with the `pane`
//   area, so a plugin pane shows in the layout toolbar's "Open" list and leaves when its plugin
//   unloads. A plugin's pane gets a small view of where it is (`PluginPaneContext`) — never the
//   connection the working panes get, which would reach every gateway method.

import { Puzzle } from "lucide-react";
import type { ReactNode } from "react";
import { NavLink } from "react-router";
import { PAGE_ICONS } from "../app/pages.tsx";
import { type PageId, routeKey } from "../app/routes.ts";
import { type PaneRegistry, paneRegistry } from "../panes/PaneShell.tsx";
import { ContributionBoundary, useContributions } from "./react.tsx";
import { type Contribution, type ContributionRegistry, contributions } from "./registry.ts";

/** The registry's route paths, in `routeKey` form. */
export function routePaths(list: readonly Contribution[]): ReadonlySet<string> {
  return new Set(list.map((c) => routeKey(String(c.path))));
}

/** The live route paths of the shell's registry. */
export function useContributedPaths(): ReadonlySet<string> {
  return routePaths(useContributions("route"));
}

/** The page of the `route` contribution at `path` (nothing when it went away meanwhile). */
export function ContributedRoute(props: { path: string }) {
  const list = useContributions("route");
  const found = list.find((c) => routeKey(String(c.path)) === props.path);
  if (found === undefined) return null;
  const render = found.render as () => ReactNode;
  return (
    <ContributionBoundary key={found.id} id={found.id}>
      <Rendered render={render} />
    </ContributionBoundary>
  );
}

function Rendered(props: { render: () => ReactNode }) {
  return <>{props.render()}</>;
}

function navIcon(c: Contribution): ReactNode {
  if (typeof c.icon === "function") {
    return (
      <ContributionBoundary id={`${c.id}.icon`}>
        <Rendered render={c.icon as () => ReactNode} />
      </ContributionBoundary>
    );
  }
  const own = typeof c.navId === "string" ? PAGE_ICONS[c.navId as PageId] : undefined;
  return own ?? <Puzzle aria-hidden="true" size={16} />;
}

/** The `sidebar-nav` entries, as list items of the sidebar's page list. */
export function ContributedNav() {
  const list = useContributions("sidebar-nav");
  return (
    <>
      {list.map((c) => (
        <li key={c.id} data-contribution={c.id}>
          <NavLink to={String(c.path)} className="nav-item">
            {navIcon(c)}
            <span>{String(c.label)}</span>
          </NavLink>
        </li>
      ))}
    </>
  );
}

/** What a contributed pane is told about where it shows. */
export interface PluginPaneContext {
  readonly sessionId: string | null;
  readonly profile: string | null;
  readonly active: boolean;
  readonly hidden: boolean;
}

/**
 * Keep `panes` in step with the `pane` area of `registry`: one pane per contribution (its id, its
 * `title`, `stateful` when it says so), dropped when the contribution goes. An id a built-in pane
 * already uses is skipped. Returns the stop function (which drops every mirrored pane).
 */
export function mirrorPaneContributions(
  registry: ContributionRegistry = contributions,
  panes: PaneRegistry = paneRegistry,
): () => void {
  const owned = new Map<string, { contribution: Contribution; dispose: () => void }>();
  const sync = () => {
    const list = registry.list("pane");
    const current = new Map(list.map((c) => [c.id, c]));
    for (const [id, entry] of [...owned]) {
      if (current.get(id) !== entry.contribution) {
        owned.delete(id);
        entry.dispose();
      }
    }
    for (const c of list) {
      if (owned.has(c.id) || panes.get(c.id) !== undefined) continue;
      const render = c.render as (context: PluginPaneContext) => ReactNode;
      const dispose = panes.register({
        id: c.id,
        stateful: c.stateful === true,
        title: String(c.title),
        render: (context) => {
          const view: PluginPaneContext = {
            sessionId: context.sessionId,
            profile: context.profile,
            active: context.active,
            hidden: context.hidden,
          };
          return (
            <ContributionBoundary id={c.id}>
              <Rendered render={() => render(view)} />
            </ContributionBoundary>
          );
        },
      });
      owned.set(c.id, { contribution: c, dispose });
    }
  };
  sync();
  const off = registry.subscribe(sync);
  return () => {
    off();
    for (const entry of owned.values()) entry.dispose();
    owned.clear();
  };
}
