// System page: the external memory provider and the provider credential pool. Pool keys go in
// once and are never shown again: the list carries masked previews only, and the key input is
// emptied as soon as the server has it.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Brain, KeyRound, LoaderCircle, Trash2 } from "lucide-react";
import { type FormEvent, useId, useState } from "react";
import { api } from "../../lib/api.ts";
import { formatSize } from "../../lib/format.ts";
import type { PoolEntryRow } from "../../lib/restTypes.ts";
import { errorText, useToast } from "../../shell/Toasts.tsx";
import {
  Badge,
  Button,
  Card,
  Field,
  IconButton,
  inputClass,
  Notice,
  QueryState,
  Select,
} from "../../ui/controls.tsx";
import { cx } from "../../ui/cx.ts";
import { ConfirmDialog } from "../../ui/Dialog.tsx";
import { SystemSection, tileLabel } from "./frame.tsx";

export function MemorySection() {
  const toast = useToast();
  const client = useQueryClient();
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
    <SystemSection icon={Brain} title="Memory">
      <Card className="flex flex-col gap-4">
        <QueryState
          loading={memory.isPending}
          error={memory.error}
          what="the memory settings"
          onRetry={() => void memory.refetch()}
        >
          {data ? (
            <>
              <div className="flex flex-wrap items-center gap-3">
                <p className="m-0 text-[0.75rem] text-ink-muted">
                  External provider:{" "}
                  {data.active ? (
                    <span className="type-data text-ink">{data.active}</span>
                  ) : (
                    "built-in only"
                  )}
                </p>
                <div className="ml-auto flex flex-wrap items-center gap-2">
                  <Select
                    aria-label="Memory provider"
                    className="w-48"
                    value={shown}
                    onChange={(e) => setChoice(e.currentTarget.value)}
                  >
                    <option value="">Built-in only</option>
                    {data.providers.map((name) => (
                      <option key={name} value={name}>
                        {name}
                      </option>
                    ))}
                  </Select>
                  <Button
                    tone="primary"
                    size="sm"
                    onClick={() => save.mutate(shown)}
                    disabled={save.isPending || shown === data.active}
                  >
                    Save
                  </Button>
                  <Button
                    tone="ghost"
                    size="sm"
                    onClick={() => save.mutate("")}
                    disabled={save.isPending || data.active === ""}
                  >
                    Reset to built-in
                  </Button>
                </div>
              </div>
              <p className="m-0 border-t border-line pt-3 text-[0.75rem] text-ink-muted">
                Built-in files —{" "}
                {Object.entries(data.builtin_files)
                  .map(([file, size]) => `${file}: ${formatSize(size)}`)
                  .join(" · ") || "none yet"}
              </p>
            </>
          ) : null}
        </QueryState>
      </Card>
    </SystemSection>
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
    <form className="flex flex-col gap-3" onSubmit={submit} noValidate>
      <div className="grid grid-cols-1 items-end gap-3 sm:grid-cols-4">
        <Field label="Provider" htmlFor={`${ids}-provider`}>
          <input
            id={`${ids}-provider`}
            className={cx(inputClass, "h-[2.125rem]")}
            list={`${ids}-providers`}
            placeholder="openrouter"
            value={provider}
            onChange={(e) => setProvider(e.currentTarget.value)}
          />
        </Field>
        <datalist id={`${ids}-providers`}>
          {providers.map((name) => (
            <option key={name} value={name} />
          ))}
        </datalist>
        <div className="sm:col-span-2">
          <Field label="API key" htmlFor={`${ids}-key`}>
            <input
              id={`${ids}-key`}
              type="password"
              autoComplete="new-password"
              placeholder="paste the key"
              className={cx(inputClass, "h-[2.125rem] w-full")}
              value={key}
              onChange={(e) => setKey(e.currentTarget.value)}
            />
          </Field>
        </div>
        <Field label="Label" htmlFor={`${ids}-label`}>
          <input
            id={`${ids}-label`}
            className={cx(inputClass, "h-[2.125rem]")}
            placeholder="optional"
            value={label}
            onChange={(e) => setLabel(e.currentTarget.value)}
          />
        </Field>
      </div>
      <div className="flex justify-end">
        <Button
          type="submit"
          tone="primary"
          size="sm"
          className="uppercase"
          icon={add.isPending ? <LoaderCircle className="animate-spin" /> : undefined}
          disabled={add.isPending}
        >
          Add key
        </Button>
      </div>
      {problem ? <Notice tone="danger">{problem}</Notice> : null}
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
    <SystemSection icon={KeyRound} title="Credential pool">
      <Card className="flex flex-col gap-4">
        <AddKey providers={(options.data?.providers ?? []).map((p) => p.name)} />
        <QueryState
          loading={pool.isPending}
          error={pool.error}
          what="the credential pool"
          onRetry={() => void pool.refetch()}
        >
          {groups.length === 0 ? (
            <p className="m-0 text-[0.875rem] text-ink-muted">No pooled credentials yet.</p>
          ) : (
            <ul aria-label="Credential pool" className="m-0 flex list-none flex-col gap-4 p-0">
              {groups.map((group) => (
                <li key={group.provider} className="flex flex-col gap-2">
                  <p className={cx("m-0", tileLabel)}>
                    {group.label}
                    {group.inherited ? (
                      <span className="normal-case"> (from the default profile)</span>
                    ) : null}
                  </p>
                  <ul className="m-0 flex list-none flex-col gap-2 p-0">
                    {group.entries.map((entry) => (
                      <li
                        key={entry.id}
                        data-testid={`pool-${group.provider}-${entry.index}`}
                        className="flex flex-wrap items-center gap-3 border border-line bg-surface px-3 py-2"
                      >
                        <span className="text-[0.875rem] font-medium text-ink">{entry.label}</span>
                        <code className="font-mono text-[0.75rem] text-ink-muted">
                          {entry.preview}
                        </code>
                        <Badge>{entry.auth_type}</Badge>
                        <Badge tone={entry.status === "ok" ? "ok" : "neutral"}>
                          {entry.status}
                        </Badge>
                        <Badge>{entry.source}</Badge>
                        <span className="flex-1" />
                        <IconButton
                          tone="danger"
                          label={`Remove ${entry.label}`}
                          onClick={() => setRemoving({ provider: group.provider, entry })}
                          disabled={group.inherited}
                        >
                          <Trash2 />
                        </IconButton>
                      </li>
                    ))}
                  </ul>
                </li>
              ))}
            </ul>
          )}
        </QueryState>
      </Card>
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
    </SystemSection>
  );
}
