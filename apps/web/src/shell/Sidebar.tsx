// Left navigation. Wide screens: an inline column (wordmark, icon + uppercase label per page,
// footer with status, actions, theme and version) that collapses to an icon rail (remembered per
// browser); Documentation is an ordinary in-app page (W52-docs-1); the rail shows a tooltip per icon instead of clipped labels. Below 1,024 px: a
// slide-over drawer opened from the header's menu button.

import {
  BookOpen,
  ChartColumn,
  Clock,
  Cpu,
  FileText,
  FolderOpen,
  KeyRound,
  type LucideIcon,
  MessageSquare,
  Package,
  PanelLeftClose,
  PanelLeftOpen,
  Plug,
  Puzzle,
  Radio,
  Settings,
  ShieldCheck,
  SquareTerminal,
  Users,
  Webhook,
  Wrench,
  X,
} from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { NavLink } from "react-router";
import { ThemeMenu } from "../theme/ThemeMenu.tsx";
import { cx } from "../ui/cx.ts";
import { LanguageMenu } from "./LanguageMenu.tsx";
import { ProfileSwitcher } from "./ProfileScope.tsx";
import type { PageRoute } from "./routes.ts";
import { VersionLine } from "./StatusStrip.tsx";

export const SIDEBAR_KEY = "indus-sidebar-collapsed";
export const WIDE_QUERY = "(min-width: 1024px)";

/** Icon per built-in page; plugin tabs fall back to the puzzle piece. */
export const NAV_ICONS: Readonly<Record<string, LucideIcon>> = {
  "/chat": SquareTerminal,
  "/sessions": MessageSquare,
  "/files": FolderOpen,
  "/analytics": ChartColumn,
  "/models": Cpu,
  "/logs": FileText,
  "/cron": Clock,
  "/skills": Package,
  "/plugins": Puzzle,
  "/mcp": Plug,
  "/channels": Radio,
  "/webhooks": Webhook,
  "/pairing": ShieldCheck,
  "/profiles": Users,
  "/config": Settings,
  "/env": KeyRound,
  "/system": Wrench,
  "/docs": BookOpen,
};

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

type FooterContent = ReactNode | ((compact: boolean) => ReactNode);

interface SidebarProps {
  items: readonly PageRoute[];
  search: string;
  wide: boolean;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  mobileOpen: boolean;
  onCloseMobile: () => void;
  /** Status block, actions and the auth widget; a function gets told whether the rail is compact. */
  footer?: FooterContent;
}

const itemClass = (compact: boolean, active: boolean) =>
  cx(
    "group relative flex h-[2.3125rem] items-center gap-3 transition-colors",
    compact ? "justify-center px-0" : "px-5",
    active ? "text-ink" : "text-ink-muted hover:text-ink",
  );

interface Tip {
  text: string;
  top: number;
  left: number;
}

/**
 * Hover/focus labels for the collapsed rail. The rail scrolls, so the label is drawn at a fixed
 * position next to the hovered icon instead of inside the clipped list.
 */
function useRailTip(enabled: boolean) {
  const [tip, setTip] = useState<Tip | null>(null);
  useEffect(() => {
    if (!enabled) setTip(null);
  }, [enabled]);
  const handlers = (text: string) =>
    enabled
      ? {
          onMouseEnter: (event: { currentTarget: HTMLElement }) => show(event.currentTarget, text),
          onFocus: (event: { currentTarget: HTMLElement }) => show(event.currentTarget, text),
          onMouseLeave: () => setTip(null),
          onBlur: () => setTip(null),
        }
      : {};
  const show = (element: HTMLElement, text: string) => {
    const box = element.getBoundingClientRect();
    setTip({ text, top: box.top + box.height / 2, left: box.right + 8 });
  };
  const node = tip ? (
    <span
      aria-hidden="true"
      className="type-nav pointer-events-none fixed z-50 -translate-y-1/2 border border-line-strong bg-canvas px-2 py-1 whitespace-nowrap text-ink shadow-lg [animation:indus-fade-in_120ms_ease-out]"
      style={{ top: tip.top, left: tip.left }}
    >
      {tip.text}
    </span>
  ) : null;
  return { handlers, node };
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
  const tip = useRailTip(compact);
  return (
    <>
      {tip.node}
      <ul className="m-0 flex list-none flex-col p-0 py-2">
        {items.map((item) => {
          const Icon = NAV_ICONS[item.path] ?? Puzzle;
          return (
            <li key={item.path}>
              <NavLink
                to={{ pathname: item.path, search }}
                className={({ isActive }) => itemClass(compact, isActive)}
                onClick={onNavigate}
                {...tip.handlers(item.title)}
                {...(compact ? { "aria-label": item.title } : {})}
              >
                {({ isActive }) => (
                  <>
                    {isActive ? (
                      <span aria-hidden="true" className="absolute inset-y-0 left-0 w-px bg-ink" />
                    ) : null}
                    <span
                      aria-hidden="true"
                      className="pointer-events-none absolute inset-x-1.5 inset-y-0.5 bg-ink opacity-0 transition-opacity group-hover:opacity-5"
                    />
                    <Icon aria-hidden="true" className="size-3.5 shrink-0" />
                    {compact ? null : <span className="type-nav truncate">{item.title}</span>}
                  </>
                )}
              </NavLink>
            </li>
          );
        })}
      </ul>
    </>
  );
}

