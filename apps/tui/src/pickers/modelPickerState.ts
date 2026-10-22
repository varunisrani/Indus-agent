// Model picker as a pure state machine (provider → model → effort, side stages key and
// disconnect) plus the apply flow that talks to the bridge.

import { GatewayRpcError } from "@indus/client";
import type { ConfigSetResult, ModelOptionsResult, ProviderOption } from "@indus/protocol";
import { digitOf, isCtrl, isErase, type KeyFlags, typedText } from "../cards/keys.ts";
import type { ConfirmOptions } from "../cards/openCards.ts";
import { errorText, type RpcLike } from "../cards/rpcLike.ts";

export const VISIBLE_ROWS = 12;

export interface EffortRow {
  label: string;
  value: string;
}

/** Reasoning effort rows; the empty value keeps whatever the session has. */
export const EFFORT_ROWS: readonly EffortRow[] = [
  { label: "minimal", value: "minimal" },
  { label: "low", value: "low" },
  { label: "medium", value: "medium" },
  { label: "high", value: "high" },
  { label: "extra high", value: "xhigh" },
  { label: "off", value: "none" },
  { label: "keep the current effort", value: "" },
];
const KEEP_EFFORT = EFFORT_ROWS.length - 1;

export type Stage = "provider" | "model" | "effort" | "key" | "disconnect";
export type Scope = "session" | "global";

export interface ProviderRow {
  slug: string;
  name: string;
  authType: string;
  authenticated: boolean;
  current: boolean;
  /** Known model ids; empty means "type the id". */
  models: string[];
}

export interface PickerState {
  stage: Stage;
  loading: boolean;
  error: string | null;
  /** A one-line note under the list (sign-in hints, save failures). */
  note: string | null;
  providers: ProviderRow[];
  currentProvider: string;
  currentModel: string;
  filter: string;
  /** Index into the filtered provider list. */
  providerIndex: number;
  /** Index into the filtered model list. */
  modelIndex: number;
  effortIndex: number;
  /** Provider chosen for the model, key and disconnect stages. */
  slug: string | null;
  pendingModel: string;
  scope: Scope;
  allowGlobal: boolean;
  /** Characters typed in the key stage; the key itself sits in a `SecretBuffer`. */
  keyLength: number;
  working: boolean;
}

export type PickerEffect =
  | { type: "apply"; provider: string; model: string; effort: string; scope: Scope }
  | { type: "saveKey"; slug: string }
  | { type: "disconnect"; slug: string }
  | { type: "close" }
  | null;

export interface PickerResult {
  state: PickerState;
  effect: PickerEffect;
}

/** Holds a typed API key outside React state and every store; `take` empties it. */
export class SecretBuffer {
  private value = "";
  get length(): number {
    return [...this.value].length;
  }
  append(text: string): void {
    this.value += text;
  }
  erase(): void {
    this.value = [...this.value].slice(0, -1).join("");
  }
  clear(): void {
    this.value = "";
  }
  take(): string {
    const value = this.value.trim();
    this.value = "";
    return value;
  }
}

export function initialPicker(allowGlobal = false): PickerState {
  return {
    stage: "provider",
    loading: true,
    error: null,
    note: null,
    providers: [],
    currentProvider: "",
    currentModel: "",
    filter: "",
    providerIndex: 0,
    modelIndex: 0,
    effortIndex: KEEP_EFFORT,
    slug: null,
    pendingModel: "",
    scope: "session",
    allowGlobal,
    keyLength: 0,
    working: false,
  };
}

export function rowOf(option: ProviderOption, currentProvider = ""): ProviderRow {
  // `models` is filled for the current custom or local endpoint (what its `/models` lists).
  const listed: unknown = option.models;
  const models = Array.isArray(listed)
    ? listed.filter((m): m is string => typeof m === "string" && m !== "")
    : [];
  return {
    slug: option.slug,
    name: option.name || option.slug,
    authType: option.auth_type,
    authenticated: option.authenticated,
    current: option.current === true || (currentProvider !== "" && option.slug === currentProvider),
    models,
  };
}

