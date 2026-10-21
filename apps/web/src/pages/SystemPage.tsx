// System (P18-25): machine operations for the selected profile — host numbers, updates, the
// daemon, the memory provider, the credential pool, maintenance operations with live output, file
// checkpoints and shell hooks. Each section lives in `system/` and fetches its own data.

import { MemorySection, PoolSection } from "./system/CredentialSections.tsx";
import { DaemonSection, HostSection, UpdateSection } from "./system/MachineSections.tsx";
import {
  CheckpointsSection,
  HooksSection,
  OperationsSection,
} from "./system/OperationSections.tsx";

export function SystemPage() {
  return (
    <div className="grid gap-4 p-4 xl:grid-cols-2">
      <HostSection />
      <UpdateSection />
      <DaemonSection />
      <MemorySection />
      <div className="xl:col-span-2">
        <PoolSection />
      </div>
      <OperationsSection />
      <CheckpointsSection />
      <div className="xl:col-span-2">
        <HooksSection />
      </div>
    </div>
  );
}
