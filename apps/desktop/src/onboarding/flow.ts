// Provider onboarding (P20-33) as a pure state machine. The overlay feeds it what the backend
// answers and what the person clicks; it never reads local storage, and it decides by
// `auth_type`, `device_login` and slugs only — never by a provider's display name.

/** One row of `GET /api/model/options`. */
export interface ProviderOption {
  readonly name: string;
  readonly label: string;
  readonly description?: string;
  readonly family?: string;
  readonly auth_type: string;
  readonly configured: boolean;
  readonly device_login: boolean;
  /** The backend marks a provider it wants first in the list (W52-m-1; none does today). */
  readonly featured?: boolean;
}

/** Why the overlay is on screen: the first run, a provider failure, or an open from Settings. */
export type OpenReason = "first-run" | "provider-error" | "manual";

export type EndpointKind = "local" | "custom";

export type Step =
  | { readonly kind: "checking" }
  | { readonly kind: "hidden" }
  | { readonly kind: "dismissed" }
  | { readonly kind: "picker" }
  | { readonly kind: "device-starting"; readonly provider: ProviderOption }
  | {
      readonly kind: "device-polling";
      readonly provider: ProviderOption;
      readonly sessionId: string;
      readonly userCode: string;
      readonly verificationUrl: string;
      readonly intervalS: number;
    }
  | {
      readonly kind: "key-form";
      /** null: the generic "paste a key" row (variable name + value). */
      readonly provider: ProviderOption | null;
      readonly error: string | null;
    }
  | { readonly kind: "saving-key"; readonly provider: ProviderOption | null }
  | {
      readonly kind: "endpoint-form";
      readonly endpoint: EndpointKind;
      readonly error: string | null;
    }
  | { readonly kind: "testing-endpoint"; readonly endpoint: EndpointKind }
  | {
      readonly kind: "confirm-model";
      /** The provider slug the model is saved under (`custom` for endpoints). */
      readonly provider: string;
      readonly models: readonly string[];
      readonly baseUrl: string | null;
      readonly error: string | null;
    }
  | {
      readonly kind: "saving-model";
      readonly provider: string;
      readonly models: readonly string[];
      readonly baseUrl: string | null;
    }
  | {
      readonly kind: "error";
      readonly provider: ProviderOption | null;
      readonly message: string;
      /** How a device sign-in ended (`denied`, `expired`, …), when that is why. */
      readonly status?: string;
    };

export interface FlowState {
  readonly step: Step;
  /** Opened from Settings: closable, no "choose later". */
  readonly manual: boolean;
  /** Why it opened (W52-m; picks the notice text). Absent: the first run. */
  readonly reason?: OpenReason;
  readonly providers: readonly ProviderOption[] | null;
  readonly currentModel: string | null;
}

export type FlowEvent =
  | {
      readonly type: "options-loaded";
      readonly providers: readonly ProviderOption[];
      readonly currentModel: string | null;
      /** The configured main provider id (`model.provider`), when the backend names one. */
      readonly currentProvider?: string | null;
    }
  | { readonly type: "choose-later" }
  | { readonly type: "close" }
  | { readonly type: "reopen"; readonly manual: boolean; readonly reason?: OpenReason }
  | { readonly type: "back" }
  | { readonly type: "pick-device"; readonly provider: ProviderOption }
  | {
      readonly type: "device-started";
      readonly sessionId: string;
      readonly userCode: string;
      readonly verificationUrl: string;
      readonly intervalS: number;
    }
  | { readonly type: "device-status"; readonly status: string; readonly message: string }
  | { readonly type: "device-failed"; readonly message: string }
  | { readonly type: "pick-key"; readonly provider: ProviderOption | null }
  | { readonly type: "key-saving" }
  | { readonly type: "key-saved" }
  | { readonly type: "key-failed"; readonly message: string }
  | { readonly type: "pick-endpoint"; readonly endpoint: EndpointKind }
  | { readonly type: "endpoint-testing" }
  | {
      readonly type: "endpoint-tested";
      readonly ok: boolean;
      readonly models: readonly string[];
      readonly message: string;
      readonly baseUrl: string;
    }
  | { readonly type: "model-saving" }
  | { readonly type: "model-saved" }
  | { readonly type: "model-failed"; readonly message: string };

export const INITIAL_FLOW: FlowState = {
  step: { kind: "checking" },
  manual: false,
  providers: null,
  currentModel: null,
};

/** True when any provider has a key, a sign-in or an endpoint (the backend's word). */
export function anyConfigured(providers: readonly ProviderOption[]): boolean {
  return providers.some((p) => p.configured);
}

/**
 * The main model runs on an OpenAI-compatible endpoint the person chose (the local-runtime and
 * custom-endpoint forms both save provider id `custom` with its base URL). Such an endpoint may
 * need no key, so no provider row reads "configured" — it is still a usable setup and the
 * overlay must not come back on every launch.
 */
export function endpointChosen(event: {
  currentProvider?: string | null;
  currentModel: string | null;
}): boolean {
  return event.currentProvider === "custom" && (event.currentModel ?? "").trim() !== "";
}

/**
 * Device sign-ins listed under "Other providers" with a row of their own instead of among the
 * always-visible device rows (W61-M2-1): `openai-codex` is "Sign in with ChatGPT".
 */