export function optionsLoaded(state: PickerState, result: ModelOptionsResult): PickerState {
  const providers = (result.providers ?? []).map((option) => rowOf(option, result.provider));
  return {
    ...state,
    loading: false,
    error: null,
    providers,
    currentProvider: result.provider ?? "",
    currentModel: result.model ?? "",
    providerIndex: Math.max(
      0,
      providers.findIndex((row) => row.current),
    ),
  };
}

export const optionsFailed = (state: PickerState, message: string): PickerState => ({
  ...state,
  loading: false,
  error: message,
});

const matches = (haystack: string, filter: string): boolean => {
  const terms = filter.toLowerCase().split(/\s+/).filter(Boolean);
  const text = haystack.toLowerCase();
  return terms.every((term) => text.includes(term));
};

export function visibleProviders(state: PickerState): ProviderRow[] {
  if (state.stage !== "provider" || state.filter.trim() === "") return state.providers;
  return state.providers.filter((row) =>
    matches(`${row.name} ${row.slug} ${row.models.join(" ")}`, state.filter),
  );
}

export const chosenProvider = (state: PickerState): ProviderRow | undefined =>
  state.slug === null ? undefined : state.providers.find((row) => row.slug === state.slug);

export function visibleModels(state: PickerState): string[] {
  const models = chosenProvider(state)?.models ?? [];
  if (state.stage !== "model" || state.filter.trim() === "") return models;
  return models.filter((model) => matches(model, state.filter));
}

/** The slice of a list to show so that `selected` stays in view. */
export function windowOf(
  count: number,
  selected: number,
  size = VISIBLE_ROWS,
): { start: number; end: number } {
  if (count <= size) return { start: 0, end: count };
  const start = Math.min(Math.max(0, selected - Math.floor(size / 2)), count - size);
  return { start, end: start + size };
}

const keep = (state: PickerState): PickerResult => ({ state, effect: null });

function toProviders(state: PickerState, buffer: SecretBuffer): PickerState {
  buffer.clear();
  const at = state.providers.findIndex((row) => row.slug === state.slug);
  return {
    ...state,
    stage: "provider",
    filter: "",
    note: null,
    keyLength: 0,
    working: false,
    pendingModel: "",
    providerIndex: at >= 0 ? at : state.providerIndex,
  };
}

function openModels(state: PickerState, row: ProviderRow): PickerState {
  const at = row.slug === state.currentProvider ? row.models.indexOf(state.currentModel) : -1;
  return {
    ...state,
    stage: "model",
    slug: row.slug,
    filter: "",
    note: null,
    modelIndex: Math.max(0, at),
  };
}

const canTakeKey = (row: ProviderRow): boolean => row.authType === "api_key";

/** Shared list keys: move, filter edit. `count` is the filtered length. */
function listKey(
  state: PickerState,
  input: string,
  key: KeyFlags,
  count: number,
  field: "providerIndex" | "modelIndex",
): PickerState | null {
  const index = state[field];
  if (key.upArrow) return { ...state, [field]: Math.max(0, index - 1) };
  if (key.downArrow) return { ...state, [field]: Math.max(0, Math.min(count - 1, index + 1)) };
  if (key.pageUp) return { ...state, [field]: Math.max(0, index - VISIBLE_ROWS) };
  if (key.pageDown)
    return { ...state, [field]: Math.max(0, Math.min(count - 1, index + VISIBLE_ROWS)) };
  if (isErase(key)) return { ...state, filter: state.filter.slice(0, -1), [field]: 0 };
  if (isCtrl(input, key, "u")) return { ...state, filter: "", [field]: 0 };
  if (isCtrl(input, key, "g")) {
    if (!state.allowGlobal) return state;
    return { ...state, scope: state.scope === "session" ? "global" : "session" };
  }
  const text = typedText(input, key);
  if (text) return { ...state, filter: state.filter + text, [field]: 0, note: null };
  return null;
}

