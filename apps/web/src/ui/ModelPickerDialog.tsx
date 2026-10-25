// "Switch model" dialog, shared by the chat rail and the Models page (W52-a / W52-b, one picker
// since the wave 53 integration). Providers on the left, models on the right, a fuzzy filter over
// both, the provider and model in use tagged CURRENT. Where the models come from:
// - a custom or local endpoint (a saved `providers.<name>` entry with a base URL, or the main
//   model's own `model.base_url`): its own `/models`, asked by our server
//   (`POST /api/model/test-endpoint`, W57-H1-5), plus the entry's saved models;
// - a vendor provider: the ids our price table lists for its own host (`models` on its
//   `/api/model/options` row, W60-L1-1);
// - any other provider: only the model in use.
// The typed model id field stays as the fallback. The caller decides what applying means
// (`onApply`), and owns the request.

import { useQuery } from "@tanstack/react-query";
import { Check, LoaderCircle, RefreshCw, Search } from "lucide-react";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import { api } from "../lib/api.ts";
import type { ConfigObject, EndpointTest, ProviderOption } from "../lib/restTypes.ts";
import { errorText } from "../shell/Toasts.tsx";
import { Button } from "./controls.tsx";
import { cx } from "./cx.ts";
import { Modal } from "./Dialog.tsx";

export interface ModelChoice {
  provider: string;
  model: string;
}

/** Letters of `query` found in order inside `text` (case-insensitive), or null. */
export function fuzzyHits(text: string, query: string): number[] | null {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const lower = text.toLowerCase();
  const at: number[] = [];
  let from = 0;
  for (const ch of q) {
    if (ch === " ") continue;
    const found = lower.indexOf(ch, from);
    if (found < 0) return null;
    at.push(found);
    from = found + 1;
  }
  return at;
}

function Highlighted({ text, query }: { text: string; query: string }) {
  const hits = fuzzyHits(text, query);
  if (!hits || hits.length === 0) return <>{text}</>;
  const set = new Set(hits);
  const out: ReactNode[] = [];
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    out.push(
      set.has(i) ? (
        <span key={i} className="font-bold text-ink underline">
          {ch}
        </span>
      ) : (
        ch
      ),
    );
  }
  return <>{out}</>;
}

/** Where a provider's models can be asked for: its endpoint and saved models, or null. */
export function endpointSource(
  config: ConfigObject | undefined,
  provider: string | null,
  currentProvider: string | null,
): { query: EndpointTest; saved: string[] } | null {
  if (!config || !provider) return null;
  const record = (value: unknown): Record<string, unknown> =>
    value !== null && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : {};
  const entry = record(record(config.providers)[provider]);
  const url = typeof entry.base_url === "string" ? entry.base_url.trim() : "";
  if (url) {
    const keyEnv = typeof entry.key_env === "string" ? entry.key_env.trim() : "";
    const saved = Array.isArray(entry.models)
      ? entry.models.filter((m): m is string => typeof m === "string" && m.trim() !== "")
      : [];
    return { query: { base_url: url, ...(keyEnv ? { key_env: keyEnv } : {}) }, saved };
  }
  const main = record(config.model);
  const mainUrl = typeof main.base_url === "string" ? main.base_url.trim() : "";
  if (provider === currentProvider && mainUrl) return { query: { base_url: mainUrl }, saved: [] };
  return null;
}

function countText(n: number): string {
  return `${n} ${n === 1 ? "model" : "models"}`;
}

function CurrentTag() {
  return <span className="type-heading shrink-0 text-[0.75rem] text-ink">CURRENT</span>;
}

