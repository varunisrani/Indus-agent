// The composer's model picker (P20-23).
//
// A pick applies to the session and becomes this device's sticky choice for the profile, which new
// chats follow. It never writes the profile default, except the very first pick on a profile that
// has none. Each model keeps its own effort and fast presets, applied with it. Favourites sort
// first. Search ignores case and separators (`gpt4o` finds `gpt-4o`). A risky switch — another
// provider in a session that already has messages, or the daemon's `CONFIRM_REQUIRED` — asks first.

import type { MethodName, MethodTypes, ModelOptionsResult } from "@indus/protocol";
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type { ChatConnection, RequestOptions } from "../gateway/connectionStore.ts";
import { useGatewayConnection } from "../gateway/react.tsx";
import { useT } from "../i18n/index.tsx";
import type { StorageLike } from "./inflightJournal.ts";
import { errorMessage, errorName } from "./rpcErrors.ts";

export interface ModelEntry {
  readonly provider: string;
  readonly providerName: string;
  readonly model: string;
  readonly authenticated: boolean;
}

export interface ModelChoice {
  readonly provider: string;
  readonly model: string;
}

export interface ModelPreset {
  readonly effort?: string;
  readonly fast?: boolean;
}

export const EFFORTS = ["minimal", "low", "medium", "high", "xhigh", "none"] as const;

export const STICKY_PREFIX = "indus.desktop.model.";
export const PRESETS_KEY = "indus.desktop.model-presets";
export const FAVOURITES_KEY = "indus.desktop.model-favourites";
export const DEFAULT_CHECKED_PREFIX = "indus.desktop.model-default-checked.";

export const modelKey = (choice: ModelChoice): string => `${choice.provider}:${choice.model}`;

/** Lower case with every separator (space, dash, underscore, dot, slash, colon) removed. */
export function searchKey(text: string): string {
  return text.toLowerCase().replace(/[\s\-_./:]+/g, "");
}

/**
 * Entries matching `query`: the whole query with separators removed inside the model id
 * (`gpt4o` → `gpt-4o`), or every word of it somewhere in provider and model (`anthropic haiku`).
 */
export function searchModels(entries: readonly ModelEntry[], query: string): ModelEntry[] {
  const whole = searchKey(query);
  if (whole === "") return [...entries];
  const words = query
    .split(/\s+/)
    .map(searchKey)
    .filter((word) => word !== "");
  return entries.filter((entry) => {
    if (searchKey(entry.model).includes(whole)) return true;
    const haystack = searchKey(`${entry.providerName} ${entry.provider} ${entry.model}`);
    return words.every((word) => haystack.includes(word));
  });
}

/** Model rows from `model.options` (a provider may carry its known `models`). */
export function entriesFrom(options: ModelOptionsResult): ModelEntry[] {
  const rows: ModelEntry[] = [];
  for (const provider of options.providers) {
    const models = (provider as { models?: unknown }).models;
    const list = Array.isArray(models)
      ? models.filter((m): m is string => typeof m === "string" && m !== "")
      : [];
    if (provider.slug === options.provider && options.model && !list.includes(options.model)) {
      list.unshift(options.model);
    }
    for (const model of list) {
      rows.push({
        provider: provider.slug,
        providerName: provider.name,
        model,
        authenticated: provider.authenticated,
      });
    }
  }
  return rows;
}

/** Favourites first, then the catalogue order. */
export function orderEntries(entries: readonly ModelEntry[], favourites: ReadonlySet<string>) {
  const fav = entries.filter((entry) => favourites.has(modelKey(entry)));
  const rest = entries.filter((entry) => !favourites.has(modelKey(entry)));
  return [...fav, ...rest];
}

// -- per-device preferences ----------------------------------------------------------------------

function defaultStorage(): StorageLike | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

function readJson<V>(storage: StorageLike | null, key: string, fallback: V): V {
  if (storage === null) return fallback;
  try {
    const raw = storage.getItem(key);
    return raw === null ? fallback : (JSON.parse(raw) as V);
  } catch {
    return fallback;
  }
}

function writeJson(storage: StorageLike | null, key: string, value: unknown): void {
  try {
    storage?.setItem(key, JSON.stringify(value));
  } catch {
    // per-device conveniences are best effort
  }
}

export class ModelPrefs {
  private readonly storage: StorageLike | null;

