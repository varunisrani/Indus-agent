// Keys (P18-20): API keys and other secrets. A value goes in once and is never shown again: after
// a save the input is emptied and only the server's redacted preview remains (R04.15).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { api } from "../lib/api.ts";
import type { EnvKeyInfo, EnvKeys } from "../lib/restTypes.ts";
import { errorText, useToast } from "../shell/Toasts.tsx";
import { Badge, Button, Card, inputClass, QueryState } from "../ui/controls.tsx";
import { ConfirmDialog } from "../ui/Dialog.tsx";
import { Switch } from "../ui/Switch.tsx";

const KEYS_QUERY = ["env", "keys"] as const;

function KeyRow({
  name,
  info,
  onDelete,
}: {
  name: string;
  info: EnvKeyInfo;
  onDelete: () => void;
}) {
  const toast = useToast();
  const client = useQueryClient();
  const [value, setValue] = useState("");

  const save = useMutation({
    mutationFn: (next: string) => api.env.set(name, next),
    onSuccess: (reply) => {
      setValue("");
      client.setQueryData<EnvKeys>(KEYS_QUERY, (all) =>
        all
          ? {
              ...all,
              [name]: { ...info, is_set: reply.is_set, redacted_value: reply.redacted_value },
            }
          : all,
      );
      toast.show(`${name} saved.`, "success");
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (value) save.mutate(value);
  };

  return (
    <li
      className="flex flex-col gap-2 border-t border-line py-3 first:border-t-0"
      data-testid={`key-${name}`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <code className="font-mono text-sm">{name}</code>
        <Badge tone={info.is_set ? "ok" : "neutral"}>{info.is_set ? "set" : "not set"}</Badge>
        {info.is_set && info.redacted_value ? (
          <code className="font-mono text-xs text-muted" data-testid={`preview-${name}`}>
            {info.redacted_value}
          </code>
        ) : null}
        {info.url ? (
          <a
            href={info.url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs text-accent underline"
          >
            Get a key ↗
          </a>
        ) : null}
      </div>
      {info.description ? <p className="text-xs text-muted">{info.description}</p> : null}
      <form className="flex flex-wrap gap-2" onSubmit={submit}>
        <input
          type="password"
          autoComplete="off"
          aria-label={`New value for ${name}`}
          placeholder={info.is_set ? "Replace the stored value" : "Paste the value"}
          className={`${inputClass} min-w-48 flex-1`}
          value={value}
          onChange={(event) => setValue(event.currentTarget.value)}
        />
        <Button type="submit" tone="primary" disabled={!value || save.isPending}>
          {info.is_set ? "Replace" : "Save"}
        </Button>
        {info.is_set ? (
          <Button tone="danger" onClick={onDelete}>
            Delete
          </Button>
        ) : null}
      </form>
    </li>
  );
}

export function EnvPage() {
  const toast = useToast();
  const client = useQueryClient();
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);
  const keys = useQuery({ queryKey: KEYS_QUERY, queryFn: () => api.env.list() });

  const remove = useMutation({
    mutationFn: (name: string) => api.env.remove(name),
    onSuccess: (_reply, name) => {
      setDeleting(null);
      client.setQueryData<EnvKeys>(KEYS_QUERY, (all) => {
        const info = all?.[name];
        return all && info
          ? { ...all, [name]: { ...info, is_set: false, redacted_value: null } }
          : all;
      });
      toast.show(`${name} removed.`, "success");
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });

  const entries = Object.entries(keys.data ?? {}).filter(
    ([, info]) => showAdvanced || !info.advanced,
  );
  const hiddenCount = Object.values(keys.data ?? {}).filter((info) => info.advanced).length;
  const groups = new Map<string, Array<[string, EnvKeyInfo]>>();
  for (const entry of entries) {
    const category = entry[1].category || "other";
    groups.set(category, [...(groups.get(category) ?? []), entry]);
  }

  return (
    <div className="flex flex-col gap-4 p-4">
      <div className="flex items-center gap-2">
        <Switch
          checked={showAdvanced}
          onCheckedChange={setShowAdvanced}
          label="Show advanced keys"
          id="show-advanced"
        />
        <label htmlFor="show-advanced" className="text-sm">
          Show advanced keys{hiddenCount && !showAdvanced ? ` (${hiddenCount} hidden)` : ""}
        </label>
      </div>
      <QueryState
        loading={keys.isPending}
        error={keys.error}
        empty={entries.length === 0}
        emptyText="No keys to show."
      >
        {[...groups.entries()].map(([category, rows]) => (
          <Card key={category}>
            <h2 className="mb-1 text-sm font-semibold capitalize">{category}</h2>
            <ul>
              {rows.map(([name, info]) => (
                <KeyRow key={name} name={name} info={info} onDelete={() => setDeleting(name)} />
              ))}
            </ul>
          </Card>
        ))}
      </QueryState>
      <ConfirmDialog
        open={deleting !== null}
        title={`Remove ${deleting ?? ""}?`}
        description="The stored value is deleted. Anything that needs it stops working until you set it again."
        confirmLabel="Remove"
        danger
        busy={remove.isPending}
        onConfirm={() => {
          if (deleting) remove.mutate(deleting);
        }}
        onCancel={() => setDeleting(null)}
      />
    </div>
  );
}
