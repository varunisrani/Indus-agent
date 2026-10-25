// Live tail of one background action (doctor, backup, daemon restart, update, profile export…).
// The server keeps the run's log; this polls `GET /api/actions/{name}/status` while the run is
// going and shows its last lines (already masked by the server), then reports how it ended.

import { useMutation, useQuery } from "@tanstack/react-query";
import { SquareTerminal, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { ApiError, api } from "../lib/api.ts";
import type { ActionStarted, ActionStatus } from "../lib/restTypes.ts";
import { useVisiblePolling } from "../lib/visibility.ts";
import { errorText, useToast } from "../shell/Toasts.tsx";
import { Badge, type BadgeTone, IconButton, Notice } from "./controls.tsx";

export const ACTION_POLL_MS = 1_000;
export const ACTION_TAIL_LINES = 400;

/** The badge for a run: running (amber), done (green), or how it ended (red). */
function stateBadge(status: ActionStatus | undefined): [string, BadgeTone] {
  if (!status) return ["starting", "warn"];
  if (status.running || status.state === "running") return ["running", "warn"];
  if (status.state === "done") return ["done", "ok"];
  if (status.state === "failed") {
    return [status.exit_code !== null ? `exit ${status.exit_code}` : "failed", "danger"];
  }
  return [status.state === "idle" ? "not run yet" : "earlier run", "neutral"];
}

export interface ActionLogProps {
  /** The action's name as the server knows it (`doctor`, `daemon-restart`, …). */
  name: string;
  /** Changes for every new start, so a second run of the same action is tailed afresh. */
  run: number;
  /** The profile the run belongs to, when it is not the selected one (a profile export). */
  profile?: string;
  title?: string;
  onFinished?: (status: ActionStatus) => void;
  onClose?: () => void;
}

export function ActionLog({ name, run, profile, title, onFinished, onClose }: ActionLogProps) {
  const status = useQuery({
    queryKey: ["action", name, profile ?? "", run],
    queryFn: () => api.actions.status(name, ACTION_TAIL_LINES, profile),
    staleTime: 0,
  });
  const running = status.data ? status.data.running : !status.error;
  useVisiblePolling(() => void status.refetch(), ACTION_POLL_MS, running);

  const reported = useRef(-1);
  const finished = useRef(onFinished);
  finished.current = onFinished;
  useEffect(() => {
    const data = status.data;
    if (!data || data.running || reported.current === run) return;
    if (data.state !== "done" && data.state !== "failed") return;
    reported.current = run;
    finished.current?.(data);
  }, [status.data, run]);

  const [label, tone] = stateBadge(status.data);
  const lines = status.data?.lines ?? [];
  const name_ = title ?? name;
  return (
    <section
      aria-label={`${name_} output`}
      className="flex flex-col gap-3 border border-line bg-canvas p-4"
    >
      <div className="flex items-center gap-2">
        <SquareTerminal aria-hidden="true" className="size-4 shrink-0 text-ink-muted" />
        <span className="type-data min-w-0 truncate text-[0.875rem] text-ink">{name_}</span>
        <Badge tone={tone}>{label}</Badge>
        <span className="flex-1" />
        {onClose ? (
          <IconButton label="Close log" onClick={onClose}>
            <X />
          </IconButton>
        ) : null}
      </div>
      {status.error ? <Notice tone="danger">{(status.error as Error).message}</Notice> : null}
      <pre
        data-testid={`action-log-${name}`}
        className="m-0 max-h-72 overflow-auto border border-line bg-wash/50 p-3 font-mono text-[0.75rem] leading-relaxed whitespace-pre-wrap break-words text-ink-muted"
      >
        {lines.length > 0 ? lines.join("\n") : status.data ? "" : "Starting…"}
      </pre>
      {status.data && !status.data.running && status.data.exit_code !== null ? (
        <p className="sr-only">Exit code {status.data.exit_code}.</p>
      ) : null}
    </section>
  );
}

/**
 * Start a background action and count its runs for `<ActionLog run={…}>`. A start refused because
 * the action is already running still bumps the count, so the running one is tailed.
 */
export function useActionStarter<A = void>(start: (arg: A) => Promise<ActionStarted>) {
  const toast = useToast();
  const [run, setRun] = useState(0);
  const [started, setStarted] = useState<ActionStarted | null>(null);
  const mutation = useMutation({
    mutationFn: start,
    onSuccess: (reply) => {
      setStarted(reply);
      setRun((n) => n + 1);
    },
    onError: (error) => {
      toast.show(errorText(error), "error");
      if (error instanceof ApiError && error.code === "action_running") setRun((n) => n + 1);
    },
  });
  return { run, started, start: (arg: A) => mutation.mutate(arg), pending: mutation.isPending };
}
