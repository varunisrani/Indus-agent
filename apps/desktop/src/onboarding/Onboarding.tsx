// The provider onboarding overlay (P20-33). Once the window's backend is usable it asks the backend
// whether any provider is configured; if none is, it covers the chat until one is set up or the
// person picks "Choose later" (this window session only — a provider or auth failure, or Settings,
// brings it back). Every state comes from the backend; keys go to the backend and are dropped from
// the renderer as soon as the call returns.

import { Copy, ExternalLink, X } from "lucide-react";
import {
  type FormEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useReducer,
  useRef,
  useState,
} from "react";
import { desktopBridge } from "../app/bridge.ts";
import { Loader } from "../app/Loader.tsx";
import { classifyFailure } from "../chat/failure.ts";
import { useT } from "../i18n/index.tsx";
import type { SaveKey } from "../settings/context.tsx";
import { SecretInput } from "../settings/customPages.tsx";
import { bridgeRest, type RestLike, rest } from "../settings/settingsApi.ts";
import {
  deviceProviders,
  type EndpointKind,
  type FlowEvent,
  INITIAL_FLOW,
  isShown,
  keyProviders,
  type ProviderOption,
  reduce,
} from "./flow.ts";
import { onOnboardingRequest } from "./requests.ts";
import "./onboarding.css";

/** Loopback URL the local-runtime form starts with (a common local server port). */
export const LOCAL_ENDPOINT_HINT = "http://127.0.0.1:11434/v1";
/** Retry the provider check while the backend is still starting. */
export const CHECK_RETRY_MS = [2_000, 4_000, 8_000, 15_000] as const;

export interface Timers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

