// A sortable data table (W52-b): serif header cells with a sort arrow, faint row rules, a hover
// tint. Clicking a header flips its direction; a new column starts descending; empty values sort
// last either way. The first column is left-aligned, the rest right-aligned.

import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
import { type ReactNode, useMemo, useState } from "react";
import { cx } from "./cx.ts";

export interface Column<T> {
  key: string;
  label: string;
  /** The value sorted on; `null` always sorts last. */
  sortValue: (row: T) => number | string | null;
  render?: (row: T) => ReactNode;
  className?: string;
}

export type SortDirection = "asc" | "desc";

export function sortRows<T>(
  rows: readonly T[],
  value: (row: T) => number | string | null,
  direction: SortDirection,
): T[] {
  const sign = direction === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const x = value(a);
    const y = value(b);
    if (x === null && y === null) return 0;
    if (x === null) return 1;
    if (y === null) return -1;
    if (typeof x === "number" && typeof y === "number") return (x - y) * sign;
    return String(x).localeCompare(String(y)) * sign;
  });
}

export function DataTable<T>({
  label,
  columns,
  rows,
  rowKey,
  defaultSort,
}: {
  label: string;
  columns: readonly Column<T>[];
  rows: readonly T[];
  rowKey: (row: T) => string;
  defaultSort: { key: string; direction: SortDirection };
}) {
  const [sort, setSort] = useState(defaultSort);
  const column = columns.find((c) => c.key === sort.key) ?? columns[0];
  const sorted = useMemo(
    () => (column ? sortRows(rows, column.sortValue, sort.direction) : [...rows]),
    [rows, column, sort.direction],
  );
  const choose = (key: string) =>
    setSort((was) =>
      was.key === key
        ? { key, direction: was.direction === "desc" ? "asc" : "desc" }
        : { key, direction: "desc" },
    );
  return (
    <table aria-label={label} className="w-full border-collapse text-[0.8125rem]">
      <thead>
        <tr>
          {columns.map((c, i) => {
            const active = c.key === sort.key;
            const Arrow = !active ? ArrowUpDown : sort.direction === "desc" ? ArrowDown : ArrowUp;
            return (
              <th
                key={c.key}
                scope="col"
                aria-sort={
                  active ? (sort.direction === "desc" ? "descending" : "ascending") : "none"
                }
                className={cx(
                  "h-12 border-b border-line p-0 font-normal",
                  i === 0 ? "text-left" : "text-right",
                )}
              >
                <button
                  type="button"
                  onClick={() => choose(c.key)}
                  className={cx(
                    "type-serif inline-flex items-center gap-1.5 text-[0.75rem] text-ink-muted hover:text-ink",
                    i > 0 && "flex-row",
                  )}
                >
                  {c.label}
                  <Arrow
                    aria-hidden="true"
                    className={cx("size-3", active ? "text-ink/80" : "text-ink-faint")}
                  />
                </button>
              </th>
            );
          })}
        </tr>
      </thead>
      <tbody>
        {sorted.map((row) => (
          <tr key={rowKey(row)} className="transition-colors hover:bg-hover/60">
            {columns.map((c, i) => (
              <td
                key={c.key}
                className={cx(
                  "type-serif h-[2.1875rem] border-b border-line/60 text-[0.8125rem]",
                  i === 0 ? "text-left text-ink" : "text-right",
                  c.className,
                )}
              >
                {c.render ? c.render(row) : String(c.sortValue(row) ?? "—")}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
