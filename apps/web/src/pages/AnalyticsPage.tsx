// Analytics (P18-21): tokens and cost per day over 7, 30 or 90 days. Bars are plain SVG coloured
// from the @indus/ui tokens, with a data table beside them for screen readers.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../lib/api.ts";
import { formatCost, formatCount } from "../lib/format.ts";
import type { UsageDay } from "../lib/restTypes.ts";
import { Card, QueryState, Segmented } from "../ui/controls.tsx";

export const RANGES = [7, 30, 90] as const;
type Range = (typeof RANGES)[number];

interface Series {
  key: string;
  label: string;
  colour: string;
  value: (day: UsageDay) => number;
}

function BarChart({ title, days, series }: { title: string; days: UsageDay[]; series: Series[] }) {
  const width = 640;
  const height = 160;
  const totals = days.map((d) => series.reduce((sum, s) => sum + s.value(d), 0));
  const max = Math.max(1, ...totals);
  const slot = width / Math.max(1, days.length);
  const bar = Math.max(1, slot * 0.7);
  return (
    <figure className="flex flex-col gap-2">
      <figcaption className="text-sm font-semibold">{title}</figcaption>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={title} className="h-40 w-full">
        <line
          x1={0}
          x2={width}
          y1={height - 0.5}
          y2={height - 0.5}
          stroke="var(--indus-color-border)"
        />
        {days.map((day, i) => {
          let y = height;
          return (
            <g key={day.day}>
              <title>{`${day.day}: ${series.map((s) => `${s.label} ${formatCount(s.value(day))}`).join(", ")}`}</title>
              {series.map((s) => {
                const h = (s.value(day) / max) * (height - 8);
                y -= h;
                return (
                  <rect
                    key={s.key}
                    x={i * slot + (slot - bar) / 2}
                    y={y}
                    width={bar}
                    height={h}
                    fill={s.colour}
                  />
                );
              })}
            </g>
          );
        })}
      </svg>
      <div className="flex gap-3 text-xs text-muted">
        {series.map((s) => (
          <span key={s.key} className="inline-flex items-center gap-1">
            <span
              aria-hidden="true"
              className="inline-block h-2 w-2 rounded-sm"
              style={{ background: s.colour }}
            />
            {s.label}
          </span>
        ))}
      </div>
    </figure>
  );
}

const TOKEN_SERIES: Series[] = [
  {
    key: "in",
    label: "input tokens",
    colour: "var(--indus-color-accent)",
    value: (d) => d.input_tokens,
  },
  {
    key: "out",
    label: "output tokens",
    colour: "var(--indus-color-success)",
    value: (d) => d.output_tokens,
  },
];
const COST_SERIES: Series[] = [
  { key: "cost", label: "cost (USD)", colour: "var(--indus-color-warning)", value: (d) => d.cost },
];

export function AnalyticsPage() {
  const [days, setDays] = useState<Range>(30);
  const usage = useQuery({
    queryKey: ["analytics", days],
    queryFn: () => api.analytics.usage(days),
  });
  const daily = usage.data?.daily ?? [];
  const totals = usage.data?.totals;
  return (
    <div className="flex flex-col gap-4 p-4">
      <Segmented
        label="Range"
        value={days}
        options={RANGES.map((r) => ({ value: r, label: `${r} days` }))}
        onChange={setDays}
      />
      <QueryState
        loading={usage.isPending}
        error={usage.error}
        empty={daily.length === 0}
        emptyText="No usage recorded in this range yet."
      >
        {totals ? (
          <dl className="flex flex-wrap gap-6 text-sm">
            <div>
              <dt className="text-xs text-muted">Input tokens</dt>
              <dd>{formatCount(totals.input_tokens)}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted">Output tokens</dt>
              <dd>{formatCount(totals.output_tokens)}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted">Cost</dt>
              <dd>{formatCost(totals.cost)}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted">Sessions</dt>
              <dd>{formatCount(totals.sessions)}</dd>
            </div>
          </dl>
        ) : null}
        <Card>
          <BarChart title="Tokens per day" days={daily} series={TOKEN_SERIES} />
        </Card>
        <Card>
          <BarChart title="Cost per day" days={daily} series={COST_SERIES} />
        </Card>
      </QueryState>
    </div>
  );
}
