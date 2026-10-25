// Analytics (P18-21, rebuilt W52-b): token use over 7, 30 or 90 days. Period buttons and a refresh
// icon sit after the page title; the body is a dotted-leader figure list beside a stacked daily bar
// chart, then a sortable daily table. Token figures are a local estimate, so the page explains
// itself instead when `dashboard.show_token_analytics` is off (W52-b-3).

import { useQuery } from "@tanstack/react-query";
import { BarChart3, TrendingUp } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { api } from "../lib/api.ts";
import { formatCost, formatCount, formatShortDay, formatTokens } from "../lib/format.ts";
import type { UsageDay } from "../lib/restTypes.ts";
import { useTokenAnalyticsFlag } from "../lib/useTokenAnalyticsFlag.ts";
import { HeaderAfterTitle } from "../shell/PageHeader.tsx";
import {
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  LeaderStatList,
  PageSpinner,
  PeriodButtons,
  RefreshButton,
} from "../ui/controls.tsx";
import { type Column, DataTable } from "../ui/DataTable.tsx";

export const RANGES = [7, 30, 90] as const;
type Range = (typeof RANGES)[number];

export const RANGE_OPTIONS = RANGES.map((r) => ({ value: r, label: `${r}d` }));

/** Nothing recorded: a faint chart icon, one line and a hint, centred in a card. */
function NoUsage() {
  return (
    <Card padded={false}>
      <EmptyState
        icon={BarChart3}
        action={
          <p className="m-0 text-[0.75rem] text-ink-faint">
            Figures appear here after a session has run.
          </p>
        }
      >
        No usage recorded in this range yet.
      </EmptyState>
    </Card>
  );
}

/** The flag is off: say what the figures are and where to turn them on. */
export function TokenAnalyticsOff() {
  return (
    <Card className="max-w-2xl py-12">
      <h2 className="type-section-title m-0 mb-3 text-[0.9375rem]">Token analytics are off</h2>
      <p className="m-0 text-[0.875rem] leading-relaxed text-ink-muted">
        The token and cost figures here are counted locally from session records, so they are an
        estimate rather than a bill. They stay hidden unless you ask for them: set{" "}
        <code className="type-data text-ink">dashboard.show_token_analytics</code> to true on the{" "}
        <Link to="/config" className="text-ink underline">
          Config
        </Link>{" "}
        page.
      </p>
    </Card>
  );
}

function Legend() {
  return (
    <div className="type-serif flex items-center gap-3 text-[0.6875rem] text-ink-soft">
      <span className="inline-flex items-center gap-1.5">
        <span aria-hidden="true" className="size-2.5 bg-series-input" />
        Input
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span aria-hidden="true" className="size-2.5 bg-series-output" />
        Output
      </span>
    </div>
  );
}

const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;
const DAY_MS = 86_400_000;

function shiftDay(day: string, by: number): string {
  const [y, m, d] = day.split("-").map(Number) as [number, number, number];
  return new Date(Date.UTC(y, m - 1, d) + by * DAY_MS).toISOString().slice(0, 10);
}

