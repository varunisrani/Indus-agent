// "Switch model" dialog, shared by the chat rail and the Models page (W52-a / W52-b, one picker
// since the wave 53 integration). Providers on the left, models on the right, a fuzzy filter over
// both, the provider and model in use tagged CURRENT. Our server lists providers but not each
// provider's models, so the model column shows the model in use (for its provider) and a field to
// type a model id. The caller decides what applying means (`onApply`), and owns the request.

import { useQuery } from "@tanstack/react-query";
import { Check, LoaderCircle, RefreshCw, Search } from "lucide-react";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import { api } from "../lib/api.ts";
import type { ProviderOption } from "../lib/restTypes.ts";
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
  const models =
    shownProvider && shownProvider === current.provider && current.model
      ? [current.model].filter((m) => fuzzyHits(m, filter) !== null)
      : [];
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
                      {inUse && current.model
                        ? " · 1 models"
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
              <p className="type-serif m-0 px-3 text-[0.75rem] text-ink-soft">
                Type the model id as the provider names it; this install cannot list them.
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
            icon={<RefreshCw className={options.isFetching ? "animate-spin" : ""} />}
            onClick={() => void options.refetch()}
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
