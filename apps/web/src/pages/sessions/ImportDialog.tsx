// "Import sessions" (W54-W1-9): the conversations of other coding tools that the server finds in its
// user's home (the session-import routes, P24-28), each with an Import button. An import lands in
// the profile this dashboard shows and the list refreshes; a file brought in before says so.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Download, LoaderCircle } from "lucide-react";
import { useState } from "react";
import { ApiError, api } from "../../lib/api.ts";
import { relativeTime } from "../../lib/format.ts";
import type { ImportCandidate } from "../../lib/restTypes.ts";
import { errorText, useToast } from "../../shell/Toasts.tsx";
import { Badge, Button, EmptyCard, Notice } from "../../ui/controls.tsx";
import { DialogActions, Modal } from "../../ui/Dialog.tsx";

const SOURCE_NAMES: Record<string, string> = { "claude-code": "Claude Code", codex: "Codex" };

function sourceName(source: string): string {
  return SOURCE_NAMES[source] ?? source;
}

function key(candidate: ImportCandidate): string {
  return `${candidate.source}:${candidate.path}`;
}

export function ImportDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const toast = useToast();
  const client = useQueryClient();
  const [done, setDone] = useState<Record<string, string>>({});
  const [problem, setProblem] = useState("");
  const candidates = useQuery({
    queryKey: ["session-import", "candidates"],
    queryFn: () => api.sessionImport.candidates(),
    enabled: open,
    staleTime: 0,
  });
  const bring = useMutation({
    mutationFn: (candidate: ImportCandidate) =>
      api.sessionImport.importFile(candidate.source, candidate.path),
    onSuccess: (result, candidate) => {
      setProblem("");
      setDone((was) => ({ ...was, [key(candidate)]: "Imported" }));
      const count = `${result.message_count} message${result.message_count === 1 ? "" : "s"}`;
      toast.show(`Imported “${result.title}” (${count}).`, "success");
      void client.invalidateQueries({ queryKey: ["sessions"] });
    },
    onError: (error, candidate) => {
      if (error instanceof ApiError && error.code === "already_imported") {
        setDone((was) => ({ ...was, [key(candidate)]: "Already imported" }));
        return;
      }
      setProblem(errorText(error));
    },
  });

  const list = candidates.data?.candidates ?? [];
  return (
    <Modal
      open={open}
      size="lg"
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
      title="Import sessions"
      description="Conversations from other coding tools on this machine. An import adds a copy to this profile's history; the original file is left as it is."
    >
      <div className="flex flex-col gap-3">
        {problem ? <Notice tone="danger">{problem}</Notice> : null}
        {candidates.isPending ? (
          <div className="flex justify-center py-8" aria-busy="true">
            <LoaderCircle aria-label="Loading" className="size-5 animate-spin text-ink-soft" />
          </div>
        ) : candidates.error ? (
          <Notice tone="danger">{errorText(candidates.error)}</Notice>
        ) : list.length === 0 ? (
          <EmptyCard>
            No conversations from{" "}
            {(candidates.data?.sources ?? Object.keys(SOURCE_NAMES)).map(sourceName).join(" or ")}{" "}
            were found in the server's home folder.
          </EmptyCard>
        ) : (
          <ul
            aria-label="Conversations to import"
            className="m-0 flex max-h-[50vh] list-none flex-col gap-1.5 overflow-y-auto p-0"
          >
            {list.map((candidate) => {
              const state = done[key(candidate)];
              const busy = bring.isPending && bring.variables === candidate;
              return (
                <li
                  key={key(candidate)}
                  className="flex items-center gap-3 border border-line px-3 py-2.5"
                >
                  <div className="flex min-w-0 flex-1 flex-col gap-1">
                    <span className="type-serif truncate text-[0.875rem] text-ink">
                      {candidate.title || "Untitled conversation"}
                    </span>
                    <span className="flex min-w-0 items-center gap-2 text-[0.75rem] text-ink-soft">
                      <Badge>{sourceName(candidate.source)}</Badge>
                      <span className="shrink-0">{relativeTime(candidate.modified)}</span>
                      {candidate.cwd ? (
                        <span className="truncate font-mono">{candidate.cwd}</span>
                      ) : null}
                    </span>
                  </div>
                  {state ? (
                    <span className="inline-flex shrink-0 items-center gap-1 text-[0.75rem] text-success">
                      <Check aria-hidden="true" className="size-3.5" />
                      {state}
                    </span>
                  ) : (
                    <Button
                      size="sm"
                      icon={busy ? <LoaderCircle className="animate-spin" /> : <Download />}
                      disabled={bring.isPending}
                      aria-label={`Import ${candidate.title || "conversation"}`}
                      onClick={() => bring.mutate(candidate)}
                    >
                      Import
                    </Button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
      <DialogActions>
        <Button size="lg" onClick={onClose}>
          Close
        </Button>
      </DialogActions>
    </Modal>
  );
}
