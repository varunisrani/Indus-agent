// Logs (P18-21): tail one log file with level, component and length filters; refreshes every 5 s
// while the tab is visible.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
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
import { HeaderActions } from "../shell/PageHeader.tsx";
import { Button, Card, QueryState, Segmented } from "../ui/controls.tsx";

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
  debug: "text-muted",
  info: "text-fg",
  warning: "text-warn",
  error: "text-danger",
  plain: "text-fg",
};

function asFile(value: string | null): LogFile {
  return (LOG_FILES as readonly string[]).includes(value ?? "") ? (value as LogFile) : "agent";
}

export function LogsPage() {
  const [params] = useSearchParams();
  const [file, setFile] = useState<LogFile>(() => asFile(params.get("file")));
  const [level, setLevel] = useState<LogLevel>("ALL");
  const [component, setComponent] = useState<LogComponent>("all");
  const [lines, setLines] = useState<LogTail>(100);

  const logs = useQuery({
    queryKey: ["logs", file, level, component, lines],
    queryFn: () => api.logs({ file, level, component, lines }),
  });
  useVisiblePolling(() => void logs.refetch(), LOG_REFRESH_MS);

  const rows = logs.data?.lines ?? [];
  return (
    <div className="flex flex-col gap-4 p-4">
      <HeaderActions>
        <Button onClick={() => void logs.refetch()}>Refresh</Button>
      </HeaderActions>
      <div className="flex flex-col gap-2">
        <Segmented
          label="File"
          value={file}
          options={LOG_FILES.map((f) => ({ value: f, label: f }))}
          onChange={setFile}
        />
        <Segmented
          label="Level"
          value={level}
          options={LOG_LEVELS.map((l) => ({ value: l, label: l === "ALL" ? "everything" : l }))}
          onChange={setLevel}
        />
        <Segmented
          label="Component"
          value={component}
          options={LOG_COMPONENTS.map((c) => ({ value: c, label: c === "all" ? "everything" : c }))}
          onChange={setComponent}
        />
        <Segmented
          label="Lines"
          value={lines}
          options={LOG_TAILS.map((n) => ({ value: n, label: String(n) }))}
          onChange={setLines}
        />
      </div>
      <Card className="min-w-0">
        <h2 className="mb-2 font-mono text-xs text-muted">{file}.log</h2>
        <QueryState
          loading={logs.isPending}
          error={logs.error}
          empty={rows.length === 0}
          emptyText="No log lines match."
        >
          <ol className="max-h-[65vh] overflow-auto font-mono text-xs" aria-label="Log lines">
            {rows.map((line, i) => {
              const severity = lineLevel(line);
              return (
                <li
                  // biome-ignore lint/suspicious/noArrayIndexKey: a tail is positional
                  key={i}
                  data-level={severity}
                  className={`whitespace-pre-wrap break-all ${LEVEL_CLASS[severity]}`}
                >
                  {line}
                </li>
              );
            })}
          </ol>
        </QueryState>
      </Card>
    </div>
  );
}
