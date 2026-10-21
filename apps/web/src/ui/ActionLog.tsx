// Live tail of one background action (doctor, backup, daemon restart, update, profile export…).
// The server keeps the run's log; this polls `GET /api/actions/{name}/status` while the run is
// going and shows its last lines (already masked by the server), then reports how it ended.

import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ApiError, api } from "../lib/api.ts";
import type { ActionStarted, ActionStatus } from "../lib/restTypes.ts";
import { useVisiblePolling } from "../lib/visibility.ts";
import { errorText, useToast } from "../shell/Toasts.tsx";
import { Badge, type BadgeTone, Button, Notice } from "./controls.tsx";

export const ACTION_POLL_MS = 1_000;
export const ACTION_TAIL_LINES = 400;

const STATE_LABEL: Record<ActionStatus["state"], [string, BadgeTone]> = {
  idle: ["not run yet", "neutral"],
  unknown: ["earlier run", "neutral"],
  running: ["running", "accent"],
  done: ["done", "ok"],
  failed: ["failed", "danger"],
};

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

  const [label, tone] = status.data ? STATE_LABEL[status.data.state] : ["starting", "accent"];
  const lines = status.data?.lines ?? [];
  return (
    <section
      aria-label={`${title ?? name} output`}
      className="flex flex-col gap-2 rounded-md border border-line bg-bg p-3"
    >
      <div className="flex items-center gap-2">
        <span className="flex-1 font-mono text-sm">{title ?? name}</span>
        <Badge tone={tone as BadgeTone}>{label}</Badge>
        {onClose ? (
          <Button tone="ghost" onClick={onClose} aria-label={`Close ${title ?? name} output`}>
            Close
          </Button>
        ) : null}
      </div>
      {status.error ? <Notice tone="danger">{(status.error as Error).message}</Notice> : null}
      <pre
        data-testid={`action-log-${name}`}
        className="max-h-72 overflow-auto rounded bg-code p-2 font-mono text-xs whitespace-pre-wrap"
      >
        {lines.join("\n")}
      </pre>
      {status.data && !status.data.running && status.data.exit_code !== null ? (
        <p className="text-xs text-muted">Exit code {status.data.exit_code}.</p>
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