export const OTHER_SIGN_INS: ReadonlySet<string> = new Set(["openai-codex"]);

/** Providers that sign in with a device code, shown at the top of the picker. */
export function deviceProviders(providers: readonly ProviderOption[]): ProviderOption[] {
  return providers.filter((p) => p.device_login && !OTHER_SIGN_INS.has(p.name));
}

/** The device sign-ins of the "Other providers" disclosure (see OTHER_SIGN_INS). */
export function otherSignIns(providers: readonly ProviderOption[]): ProviderOption[] {
  return providers.filter((p) => p.device_login && OTHER_SIGN_INS.has(p.name));
}

/** Providers that take a pasted API key (local servers and custom endpoints have their own form). */
export function keyProviders(providers: readonly ProviderOption[]): ProviderOption[] {
  return providers.filter((p) => p.auth_type === "api_key" && p.family !== "custom");
}

/** Whether the overlay covers the window in this state. */
export function isShown(state: FlowState): boolean {
  return state.step.kind !== "hidden" && state.step.kind !== "dismissed";
}

function to(state: FlowState, step: Step): FlowState {
  return { ...state, step };
}

const DEVICE_FAILED = new Set(["denied", "expired", "failed", "cancelled"]);

export function reduce(state: FlowState, event: FlowEvent): FlowState {
  const step = state.step;
  switch (event.type) {
    case "options-loaded": {
      const next = { ...state, providers: event.providers, currentModel: event.currentModel };
      if (step.kind !== "checking") return next;
      if (!state.manual && (anyConfigured(event.providers) || endpointChosen(event)))
        return to(next, { kind: "hidden" });
      return to(next, { kind: "picker" });
    }
    case "choose-later":
      return state.manual || !isShown(state) ? state : to(state, { kind: "dismissed" });
    case "close":
      return state.manual ? to(state, { kind: "hidden" }) : state;
    case "reopen":
      // A provider error does not interrupt a flow that is already on screen.
      if (isShown(state) && step.kind !== "checking") return state;
      return {
        ...state,
        manual: event.manual,
        reason: event.reason ?? (event.manual ? "manual" : "provider-error"),
        step: { kind: "checking" },
      };
    case "back":
      return step.kind === "checking" || !isShown(state) ? state : to(state, { kind: "picker" });
    case "pick-device":
      return step.kind === "picker" || step.kind === "error"
        ? to(state, { kind: "device-starting", provider: event.provider })
        : state;
    case "device-started":
      return step.kind === "device-starting"
        ? to(state, {
            kind: "device-polling",
            provider: step.provider,
            sessionId: event.sessionId,
            userCode: event.userCode,
            verificationUrl: event.verificationUrl,
            intervalS: event.intervalS,
          })
        : state;
    case "device-status":
      if (step.kind !== "device-polling") return state;
      if (event.status === "approved") {
        return to(state, {
          kind: "confirm-model",
          provider: step.provider.name,
          models: [],
          baseUrl: null,
          error: null,
        });
      }
      if (DEVICE_FAILED.has(event.status)) {
        return to(state, {
          kind: "error",
          provider: step.provider,
          message: event.message,
          status: event.status,
        });
      }
      return state;
    case "device-failed":
      return step.kind === "device-starting" || step.kind === "device-polling"
        ? to(state, { kind: "error", provider: step.provider, message: event.message })
        : state;
    case "pick-key":
      return step.kind === "picker" || step.kind === "error"
        ? to(state, { kind: "key-form", provider: event.provider, error: null })
        : state;
    case "key-saving":
      return step.kind === "key-form"
        ? to(state, { kind: "saving-key", provider: step.provider })
        : state;
    case "key-saved":
      return step.kind === "saving-key"
        ? to(state, {
            kind: "confirm-model",
            provider: step.provider?.name ?? "",
            models: [],
            baseUrl: null,
            error: null,
          })
        : state;
    case "key-failed":
      return step.kind === "saving-key"
        ? to(state, { kind: "key-form", provider: step.provider, error: event.message })
        : state;
    case "pick-endpoint":
      return step.kind === "picker" || step.kind === "error"
        ? to(state, { kind: "endpoint-form", endpoint: event.endpoint, error: null })
        : state;
    case "endpoint-testing":
      return step.kind === "endpoint-form"
        ? to(state, { kind: "testing-endpoint", endpoint: step.endpoint })
        : state;
    case "endpoint-tested":
      if (step.kind !== "testing-endpoint") return state;
      return event.ok
        ? to(state, {
            kind: "confirm-model",
            provider: "custom",
            models: event.models,
            baseUrl: event.baseUrl,
            error: null,
          })
        : to(state, { kind: "endpoint-form", endpoint: step.endpoint, error: event.message });
    case "model-saving":
      return step.kind === "confirm-model"
        ? to(state, {
            kind: "saving-model",
            provider: step.provider,
            models: step.models,
            baseUrl: step.baseUrl,
          })
        : state;
    case "model-saved":
      return step.kind === "saving-model"
        ? { ...state, manual: false, step: { kind: "hidden" } }
        : state;
    case "model-failed":
      return step.kind === "saving-model"
        ? to(state, {
            kind: "confirm-model",
            provider: step.provider,
            models: step.models,
            baseUrl: step.baseUrl,
            error: event.message,
          })
        : state;
  }
}