/** The viewer's calendar day ("2026-10-25"), the zone the server counts in for a local dashboard. */
export function localDay(now: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/**
 * Every calendar day of the period, oldest first, with zeros where nothing ran. The server lists
 * only the days that had a session, so drawing its rows alone gave one day the whole chart width:
 * a single solid block with no bars to read (W54-W1-3). The period ends today, or on the last
 * listed day when the server's clock is already past ours, and stretches back over any listed day
 * that falls before it, so no listed day is ever dropped.
 */
export function chartDays(
  daily: readonly UsageDay[],
  days: number,
  today: string = localDay(),
): UsageDay[] {
  const byDay = new Map(daily.map((d) => [d.day, d]));
  const listed = daily
    .map((d) => d.day)
    .filter((d) => ISO_DAY.test(d))
    .sort();
  const latest = listed.at(-1);
  const end = latest !== undefined && latest > today ? latest : today;
  const earliest = listed[0];
  const from = shiftDay(end, -(Math.max(1, days) - 1));
  const start = earliest !== undefined && earliest < from ? earliest : from;
  const out: UsageDay[] = [];
  for (let day = start; day <= end; day = shiftDay(day, 1)) {
    out.push(byDay.get(day) ?? { day, input_tokens: 0, output_tokens: 0, cost: 0, sessions: 0 });
  }
  return out;
}

function barHeight(value: number, max: number): string {
  // A day with any tokens keeps at least a hairline, so a small day never vanishes.
  return value > 0 ? `max(1px, ${(value / max) * 100}%)` : "0";
}

/** One column per calendar day: input stacked over output, heights against the busiest day. */
function DailyChart({ daily, days }: { daily: readonly UsageDay[]; days: number }) {
  const slots = chartDays(daily, days);
  const max = Math.max(1, ...slots.map((d) => d.input_tokens + d.output_tokens));
  const first = slots[0];
  const last = slots.at(-1);
  const middle = slots.length > 2 ? slots[Math.floor(slots.length / 2)] : undefined;
  return (
    <div className="flex flex-col gap-2 p-4">
      <div
        role="img"
        aria-label="Daily token usage"
        className="flex h-40 items-end gap-0.5 border-b border-line"
      >
        {slots.map((day) => {
          const total = day.input_tokens + day.output_tokens;
          const summary = `${formatShortDay(day.day)}: input ${formatTokens(day.input_tokens)}, output ${formatTokens(day.output_tokens)}, total ${formatTokens(total)}, cost ${formatCost(day.cost)}`;
          return (
            <div
              key={day.day}
              data-day={day.day}
              title={summary}
              className="group relative flex h-full min-w-0 flex-1 flex-col justify-end"
            >
              <div
                data-series="input"
                className="w-full bg-series-input/80 group-hover:bg-series-input"
                style={{ height: barHeight(day.input_tokens, max) }}
              />
              <div
                data-series="output"
                className="w-full bg-series-output/80 group-hover:bg-series-output"
                style={{ height: barHeight(day.output_tokens, max) }}
              />
              {total > 0 ? (
                <div
                  aria-hidden="true"
                  className="pointer-events-none absolute bottom-full left-1/2 z-10 mb-2 hidden -translate-x-1/2 border border-line-strong bg-canvas px-2.5 py-1.5 text-[0.75rem] whitespace-nowrap text-ink shadow-lg group-hover:block"
                >
                  <p className="m-0 font-medium">{formatShortDay(day.day)}</p>
                  <p className="m-0 text-ink-muted">Input {formatTokens(day.input_tokens)}</p>
                  <p className="m-0 text-ink-muted">Output {formatTokens(day.output_tokens)}</p>
                  <p className="m-0 text-ink-muted">Total {formatTokens(total)}</p>
                  <p className="m-0 text-ink-muted">Cost {formatCost(day.cost)}</p>
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
      <div className="type-serif flex justify-between text-[0.6875rem] text-ink-faint">
        <span>{first ? formatShortDay(first.day) : ""}</span>
        {middle ? <span>{formatShortDay(middle.day)}</span> : null}
        {last && last !== first ? <span>{formatShortDay(last.day)}</span> : null}
      </div>
    </div>
  );
}

const DAILY_COLUMNS: readonly Column<UsageDay>[] = [
  { key: "day", label: "Date", sortValue: (d) => d.day, render: (d) => formatShortDay(d.day) },
  {
    key: "sessions",
    label: "Sessions",
    sortValue: (d) => d.sessions,
    render: (d) => formatCount(d.sessions),
    className: "text-ink-muted",
  },
  {
    key: "input",
    label: "Input",
    sortValue: (d) => d.input_tokens,
    render: (d) => formatTokens(d.input_tokens),
    className: "text-series-input",
  },
  {
    key: "output",
    label: "Output",
    sortValue: (d) => d.output_tokens,
    render: (d) => formatTokens(d.output_tokens),
    className: "text-series-output",
  },
];

function Usage({ days }: { days: Range }) {
  const usage = useQuery({
    queryKey: ["analytics", days],
    queryFn: () => api.analytics.usage(days),
  });
  const daily = usage.data?.daily ?? [];
  const totals = usage.data?.totals;
  const perDay = totals ? totals.sessions / days : 0;
  return (
    <>
      <HeaderAfterTitle>
        <RefreshButton onClick={() => void usage.refetch()} busy={usage.isFetching} />
      </HeaderAfterTitle>
      {usage.isPending ? (
        <PageSpinner />
      ) : usage.error ? (
        <ErrorState
          message={usage.error instanceof Error ? usage.error.message : "Usage could not be read."}
          onRetry={() => void usage.refetch()}
        />
      ) : daily.length === 0 || !totals ? (
        <NoUsage />
      ) : (
        <>
          <div className="grid grid-cols-1 gap-[1.4375rem] lg:grid-cols-2">
            <Card className="px-4 py-6">
              <LeaderStatList
                label="Usage totals"
                items={[
                  {
                    label: "Total tokens",
                    value: formatTokens(totals.input_tokens + totals.output_tokens),
                  },
                  { label: "Input", value: formatTokens(totals.input_tokens) },
                  { label: "Output", value: formatTokens(totals.output_tokens) },
                  {
                    label: "Total sessions",
                    value:
                      `${formatCount(totals.sessions)} (~${perDay.toFixed(1)}/day avg)`.toUpperCase(),
                  },
                  { label: "Est. cost", value: formatCost(totals.cost) },
                ]}
              />
            </Card>
            <Card padded={false}>
              <CardHeader icon={BarChart3} title="Daily token usage" below={<Legend />} />
              <DailyChart daily={daily} days={days} />
            </Card>
          </div>
          <Card padded={false}>
            <CardHeader icon={TrendingUp} title="Daily breakdown" />
            <div className="px-4 pb-4">
              <DataTable
                label="Daily breakdown"
                columns={DAILY_COLUMNS}
                rows={daily}
                rowKey={(d) => d.day}
                defaultSort={{ key: "day", direction: "desc" }}
              />
            </div>
          </Card>
        </>
      )}
    </>
  );
}

export function AnalyticsPage() {
  const [days, setDays] = useState<Range>(30);
  const flag = useTokenAnalyticsFlag();
  return (
    <div className="flex flex-col gap-[1.4375rem] px-4 py-[1.4375rem] lg:px-[1.4375rem]">
      {flag.enabled ? (
        <>
          <HeaderAfterTitle>
            <PeriodButtons value={days} options={RANGE_OPTIONS} onChange={setDays} />
          </HeaderAfterTitle>
          <Usage days={days} />
        </>
      ) : flag.settled ? (
        <TokenAnalyticsOff />
      ) : (
        <PageSpinner />
      )}
    </div>
  );
}
