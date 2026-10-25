// The language button beside the theme picker. The dashboard has one language today, so the menu
// lists English alone (checked); it is here so the shell keeps its shape when translations land.

import { Check } from "lucide-react";
import { useCallback, useId, useRef, useState } from "react";
import { useDismiss } from "../ui/useDismiss.ts";

const LANGUAGES = [{ code: "en", short: "EN", name: "English" }] as const;

export function LanguageMenu() {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const menuId = useId();
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, root, close);
  const current = LANGUAGES[0];

  return (
    <div ref={root} className="relative">
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-label={`Language: ${current.name}`}
        title={`Language: ${current.name}`}
        onClick={() => setOpen((was) => !was)}
        className="inline-flex h-8 min-w-6 items-center justify-center text-[0.75rem] tracking-[0.06em] text-ink-muted hover:text-ink"
      >
        {current.short}
      </button>
      {open ? (
        <div
          id={menuId}
          role="menu"
          aria-label="Language"
          className="fixed bottom-[4.5rem] left-3 z-50 w-56 border border-line-strong bg-canvas text-ink shadow-2xl [animation:indus-fade-in_120ms_ease-out]"
        >
          <p className="m-0 border-b border-line px-3 py-2.5 text-[0.75rem] tracking-[0.08em] text-ink-muted">
            Language
          </p>
          <ul className="m-0 list-none p-0 py-1">
            {LANGUAGES.map((language) => (
              <li key={language.code}>
                <button
                  type="button"
                  role="menuitemradio"
                  aria-checked={language.code === current.code}
                  onClick={close}
                  className="flex w-full items-center gap-3 bg-selected px-3 py-2 text-left text-[0.8125rem] hover:bg-hover"
                >
                  <span className="type-badge w-6 text-ink-soft">{language.short}</span>
                  <span className="flex-1">{language.name}</span>
                  <Check aria-hidden="true" className="size-3.5" />
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