export function ModelPickerDialog({
  open,
  onOpenChange,
  onApply,
  busy = false,
  error = null,
  note = "Saved to config; new sessions use it.",
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Apply the picked provider and model; the caller closes the dialog when it is done. */
  onApply: (choice: ModelChoice) => void;
  /** True while the caller's request runs: Switch shows a spinner and the dialog stays open. */
  busy?: boolean;
  /** The caller's last failure, shown above the footer. */
  error?: unknown;
  /** The footer's left note. */
  note?: string;
}) {
  const options = useQuery({
    queryKey: ["models", "options"],
    queryFn: () => api.model.options(),
    enabled: open,
  });
  const current = options.data?.current ?? { provider: null, model: null };
  const [filter, setFilter] = useState("");
  const [provider, setProvider] = useState<string | null>(null);
  const [model, setModel] = useState<string | null>(null);
  const [typed, setTyped] = useState("");

  useEffect(() => {
    if (!open) return;
    setFilter("");
    setTyped("");
    setModel(null);
    setProvider(null);
  }, [open]);

  const providers = useMemo(() => {
    const list = [...(options.data?.providers ?? [])];
    // The provider in use first, then configured ones, then the rest (each in server order).
    const rank = (p: ProviderOption) => (p.name === current.provider ? 0 : p.configured ? 1 : 2);
    list.sort((a, b) => rank(a) - rank(b));
    return list.filter(
      (p) =>
        !filter.trim() ||
        fuzzyHits(`${p.label} ${p.name}`, filter) !== null ||
        (p.name === current.provider && current.model && fuzzyHits(current.model, filter)),
    );
  }, [options.data, current.provider, current.model, filter]);

  const shownProvider = provider ?? current.provider ?? providers[0]?.name ?? null;
  const config = useQuery({
    queryKey: ["config", "file"],
    queryFn: () => api.config.get(),
    enabled: open,
  });
  const source = endpointSource(config.data, shownProvider, current.provider);
  const shownRow = options.data?.providers.find((p) => p.name === shownProvider);
  const catalogue = shownRow?.models ?? [];
  const listed = useQuery({
    queryKey: ["models", "endpoint", source?.query.base_url ?? "", source?.query.key_env ?? ""],
    queryFn: () => api.model.testEndpoint(source?.query ?? { base_url: "" }),
    enabled: open && source !== null,
    staleTime: 60_000,
    retry: false,
  });
  const listedModels = source !== null && listed.data?.ok ? listed.data.models : [];
  // the shown provider's models: the one in use, the endpoint's list, the entry's saved ones
  const known = [
    ...new Set([
      ...(shownProvider && shownProvider === current.provider && current.model
        ? [current.model]
        : []),
      ...listedModels,
      ...(source?.saved ?? []),
      ...catalogue,
    ]),
  ];
  const models = known.filter((m) => fuzzyHits(m, filter) !== null);
  const listHint =
    source === null
      ? catalogue.length > 0
        ? `From our price table${shownRow?.models_checked ? ` (checked ${shownRow.models_checked})` : ""}. Pick a model, or type another id.`
        : "Type the model id as the provider names it; this install cannot list them."
      : listed.isFetching && !listed.data
        ? "Asking the endpoint for its models…"
        : listed.error
          ? `${errorText(listed.error)} Type the model id instead.`
          : listed.data && !listed.data.ok
            ? `${listed.data.message} Type the model id instead.`
            : "Pick a model, or type another id.";
  const chosen = model ?? (shownProvider === current.provider ? current.model : null);
  const changed =
    !!chosen && !!shownProvider && (chosen !== current.model || shownProvider !== current.provider);

  const switchTo = (pickModel: string | null = chosen) => {
    if (!pickModel || !shownProvider || busy) return;
    onApply({ provider: shownProvider, model: pickModel });
  };

  const currentLabel =
    current.model || current.provider
      ? `current: ${current.model ?? "none"}${current.provider ? ` · ${current.provider}` : ""}`
      : "current: none";

  return (
    <Modal
      open={open}
      onOpenChange={(next) => {
        if (!busy) onOpenChange(next);
      }}
      size="lg"
      title="Switch model"
      description={currentLabel}
      className="flex h-[80vh] max-h-[45rem] flex-col overflow-hidden bg-surface"
      overlayClassName="bg-canvas/85"
      titleClassName="type-serif m-0 text-[0.875rem] tracking-[0.08em] text-ink uppercase"
      descriptionClassName="type-data m-0 mt-1.5 text-[0.75rem] text-ink-muted"
      bodyClassName="flex min-h-0 flex-1 flex-col"
    >
      <div className="border-b border-line px-5 py-3">
        <label className="flex h-8 items-center gap-2 border border-line-strong px-2.5 focus-within:border-ink">
          <Search aria-hidden="true" className="size-3.5 shrink-0 text-ink-soft" />
          <input
            aria-label="Filter providers and models"
            placeholder="Filter providers and models…"
            className="type-data min-w-0 flex-1 bg-transparent text-[0.875rem] text-ink outline-none placeholder:text-ink-faint"
            value={filter}
            onChange={(event) => setFilter(event.currentTarget.value)}
          />
        </label>
      </div>
      <div className="flex min-h-0 flex-1">
        <ul
          aria-label="Providers"
          className="m-0 w-[12.5rem] shrink-0 list-none overflow-y-auto border-r border-line p-0"
        >
          {options.isPending ? (
            <li className="flex items-center gap-2 px-3 py-3 font-mono text-[0.75rem] text-ink-soft">
              <LoaderCircle aria-hidden="true" className="size-3.5 animate-spin" /> loading…
            </li>
          ) : options.error ? (
            <li role="alert" className="px-3 py-3 text-[0.75rem] text-danger">
              {errorText(options.error)}
            </li>
          ) : (options.data?.providers.length ?? 0) === 0 ? (
            <li className="px-3 py-3 text-[0.75rem] text-ink-soft">
              No providers yet. Add a key on the <Link to="/env">Keys</Link> page or pick one on the{" "}
              <Link to="/models">Models</Link> page.
            </li>
          ) : providers.length === 0 ? (
            <li className="px-3 py-3 text-[0.75rem] text-ink-soft italic">Nothing matches.</li>
          ) : (
            providers.map((p) => {
              const picked = p.name === shownProvider;
              const inUse = p.name === current.provider;
              return (
                <li key={p.name}>
                  <button
                    type="button"
                    aria-pressed={picked}
                    onClick={() => {
                      setProvider(p.name);
                      setModel(null);
                      setTyped("");
                    }}
                    className={cx(
                      "flex w-full flex-col border-l-2 px-3 py-1.5 text-left font-mono text-[0.75rem]",
                      picked
                        ? "border-ink bg-selected text-ink"
                        : "border-transparent text-ink-muted hover:bg-hover",
                    )}
                  >
                    <span className="flex min-w-0 items-center gap-1.5">
                      <span className="truncate font-medium">
                        <Highlighted text={p.label} query={filter} />
                      </span>
                      {inUse ? <CurrentTag /> : null}
                    </span>
                    <span className="truncate text-ink-muted">
                      {p.name}
                      {picked && known.length > 0
                        ? ` · ${countText(known.length)}`
                        : (p.models?.length ?? 0) > 0
                          ? ` · ${countText(p.models?.length ?? 0)}`
                          : inUse && current.model
                            ? " · 1 model"
                            : p.configured
                              ? " · configured"
                              : " · no key"}
                    </span>
                  </button>
                </li>
              );
            })
          )}
        </ul>
        <div className="flex min-w-0 flex-1 flex-col overflow-y-auto">
          {shownProvider ? (
            <>
              <ul aria-label="Models" className="m-0 list-none p-0">
                {models.map((id) => (
                  <li key={id}>
                    <button
                      type="button"
                      aria-pressed={chosen === id}
                      onClick={() => setModel(id)}
                      onDoubleClick={() => switchTo(id)}
                      className="flex h-7 w-full items-center gap-2 px-3 text-left font-mono text-[0.75rem] text-ink-muted hover:bg-hover"
                    >
                      <Check
                        aria-hidden="true"
                        className={cx("size-3 shrink-0", chosen === id ? "text-ink" : "invisible")}
                      />
                      <span className="min-w-0 flex-1 truncate">
                        <Highlighted text={id} query={filter} />
                      </span>
                      {id === current.model && shownProvider === current.provider ? (
                        <CurrentTag />
                      ) : null}
                    </button>
                  </li>
                ))}
              </ul>
              <form
                className="flex items-center gap-2 px-3 py-2"
                onSubmit={(event) => {
                  event.preventDefault();
                  const id = typed.trim();
                  if (id) setModel(id);
                }}
              >
                <input
                  aria-label="Model id"
                  placeholder="model id"
                  className="type-data h-7 min-w-0 flex-1 border border-line bg-transparent px-2 text-[0.75rem] text-ink outline-none placeholder:text-ink-faint focus:border-ink"
                  value={typed}
                  onChange={(event) => {
                    setTyped(event.currentTarget.value);
                    const id = event.currentTarget.value.trim();
                    setModel(id || null);
                  }}
                />
              </form>
              <p
                className="type-serif m-0 px-3 text-[0.75rem] text-ink-soft"
                data-testid="model-list-hint"
              >
                {listHint}
              </p>
            </>
          ) : null}
        </div>
      </div>
      {error ? (
        <p role="alert" className="m-0 border-t border-line px-5 py-2 text-[0.75rem] text-danger">
          {errorText(error)}
        </p>
      ) : null}
      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line px-3 py-3">
        <p className="type-serif m-0 text-[0.6875rem] text-ink-muted">{note}</p>
        <div className="flex items-center gap-2">
          <Button
            size="lg"
            icon={
              <RefreshCw
                className={options.isFetching || listed.isFetching ? "animate-spin" : ""}
              />
            }
            onClick={() => {
              void options.refetch();
              if (source !== null) void listed.refetch();
            }}
          >
            Refresh Models
          </Button>
          <Button size="lg" onClick={() => onOpenChange(false)} disabled={busy}>
            Cancel
          </Button>
          <Button
            size="lg"
            tone="primary"
            disabled={!changed || busy}
            onClick={() => switchTo()}
            {...(busy ? { icon: <LoaderCircle className="animate-spin" /> } : {})}
          >
            Switch
          </Button>
        </div>
      </div>
    </Modal>
  );
}
