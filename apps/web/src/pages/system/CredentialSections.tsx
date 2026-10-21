// System page: the external memory provider and the provider credential pool. Pool keys go in
// once and are never shown again: the list carries masked previews only, and the key input is
// emptied as soon as the server has it.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";
import { api } from "../../lib/api.ts";
import type { PoolEntryRow } from "../../lib/restTypes.ts";
import { errorText, useToast } from "../../shell/Toasts.tsx";
import { Badge, Button, Card, Field, inputClass, Notice, QueryState } from "../../ui/controls.tsx";
import { ConfirmDialog } from "../../ui/Dialog.tsx";

export function MemorySection() {
  const toast = useToast();
  const client = useQueryClient();
  const ids = useId();
  const memory = useQuery({ queryKey: ["system", "memory"], queryFn: () => api.system.memory() });
  const [choice, setChoice] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: (provider: string) => api.system.setMemoryProvider(provider),
    onSuccess: (reply) => {
      setChoice(null);
      toast.show(
        reply.active ? `Memory provider ${reply.active} selected.` : "Built-in memory only.",
        "success",
      );
      void client.invalidateQueries({ queryKey: ["system", "memory"] });
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });
  const data = memory.data;
  const shown = choice ?? data?.active ?? "";
  return (
    <Card className="flex flex-col gap-2">
      <h2 className="text-sm font-semibold">Memory</h2>
      <QueryState loading={memory.isPending} error={memory.error}>
        {data ? (
          <>
            <p className="text-xs text-muted">
              Built-in notes:{" "}
              {Object.entries(data.builtin_files)
                .map(([file, size]) => `${file} ${size} B`)
                .join(", ")}
              . An external provider adds its own recall on top.
            </p>
            <div className="flex flex-wrap items-end gap-2">
              <Field label="Memory provider" htmlFor={`${ids}-provider`}>
                <select
                  id={`${ids}-provider`}
                  className={inputClass}
                  value={shown}
                  onChange={(e) => setChoice(e.currentTarget.value)}
                >
                  <option value="">Built-in only</option>
                  {data.providers.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
              </Field>
              <Button
                tone="primary"
                onClick={() => save.mutate(shown)}
                disabled={save.isPending || shown === data.active}
              >
                Save
              </Button>
              <Button
                onClick={() => save.mutate("")}
                disabled={save.isPending || data.active === ""}
              >
                Reset to built-in
              </Button>
            </div>
          </>
        ) : null}
      </QueryState>
    </Card>
  );
}

function AddKey({ providers }: { providers: string[] }) {
  const toast = useToast();
  const client = useQueryClient();
  const ids = useId();
  const [provider, setProvider] = useState("");
  const [key, setKey] = useState("");
  const [label, setLabel] = useState("");
  const [problem, setProblem] = useState("");
  const add = useMutation({
    mutationFn: () =>
      api.system.addToPool({
        provider: provider.trim(),
        api_key: key.trim(),
        ...(label.trim() ? { label: label.trim() } : {}),
      }),
    onSuccess: (reply) => {
      setKey("");
      setLabel("");
      setProblem("");
      toast.show(`Key added to ${reply.provider} as entry ${reply.index}.`, "success");
      void client.invalidateQueries({ queryKey: ["system", "pool"] });
    },
    onError: (error) => {
      // A refused key is not kept in the field either.
      setKey("");
      setProblem(errorText(error));
    },
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!provider.trim() || !key.trim()) {
      setProblem("Pick the provider and paste the key.");
      return;
    }
    add.mutate();
  };
  return (
    <form className="flex flex-wrap items-end gap-2" onSubmit={submit} noValidate>
      <Field label="Provider" htmlFor={`${ids}-provider`}>
        <input
          id={`${ids}-provider`}
          className={inputClass}
          list={`${ids}-providers`}
          value={provider}
          onChange={(e) => setProvider(e.currentTarget.value)}
        />
      </Field>
      <datalist id={`${ids}-providers`}>
        {providers.map((name) => (
          <option key={name} value={name} />
        ))}
      </datalist>
      <Field label="API key" htmlFor={`${ids}-key`}>
        <input
          id={`${ids}-key`}
          type="password"
          autoComplete="new-password"
          className={`${inputClass} font-mono`}
          value={key}
          onChange={(e) => setKey(e.currentTarget.value)}
        />
      </Field>
      <Field label="Label (optional)" htmlFor={`${ids}-label`}>
        <input
          id={`${ids}-label`}
          className={inputClass}
          value={label}
          onChange={(e) => setLabel(e.currentTarget.value)}
        />
      </Field>
      <Button type="submit" tone="primary" disabled={add.isPending}>
        Add key
      </Button>
      {problem ? (
        <div className="w-full">
          <Notice tone="danger">{problem}</Notice>
        </div>
      ) : null}
    </form>
  );
}

