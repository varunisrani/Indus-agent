// The page header: the current page's title in the wide display voice, a slot right after the
// title (<HeaderAfterTitle>: count chips, range buttons, a refresh icon, filter badges, switches)
// and an end slot each page fills with its own actions (<HeaderActions>). Both are portals, so the
// header never re-renders the page. A page may also replace the route title for as long as it is
// mounted (<HeaderTitle>, W52-a: Chat shows the session title).

import { createContext, type ReactNode, useContext, useEffect, useState } from "react";
import { createPortal } from "react-dom";

interface Slots {
  aside: HTMLElement | null;
  end: HTMLElement | null;
}

const SlotsContext = createContext<Slots>({ aside: null, end: null });
const TitleContext = createContext<(title: string | null) => void>(() => {});

export function PageHeader({
  title,
  start,
  children,
}: {
  title: string;
  /** Leading control (the menu button on narrow screens). */
  start?: ReactNode;
  children: ReactNode;
}) {
  const [aside, setAside] = useState<HTMLElement | null>(null);
  const [end, setEnd] = useState<HTMLElement | null>(null);
  const [override, setOverride] = useState<string | null>(null);
  return (
    <TitleContext.Provider value={setOverride}>
      <SlotsContext.Provider value={{ aside, end }}>
        <header className="flex h-[3.25rem] shrink-0 items-center gap-3 border-b border-line bg-canvas px-4 lg:px-[1.375rem]">
          {start}
          <div className="flex min-w-0 flex-1 items-center gap-3">
            <h1 className="type-page-title m-0 truncate text-ink">{override || title}</h1>
            <div
              ref={setAside}
              data-testid="page-header-after-title"
              className="flex min-w-0 items-center gap-2"
            />
          </div>
          <div ref={setEnd} data-testid="page-header-end" className="flex items-center gap-2" />
        </header>
        {children}
      </SlotsContext.Provider>
    </TitleContext.Provider>
  );
}

/** Render page actions into the header's end slot. */
export function HeaderActions({ children }: { children: ReactNode }) {
  const { end } = useContext(SlotsContext);
  return end ? createPortal(children, end) : null;
}

/** Render a small companion right after the page title (count chip, filters, summary). */
export function HeaderAfterTitle({ children }: { children: ReactNode }) {
  const { aside } = useContext(SlotsContext);
  return aside ? createPortal(children, aside) : null;
}

/**
 * Replace the header title while mounted and `title` is set (null or "" keeps the route title).
 * Renders nothing.
 */
export function HeaderTitle({ title }: { title: string | null }) {
  const set = useContext(TitleContext);
  useEffect(() => {
    set(title || null);
    return () => set(null);
  }, [set, title]);
  return null;
}
