// The composer's model picker (P20-23).
//
// A pick applies to the session and becomes this device's sticky choice for the profile, which new
// chats follow. It never writes the profile default, except the very first pick on a profile that
// has none. Each model keeps its own effort and fast presets, applied with it. Favourites sort
// first. Search ignores case and separators (`gpt4o` finds `gpt-4o`). A risky switch — another
// provider in a session that already has messages, or the daemon's `CONFIRM_REQUIRED` — asks first.

import type { MethodName, MethodTypes, ModelOptionsResult } from "@indus/protocol";
import {
  ChevronLeft,
  ChevronRight,
  LoaderCircle,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  Star,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import type { ChatConnection, RequestOptions } from "../gateway/connectionStore.ts";
import { useGatewayConnection } from "../gateway/react.tsx";
import { useT } from "../i18n/index.tsx";
import { useKeyAction } from "../settings/keyActions.ts";
import { Button, Chip, MenuLabel, MenuRow, MenuSeparator } from "../ui/components.tsx";
import { EffortChip, EffortOptions, stepEffort } from "./EffortChip.tsx";
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

export const EFFORTS = [
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
  "ultra",
  "none",
] as const;

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

interface ReadySource {
  subscribe(listener: () => void): () => void;
  getState(): { ready: boolean };
}

function hasReady(connection: unknown): connection is ReadySource {
  const candidate = connection as Partial<ReadySource> | null;
  return typeof candidate?.subscribe === "function" && typeof candidate.getState === "function";
}

/** Whether the backend answers yet (connections without a state, such as fakes, always do). */
function useConnectionReady(connection: ChatConnection | null): boolean {
  return useSyncExternalStore(
    (listener) => (hasReady(connection) ? connection.subscribe(listener) : () => undefined),
    () => {
      if (connection === null) return false;
      return hasReady(connection) ? connection.getState().ready : true;
    },
  );
}

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
  /** "Edit models…": open the model settings (no row without it). */
  onEditModels?: () => void;
}

type Confirming = { message: string; resolve: (ok: boolean) => void };

interface Group {
  readonly key: string;
  readonly label: string;
  readonly rows: readonly ModelEntry[];
}

