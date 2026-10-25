// Logs (P18-21, rebuilt W52-b): tail one log file with level, component and length filters. The
// header carries a filter summary, a refresh icon and the auto-refresh switch (off by default,
// W52-b-4; while on it refreshes every 5 s with the tab visible). The tail opens scrolled to the
// newest line.

import { useQuery } from "@tanstack/react-query";
import { FileText } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import { api } from "../lib/api.ts";
import {
  LOG_COMPONENTS,
  LOG_FILES,
  LOG_LEVELS,
  LOG_TAILS,
  type LogComponent,
  type LogFile,
  type LogLevel,
  type LogTail,
} from "../lib/restTypes.ts";
import { useVisiblePolling } from "../lib/visibility.ts";
import { HeaderActions, HeaderAfterTitle } from "../shell/PageHeader.tsx";
import { Badge, Card, CardHeader, Loading, RefreshButton, Segmented } from "../ui/controls.tsx";
import { Switch } from "../ui/Switch.tsx";

export const LOG_REFRESH_MS = 5_000;

export type LineLevel = "debug" | "info" | "warning" | "error" | "plain";

/** The severity a log line carries, by its level word. */
export function lineLevel(line: string): LineLevel {
  const match = /\b(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\b/.exec(line);
  switch (match?.[1]) {
    case "DEBUG":
      return "debug";
    case "INFO":
      return "info";
    case "WARNING":
    case "WARN":
      return "warning";
    case "ERROR":
    case "CRITICAL":
    case "FATAL":
      return "error";
    default:
      return "plain";
  }
}

const LEVEL_CLASS: Record<LineLevel, string> = {
  debug: "text-ink-soft",
  info: "text-ink",
  warning: "text-warning",
  error: "text-danger",
  plain: "text-ink",
};

function asFile(value: string | null): LogFile {
  return (LOG_FILES as readonly string[]).includes(value ?? "") ? (value as LogFile) : "agent";
}

export function LogsPage() {
  const [params] = useSearchParams();
  const switchId = useId();
  const [file, setFile] = useState<LogFile>(() => asFile(params.get("file")));
  const [level, setLevel] = useState<LogLevel>("ALL");
  const [component, setComponent] = useState<LogComponent>("all");
  const [lines, setLines] = useState<LogTail>(100);
  const [live, setLive] = useState(false);
  const scroller = useRef<HTMLOListElement>(null);

  const logs = useQuery({
    queryKey: ["logs", file, level, component, lines],
    queryFn: () => api.logs({ file, level, component, lines }),
  });
  useVisiblePolling(() => void logs.refetch(), LOG_REFRESH_MS, live);

  const rows = logs.data?.lines ?? [];
  // Open on the newest line, and follow it after every fetch.
  // biome-ignore lint/correctness/useExhaustiveDependencies: runs when a new tail arrives
  useEffect(() => {
    const list = scroller.current;
    if (list) list.scrollTop = list.scrollHeight;
  }, [logs.data]);

  return (
    <div className="flex flex-col gap-4 px-4 py-[1.4375rem] lg:px-[1.4375rem]">
      <HeaderAfterTitle>
        <Badge className="bg-selected tracking-[0.12em] uppercase">
          <span data-testid="log-filter-summary">{`${file} · ${level} · ${component}`}</span>
        </Badge>
        <RefreshButton onClick={() => void logs.refetch()} busy={logs.isFetching} />
      </HeaderAfterTitle>
      <HeaderActions>
        <label
          htmlFor={switchId}
          className="type-serif cursor-pointer text-[0.6875rem] tracking-[0.12em] text-ink uppercase"
        >
          Auto-refresh
        </label>
        <Switch id={switchId} label="Auto-refresh" checked={live} onCheckedChange={setLive} />
        {live ? (
          <Badge
            tone="ok"
            icon={<span className="size-1.5 animate-pulse rounded-full bg-success" />}
          >
            Live
          </Badge>
        ) : null}
      </HeaderActions>
      <div
        role="toolbar"
        aria-label="Log filters"
        className="flex flex-wrap items-center gap-x-6 gap-y-3 [&_button]:h-7 [&_button]:px-2.5 [&_button]:text-[0.6875rem] [&_button]:uppercase [&_legend]:text-[0.6875rem]"
      >
        <Segmented
          showLabel
          label="File"
          value={file}
          options={LOG_FILES.map((f) => ({ value: f, label: f }))}
          onChange={setFile}
        />
        <Segmented
          showLabel
          label="Level"
          value={level}
          options={LOG_LEVELS.map((l) => ({ value: l, label: l }))}
          onChange={setLevel}
        />
        <Segmented
          showLabel
          label="Component"
          value={component}
          options={LOG_COMPONENTS.map((c) => ({ value: c, label: c }))}
          onChange={setComponent}
        />
        <Segmented
          showLabel
          label="Lines"
          value={lines}
          options={LOG_TAILS.map((n) => ({ value: n, label: String(n) }))}
          onChange={setLines}
        />
      </div>
      <Card padded={false} className="min-w-0">
        <CardHeader icon={FileText} title={`${file}.log`} />
        {logs.error ? (
          <p
            role="alert"
            className="m-0 border-b border-danger/20 bg-danger-wash px-3 py-3 text-[0.875rem] text-danger"
          >
            {logs.error instanceof Error ? logs.error.message : "The log could not be read."}
          </p>
        ) : null}
        {logs.isPending ? (
          <div className="p-4">
            <Loading />
          </div>
        ) : rows.length === 0 && !logs.error ? (
          <p className="m-0 py-8 text-center text-[0.875rem] text-ink-soft">No log lines match.</p>
        ) : rows.length > 0 ? (
          <ol
            ref={scroller}
            aria-label="Log lines"
            className="m-0 max-h-[calc(100vh-220px)] min-h-[400px] list-none overflow-auto p-4 font-mono text-[0.75rem] leading-5"
          >
            {rows.map((line, i) => {
              const severity = lineLevel(line);
              return (
                <li
                  // biome-ignore lint/suspicious/noArrayIndexKey: a tail is positional
                  key={i}
                  data-level={severity}
                  className={`-mx-1 px-1 break-words whitespace-pre-wrap hover:bg-hover/60 ${LEVEL_CLASS[severity]}`}
                >
                  {line}
                </li>
              );
            })}
          </ol>
        ) : null}
      </Card>
    </div>
  );
}
