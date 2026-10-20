// Left navigation. Wide screens: an inline column that collapses to a narrow rail (remembered per
// browser). Below 1,024 px: a slide-over drawer opened from the header's menu button.

import { type ReactNode, useEffect, useState } from "react";
import { NavLink } from "react-router";
import { ProfileSwitcher } from "./ProfileScope.tsx";
import { DOCS_LINK, type PageRoute } from "./routes.ts";
import { ThemeSwitch } from "./ThemeSwitch.tsx";

export const SIDEBAR_KEY = "indus-sidebar-collapsed";
export const WIDE_QUERY = "(min-width: 1024px)";

export function readCollapsed(): boolean {
  try {
    return window.localStorage.getItem(SIDEBAR_KEY) === "true";
  } catch {
    return false;
  }
}

export function writeCollapsed(value: boolean): void {
  try {
    window.localStorage.setItem(SIDEBAR_KEY, String(value));
  } catch {
    // private browsing: not remembered
  }
}

/** True at 1,024 px and wider (also when the browser cannot tell). */
export function useWideScreen(): boolean {
  const [query] = useState(() =>
    typeof window.matchMedia === "function" ? window.matchMedia(WIDE_QUERY) : null,
  );
  const [wide, setWide] = useState(query ? query.matches : true);
  useEffect(() => {
    if (!query) return;
    const update = () => setWide(query.matches);
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, [query]);
  return wide;
}

interface SidebarProps {
  items: readonly PageRoute[];
  search: string;
  wide: boolean;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  mobileOpen: boolean;
  onCloseMobile: () => void;
  /** Status strip (P18-25) and auth widget (P18-28) slots. */
  footer?: ReactNode;
}

function NavItems({
  items,
  search,
  compact,
  onNavigate,
}: {
  items: readonly PageRoute[];
  search: string;
  compact: boolean;
  onNavigate: () => void;
}) {
  const linkClass = ({ isActive }: { isActive: boolean }) =>
    `flex items-center gap-2 rounded-md px-2 py-1.5 text-sm ${
      isActive ? "bg-accent text-accent-fg" : "text-fg hover:bg-code"
    }`;
  return (
    <ul className="flex flex-col gap-0.5">
      {items.map((item) => (
        <li key={item.path}>
          <NavLink
            to={{ pathname: item.path, search }}
            className={linkClass}
            onClick={onNavigate}
            {...(compact ? { "aria-label": item.title, title: item.title } : {})}
          >
            {compact ? <span aria-hidden="true">{item.title.slice(0, 2)}</span> : item.title}
          </NavLink>
        </li>
      ))}
      <li>
        <a
          href={DOCS_LINK.href}
          target="_blank"
          rel="noopener noreferrer"
          className="flex items-center gap-2 rounded-md px-2 py-1.5 text-sm text-fg hover:bg-code"
          {...(compact ? { "aria-label": DOCS_LINK.title, title: DOCS_LINK.title } : {})}
        >
          {compact ? <span aria-hidden="true">Do</span> : `${DOCS_LINK.title} ↗`}
        </a>
      </li>
    </ul>
  );
}

export function Sidebar(props: SidebarProps) {
  const { items, search, wide, collapsed, onToggleCollapsed, mobileOpen, onCloseMobile, footer } =
    props;
  const compact = wide && collapsed;

  const body = (
    <nav aria-label="Dashboard" className="flex h-full flex-col gap-3 p-2">
      <div className="flex items-center justify-between gap-2 px-1">
        {compact ? null : <span className="text-sm font-semibold">Indus Agent</span>}
        {wide ? (
          <button
            type="button"
            className="rounded-md border border-line px-2 py-1 text-xs"
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-expanded={!collapsed}
            onClick={onToggleCollapsed}
          >
            {collapsed ? "»" : "«"}
          </button>
        ) : (
          <button
            type="button"
            className="rounded-md border border-line px-2 py-1 text-xs"
            onClick={onCloseMobile}
          >
            Close
          </button>
        )}
      </div>
      <ProfileSwitcher compact={compact} />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <NavItems
          items={items}
          search={search}
          compact={compact}
          onNavigate={wide ? () => {} : onCloseMobile}
        />
      </div>
      <div className="flex flex-col gap-2 border-t border-line pt-2">
        {footer}
        <ThemeSwitch compact={compact} />
      </div>
    </nav>
  );

  if (wide) {
    return (
      <aside
        data-testid="sidebar"
        data-collapsed={collapsed ? "true" : "false"}
        className={`shrink-0 border-r border-line bg-raised ${collapsed ? "w-16" : "w-56"}`}
      >
        {body}
      </aside>
    );
  }
  if (!mobileOpen) return null;
  return (
    <div className="fixed inset-0 z-40 flex" data-testid="sidebar-drawer">
      <aside data-testid="sidebar" className="h-full w-64 border-r border-line bg-raised">
        {body}
      </aside>
      <button
        type="button"
        aria-label="Close navigation"
        className="flex-1 bg-black/40"
        onClick={onCloseMobile}
      />
    </div>
  );
}