const realTimers: Timers = {
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

/** A raw daemon event (what the window's connection delivers). */
export type EventSource = (listener: (event: unknown) => void) => () => void;

export interface OnboardingProps {
  /** The window's backend is usable (its connection is open). */
  ready: boolean;
  /** Default: main's REST proxy. */
  api?: RestLike | null;
  /** `model.save_key` over the daemon; null when there is no connection. */
  saveKey: SaveKey | null;
  /** The connection's events, to notice provider and auth failures. */
  events?: EventSource | null;
  openExternal?: (url: string) => void;
  copy?: (text: string) => Promise<void>;
  timers?: Timers;
}

/** True for a finished turn that failed at the provider or with credentials. */
export function isProviderFailure(event: unknown): boolean {
  const body = event as { type?: unknown; reason?: unknown; failure?: unknown } | null;
  if (body === null || typeof body !== "object" || body.type !== "turn.done") return false;
  if (body.failure === null || body.failure === undefined) return false;
  const reason = typeof body.reason === "string" ? body.reason : "";
  const text = String((body.failure as { reason?: unknown }).reason ?? "");
  const layer = classifyFailure(reason, text);
  return layer === "provider" || layer === "auth";
}

interface Options {
  providers: ProviderOption[];
  current: { provider: string | null; model: string | null };
}

export function Onboarding(props: OnboardingProps) {
  const t = useT();
  const api = props.api === undefined ? bridgeRest() : props.api;
  const timers = props.timers ?? realTimers;
  const [state, dispatch] = useReducer(reduce, INITIAL_FLOW);
  const [checkTick, setCheckTick] = useState(0);
  const attempts = useRef(0);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);
  const send = useCallback((event: FlowEvent) => {
    if (live.current) dispatch(event);
  }, []);

  // Ask the backend; while it is still starting, ask again with growing pauses.
  // biome-ignore lint/correctness/useExhaustiveDependencies: checkTick asks again
  useEffect(() => {
    if (!props.ready || state.step.kind !== "checking") return;
    let handle: unknown = null;
    let cancelled = false;
    void rest<Options>(api, "GET", "/api/model/options").then((answer) => {
      if (cancelled) return;
      if (answer.ok && Array.isArray(answer.data.providers)) {
        attempts.current = 0;
        send({
          type: "options-loaded",
          providers: answer.data.providers,
          currentModel: answer.data.current?.model ?? null,
          currentProvider: answer.data.current?.provider ?? null,
        });
        return;
      }
      const delay = CHECK_RETRY_MS[Math.min(attempts.current, CHECK_RETRY_MS.length - 1)] ?? 15_000;
      attempts.current += 1;
      handle = timers.setTimeout(() => setCheckTick((n) => n + 1), delay);
    });
    return () => {
      cancelled = true;
      if (handle !== null) timers.clearTimeout(handle);
    };
  }, [props.ready, state.step.kind, api, checkTick, timers]);

  // Explicit opens (Settings) and provider/auth failures bring the overlay back.
  useEffect(
    () =>
      onOnboardingRequest((request) =>
        send({ type: "reopen", manual: request.reason === "manual" }),
      ),
    [send],
  );
  useEffect(() => {
    if (props.events === null || props.events === undefined) return;
    return props.events((event) => {
      if (isProviderFailure(event)) send({ type: "reopen", manual: false });
    });
  }, [props.events, send]);

  // Device sign-in: poll the backend's session at the provider's pace; cancel when abandoned.
  const step = state.step;
  const pollingId = step.kind === "device-polling" ? step.sessionId : null;
  const pollingInterval = step.kind === "device-polling" ? step.intervalS : 5;
  useEffect(() => {
    if (pollingId === null) return;
    let handle: unknown = null;
    let finished = false;
    const tick = () => {
      handle = timers.setTimeout(async () => {
        const answer = await rest<{ status: string; message: string }>(
          api,
          "GET",
          `/api/providers/login/${encodeURIComponent(pollingId)}`,
        );
        if (finished) return;
        if (answer.ok) {
          send({ type: "device-status", status: answer.data.status, message: answer.data.message });
          if (answer.data.status === "pending") tick();
          else finished = true;
        } else {
          finished = true;
          send({ type: "device-failed", message: answer.message });
        }
      }, Math.max(1, pollingInterval) * 1000);
    };
    tick();
    return () => {
      if (handle !== null) timers.clearTimeout(handle);
      if (!finished) {
        finished = true;
        void rest(api, "DELETE", `/api/providers/login/${encodeURIComponent(pollingId)}`);
      }
    };
  }, [pollingId, pollingInterval, api, send, timers]);

  // The overlay is modal: focus moves into it when it appears.
  const cardRef = useRef<HTMLElement | null>(null);
  const shown = props.ready && isShown(state);
  useEffect(() => {
    if (shown) cardRef.current?.focus();
  }, [shown]);

  if (!shown) return null;

  const providers = state.providers ?? [];
  const startDevice = async (provider: ProviderOption) => {
    send({ type: "pick-device", provider });
    const answer = await rest<{
      session_id: string;
      user_code: string;
      verification_url: string;
      interval?: number;
    }>(api, "POST", "/api/providers/login/start", { body: { provider: provider.name } });
    if (answer.ok) {
      send({
        type: "device-started",
        sessionId: answer.data.session_id,
        userCode: answer.data.user_code,
        verificationUrl: answer.data.verification_url,
        intervalS: answer.data.interval ?? 5,
      });
    } else send({ type: "device-failed", message: answer.message });
  };
  let body: ReactNode;
  switch (step.kind) {
    case "checking":
      body = (
        <div className="onboarding-status" role="status">
          <Loader />
          <span>{t("onboarding.checking")}</span>
        </div>
      );
      break;
    case "picker":
      body = (
        <Picker
          providers={providers}
          manual={state.manual}
          onDevice={(provider) => void startDevice(provider)}
          onKey={(provider) => send({ type: "pick-key", provider })}
          onEndpoint={(endpoint) => send({ type: "pick-endpoint", endpoint })}
          onLater={() => send({ type: "choose-later" })}
        />
      );
      break;
    case "device-starting":
      body = (
        <div className="onboarding-status" role="status">
          <Loader />
          <span>{t("onboarding.device.starting", { provider: step.provider.label })}</span>
        </div>
      );
      break;
    case "device-polling":
      body = (
        <DeviceCode
          provider={step.provider}
          code={step.userCode}
          url={step.verificationUrl}
          onOpen={() => (props.openExternal ?? defaultOpen)(step.verificationUrl)}
          onCopy={() => (props.copy ?? defaultCopy)(step.userCode)}
          onCancel={() => send({ type: "back" })}
        />
      );
      break;
    case "key-form":
    case "saving-key":
      body = (
        <KeyForm
          providers={keyProviders(providers)}
          initial={step.provider}
          busy={step.kind === "saving-key"}
          error={step.kind === "key-form" ? step.error : null}
          onBack={() => send({ type: "back" })}
          onSave={async (provider, name, value) => {
            send({ type: "key-saving" });
            let failure: string | null = null;
            if (provider !== null) {
              if (props.saveKey === null) failure = t("onboarding.notConnected");
              else {
                try {
                  await props.saveKey(provider.name, value);
                } catch (error) {
                  failure = error instanceof Error ? error.message : String(error);
                }
              }
            } else {
              const answer = await rest(api, "PUT", "/api/env", { body: { key: name, value } });
              failure = answer.ok ? null : answer.message;
            }
            send(
              failure === null ? { type: "key-saved" } : { type: "key-failed", message: failure },
            );
            return failure === null;
          }}
        />
      );
      break;
    case "endpoint-form":
    case "testing-endpoint":
      body = (
        <EndpointForm
          endpoint={step.endpoint}
          busy={step.kind === "testing-endpoint"}
          error={step.kind === "endpoint-form" ? step.error : null}
          onBack={() => send({ type: "back" })}
          onTest={async (url, key) => {
            send({ type: "endpoint-testing" });
            const answer = await rest<{ ok: boolean; models: string[]; message: string }>(
              api,
              "POST",
              "/api/model/test-endpoint",
              { body: { base_url: url, ...(key === "" ? {} : { api_key: key }) } },
            );
            if (answer.ok && answer.data.ok && key !== "" && props.saveKey !== null) {
              try {
                await props.saveKey("custom", key);
              } catch (error) {
                send({
                  type: "endpoint-tested",
                  ok: false,
                  models: [],
                  message: error instanceof Error ? error.message : String(error),
                  baseUrl: url,
                });
                return;
              }
            }
            send({
              type: "endpoint-tested",
              ok: answer.ok && answer.data.ok,
              models: answer.ok ? answer.data.models : [],
              message: answer.ok ? answer.data.message : answer.message,
              baseUrl: url,
            });
          }}
        />
      );
      break;
    case "confirm-model":
    case "saving-model":
      body = (
        <ConfirmModel
          models={step.models}
          initial={step.models[0] ?? state.currentModel ?? ""}
          busy={step.kind === "saving-model"}
          error={step.kind === "confirm-model" ? step.error : null}
          onSave={async (model) => {
            send({ type: "model-saving" });
            const answer = await rest(api, "POST", "/api/model/set", {
              body: {
                scope: "main",
                provider: step.provider,
                model,
                ...(step.baseUrl === null ? {} : { base_url: step.baseUrl }),
              },
            });
            send(
              answer.ok
                ? { type: "model-saved" }
                : { type: "model-failed", message: answer.message },
            );
          }}
        />
      );
      break;
    case "error":
      body = (
        <div className="onboarding-error" role="alert">
          <p>{step.message || t("onboarding.error.default")}</p>
          <div className="onboarding-actions">
            <button
              type="button"
              className="button button-secondary"
              onClick={() => send({ type: "back" })}
            >
              {t("onboarding.error.other")}
            </button>
            <button
              type="button"
              className="button button-secondary"
              onClick={() => send({ type: "pick-key", provider: null })}
            >
              {t("onboarding.error.useKey")}
            </button>
            {step.provider?.device_login === true && (
              <button
                type="button"
                className="button button-primary"
                onClick={() => {
                  if (step.provider !== null) void startDevice(step.provider);
                }}
              >
                {t("onboarding.error.retry")}
              </button>
            )}
          </div>
        </div>
      );
      break;
    default:
      body = null;
  }

  return (
    <div className="onboarding-overlay">
      <section
        ref={cardRef}
        tabIndex={-1}
        className="onboarding-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="onboarding-title"
        onKeyDown={(event) => {
          // Escape never dismisses the first-run overlay; Settings' manual open can be closed.
          if (event.key === "Escape") {
            event.preventDefault();
            if (state.manual) send({ type: "close" });
          }
        }}
      >
        <header className="onboarding-header">
          <h2 id="onboarding-title">{t("onboarding.title")}</h2>
          {state.manual && (
            <button
              type="button"
              className="icon-button"
              aria-label={t("common.close")}
              onClick={() => send({ type: "close" })}
            >
              <X aria-hidden="true" size={16} />
            </button>
          )}
        </header>
        <p className="onboarding-lead">{t("onboarding.lead")}</p>
        {body}
      </section>
    </div>
  );
}