/** Apply one key. */
export function pickerStep(
  state: PickerState,
  input: string,
  key: KeyFlags,
  buffer: SecretBuffer,
): PickerResult {
  if (state.loading || state.error !== null) {
    return key.escape || isCtrl(input, key, "c")
      ? { state, effect: { type: "close" } }
      : keep(state);
  }
  if (state.working) return keep(state);

  switch (state.stage) {
    case "provider": {
      const rows = visibleProviders(state);
      const row = rows[state.providerIndex];
      if (key.escape) {
        if (state.filter !== "") {
          const at = row ? state.providers.indexOf(row) : 0;
          return keep({ ...state, filter: "", providerIndex: Math.max(0, at) });
        }
        return { state, effect: { type: "close" } };
      }
      if (isCtrl(input, key, "c")) return { state, effect: { type: "close" } };
      if (key.return) {
        if (row === undefined) return keep(state);
        if (row.authenticated) return keep(openModels(state, row));
        if (canTakeKey(row))
          return keep({ ...state, stage: "key", slug: row.slug, filter: "", note: null });
        return keep({
          ...state,
          note: `${row.name} needs a sign-in: run \`indus model\` in a shell first`,
        });
      }
      if (isCtrl(input, key, "d")) {
        if (row?.authenticated)
          return keep({ ...state, stage: "disconnect", slug: row.slug, filter: "" });
        return keep(state);
      }
      if (isCtrl(input, key, "k")) {
        if (row && canTakeKey(row))
          return keep({ ...state, stage: "key", slug: row.slug, filter: "" });
        return keep(state);
      }
      return keep(listKey(state, input, key, rows.length, "providerIndex") ?? state);
    }

    case "model": {
      const models = visibleModels(state);
      if (key.escape) {
        if (state.filter !== "") return keep({ ...state, filter: "", modelIndex: 0 });
        return keep(toProviders(state, buffer));
      }
      if (isCtrl(input, key, "c")) return { state, effect: { type: "close" } };
      if (key.return) {
        const picked = models[state.modelIndex] ?? state.filter.trim();
        if (picked === "") return keep(state);
        return keep({ ...state, stage: "effort", pendingModel: picked, effortIndex: KEEP_EFFORT });
      }
      return keep(listKey(state, input, key, models.length, "modelIndex") ?? state);
    }

    case "effort": {
      if (key.escape) return keep({ ...state, stage: "model", pendingModel: "" });
      if (isCtrl(input, key, "c")) return { state, effect: { type: "close" } };
      if (key.upArrow) return keep({ ...state, effortIndex: Math.max(0, state.effortIndex - 1) });
      if (key.downArrow) {
        return keep({
          ...state,
          effortIndex: Math.min(EFFORT_ROWS.length - 1, state.effortIndex + 1),
        });
      }
      const digit = digitOf(input, key);
      if (digit !== null && digit <= EFFORT_ROWS.length)
        return keep({ ...state, effortIndex: digit - 1 });
      if (isCtrl(input, key, "g") && state.allowGlobal) {
        return keep({ ...state, scope: state.scope === "session" ? "global" : "session" });
      }
      if (key.return && state.slug !== null) {
        return {
          state: { ...state, working: true },
          effect: {
            type: "apply",
            provider: state.slug,
            model: state.pendingModel,
            effort: EFFORT_ROWS[state.effortIndex]?.value ?? "",
            scope: state.scope,
          },
        };
      }
      return keep(state);
    }

    case "key": {
      if (key.escape) return keep(toProviders(state, buffer));
      if (isCtrl(input, key, "c")) {
        buffer.clear();
        return { state: { ...state, keyLength: 0 }, effect: { type: "close" } };
      }
      if (key.return) {
        if (buffer.length === 0 || state.slug === null) return keep(state);
        return {
          state: { ...state, working: true, note: null },
          effect: { type: "saveKey", slug: state.slug },
        };
      }
      if (isErase(key)) buffer.erase();
      else if (isCtrl(input, key, "u")) buffer.clear();
      else {
        const text = typedText(input, key);
        if (!text) return keep(state);
        buffer.append(text);
      }
      return keep({ ...state, keyLength: buffer.length });
    }

    case "disconnect": {
      const letter = key.ctrl ? "" : input.toLowerCase();
      if (key.escape || letter === "n") return keep(toProviders(state, buffer));
      if (isCtrl(input, key, "c")) return { state, effect: { type: "close" } };
      if ((key.return || letter === "y") && state.slug !== null) {
        return {
          state: { ...state, working: true },
          effect: { type: "disconnect", slug: state.slug },
        };
      }
      return keep(state);
    }
  }
}

