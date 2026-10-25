// Keys (P18-20): API keys and other secrets. A value goes in once and is never shown again: after
// a save the editor closes and only the server's redacted preview remains (R04.15); there is no
// way to reveal a stored value.
// Look (W52-d): jump chips after the title, an OAuth sign-in card (device-code login from the
// Models page), provider keys grouped per provider in collapsible rows, one card per other
// category, and a custom-keys card with an add box.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronDown,
  ChevronRight,
  ExternalLink,
  KeyRound,
  LogIn,
  Pencil,
  Plus,
  RefreshCw,
  Save,
  ShieldCheck,
  ShieldOff,
  Trash2,
  X,
  Zap,
} from "lucide-react";
import { type FormEvent, type ReactNode, useId, useState } from "react";
import { api } from "../lib/api.ts";
import type { EnvKeyInfo, EnvKeys, ProviderOption } from "../lib/restTypes.ts";
import { HeaderAfterTitle } from "../shell/PageHeader.tsx";
import { errorText, useToast } from "../shell/Toasts.tsx";
import { CopyButton } from "../ui/CopyButton.tsx";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  CodeChip,
  EmptyCard,
  IconButton,
  Input,
  PageSpinner,
  QueryState,
} from "../ui/controls.tsx";
import { ConfirmDialog } from "../ui/Dialog.tsx";
import { DeviceLogin } from "./ModelsPage.tsx";

const KEYS_QUERY = ["env", "keys"] as const;
const OPTIONS_QUERY = ["models", "options"] as const;
const ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;

type Entry = [string, EnvKeyInfo];

function sectionId(category: string): string {
  return `section-${category.replace(/[^a-z0-9]+/gi, "-").toLowerCase()}`;
}

function categoryLabel(category: string): string {
  if (category === "custom") return "Custom keys";
  if (category === "provider") return "Providers";
  return category.charAt(0).toUpperCase() + category.slice(1);
}

/** Provider name a key belongs to: our catalog writes "API key for X" / "… override for X". */
export function providerOf(name: string, info: EnvKeyInfo): string {
  const at = info.description.lastIndexOf(" for ");
  return at >= 0 ? info.description.slice(at + 5).trim() || name : name;
}

function jump(id: string) {
  document.getElementById(id)?.scrollIntoView?.({ behavior: "smooth", block: "start" });
}

function GetKey({ url }: { url: string }) {
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-1 text-[0.75rem] text-ink underline-offset-2 hover:underline"
    >
      Get a key
      <ExternalLink aria-hidden="true" className="size-3" />
    </a>
  );
}

// -- one key: compact (unset), full (set) and editing ------------------------------------------