function defaultOpen(url: string): void {
  void desktopBridge()?.external.open({ url });
}

async function defaultCopy(text: string): Promise<void> {
  await navigator.clipboard?.writeText(text);
}

function Picker(props: {
  providers: readonly ProviderOption[];
  manual: boolean;
  onDevice: (provider: ProviderOption) => void;
  onKey: (provider: ProviderOption | null) => void;
  onEndpoint: (endpoint: EndpointKind) => void;
  onLater: () => void;
}) {
  const t = useT();
  const device = deviceProviders(props.providers);
  return (
    <div className="onboarding-picker">
      {device.length > 0 && (
        <section aria-labelledby="onboarding-device">
          <h3 id="onboarding-device">{t("onboarding.device.title")}</h3>
          <ul className="onboarding-list">
            {device.map((provider) => (
              <li key={provider.name}>
                <button
                  type="button"
                  className="onboarding-option"
                  onClick={() => props.onDevice(provider)}
                >
                  <span className="onboarding-option-title">{provider.label}</span>
                  {provider.description !== undefined && provider.description !== "" && (
                    <span className="onboarding-option-hint">{provider.description}</span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
      <section aria-labelledby="onboarding-other">
        <h3 id="onboarding-other">{t("onboarding.other.title")}</h3>
        <ul className="onboarding-list">
          <li>
            <button
              type="button"
              className="onboarding-option"
              onClick={() => props.onKey(keyProviders(props.providers)[0] ?? null)}
            >
              <span className="onboarding-option-title">{t("onboarding.key.option")}</span>
              <span className="onboarding-option-hint">{t("onboarding.key.optionHint")}</span>
            </button>
          </li>
          <li>
            <button
              type="button"
              className="onboarding-option"
              onClick={() => props.onEndpoint("local")}
            >
              <span className="onboarding-option-title">{t("onboarding.local.option")}</span>
              <span className="onboarding-option-hint">{t("onboarding.local.optionHint")}</span>
            </button>
          </li>
          <li>
            <button
              type="button"
              className="onboarding-option"
              onClick={() => props.onEndpoint("custom")}
            >
              <span className="onboarding-option-title">{t("onboarding.custom.option")}</span>
              <span className="onboarding-option-hint">{t("onboarding.custom.optionHint")}</span>
            </button>
          </li>
        </ul>
      </section>
      {!props.manual && (
        <div className="onboarding-actions">
          <button type="button" className="text-button" onClick={props.onLater}>
            {t("onboarding.later")}
          </button>
        </div>
      )}
    </div>
  );
}

function DeviceCode(props: {
  provider: ProviderOption;
  code: string;
  url: string;
  onOpen: () => void;
  onCopy: () => Promise<void>;
  onCancel: () => void;
}) {
  const t = useT();
  const [copied, setCopied] = useState(false);
  return (
    <div className="onboarding-device">
      <p>{t("onboarding.device.instructions", { provider: props.provider.label })}</p>
      <output className="onboarding-code" aria-label={t("onboarding.device.code")}>
        {props.code}
      </output>
      <div className="onboarding-actions">
        <button
          type="button"
          className="button button-secondary"
          onClick={async () => {
            try {
              await props.onCopy();
              setCopied(true);
            } catch {
              setCopied(false);
            }
          }}
        >
          <Copy aria-hidden="true" size={14} />
          {t(copied ? "onboarding.device.copied" : "onboarding.device.copy")}
        </button>
        <button type="button" className="button button-primary" onClick={props.onOpen}>
          <ExternalLink aria-hidden="true" size={14} />
          {t("onboarding.device.open")}
        </button>
      </div>
      <p className="onboarding-status" role="status">
        <Loader />
        <span>{t("onboarding.device.waiting")}</span>
      </p>
      <button type="button" className="text-button" onClick={props.onCancel}>
        {t("onboarding.cancel")}
      </button>
    </div>
  );
}

function KeyForm(props: {
  providers: readonly ProviderOption[];
  initial: ProviderOption | null;
  busy: boolean;
  error: string | null;
  onBack: () => void;
  onSave: (provider: ProviderOption | null, name: string, value: string) => Promise<boolean>;
}) {
  const t = useT();
  const [choice, setChoice] = useState<string>(props.initial?.name ?? "");
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const selectId = useId();
  const provider = props.providers.find((p) => p.name === choice) ?? null;
  const generic = provider === null;
  const valid = value.trim() !== "" && (!generic || /^[A-Z][A-Z0-9_]*$/.test(name));
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!valid || props.busy) return;
    const saved = await props.onSave(provider, name, value.trim());
    setValue(""); // the key leaves the renderer either way
    if (saved) setName("");
  };
  return (
    <form className="onboarding-form" onSubmit={submit}>
      <label htmlFor={selectId}>{t("onboarding.key.provider")}</label>
      <select
        id={selectId}
        className="settings-input"
        value={choice}
        onChange={(event) => setChoice(event.currentTarget.value)}
      >
        {props.providers.map((p) => (
          <option key={p.name} value={p.name}>
            {p.label}
          </option>
        ))}
        <option value="">{t("onboarding.key.generic")}</option>
      </select>
      {generic && (
        <input
          className="settings-input"
          aria-label={t("onboarding.key.name")}
          placeholder="EXAMPLE_API_KEY"
          value={name}
          spellCheck={false}
          onChange={(event) => setName(event.currentTarget.value.toUpperCase())}
        />
      )}
      <SecretInput
        label={t("onboarding.key.value")}
        placeholder={t("onboarding.key.value")}
        value={value}
        onChange={setValue}
      />
      {props.error !== null && (
        <p className="settings-row-error" role="alert">
          {props.error}
        </p>
      )}
      <div className="onboarding-actions">
        <button type="button" className="text-button" onClick={props.onBack}>
          {t("onboarding.back")}
        </button>
        <button type="submit" className="button button-primary" disabled={!valid || props.busy}>
          {t("onboarding.key.save")}
        </button>
      </div>
    </form>
  );
}

function EndpointForm(props: {
  endpoint: EndpointKind;
  busy: boolean;
  error: string | null;
  onBack: () => void;
  onTest: (url: string, key: string) => Promise<void>;
}) {
  const t = useT();
  const [url, setUrl] = useState(props.endpoint === "local" ? LOCAL_ENDPOINT_HINT : "");
  const [key, setKey] = useState("");
  const urlId = useId();
  const valid = /^https?:\/\/\S+$/i.test(url.trim());
  return (
    <form
      className="onboarding-form"
      onSubmit={async (event) => {
        event.preventDefault();
        if (!valid || props.busy) return;
        await props.onTest(url.trim(), key.trim());
        setKey("");
      }}
    >
      <p>{t(props.endpoint === "local" ? "onboarding.local.body" : "onboarding.custom.body")}</p>
      <label htmlFor={urlId}>{t("onboarding.endpoint.url")}</label>
      <input
        id={urlId}
        className="settings-input"
        value={url}
        spellCheck={false}
        onChange={(event) => setUrl(event.currentTarget.value)}
      />
      {props.endpoint === "custom" && (
        <SecretInput
          label={t("onboarding.endpoint.key")}
          placeholder={t("onboarding.endpoint.key")}
          value={key}
          onChange={setKey}
        />
      )}
      {props.error !== null && (
        <p className="settings-row-error" role="alert">
          {props.error}
        </p>
      )}
      <div className="onboarding-actions">
        <button type="button" className="text-button" onClick={props.onBack}>
          {t("onboarding.back")}
        </button>
        <button type="submit" className="button button-primary" disabled={!valid || props.busy}>
          {t("onboarding.endpoint.test")}
        </button>
      </div>
    </form>
  );
}

function ConfirmModel(props: {
  models: readonly string[];
  initial: string;
  busy: boolean;
  error: string | null;
  onSave: (model: string) => Promise<void>;
}) {
  const t = useT();
  const [model, setModel] = useState(props.initial);
  const inputId = useId();
  const listId = useId();
  return (
    <form
      className="onboarding-form"
      onSubmit={async (event) => {
        event.preventDefault();
        if (model.trim() !== "" && !props.busy) await props.onSave(model.trim());
      }}
    >
      <p>{t("onboarding.model.body")}</p>
      <label htmlFor={inputId}>{t("onboarding.model.label")}</label>
      <input
        id={inputId}
        className="settings-input"
        list={listId}
        value={model}
        spellCheck={false}
        onChange={(event) => setModel(event.currentTarget.value)}
      />
      <datalist id={listId}>
        {props.models.map((name) => (
          <option key={name} value={name} />
        ))}
      </datalist>
      {props.error !== null && (
        <p className="settings-row-error" role="alert">
          {props.error}
        </p>
      )}
      <div className="onboarding-actions">
        <button
          type="submit"
          className="button button-primary"
          disabled={model.trim() === "" || props.busy}
        >
          {t("onboarding.model.save")}
        </button>
      </div>
    </form>
  );
}
