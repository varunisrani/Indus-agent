// The raw YAML view (W52-pc-10, editable since W58-J2-2): the config file's own settings (no
// defaults, `${NAME}` references as written, credentials shown as the mask) written as YAML.
// Save sends the text to `PUT /api/config/raw`, which parses it, puts the hidden values back,
// checks it against the schema and only then writes the whole file through the config writer; a
// problem is a 422 that lists every one and changes nothing. Comments typed here are not kept
// (the file's own comments are).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText } from "lucide-react";
import { useState } from "react";
import { ApiError, api } from "../../lib/api.ts";
import { toYaml } from "../../lib/yaml.ts";
import { errorText, useToast } from "../../shell/Toasts.tsx";
import { Card, CardHeader, Loading, Notice, QueryState } from "../../ui/controls.tsx";

export const EMPTY_YAML = "# config.yaml has no settings yet\n";

/** The `problems` list of a 422 `config_invalid` answer, else none. */
export function problemsOf(error: unknown): string[] {
  if (!(error instanceof ApiError) || typeof error.body !== "object" || error.body === null)
    return [];
  const detail = (error.body as { detail?: unknown }).detail;
  if (typeof detail !== "object" || detail === null) return [];
  const problems = (detail as { problems?: unknown }).problems;
  return Array.isArray(problems) ? problems.map(String) : [];
}

export interface YamlDraft {
  loading: boolean;
  error: unknown;
  retry: () => void;
  text: string;
  setText: (next: string) => void;
  dirty: boolean;
  saving: boolean;
  problems: string[];
  save: () => void;
}

/** The YAML text being edited, kept by the page so the toolbar's Save can send it. */
export function useYamlDraft(active: boolean, onSaved: () => void): YamlDraft {
  const client = useQueryClient();
  const toast = useToast();
  const file = useQuery({
    queryKey: ["config", "raw"],
    queryFn: () => api.config.raw(),
    enabled: active,
  });
  const loaded = file.data ? toYaml(file.data.config) || EMPTY_YAML : null;
  const [draft, setDraft] = useState<string | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  const text = draft ?? loaded ?? "";
  const save = useMutation({
    mutationFn: (yaml: string) => api.config.putRaw(yaml),
    onSuccess: () => {
      setDraft(null);
      setProblems([]);
      toast.show("Saved config.yaml. Changes apply to the next session.", "success");
      onSaved();
      void client.invalidateQueries({ queryKey: ["config"] });
    },
    onError: (error) => {
      setProblems(problemsOf(error));
      toast.show(errorText(error), "error");
    },
  });
  return {
    loading: file.isPending && active,
    error: file.error,
    retry: () => void file.refetch(),
    text,
    setText: (next) => setDraft(next),
    dirty: draft !== null && draft !== loaded,
    saving: save.isPending,
    problems,
    save: () => save.mutate(text),
  };
}

export function YamlView({ draft }: { draft: YamlDraft }) {
  return (
    <Card padded={false}>
      <CardHeader icon={FileText} title="Raw YAML configuration" />
      {draft.loading ? (
        <div className="flex justify-center py-12">
          <Loading />
        </div>
      ) : (
        <QueryState loading={false} error={draft.error} onRetry={draft.retry}>
          {draft.problems.length > 0 ? (
            <div className="px-4 pt-3">
              <Notice tone="danger">
                <div className="min-w-0">
                  <span className="block">Not saved: fix these first.</span>
                  <ul className="m-0 mt-1 list-disc pl-5">
                    {draft.problems.map((problem) => (
                      <li key={problem} className="type-data text-[0.75rem] break-words">
                        {problem}
                      </li>
                    ))}
                  </ul>
                </div>
              </Notice>
            </div>
          ) : null}
          <textarea
            aria-label="Raw YAML configuration"
            spellCheck={false}
            value={draft.text}
            onChange={(e) => draft.setText(e.currentTarget.value)}
            className="type-data block min-h-[600px] w-full resize-y border-0 bg-transparent px-4 py-3 text-[0.8125rem] leading-[21px] text-ink outline-none"
          />
        </QueryState>
      )}
    </Card>
  );
}