  constructor(storage: StorageLike | null = defaultStorage()) {
    this.storage = storage;
  }

  private profileKey(profile: string | null): string {
    return profile ?? "default";
  }

  sticky(profile: string | null): ModelChoice | null {
    return readJson<ModelChoice | null>(
      this.storage,
      STICKY_PREFIX + this.profileKey(profile),
      null,
    );
  }

  setSticky(profile: string | null, choice: ModelChoice): void {
    writeJson(this.storage, STICKY_PREFIX + this.profileKey(profile), choice);
  }

  favourites(): Set<string> {
    return new Set(readJson<string[]>(this.storage, FAVOURITES_KEY, []));
  }

  toggleFavourite(key: string): Set<string> {
    const now = this.favourites();
    if (now.has(key)) now.delete(key);
    else now.add(key);
    writeJson(this.storage, FAVOURITES_KEY, [...now]);
    return now;
  }

  preset(key: string): ModelPreset {
    return readJson<Record<string, ModelPreset>>(this.storage, PRESETS_KEY, {})[key] ?? {};
  }

  setPreset(key: string, preset: ModelPreset): void {
    const all = readJson<Record<string, ModelPreset>>(this.storage, PRESETS_KEY, {});
    all[key] = preset;
    writeJson(this.storage, PRESETS_KEY, all);
  }

  defaultChecked(profile: string | null): boolean {
    return readJson(this.storage, DEFAULT_CHECKED_PREFIX + this.profileKey(profile), false);
  }

  markDefaultChecked(profile: string | null): void {
    writeJson(this.storage, DEFAULT_CHECKED_PREFIX + this.profileKey(profile), true);
  }
}

// -- applying a pick -----------------------------------------------------------------------------

type Request = <M extends MethodName>(
  method: M,
  params: MethodTypes[M]["params"],
  options?: RequestOptions,
) => Promise<MethodTypes[M]["result"]>;

export interface ApplyPick {
  readonly request: Request;
  readonly sessionId: string | null;
  readonly profile: string | null;
  readonly choice: ModelChoice;
  /** The session's model now, if known. */
  readonly current: ModelChoice | null;
  /** The session already has messages. */
  readonly hasHistory: boolean;
  readonly prefs: ModelPrefs;
  /** Ask the person; resolves true to go ahead. */
  confirm(message: string): Promise<boolean>;
}

export type ApplyOutcome = "applied" | "deferred" | "sticky" | "cancelled" | "failed";

function isEmptyDefault(value: unknown): boolean {
  if (value === null || value === undefined) return true;
  if (typeof value === "string") return value.trim() === "";
  if (typeof value === "object") {
    const inner =
      (value as { default?: unknown; model?: unknown }).default ??
      (value as { model?: unknown }).model;
    return inner === undefined || inner === null || String(inner).trim() === "";
  }
  return false;
}

/** The first pick on a profile without a default writes it, once; later picks never do. */
async function writeDefaultIfNone(pick: ApplyPick, value: string): Promise<void> {
  if (pick.prefs.defaultChecked(pick.profile)) return;
  let empty = false;
  try {
    const current = await pick.request("config.get", { key: "model.default" });
    empty = isEmptyDefault(current.value);
  } catch {
    return; // unknown: try again on the next pick
  }
  if (empty) {
    try {
      await pick.request("config.set", { key: "model", value });
    } catch {
      return;
    }
  }
  pick.prefs.markDefaultChecked(pick.profile);
}

export async function applyModelPick(
  pick: ApplyPick,
): Promise<{ outcome: ApplyOutcome; error?: string }> {
  const { choice, sessionId, request } = pick;
  const value = modelKey(choice);
  const crossesProvider =
    pick.current !== null &&
    pick.current.provider !== "" &&
    pick.current.provider !== choice.provider;
  if (sessionId !== null && pick.hasHistory && crossesProvider) {
    if (!(await pick.confirm("provider"))) return { outcome: "cancelled" };
  }
  pick.prefs.setSticky(pick.profile, choice);
  if (sessionId === null) {
    await writeDefaultIfNone(pick, value);
    return { outcome: "sticky" };
  }
  const params = { key: "model" as const, value, session_id: sessionId };
  let applied: string;
  try {
    applied = (await request("config.set", params)).applied;
  } catch (error) {
    if (errorName(error) !== "CONFIRM_REQUIRED")
      return { outcome: "failed", error: errorMessage(error) };
    if (!(await pick.confirm(errorMessage(error)))) return { outcome: "cancelled" };
    try {
      applied = (await request("config.set", { ...params, confirm: true })).applied;
    } catch (again) {
      return { outcome: "failed", error: errorMessage(again) };
    }
  }
  const preset = pick.prefs.preset(value);
  if (preset.effort !== undefined) {
    await request("config.set", {
      key: "reasoning",
      value: preset.effort,
      session_id: sessionId,
    }).catch(() => {});
  }
  if (preset.fast !== undefined) {
    await request("config.set", { key: "fast", value: preset.fast, session_id: sessionId }).catch(
      () => {},
    );
  }
  await writeDefaultIfNone(pick, value);
  return { outcome: applied === "deferred" ? "deferred" : "applied" };
}

