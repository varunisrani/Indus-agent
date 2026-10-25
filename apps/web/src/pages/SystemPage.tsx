// System (P18-25, W52-f look): machine operations for the selected profile in one column of headed
// sections — Host (numbers + update check), Daemon, Memory, Credential pool, Operations,
// Checkpoints, Shell hooks. Each section fetches its own data and degrades on its own; the page
// keeps one action log on top for whichever background action ran last.

import { useIsFetching } from "@tanstack/react-query";
import { LoaderCircle } from "lucide-react";
import { useCallback, useRef, useState } from "react";
import { ActionLog } from "../ui/ActionLog.tsx";
import { cx } from "../ui/cx.ts";
import { MemorySection, PoolSection } from "./system/CredentialSections.tsx";
import { type PageLog, SystemLogContext } from "./system/frame.tsx";
import { DaemonSection, HostSection } from "./system/MachineSections.tsx";
import {
  CheckpointsSection,
  HooksSection,
  OperationsSection,
} from "./system/OperationSections.tsx";

export function SystemPage() {
  const [log, setLog] = useState<PageLog | null>(null);
  const show = useCallback((next: PageLog) => setLog(next), []);
  // One spinner for the whole page until the first round of requests has settled.
  const fetching = useIsFetching({ queryKey: ["system"] });
  const settled = useRef(false);
  if (fetching === 0) settled.current = true;
  const first = !settled.current;
  return (
    <SystemLogContext.Provider value={show}>
      {first ? (
        <div className="flex justify-center pt-24" aria-busy="true">
          <LoaderCircle aria-label="Loading" className="size-6 animate-spin text-ink-muted" />
        </div>
      ) : null}
      <div className={cx("flex flex-col gap-8 px-[1.4375rem] py-[1.4375rem]", first && "hidden")}>
        {log ? (
          <ActionLog
            key={`${log.name}-${log.run}`}
            name={log.name}
            run={log.run}
            title={log.title}
            {...(log.onFinished ? { onFinished: log.onFinished } : {})}
            onClose={() => setLog(null)}
          />
        ) : null}
        <HostSection />
        <DaemonSection />
        <MemorySection />
        <PoolSection />
        <OperationsSection />
        <CheckpointsSection />
        <HooksSection />
      </div>
    </SystemLogContext.Provider>
  );
}
