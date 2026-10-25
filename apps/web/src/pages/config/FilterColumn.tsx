// The Config page's section list: a bordered side panel with FILTERS / SECTIONS captions and one
// row per category (icon, name, field count). On phones it becomes a scrolling strip.

import { Funnel } from "lucide-react";
import { cx } from "../../ui/cx.ts";
import { categoryIcon, categoryName } from "./categories.ts";

export function FilterColumn({
  categories,
  counts,
  active,
  onPick,
}: {
  categories: readonly string[];
  counts: Readonly<Record<string, number>>;
  /** The selected category, or null while a search is showing. */
  active: string | null;
  onPick: (category: string) => void;
}) {
  return (
    <nav
      aria-label="Config sections"
      className="flex shrink-0 flex-col border border-line bg-surface lg:sticky lg:top-4 lg:w-[210px] lg:self-start"
    >
      <div className="flex h-[31px] items-center gap-2 border-b border-line px-3">
        <Funnel aria-hidden="true" className="size-3 text-ink-soft" />
        <span className="type-label text-[0.6875rem] tracking-[0.12em] text-ink-muted">
          Filters
        </span>
      </div>
      <p className="type-label m-0 px-3 pt-2 text-[0.6875rem] text-ink-soft">Sections</p>
      <ul className="m-0 flex list-none gap-px overflow-x-auto p-2 lg:max-h-[calc(100vh-260px)] lg:flex-col lg:overflow-x-visible lg:overflow-y-auto">
        {categories.map((category) => {
          const Icon = categoryIcon(category);
          const on = category === active;
          return (
            <li key={category} className="shrink-0">
              <button
                type="button"
                aria-pressed={on}
                onClick={() => onPick(category)}
                className={cx(
                  "flex h-[23px] w-full items-center gap-2 px-2 text-left transition-colors",
                  on ? "bg-selected text-ink" : "text-ink-muted hover:bg-hover",
                )}
              >
                <Icon aria-hidden="true" className="size-3.5 shrink-0" />
                <span className="type-data min-w-0 flex-1 truncate text-[0.75rem]">
                  {categoryName(category)}
                </span>
                <span
                  className={cx(
                    "pl-2 text-[0.75rem] tabular-nums",
                    on ? "text-ink-muted" : "text-ink-soft",
                  )}
                >
                  {counts[category] ?? 0}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