/** Favourites first (when any), then one group per provider in catalogue order. */
export function groupEntries(
  entries: readonly ModelEntry[],
  favourites: ReadonlySet<string>,
  favouritesLabel: string,
): Group[] {
  const groups: Group[] = [];
  const fav = entries.filter((entry) => favourites.has(modelKey(entry)));
  if (fav.length > 0) groups.push({ key: "fav", label: favouritesLabel, rows: fav });
  const byProvider = new Map<string, ModelEntry[]>();
  for (const entry of entries) {
    const list = byProvider.get(entry.provider) ?? [];
    list.push(entry);
    byProvider.set(entry.provider, list);
  }
  for (const [provider, rows] of byProvider) {
    groups.push({ key: `p:${provider}`, label: rows[0]?.providerName ?? provider, rows });
  }
  return groups;
}

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
  const [detail, setDetail] = useState<ModelEntry | null>(null);
  const [custom, setCustom] = useState(false);
  const [, setPresetVersion] = useState(0);
  const bumpPresets = () => setPresetVersion((v) => v + 1);
  const listId = useId();
  const searchRef = useRef<HTMLInputElement>(null);
  const root = useRef<HTMLDivElement>(null);

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
    } else {
      setDetail(null);
      setCustom(false);
    }
  }, [open, load]);

  // click-away closes (the trigger toggles itself)
  useEffect(() => {
    if (!open) return;
    const away = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", away);
    return () => document.removeEventListener("pointerdown", away);
  }, [open, setOpen]);

  // W51-b: ask once up front so the chip names the configured model before the picker is opened
  // (it said "Choose a model" while the status bar already named one). Asked again when the
  // backend becomes ready; a failure leaves the chip as it was and the picker asks on open.
  const ready = useConnectionReady(connection);
  useEffect(() => {
    if (!ready || connection === null || props.current) return;
    let live = true;
    connection.request("model.options", { session_id: props.sessionId }).then(
      (result) => {
        if (live) setOptions((now) => now ?? result);
      },
      () => undefined,
    );
    return () => {
      live = false;
    };
  }, [ready, connection, props.sessionId, props.current]);

  const current: ModelChoice | null =
    chosen ??
    props.current ??
    (options ? { provider: options.provider, model: options.model } : null) ??
    prefs.sticky(props.profile);
  const entries = useMemo(
    () => orderEntries(searchModels(options ? entriesFrom(options) : [], query), favourites),
    [options, query, favourites],
  );
  const groups = useMemo(
    () => groupEntries(entries, favourites, t("model.favourites")),
    [entries, favourites, t],
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

  const pickCustom = () => {
    const id = query.trim();
    if (id === "") return;
    const provider = current?.provider ?? options?.provider ?? "";
    const known = options?.providers.find((p) => p.slug === provider);
    void pick({
      provider,
      providerName: known?.name ?? provider,
      model: id,
      authenticated: true,
    });
  };

  const effort =
    props.current?.effort ?? (current ? prefs.preset(modelKey(current)).effort : undefined);
  const setEffort = (key: string, value: string | null) => {
    const { effort: _previous, ...rest } = prefs.preset(key);
    prefs.setPreset(key, value === null ? rest : { ...rest, effort: value });
    bumpPresets();
  };

  /** The composer's effort choice: this model's preset, and the running session when there is one. */
  const pickEffort = (value: string | null) => {
    if (current === null) return;
    setEffort(modelKey(current), value);
    if (connection !== null && props.sessionId !== null && value !== null) {
      void connection
        .request("config.set", { key: "reasoning", value, session_id: props.sessionId })
        .catch(() => {});
    }
  };

  // the rebindable keys (W59-K1-2): open this picker, step the effort up or down
  useKeyAction(["model.picker", "effort.up", "effort.down"], (id) => {
    if (id === "model.picker") {
      setOpen(true);
      return true;
    }
    if (current === null) return false;
    pickEffort(stepEffort(effort ?? null, id === "effort.up" ? 1 : -1));
    return true;
  });

  const onMenuKey = (event: React.KeyboardEvent) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      if (detail !== null) setDetail(null);
      else setOpen(false);
      return;
    }
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    const rows = [
      ...(root.current?.querySelectorAll<HTMLElement>(
        ".model-menu .model-row-name, .model-menu [role^='menuitem']",
      ) ?? []),
    ];
    if (rows.length === 0) return;
    const at = rows.indexOf(document.activeElement as HTMLElement);
    const step = event.key === "ArrowDown" ? 1 : -1;
    rows[
      at === -1 ? (step === 1 ? 0 : rows.length - 1) : (at + step + rows.length) % rows.length
    ]?.focus();
    event.preventDefault();
  };

  const detailPreset = detail ? prefs.preset(modelKey(detail)) : {};

  return (
    <div className="model-picker" data-testid="model-picker" ref={root}>
      <Chip
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={current ? undefined : t("model.loading")}
        onClick={() => setOpen(!open)}
      >
        {current ? current.model : <LoaderCircle aria-hidden="true" size={12} className="spin" />}
      </Chip>
      {current !== null && <EffortChip effort={effort ?? null} onPick={pickEffort} />}
      {note !== null ? <span className="model-picker-note">{note}</span> : null}
      {open ? (
        <div
          role="dialog"
          aria-label={t("model.title")}
          className="desk-menu model-menu"
          onKeyDown={onMenuKey}
        >
          {detail !== null ? (
            <div role="menu" aria-label={t("model.optionsFor", { model: detail.model })}>
              <MenuRow
                icon={<ChevronLeft aria-hidden="true" />}
                label={detail.model}
                ariaLabel={t("model.back")}
                onSelect={() => setDetail(null)}
              />
              <MenuSeparator />
              <EffortOptions
                effort={detailPreset.effort ?? null}
                onPick={(value) => setEffort(modelKey(detail), value)}
              />
              <MenuSeparator />
              <MenuRow
                kind="switch"
                label={t("model.fast")}
                ariaLabel={t("model.fastFor", { model: detail.model })}
                checked={detailPreset.fast === true}
                onSelect={() => {
                  prefs.setPreset(modelKey(detail), {
                    ...detailPreset,
                    fast: detailPreset.fast !== true,
                  });
                  bumpPresets();
                }}
              />
            </div>
          ) : (
            <>
              <div className="model-menu-search">
                {custom ? (
                  <Plus aria-hidden="true" size={13} />
                ) : (
                  <Search aria-hidden="true" size={13} />
                )}
                <input
                  ref={searchRef}
                  type="search"
                  aria-label={t(custom ? "model.custom" : "model.search")}
                  aria-controls={listId}
                  placeholder={t(custom ? "model.customPlaceholder" : "model.search")}
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && custom) {
                      event.preventDefault();
                      pickCustom();
                    }
                  }}
                />
              </div>
              {loadError !== null ? (
                <p role="alert" className="model-menu-note">
                  {t("model.loadFailed", { message: loadError })}{" "}
                  <button
                    type="button"
                    className="desk-btn"
                    data-variant="text"
                    onClick={() => void load()}
                  >
                    {t("model.retry")}
                  </button>
                </p>
              ) : null}
              <div id={listId}>
                {groups.map((group) => (
                  <div key={group.key} className="model-group">
                    <MenuLabel>{group.label}</MenuLabel>
                    <ul aria-label={group.label} className="model-menu-list">
                      {group.rows.map((entry) => {
                        const key = modelKey(entry);
                        const selected = current !== null && modelKey(current) === key;
                        const starred = favourites.has(key);
                        return (
                          <li
                            key={key}
                            data-model={key}
                            className="model-row"
                            data-dim={entry.authenticated ? undefined : "true"}
                          >
                            <button
                              type="button"
                              className="model-star"
                              aria-label={t(starred ? "model.unfavourite" : "model.favourite", {
                                model: entry.model,
                              })}
                              aria-pressed={starred}
                              onClick={() => setFavourites(prefs.toggleFavourite(key))}
                            >
                              <Star aria-hidden="true" />
                            </button>
                            <button
                              type="button"
                              className="model-row-name"
                              aria-current={selected ? "true" : undefined}
                              onClick={() => void pick(entry)}
                              onKeyDown={(event) => {
                                if (event.key === "ArrowRight") {
                                  event.preventDefault();
                                  setDetail(entry);
                                } else if (event.key === "Enter" && event.shiftKey) {
                                  event.preventDefault();
                                  setFavourites(prefs.toggleFavourite(key));
                                }
                              }}
                            >
                              {entry.model}
                            </button>
                            {!entry.authenticated && (
                              <span className="model-row-note">{t("model.signIn")}</span>
                            )}
                            <button
                              type="button"
                              className="model-row-more"
                              aria-label={t("model.optionsFor", { model: entry.model })}
                              onClick={() => setDetail(entry)}
                            >
                              <ChevronRight aria-hidden="true" />
                            </button>
                          </li>
                        );
                      })}
                    </ul>
                  </div>
                ))}
                {options !== null && entries.length === 0 && !custom ? (
                  <p className="model-menu-note">{t("model.noMatch")}</p>
                ) : null}
              </div>
              <MenuSeparator />
              <div role="menu" aria-label={t("model.title")}>
                <MenuRow
                  icon={<RefreshCw aria-hidden="true" />}
                  label={t("model.refresh")}
                  onSelect={() => void load()}
                />
                <MenuRow
                  icon={<Plus aria-hidden="true" />}
                  label={t("model.addCustom")}
                  onSelect={() => {
                    setCustom(true);
                    setQuery("");
                    searchRef.current?.focus();
                  }}
                />
                {props.onEditModels !== undefined && (
                  <MenuRow
                    icon={<Settings2 aria-hidden="true" />}
                    label={t("model.edit")}
                    onSelect={() => {
                      setOpen(false);
                      props.onEditModels?.();
                    }}
                  />
                )}
              </div>
            </>
          )}
          {confirming !== null ? (
            <div role="alertdialog" aria-label={t("model.confirmTitle")} className="model-confirm">
              <p>{confirming.message}</p>
              <div className="model-confirm-actions">
                <Button variant="ghost" size="sm" onClick={() => confirming.resolve(false)}>
                  {t("model.keep")}
                </Button>
                <Button variant="primary" size="sm" onClick={() => confirming.resolve(true)}>
                  {t("model.switch")}
                </Button>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
