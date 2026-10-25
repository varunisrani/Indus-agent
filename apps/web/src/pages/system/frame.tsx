// System page frame (W52-f): the headed section every block sits in, and the one action log the
// page shows on top. Sections start their background actions through `useLoggedAction`; the page
// keeps only the latest run (starting another replaces it) and draws it above Host.

import type { LucideIcon } from "lucide-react";
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useId,
  useRef,
} from "react";
import type { ActionStarted, ActionStatus } from "../../lib/restTypes.ts";
import { useActionStarter } from "../../ui/ActionLog.tsx";

export interface PageLog {
  /** The action's server name (`doctor`, `daemon-stop`, …). */
  name: string;
  title: string;
  run: number;
  onFinished?: (status: ActionStatus) => void;
}

export const SystemLogContext = createContext<(log: PageLog) => void>(() => {});

interface Described {
  name: string;
  title: string;
}

/**
 * `useActionStarter` whose runs are shown in the page's action log. `describe` names the run for
 * an argument; `onFinished` hears how it ended (with what the start answered, e.g. the archive).
 */
export function useLoggedAction<A = void>(
  start: (arg: A) => Promise<ActionStarted>,
  describe: (arg: A) => Described,
  onFinished?: (status: ActionStatus, started: ActionStarted | null) => void,
) {
  const show = useContext(SystemLogContext);
  const action = useActionStarter(start);
  const described = useRef<Described | null>(null);
  const finished = useRef(onFinished);
  finished.current = onFinished;
  const started = useRef(action.started);
  started.current = action.started;
  const report = useCallback((status: ActionStatus) => {
    finished.current?.(status, started.current);
  }, []);
  useEffect(() => {
    if (action.run === 0 || !described.current) return;
    show({ ...described.current, run: action.run, onFinished: report });
  }, [action.run, show, report]);
  return {
    ...action,
    start: (arg: A) => {
      described.current = describe(arg);
      action.start(arg);
    },
  };
}

/** One System block: an icon and a spaced heading over its cards (12 px apart). */
export function SystemSection({
  icon: Icon,
  title,
  aside,
  children,
}: {
  icon: LucideIcon;
  title: string;
  aside?: ReactNode;
  children: ReactNode;
}) {
  const id = useId();
  return (
    <section aria-labelledby={id} className="flex flex-col gap-3">
      <div className="flex min-h-5 items-center gap-2">
        <Icon aria-hidden="true" className="size-4 shrink-0 text-ink" />
        <h2 id={id} className="type-heading m-0 flex-1 text-[0.9375rem] text-ink">
          {title}
        </h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

/** Field-label voice used across the page (12 px spaced capitals). */
export const tileLabel = "text-[0.75rem] tracking-[0.05em] text-ink-muted uppercase";