function Wordmark() {
  return (
    <span className="type-wordmark text-ink select-none">
      <span className="block">Indus</span> <span className="block">Agent</span>
    </span>
  );
}

export function Sidebar(props: SidebarProps) {
  const { items, search, wide, collapsed, onToggleCollapsed, mobileOpen, onCloseMobile, footer } =
    props;
  const compact = wide && collapsed;
  const footerContent = typeof footer === "function" ? footer(compact) : footer;

  const body = (
    <nav aria-label="Dashboard" className="flex h-full min-h-0 flex-col">
      <div
        className={cx(
          "flex h-[3.25rem] shrink-0 items-center border-b border-line",
          compact ? "justify-center" : "justify-between pr-4 pl-4",
        )}
      >
        {compact ? null : <Wordmark />}
        {wide ? (
          <button
            type="button"
            className="inline-flex size-7 items-center justify-center text-ink-muted hover:text-ink"
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-expanded={!collapsed}
            title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            onClick={onToggleCollapsed}
          >
            {collapsed ? (
              <PanelLeftOpen aria-hidden="true" className="size-4" />
            ) : (
              <PanelLeftClose aria-hidden="true" className="size-4" />
            )}
          </button>
        ) : (
          <button
            type="button"
            className="inline-flex h-7 items-center gap-1 px-1 text-ink-muted hover:text-ink"
            onClick={onCloseMobile}
          >
            <X aria-hidden="true" className="size-4" />
            Close
          </button>
        )}
      </div>
      <ProfileSwitcher compact={compact} />
      <div className="min-h-0 flex-1 overflow-x-hidden overflow-y-auto">
        <NavItems
          items={items}
          search={search}
          compact={compact}
          onNavigate={wide ? () => {} : onCloseMobile}
        />
      </div>
      <div className="flex shrink-0 flex-col gap-2 border-t border-line pt-2.5 pb-2">
        {footerContent}
      </div>
      <div
        className={cx(
          "flex shrink-0 items-center border-t border-line",
          compact ? "justify-center py-1" : "h-10 px-4",
        )}
      >
        <div className={cx("flex items-center", compact ? "flex-col" : "gap-4")}>
          <ThemeMenu compact={compact} />
          <LanguageMenu />
        </div>
      </div>
      {compact ? null : (
        <div className="flex h-[2.125rem] shrink-0 items-center border-t border-line">
          <VersionLine />
        </div>
      )}
    </nav>
  );

  if (wide) {
    return (
      <aside
        data-testid="sidebar"
        data-collapsed={collapsed ? "true" : "false"}
        className={cx(
          "h-full shrink-0 border-r border-line bg-canvas transition-[width] duration-200",
          collapsed ? "w-[3.25rem]" : "w-60",
        )}
      >
        {body}
      </aside>
    );
  }
  if (!mobileOpen) return null;
  return (
    <div className="fixed inset-0 z-40 flex" data-testid="sidebar-drawer">
      <aside data-testid="sidebar" className="h-full w-64 border-r border-line bg-canvas">
        {body}
      </aside>
      <button
        type="button"
        aria-label="Close navigation"
        className="flex-1 bg-overlay"
        onClick={onCloseMobile}
      />
    </div>
  );
}
