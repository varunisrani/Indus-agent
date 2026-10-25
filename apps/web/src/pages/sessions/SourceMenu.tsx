// The source filter: an outlined button ("Any chat source", "Telegram", "3 sources") that opens a
// small menu of the tab's sources, each with a checkbox, its icon and its session count. Several
// may be picked; Clear goes back to the tab's own filter.

import { ChevronDown, ListFilter } from "lucide-react";
import { useCallback, useId, useRef, useState } from "react";
import { cx } from "../../ui/cx.ts";
import { useDismiss } from "../../ui/useDismiss.ts";
import { SourceIcon } from "./SourceIcon.tsx";
import { type SessionTab, sourceButtonLabel, sourceLabel, sourceMenuTitle } from "./sources.ts";

export function SourceMenu({
  tab,
  sources,
  counts,
  picked,
  onChange,
}: {
  tab: SessionTab;
  /** The sources this tab can show. */
  sources: readonly string[];
  counts: Readonly<Record<string, number>>;
  picked: readonly string[];
  onChange: (next: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const menuId = useId();
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, root, close);
  const shown = [...sources, ...picked.filter((s) => !sources.includes(s))];

  const toggle = (source: string) => {
    onChange(picked.includes(source) ? picked.filter((s) => s !== source) : [...picked, source]);
  };

  return (
    <div ref={root} className="relative">
      <button
        type="button"
        aria-label="Source"
        aria-haspopup="true"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={() => setOpen(!open)}
        className="type-button inline-flex h-[1.875rem] w-[13.25rem] items-center gap-2 border border-ink bg-canvas px-3 text-ink hover:bg-hover"
      >
        <ListFilter aria-hidden="true" className="size-3.5 shrink-0" />
        <span className="min-w-0 flex-1 truncate text-left">{sourceButtonLabel(tab, picked)}</span>
        <ChevronDown
          aria-hidden="true"
          className={cx("size-3.5 shrink-0 transition-transform", open && "rotate-180")}
        />
      </button>
      {open ? (
        <fieldset
          id={menuId}
          aria-label={sourceMenuTitle(tab)}
          className="absolute top-full left-0 z-30 m-0 mt-1 w-72 min-w-0 p-0 border border-line-strong bg-canvas shadow-xl [animation:indus-fade-in_120ms_ease-out]"
        >
          <div className="flex h-9 items-center justify-between border-b border-line px-3">
            <span className="text-[0.75rem] text-ink-muted">{sourceMenuTitle(tab)}</span>
            {picked.length > 0 ? (
              <button
                type="button"
                className="type-button h-6 px-2 text-[0.6875rem] text-ink hover:bg-hover"
                onClick={() => onChange([])}
              >
                Clear
              </button>
            ) : null}
          </div>
          {shown.length === 0 ? (
            <p className="m-0 px-3 py-3 text-[0.75rem] text-ink-soft">No sources yet.</p>
          ) : (
            <ul className="m-0 max-h-72 list-none overflow-y-auto p-1">
              {shown.map((source) => (
                <li key={source}>
                  <label className="flex h-8 cursor-pointer items-center gap-2.5 px-2 text-[0.8125rem] text-ink hover:bg-hover">
                    <input
                      type="checkbox"
                      checked={picked.includes(source)}
                      onChange={() => toggle(source)}
                      className="size-3.5 shrink-0 cursor-pointer appearance-none rounded-[2px] border border-line-strong checked:border-ink checked:bg-ink"
                    />
                    <SourceIcon source={source} className="size-3.5 shrink-0" />
                    <span className="min-w-0 flex-1 truncate">{sourceLabel(source)}</span>
                    <span className="text-[0.75rem] text-ink-soft tabular-nums">
                      {counts[source] ?? 0}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          )}
        </fieldset>
      ) : null}
    </div>
  );
}