function KeyRow({
  name,
  info,
  onDelete,
  startEditing = false,
  onCancelNew,
}: {
  name: string;
  info: EnvKeyInfo;
  onDelete: () => void;
  startEditing?: boolean;
  /** For a key added in the custom box that was never saved: Cancel drops the row. */
  onCancelNew?: () => void;
}) {
  const toast = useToast();
  const client = useQueryClient();
  const [editing, setEditing] = useState(startEditing);
  const [value, setValue] = useState("");

  const save = useMutation({
    mutationFn: (next: string) => api.env.set(name, next),
    onSuccess: (reply) => {
      setValue("");
      setEditing(false);
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
  const cancel = () => {
    setValue("");
    setEditing(false);
    onCancelNew?.();
  };

  const head = (
    <div className="flex flex-wrap items-center gap-2">
      <code className="font-mono text-[0.8125rem] text-ink">{name}</code>
      <Badge tone={info.is_set ? "ok" : "neutral"}>{info.is_set ? "set" : "not set"}</Badge>
    </div>
  );
  const description = info.description ? (
    <p className="m-0 text-[0.75rem] text-ink-muted">{info.description}</p>
  ) : null;

  if (editing) {
    return (
      <li data-testid={`key-${name}`} className="flex flex-col gap-2 border border-line p-4">
        {head}
        {description}
        <form className="flex flex-wrap items-center gap-2" onSubmit={submit}>
          <Input
            type="password"
            autoComplete="off"
            autoFocus
            aria-label={`New value for ${name}`}
            placeholder={info.is_set ? "Replace the stored value" : "Paste the value"}
            className="min-w-48 flex-1"
            value={value}
            onChange={(event) => setValue(event.currentTarget.value)}
          />
          <Button
            type="submit"
            tone="primary"
            size="sm"
            className="uppercase"
            icon={<Save />}
            disabled={!value || save.isPending}
          >
            Save
          </Button>
          <Button size="sm" className="uppercase" icon={<X />} onClick={cancel}>
            Cancel
          </Button>
        </form>
      </li>
    );
  }

  if (info.is_set) {
    return (
      <li data-testid={`key-${name}`} className="flex flex-col gap-2 border border-line p-4">
        {head}
        {description}
        <div className="flex flex-wrap items-center gap-2">
          <code
            data-testid={`preview-${name}`}
            className="min-h-7 min-w-48 flex-1 border border-line bg-wash px-3 py-1 font-mono text-[0.75rem] text-ink-muted"
          >
            {info.redacted_value ?? "••••"}
          </code>
          <Button
            size="sm"
            className="uppercase"
            icon={<Pencil />}
            onClick={() => setEditing(true)}
          >
            Replace
          </Button>
          <Button
            tone="danger-outline"
            size="sm"
            className="uppercase"
            icon={<Trash2 />}
            onClick={onDelete}
          >
            Clear
          </Button>
        </div>
      </li>
    );
  }

  return (
    <li
      data-testid={`key-${name}`}
      className="group flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5"
    >
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        {head}
        {description}
      </div>
      <div className="flex shrink-0 items-center gap-3">
        {info.url ? <GetKey url={info.url} /> : null}
        <Button size="sm" className="uppercase" icon={<Pencil />} onClick={() => setEditing(true)}>
          Set
        </Button>
      </div>
    </li>
  );
}

function KeyList({
  entries,
  onDelete,
  extra,
}: {
  entries: Entry[];
  onDelete: (name: string) => void;
  extra?: ReactNode;
}) {
  return (
    <ul className="m-0 flex list-none flex-col gap-3 p-0">
      {entries.map(([name, info]) => (
        <KeyRow key={name} name={name} info={info} onDelete={() => onDelete(name)} />
      ))}
      {extra}
    </ul>
  );
}

// -- OAuth sign-ins ------------------------------------------------------------------------------

function OAuthRow({ provider }: { provider: ProviderOption }) {
  const [open, setOpen] = useState(false);
  const command = `indus auth add ${provider.name} --type oauth`;
  return (
    <li data-testid={`oauth-${provider.name}`} className="border-t border-line first:border-t-0">
      <div className="flex items-center gap-3 px-4 py-4 sm:px-6">
        {provider.configured ? (
          <ShieldCheck aria-hidden="true" className="size-5 shrink-0 self-start text-success" />
        ) : (
          <ShieldOff aria-hidden="true" className="size-5 shrink-0 self-start text-ink-soft" />
        )}
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[0.875rem] font-medium text-ink">{provider.label}</span>
            <Badge>{provider.device_login ? "Device code" : "CLI sign-in"}</Badge>
          </div>
          <p className="m-0 text-[0.75rem] text-ink-muted">
            {provider.configured
              ? "Connected."
              : "Not connected. Sign in here when offered, or from a terminal."}
          </p>
          <div className="flex flex-wrap items-center gap-1">
            <CodeChip faded>{command}</CodeChip>
            <CopyButton
              value={command}
              label={`Copy the sign-in command for ${provider.label}`}
              look="text"
            />
          </div>
        </div>
        {provider.device_login && !open ? (
          <Button
            tone="primary"
            size="sm"
            className="uppercase"
            icon={<LogIn />}
            onClick={() => setOpen(true)}
          >
            Login
          </Button>
        ) : null}
      </div>
      {open ? (
        <div className="px-4 pb-4 sm:px-6">
          <DeviceLogin provider={provider} onDone={() => setOpen(false)} />
        </div>
      ) : null}
    </li>
  );
}

function OAuthCard() {
  const client = useQueryClient();
  const options = useQuery({ queryKey: OPTIONS_QUERY, queryFn: () => api.model.options() });
  const list = (options.data?.providers ?? []).filter(
    (p) => p.device_login || p.auth_type.includes("oauth"),
  );
  const connected = list.filter((p) => p.configured).length;
  return (
    <Card padded={false} id="section-oauth" aria-label="Provider sign-ins">
      <CardHeader
        icon={ShieldCheck}
        title="Provider logins (OAuth)"
        below={`${connected} of ${list.length} connected. Log in here where the provider offers a device code; the command works from any terminal.`}
        aside={
          <IconButton
            label="Refresh sign-ins"
            onClick={() => void client.invalidateQueries({ queryKey: OPTIONS_QUERY })}
          >
            <RefreshCw />
          </IconButton>
        }
      />
      {options.isPending ? (
        <PageSpinner />
      ) : (
        <QueryState loading={false} error={options.error}>
          {list.length === 0 ? (
            <p className="m-0 px-6 py-8 text-center text-[0.875rem] text-ink-muted">
              No sign-in providers.
            </p>
          ) : (
            <ul className="m-0 list-none p-0">
              {list.map((provider) => (
                <OAuthRow key={provider.name} provider={provider} />
              ))}
            </ul>
          )}
        </QueryState>
      )}
    </Card>
  );
}

// -- provider keys, grouped ------------------------------------------------------------------------

function ProviderGroup({
  label,
  entries,
  onDelete,
}: {
  label: string;
  entries: Entry[];
  onDelete: (name: string) => void;
}) {
  const ids = useId();
  const [open, setOpen] = useState(false);
  const set = entries.filter(([, info]) => info.is_set).length;
  const url = entries.find(([, info]) => info.url)?.[1].url ?? null;
  return (
    <li className="border-t border-line first:border-t-0">
      <div className="flex items-center gap-3 px-4 py-3 sm:px-6">
        <button
          type="button"
          aria-expanded={open}
          aria-controls={`${ids}-keys`}
          onClick={() => setOpen(!open)}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
        >
          {open ? (
            <ChevronDown aria-hidden="true" className="size-4 shrink-0 text-ink-soft" />
          ) : (
            <ChevronRight aria-hidden="true" className="size-4 shrink-0 text-ink-soft" />
          )}
          <span className="truncate text-[0.875rem] font-bold tracking-[0.04em] text-ink">
            {label}
          </span>
          {set ? <Badge tone="ok">{set} set</Badge> : null}
        </button>
        {url ? <GetKey url={url} /> : null}
        <span className="shrink-0 text-[0.75rem] text-ink-soft">
          {entries.length} {entries.length === 1 ? "key" : "keys"}
        </span>
      </div>
      {open ? (
        <div id={`${ids}-keys`} className="px-4 pb-4 sm:px-6">
          <KeyList entries={entries} onDelete={onDelete} />
        </div>
      ) : null}
    </li>
  );
}

function ProvidersCard({
  entries,
  onDelete,
}: {
  entries: Entry[];
  onDelete: (name: string) => void;
}) {
  const groups = new Map<string, Entry[]>();
  for (const entry of entries) {
    const label = providerOf(entry[0], entry[1]);
    groups.set(label, [...(groups.get(label) ?? []), entry]);
  }
  const sorted = [...groups.entries()].sort(([a], [b]) => a.localeCompare(b));
  const configured = sorted.filter(([, rows]) => rows.some(([, info]) => info.is_set)).length;
  return (
    <Card padded={false} id="section-providers" aria-label="Providers">
      <CardHeader
        icon={Zap}
        title="Providers"
        below={`${configured} of ${sorted.length} providers configured.`}
      />
      <ul className="m-0 list-none p-0">
        {sorted.map(([label, rows]) => (
          <ProviderGroup key={label} label={label} entries={rows} onDelete={onDelete} />
        ))}
      </ul>
    </Card>
  );
}

// -- other categories and custom keys ----------------------------------------------------------------

function CategoryCard({
  category,
  entries,
  onDelete,
}: {
  category: string;
  entries: Entry[];
  onDelete: (name: string) => void;
}) {
  const set = entries.filter(([, info]) => info.is_set).length;
  return (
    <Card padded={false} id={sectionId(category)} aria-label={categoryLabel(category)}>
      <CardHeader
        icon={KeyRound}
        title={categoryLabel(category)}
        below={`${set} of ${entries.length} set.`}
      />
      <div className="p-4 sm:px-6">
        <KeyList entries={entries} onDelete={onDelete} />
      </div>
    </Card>
  );
}

const BLANK: EnvKeyInfo = {
  category: "custom",
  description: "",
  advanced: false,
  is_password: true,
  is_set: false,
  redacted_value: null,
  url: null,
};

function CustomCard({
  entries,
  known,
  onDelete,
}: {
  entries: Entry[];
  known: ReadonlySet<string>;
  onDelete: (name: string) => void;
}) {
  const ids = useId();
  const [draft, setDraft] = useState("");
  const [problem, setProblem] = useState("");
  const [added, setAdded] = useState<string[]>([]);
  const pending = added.filter((name) => !known.has(name));

  const add = (event: FormEvent) => {
    event.preventDefault();
    const name = draft.trim();
    if (!ENV_NAME.test(name)) {
      setProblem("Use letters, digits and '_', not starting with a digit.");
      return;
    }
    if (known.has(name) || pending.includes(name)) {
      setProblem(`${name} is already listed.`);
      return;
    }
    setProblem("");
    setDraft("");
    setAdded([...added, name]);
  };

  return (
    <Card padded={false} id="section-custom" aria-label="Custom keys">
      <CardHeader
        icon={KeyRound}
        title="Custom keys"
        below={`${entries.length} custom ${entries.length === 1 ? "key" : "keys"}.`}
      />
      <div className="flex flex-col gap-3 p-4 sm:px-6">
        <p className="m-0 text-[0.75rem] text-ink-soft">
          Any other variable a tool or skill reads from the environment; stored with the rest.
        </p>
        {entries.length || pending.length ? (
          <KeyList
            entries={entries}
            onDelete={onDelete}
            extra={pending.map((name) => (
              <KeyRow
                key={name}
                name={name}
                info={BLANK}
                onDelete={() => onDelete(name)}
                startEditing
                onCancelNew={() => setAdded((all) => all.filter((n) => n !== name))}
              />
            ))}
          />
        ) : null}
        <form
          className="flex flex-col gap-2 border border-dashed border-line-strong p-4"
          onSubmit={add}
          noValidate
        >
          <label htmlFor={`${ids}-name`} className="type-label text-ink-muted">
            Add a key
          </label>
          <div className="flex flex-wrap items-center gap-2">
            <Input
              id={`${ids}-name`}
              placeholder="MY_SERVICE_TOKEN"
              autoComplete="off"
              className="min-w-48 flex-1"
              value={draft}
              onChange={(e) => setDraft(e.currentTarget.value.toUpperCase())}
            />
            <Button type="submit" tone="primary" size="sm" className="uppercase" icon={<Plus />}>
              Add
            </Button>
          </div>
          {problem ? (
            <p role="alert" className="m-0 text-[0.75rem] text-danger">
              {problem}
            </p>
          ) : null}
        </form>
      </div>
    </Card>
  );
}

// -- the page ------------------------------------------------------------------------------------------

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

  const all = Object.entries(keys.data ?? {});
  const entries = all.filter(([, info]) => showAdvanced || !info.advanced);
  const hiddenCount = all.filter(([, info]) => info.advanced).length;
  const providerKeys = entries.filter(([, info]) => info.category === "provider");
  const customKeys = entries.filter(([, info]) => info.category === "custom");
  const others = new Map<string, Entry[]>();
  for (const entry of entries) {
    const category = entry[1].category || "other";
    if (category === "provider" || category === "custom") continue;
    others.set(category, [...(others.get(category) ?? []), entry]);
  }
  const chips: Array<[string, string]> = [
    ["section-oauth", "OAuth"],
    ...(providerKeys.length ? [["section-providers", "Providers"] as [string, string]] : []),
    ...[...others.keys()].map((c) => [sectionId(c), categoryLabel(c)] as [string, string]),
    ["section-custom", "Custom keys"],
  ];
  const onDelete = (name: string) => setDeleting(name);

  return (
    <div className="flex flex-col gap-6 px-4 py-[1.375rem] lg:pr-8 lg:pl-[1.4375rem]">
      <HeaderAfterTitle>
        <nav aria-label="Jump to section" className="hidden flex-wrap gap-1 md:flex">
          {chips.map(([id, label]) => (
            <button
              key={id}
              type="button"
              onClick={() => jump(id)}
              className="type-serif h-[1.3125rem] border border-line px-2 text-[0.6875rem] tracking-[0.08em] text-ink-muted uppercase hover:text-ink"
            >
              {label}
            </button>
          ))}
        </nav>
      </HeaderAfterTitle>
      <div className="flex flex-wrap items-start gap-4">
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <p className="m-0 text-[0.875rem] text-ink-muted">
            API keys and secrets live in this profile's secret store; stored values are never shown
            again.
          </p>
          <p className="m-0 text-[0.75rem] text-ink-soft">
            Saved at once. New turns pick a changed key up without a restart.
          </p>
        </div>
        <Button
          size="sm"
          caps={false}
          aria-pressed={showAdvanced}
          title={
            hiddenCount
              ? `${hiddenCount} advanced ${hiddenCount === 1 ? "key" : "keys"}`
              : undefined
          }
          onClick={() => setShowAdvanced(!showAdvanced)}
        >
          {showAdvanced ? "Hide advanced" : "Show advanced"}
        </Button>
      </div>
      <OAuthCard />
      {keys.isPending ? (
        <PageSpinner />
      ) : (
        <QueryState loading={false} error={keys.error}>
          {all.length === 0 ? <EmptyCard>No keys to show.</EmptyCard> : null}
          {providerKeys.length ? (
            <ProvidersCard entries={providerKeys} onDelete={onDelete} />
          ) : null}
          {[...others.entries()].map(([category, rows]) => (
            <CategoryCard key={category} category={category} entries={rows} onDelete={onDelete} />
          ))}
          <CustomCard
            entries={customKeys}
            known={new Set(all.map(([name]) => name))}
            onDelete={onDelete}
          />
        </QueryState>
      )}
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