/** The key was stored: the provider row is refreshed and its models open. */
export function keySaved(state: PickerState, option: ProviderOption): PickerState {
  const row = rowOf(option, state.currentProvider);
  const providers = state.providers.map((p) => (p.slug === row.slug ? row : p));
  const next = { ...state, providers, keyLength: 0, working: false, note: null };
  return row.authenticated
    ? openModels(next, row)
    : {
        ...next,
        stage: "provider",
        note: "the key was stored but the provider still reports no sign-in",
      };
}

export const keyFailed = (state: PickerState, message: string): PickerState => ({
  ...state,
  working: false,
  keyLength: 0,
  note: `the key was not saved: ${message}`,
});

export function disconnected(state: PickerState, slug: string, removed: boolean): PickerState {
  const providers = state.providers.map((row) =>
    row.slug === slug && removed ? { ...row, authenticated: false } : row,
  );
  const at = providers.findIndex((row) => row.slug === slug);
  return {
    ...state,
    providers,
    stage: "provider",
    working: false,
    filter: "",
    providerIndex: at >= 0 ? at : 0,
    note: removed
      ? "signed out; the stored key was removed"
      : "nothing was stored for that provider",
  };
}

// -- applying a choice -----------------------------------------------------------------------------

export interface ApplyHooks {
  confirm(options: ConfirmOptions): Promise<boolean>;
  systemLine(text: string): void;
}

export interface ModelChoice {
  sessionId: string | null;
  /** Provider slug, or "" when `model` is a full spec already. */
  provider: string;
  model: string;
  /** Reasoning effort to set afterwards; "" keeps the current one. */
  effort?: string;
}

export type ApplyOutcome = "applied" | "deferred" | "cancelled" | "failed";

const isConfirmRequired = (error: unknown): error is GatewayRpcError =>
  error instanceof GatewayRpcError && error.errorName === "CONFIRM_REQUIRED";

/** Switch the session's model; asks for confirmation when the bridge wants one. */
export async function applyModelChoice(
  rpc: RpcLike,
  choice: ModelChoice,
  hooks: ApplyHooks,
): Promise<ApplyOutcome> {
  if (choice.sessionId === null) {
    hooks.systemLine("No session yet: send a message first, then switch the model.");
    return "failed";
  }
  const value = choice.provider ? `${choice.provider}:${choice.model}` : choice.model;
  const params = { key: "model", value, session_id: choice.sessionId };
  let result: ConfigSetResult;
  try {
    result = (await rpc.call("config.set", params)) as ConfigSetResult;
  } catch (error) {
    if (!isConfirmRequired(error)) {
      hooks.systemLine(`Model not changed: ${errorText(error)}`);
      return "failed";
    }
    const yes = await hooks.confirm({
      title: `Switch to ${choice.model}?`,
      detail: error.message,
      danger: true,
      confirmLabel: "switch anyway",
      cancelLabel: "keep the current model",
    });
    if (!yes) {
      hooks.systemLine("Model unchanged.");
      return "cancelled";
    }
    try {
      result = (await rpc.call("config.set", { ...params, confirm: true })) as ConfigSetResult;
    } catch (again) {
      hooks.systemLine(`Model not changed: ${errorText(again)}`);
      return "failed";
    }
  }
  const deferred = result?.applied === "deferred";
  hooks.systemLine(
    deferred
      ? `${choice.model} takes over when the current reply ends.`
      : `Now using ${choice.model} for this session.`,
  );
  if (choice.effort) {
    try {
      await rpc.call("config.set", {
        key: "reasoning",
        value: choice.effort,
        session_id: choice.sessionId,
      });
      hooks.systemLine(`Reasoning effort: ${choice.effort}.`);
    } catch (error) {
      hooks.systemLine(`Reasoning effort not changed: ${errorText(error)}`);
    }
  }
  return deferred ? "deferred" : "applied";
}
