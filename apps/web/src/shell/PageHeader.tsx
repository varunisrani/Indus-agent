// The page header: the current page's title and an "end" slot each page fills with its own
// actions through <HeaderActions> (a portal, so the header never re-renders the page).

import { createContext, type ReactNode, useContext, useState } from "react";
import { createPortal } from "react-dom";

const EndSlotContext = createContext<HTMLElement | null>(null);

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
  const [endSlot, setEndSlot] = useState<HTMLElement | null>(null);
  return (
    <EndSlotContext.Provider value={endSlot}>
      <header className="flex min-h-14 items-center gap-3 border-b border-line bg-raised px-4">
        {start}
        <h1 className="flex-1 truncate text-base font-semibold">{title}</h1>
        <div ref={setEndSlot} data-testid="page-header-end" className="flex items-center gap-2" />
      </header>
      {children}
    </EndSlotContext.Provider>
  );
}

/** Render page actions into the header's end slot. */
export function HeaderActions({ children }: { children: ReactNode }) {
  const slot = useContext(EndSlotContext);
  return slot ? createPortal(children, slot) : null;
}
