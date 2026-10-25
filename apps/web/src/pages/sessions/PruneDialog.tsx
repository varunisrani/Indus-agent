// "Prune old sessions": a day count; the dialog asks the server for a dry run by itself (on open and
// shortly after each edit) and says how many would go, then Prune runs the real clean-up (W52-sc-2).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../../lib/api.ts";
import { useToast } from "../../shell/Toasts.tsx";
import { Button, inputClass, Notice } from "../../ui/controls.tsx";
import { cx } from "../../ui/cx.ts";
import { DialogActions, Modal } from "../../ui/Dialog.tsx";
import { conflictMessage } from "./sources.ts";

const SETTLE_MS = 300;

function plural(count: number): string {
  return `${count} session${count === 1 ? "" : "s"}`;
}

export function PruneDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const toast = useToast();
  const client = useQueryClient();
  const [days, setDays] = useState("90");
  const [settled, setSettled] = useState("90");
  const [problem, setProblem] = useState("");
  const valid = /^\d+$/.test(days.trim());
  const parsed = Number.parseInt(settled, 10);
  const settledValid = /^\d+$/.test(settled.trim());

  useEffect(() => {
    const timer = setTimeout(() => setSettled(days), SETTLE_MS);
    return () => clearTimeout(timer);
  }, [days]);

  const preview = useQuery({
    queryKey: ["prune-preview", parsed],
    queryFn: () => api.sessions.prune(parsed, true),
    enabled: open && settledValid,
    retry: false,
    gcTime: 0,
    staleTime: 0,
  });

  const prune = useMutation({
    mutationFn: () => api.sessions.prune(parsed, false),
    onSuccess: (result) => {
      toast.show(`Removed ${plural(result.count)}.`, "success");
      void client.invalidateQueries({ queryKey: ["sessions"] });
      close();
    },
    onError: (error) => setProblem(conflictMessage(error)),
  });

  const close = () => {
    if (prune.isPending) return;
    setProblem("");
    onClose();
  };

  const ready = valid && settled === days && preview.data !== undefined;
  const count = ready ? (preview.data?.count ?? 0) : 0;
  const notice = problem || (preview.error ? conflictMessage(preview.error) : "");

  return (
    <Modal
      open={open}
      size="sm"
      onOpenChange={(next) => (next ? undefined : close())}
      title="Prune old sessions"
      description="Remove ended sessions older than a number of days. Open sessions are never removed."
    >
      <form
        className="flex flex-col gap-1.5"
        onSubmit={(event) => {
          event.preventDefault();
          if (ready && count > 0) prune.mutate();
        }}
      >
        <label htmlFor="prune-days" className="text-[0.75rem] text-ink-muted">
          Older than (days)
        </label>
        <input
          id="prune-days"
          type="number"
          min={0}
          className={cx(inputClass, "h-[2.125rem] w-full border-line-strong")}
          value={days}
          disabled={prune.isPending}
          onChange={(event) => setDays(event.currentTarget.value)}
        />
      </form>
      {!valid ? (
        <p className="m-0 mt-2 text-[0.75rem] text-danger">Enter a whole number of days.</p>
      ) : ready && preview.data ? (
        <p className="m-0 mt-2 text-[0.75rem] text-ink-muted" data-testid="prune-preview">
          {plural(preview.data.count)} would be removed
          {preview.data.skipped_open ? ` (${preview.data.skipped_open} open kept)` : ""}.
        </p>
      ) : null}
      {notice ? (
        <div className="mt-3">
          <Notice tone="danger">{notice}</Notice>
        </div>
      ) : null}
      <DialogActions>
        <Button size="lg" onClick={close} disabled={prune.isPending}>
          Cancel
        </Button>
        <Button
          size="lg"
          tone="danger"
          disabled={!ready || count === 0 || prune.isPending}
          onClick={() => prune.mutate()}
          {...(prune.isPending ? { icon: <LoaderCircle className="animate-spin" /> } : {})}
        >
          Prune
        </Button>
      </DialogActions>
    </Modal>
  );
}