interface Removal {
  provider: string;
  entry: PoolEntryRow;
}

export function PoolSection() {
  const toast = useToast();
  const client = useQueryClient();
  const [removing, setRemoving] = useState<Removal | null>(null);
  const pool = useQuery({ queryKey: ["system", "pool"], queryFn: () => api.system.pool() });
  const options = useQuery({
    queryKey: ["system", "providers"],
    queryFn: () => api.model.options(),
  });
  const remove = useMutation({
    mutationFn: (r: Removal) => api.system.removeFromPool(r.provider, r.entry.index),
    onSuccess: (_reply, r) => {
      setRemoving(null);
      toast.show(`${r.entry.label} removed from ${r.provider}.`, "success");
      void client.invalidateQueries({ queryKey: ["system", "pool"] });
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });
  const groups = pool.data?.providers ?? [];
  return (
    <Card className="flex flex-col gap-3">
      <h2 className="text-sm font-semibold">Credential pool</h2>
      <p className="text-xs text-muted">
        Several keys per provider are used in turn; a key that hits its limit rests while the next
        one works.
      </p>
      <QueryState
        loading={pool.isPending}
        error={pool.error}
        empty={groups.length === 0}
        emptyText="No pooled credentials yet."
      >
        <ul aria-label="Credential pool" className="flex flex-col gap-3">
          {groups.map((group) => (
            <li key={group.provider} className="flex flex-col gap-1">
              <p className="text-sm font-medium">
                {group.label}
                {group.inherited ? (
                  <span className="text-xs text-muted"> (from the default profile)</span>
                ) : null}
              </p>
              <ul className="flex flex-col gap-1">
                {group.entries.map((entry) => (
                  <li
                    key={entry.id}
                    data-testid={`pool-${group.provider}-${entry.index}`}
                    className="flex flex-wrap items-center gap-2 text-sm"
                  >
                    <span className="w-6 text-muted">{entry.index}.</span>
                    <span className="flex-1">{entry.label}</span>
                    <code className="font-mono text-xs">{entry.preview}</code>
                    <Badge tone={entry.status === "ok" ? "ok" : "warn"}>{entry.status}</Badge>
                    <Badge>{entry.source}</Badge>
                    <Button
                      tone="danger"
                      onClick={() => setRemoving({ provider: group.provider, entry })}
                      disabled={group.inherited}
                      aria-label={`Remove ${entry.label}`}
                    >
                      Remove
                    </Button>
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      </QueryState>
      <AddKey providers={(options.data?.providers ?? []).map((p) => p.name)} />
      <ConfirmDialog
        open={removing !== null}
        title={`Remove ${removing?.entry.label ?? ""}?`}
        description={
          removing?.entry.source === "env"
            ? "The key stays in its environment variable; it is only left out of the pool."
            : "The key is deleted from this profile's secret store."
        }
        confirmLabel="Remove"
        danger
        busy={remove.isPending}
        onConfirm={() => {
          if (removing) remove.mutate(removing);
        }}
        onCancel={() => setRemoving(null)}
      />
    </Card>
  );
}