// -- the component -------------------------------------------------------------------------------

export interface ModelPickerProps {
  sessionId: string | null;
  profile: string | null;
  connection?: ChatConnection | null;
  /** The session's model and effort now (from `session.info`). */
  current?: (ModelChoice & { effort?: string | null }) | null;
  hasHistory?: boolean;
  prefs?: ModelPrefs;
  /** Opened from outside (the `/model` command). */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}

type Confirming = { message: string; resolve: (ok: boolean) => void };

export function ModelPicker(props: ModelPickerProps) {
  const t = useT();
  const active = useGatewayConnection();
  const connection = props.connection === undefined ? active : props.connection;
  const prefs = useMemo(() => props.prefs ?? new ModelPrefs(), [props.prefs]);
  const [openState, setOpenState] = useState(false);
  const open = props.open ?? openState;
  const setOpen = useCallback(
    (next: boolean) => {
      setOpenState(next);
      props.onOpenChange?.(next);
    },
    [props.onOpenChange],
  );
  const [options, setOptions] = useState<ModelOptionsResult | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [favourites, setFavourites] = useState<Set<string>>(() => prefs.favourites());
  const [confirming, setConfirming] = useState<Confirming | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [chosen, setChosen] = useState<ModelChoice | null>(null);
  const [focusKey, setFocusKey] = useState<string | null>(null);
  const [, setPresetVersion] = useState(0);
  const bumpPresets = () => setPresetVersion((v) => v + 1);
  const listId = useId();
  const searchRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    if (connection === null) return;
    setLoadError(null);
    try {
      setOptions(await connection.request("model.options", { session_id: props.sessionId }));
    } catch (error) {
      setLoadError(errorMessage(error));
    }
  }, [connection, props.sessionId]);

  useEffect(() => {
    if (open) {
      void load();
      searchRef.current?.focus();
    }
  }, [open, load]);

  const current: ModelChoice | null =
    chosen ??
    props.current ??
    (options ? { provider: options.provider, model: options.model } : null) ??
    prefs.sticky(props.profile);
  const entries = useMemo(
    () => orderEntries(searchModels(options ? entriesFrom(options) : [], query), favourites),
    [options, query, favourites],
  );

  const pick = async (entry: ModelEntry) => {
    if (connection === null) return;
    setNote(null);
    const choice = { provider: entry.provider, model: entry.model };
    const result = await applyModelPick({
      request: connection.request.bind(connection) as Request,
      sessionId: props.sessionId,
      profile: props.profile,
      choice,
      current,
      hasHistory: props.hasHistory ?? false,
      prefs,
      confirm: (message) =>
        new Promise<boolean>((resolve) => {
          setConfirming({
            message:
              message === "provider" ? t("model.confirmProvider", { model: entry.model }) : message,
            resolve,
          });
        }),
    });
    setConfirming(null);
    if (result.outcome === "failed") {
      setNote(t("model.failed", { message: result.error ?? "" }));
      return;
    }
    if (result.outcome === "cancelled") return;
    setChosen(choice);
    if (result.outcome === "deferred") setNote(t("model.deferred", { model: entry.model }));
    setOpen(false);
  };

  const effort =
    props.current?.effort ?? (current ? prefs.preset(modelKey(current)).effort : undefined);
  const focused = entries.find((entry) => modelKey(entry) === focusKey) ?? null;
  const focusedPreset = focused ? prefs.preset(modelKey(focused)) : {};

  return (
    <div className="relative inline-block" data-testid="model-picker">
      <button
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        className="rounded-full border border-line px-2 py-0.5 text-xs hover:bg-raised"
        onClick={() => setOpen(!open)}
      >
        {current ? current.model : t("model.none")}
        {effort ? ` · ${effort}` : ""}
      </button>
      {note !== null ? <span className="ml-2 text-xs text-muted">{note}</span> : null}
      {open ? (
        <div
          role="dialog"
          aria-label={t("model.title")}
          className="absolute bottom-full z-30 mb-1 w-80 rounded-lg border border-line bg-raised p-2 shadow-lg"
          onKeyDown={(event) => {
            if (event.key === "Escape") setOpen(false);
          }}
        >
          <input
            ref={searchRef}
            type="search"
            aria-label={t("model.search")}
            aria-controls={listId}
            placeholder={t("model.search")}
            className="mb-2 w-full rounded border border-line bg-bg px-2 py-1 text-sm"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          {loadError !== null ? (
            <p role="alert" className="text-xs text-danger">
              {t("model.loadFailed", { message: loadError })}{" "}
              <button type="button" className="underline" onClick={() => void load()}>
                {t("model.retry")}
              </button>
            </p>
          ) : null}
          <ul id={listId} aria-label={t("model.title")} className="max-h-64 overflow-auto">
            {entries.map((entry) => {
              const key = modelKey(entry);
              const selected = current !== null && modelKey(current) === key;
              return (
                <li
                  key={key}
                  data-model={key}
                  className="flex items-center gap-2 rounded px-1 text-sm hover:bg-bg"
                >
                  <button
                    type="button"
                    aria-label={t(favourites.has(key) ? "model.unfavourite" : "model.favourite", {
                      model: entry.model,
                    })}
                    aria-pressed={favourites.has(key)}
                    className="text-xs"
                    onClick={() => setFavourites(prefs.toggleFavourite(key))}
                  >
                    {favourites.has(key) ? "★" : "☆"}
                  </button>
                  <button
                    type="button"
                    className="flex-1 truncate text-left py-1"
                    aria-current={selected ? "true" : undefined}
                    onFocus={() => setFocusKey(key)}
                    onMouseEnter={() => setFocusKey(key)}
                    onClick={() => void pick(entry)}
                  >
                    {entry.model}
                    <span className="ml-2 text-xs text-muted">{entry.providerName}</span>
                  </button>
                </li>
              );
            })}
            {options !== null && entries.length === 0 ? (
              <li className="px-1 py-1 text-xs text-muted">{t("model.noMatch")}</li>
            ) : null}
          </ul>
          {focused !== null ? (
            <div className="mt-2 flex items-center gap-2 border-t border-line pt-2 text-xs">
              <label className="flex items-center gap-1">
                {t("model.effort")}
                <select
                  aria-label={t("model.effortFor", { model: focused.model })}
                  value={focusedPreset.effort ?? ""}
                  onChange={(event) => {
                    const { effort: _previous, ...rest } = focusedPreset;
                    const value = event.target.value;
                    prefs.setPreset(
                      modelKey(focused),
                      value === "" ? rest : { ...rest, effort: value },
                    );
                    bumpPresets();
                  }}
                >
                  <option value="">{t("model.effortKeep")}</option>
                  {EFFORTS.map((level) => (
                    <option key={level} value={level}>
                      {level}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex items-center gap-1">
                <input
                  type="checkbox"
                  aria-label={t("model.fastFor", { model: focused.model })}
                  checked={focusedPreset.fast === true}
                  onChange={(event) => {
                    prefs.setPreset(modelKey(focused), {
                      ...focusedPreset,
                      fast: event.target.checked,
                    });
                    bumpPresets();
                  }}
                />
                {t("model.fast")}
              </label>
            </div>
          ) : null}
          {confirming !== null ? (
            <div
              role="alertdialog"
              aria-label={t("model.confirmTitle")}
              className="mt-2 rounded border border-warn p-2 text-xs"
            >
              <p>{confirming.message}</p>
              <div className="mt-1 flex justify-end gap-2">
                <button type="button" onClick={() => confirming.resolve(false)}>
                  {t("model.keep")}
                </button>
                <button
                  type="button"
                  className="rounded bg-accent px-2 text-accent-fg"
                  onClick={() => confirming.resolve(true)}
                >
                  {t("model.switch")}
                </button>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
