// Settings › Providers (W52-j): Accounts (sign-in cards, local models card, the other providers),
// Custom endpoints (saved endpoint cards and the add / edit form) and Local models (our local
// server presets with Test and Use).

import { ChevronDown, ChevronRight, KeyRound, Plus, Save, Zap } from "lucide-react";
import { useEffect, useId, useState } from "react";
import { useT } from "../../i18n/index.tsx";
import { useSettings } from "../context.tsx";
import {
  CompactSelect,
  OutlineButton,
  PageBody,
  Pill,
  PrimaryButton,
  SectionHeading,
  Segmented,
  SettingRow,
  SettingsEmpty,
  TextButton,
  TextField,
} from "../primitives.tsx";
import { patchFor, type RestLike, rest, valueAt } from "../settingsApi.ts";
import { CHATGPT_PROVIDER, ChatGptAccount } from "./chatgptAccount.tsx";
import { Note, SecretInput } from "./kit.tsx";

export interface ProviderRow {
  readonly name: string;
  readonly label: string;
  readonly description?: string;
  readonly family?: string;
  readonly auth_type: string;
  readonly configured: boolean;
  readonly device_login: boolean;
}

export interface ModelOptions {
  readonly providers: readonly ProviderRow[];
  readonly current: { readonly provider: string | null; readonly model: string | null };
}

function useModelOptions() {
  const { api, profile } = useSettings();
  const [options, setOptions] = useState<ModelOptions | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  // biome-ignore lint/correctness/useExhaustiveDependencies: tick reads again
  useEffect(() => {
    void rest<ModelOptions>(api, "GET", "/api/model/options", { profile }).then((answer) => {
      if (answer.ok) setOptions(answer.data);
      else setError(answer.message);
    });
  }, [api, profile, tick]);
  return { options, error, reload: () => setTick((n) => n + 1) };
}

/** `POST /api/model/set` for the main model (the call the model picker makes). */
async function chooseProvider(
  api: RestLike | null,
  profile: string | null,
  provider: string,
  model: string,
): Promise<string | null> {
  const answer = await rest(api, "POST", "/api/model/set", {
    body: { scope: "main", provider, model },
    profile,
  });
  return answer.ok ? null : answer.message;
}

// -- accounts ------------------------------------------------------------------------------------

function ChoiceCard(props: {
  title: string;
  body?: string;
  highlighted?: boolean;
  pill?: string;
  onClick: () => void;
}) {
  const bodyId = useId();
  return (
    <button
      type="button"
      className="sb-choice"
      data-highlighted={props.highlighted ? "true" : undefined}
      aria-label={props.title}
      aria-describedby={props.body === undefined ? undefined : bodyId}
      onClick={props.onClick}
    >
      <span className="sb-choice-text">
        <span className="sb-choice-title">
          {props.title}
          {props.pill !== undefined && <Pill>{props.pill}</Pill>}
        </span>
        {props.body !== undefined && (
          <span id={bodyId} className="sb-choice-body">
            {props.body}
          </span>
        )}
      </span>
      <ChevronRight aria-hidden="true" size={15} className="sb-choice-chevron" />
    </button>
  );
}

export function AccountsPage() {
  const t = useT();
  const { openOnboarding, go, focusRow } = useSettings();
  const { options, error, reload } = useModelOptions();
  const [others, setOthers] = useState(false);
  const rows = options?.providers ?? [];
  // The ChatGPT sign-in has its own card with Sign in / Sign out (W61-M2).
  const chatgpt = rows.some((row) => row.name === CHATGPT_PROVIDER);
  const device = rows.filter((row) => row.device_login && row.name !== CHATGPT_PROVIDER);
  const rest_ = rows.filter((row) => !row.device_login);
  return (
    <PageBody>
      <div
        data-row="device-sign-in"
        data-highlighted={focusRow === "device-sign-in" ? "true" : undefined}
      >
        <SectionHeading
          icon={<KeyRound aria-hidden="true" />}
          title={t("settings.accounts2.heading")}
          aside={
            <TextButton onClick={() => go?.({ view: "providers", page: "keys" })}>
              {t("settings.accounts2.haveKey")}
            </TextButton>
          }
        />
        <p className="settings-intro">{t("settings.accounts2.intro")}</p>
        <div className="sb-cards">
          {chatgpt && <ChatGptAccount onChanged={reload} />}
          {device.map((row) => (
            <ChoiceCard
              key={row.name}
              highlighted
              title={row.label}
              {...(row.description ? { body: row.description } : {})}
              {...(row.configured ? { pill: t("settings.accounts.signedIn") } : {})}
              onClick={openOnboarding}
            />
          ))}
          {device.length === 0 && !chatgpt && (
            <ChoiceCard
              highlighted
              title={t("settings.accounts.setup")}
              body={t("settings.accounts2.setupBody")}
              onClick={openOnboarding}
            />
          )}
          <ChoiceCard
            title={t("settings.accounts2.local")}
            body={t("settings.accounts2.localBody")}
            onClick={() => go?.({ view: "providers", page: "local" })}
          />
        </div>
      </div>
      {rest_.length > 0 && (
        <div className="sb-others">
          <button
            type="button"
            className="sb-others-toggle"
            aria-expanded={others}
            onClick={() => setOthers(!others)}
          >
            {t("settings.accounts2.others")}
            <ChevronDown aria-hidden="true" size={13} />
          </button>
          {others && (
            <div className="sb-others-list">
              {rest_.map((row) => (
                <ChoiceCard
                  key={row.name}
                  title={row.label}
                  {...(row.description ? { body: row.description } : {})}
                  {...(row.configured ? { pill: t("settings.accounts2.keySaved") } : {})}
                  onClick={() => go?.({ view: "providers", page: "keys" })}
                />
              ))}
            </div>
          )}
        </div>
      )}
      <Note text={error} error />
    </PageBody>
  );
}

// -- custom endpoints ----------------------------------------------------------------------------

export interface EndpointTestResult {
  readonly ok: boolean;
  readonly status: number | null;
  readonly models: readonly string[];
  readonly message: string;
}

/** `POST /api/model/test-endpoint`: the endpoint's own `/models`, asked from the backend. */
export async function testEndpoint(
  api: RestLike | null,
  body: { base_url: string; api_key?: string; key_env?: string },
  profile: string | null,
): Promise<EndpointTestResult> {
  const answer = await rest<EndpointTestResult>(api, "POST", "/api/model/test-endpoint", {
    body,
    profile,
  });
  if (answer.ok) return answer.data;
  return { ok: false, status: answer.status, models: [], message: answer.message };
}

/** `MY-LLM` → `MY_LLM_API_KEY` (the variable a custom endpoint's key is stored under). */
export function keyEnvFor(name: string): string {
  const base = name
    .toUpperCase()
    .replace(/[^A-Z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return `${base === "" ? "CUSTOM" : base}_API_KEY`;
}

interface CustomEntry {
  readonly base_url?: string;
  readonly key_env?: string;
  readonly api_mode?: string;
  readonly models?: readonly string[];
  readonly context_length?: number | null;
  readonly display_name?: string;
}

type ApiMode = "" | "chat_completions" | "responses" | "anthropic_messages";

export function EndpointsPage() {
  const t = useT();
  const { api, profile, config, reloadConfig, focusRow } = useSettings();
  const providers = (valueAt(config, "providers") ?? {}) as Readonly<Record<string, CustomEntry>>;
  const current = valueAt(config, "model.provider");
  const saved = Object.entries(providers).filter(
    ([, entry]) => typeof entry?.base_url === "string" && entry.base_url !== "",
  );
  const [editing, setEditing] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [display, setDisplay] = useState("");
  const [url, setUrl] = useState("");
  const [mode, setMode] = useState<ApiMode>("");
  const [model, setModel] = useState("");
  const [context, setContext] = useState("");
  const [key, setKey] = useState("");
  const [useNow, setUseNow] = useState(false);
  const [result, setResult] = useState<EndpointTestResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ text: string; error: boolean } | null>(null);
  const [rowNotes, setRowNotes] = useState<
    Readonly<Record<string, { text: string; error: boolean }>>
  >({});
  // W54-D2-8: the main model may point at an endpoint of its own (`model.base_url`, provider
  // `custom`) that is not one of the saved entries; it is the one in use, so it is listed too.
  const inlineUrl = valueAt(config, "model.base_url");
  const inlineModel = valueAt(config, "model.default");
  const inline =
    typeof inlineUrl === "string" &&
    inlineUrl !== "" &&
    !saved.some(([entryName]) => entryName === current)
      ? { url: inlineUrl, model: typeof inlineModel === "string" ? inlineModel : "" }
      : null;
  const [inlineNote, setInlineNote] = useState<{ text: string; error: boolean } | null>(null);
  const urlId = useId();
  const valid = /^[a-z0-9][a-z0-9._-]*$/i.test(name) && /^https?:\/\//i.test(url.trim());

  const clear = () => {
    setEditing(null);
    setName("");
    setDisplay("");
    setUrl("");
    setMode("");
    setModel("");
    setContext("");
    setKey("");
    setUseNow(false);
    setResult(null);
  };
  const load = (entryName: string, entry: CustomEntry) => {
    setEditing(entryName);
    setName(entryName);
    setDisplay(entry.display_name ?? "");
    setUrl(entry.base_url ?? "");
    setMode((entry.api_mode ?? "") as ApiMode);
    setModel(entry.models?.[0] ?? "");
    setContext(entry.context_length ? String(entry.context_length) : "");
    setKey("");
    setResult(null);
  };
  const use = async (entryName: string, entry: CustomEntry, fallbackModel = "") => {
    const chosen = entry.models?.[0] ?? fallbackModel;
    if (chosen === "") {
      setRowNotes((now) => ({
        ...now,
        [entryName]: { text: t("settings.endpoints2.needModel"), error: true },
      }));
      return;
    }
    const failure = await chooseProvider(api, profile, entryName, chosen);
    setRowNotes((now) => ({
      ...now,
      [entryName]:
        failure === null
          ? { text: t("settings.endpoints2.inUse"), error: false }
          : { text: failure, error: true },
    }));
    if (failure === null) reloadConfig();
  };

  return (
    <PageBody>
      <div
        data-row="custom-endpoints"
        data-highlighted={focusRow === "custom-endpoints" ? "true" : undefined}
      >
        <p className="settings-intro">
          <span className="sb-count">{saved.length + (inline === null ? 0 : 1)}</span>
          {t("settings.endpoints.body")}
        </p>
        {inline !== null && (
          <div className="sb-endpoint" data-endpoint="main-model">
            <button
              type="button"
              className="sb-endpoint-main"
              aria-label={t("settings.endpoints2.keepInline")}
              onClick={() => {
                clear();
                setUrl(inline.url);
                setModel(inline.model);
              }}
            />
            <div className="sb-endpoint-text">
              <span className="sb-endpoint-name">
                {t("settings.endpoints2.inline")}
                <Pill tone="accent">{t("settings.endpoints2.active")}</Pill>
              </span>
              <span className="sb-mono">{inline.url}</span>
              <span className="sb-mono sb-faint">
                {inline.model || t("settings.endpoints2.noModel")} ·{" "}
                {t("settings.endpoints2.inlineWhere")}
              </span>
              <Note text={inlineNote?.text ?? null} error={inlineNote?.error ?? false} />
            </div>
            <span className="sb-endpoint-actions">
              <OutlineButton
                onClick={async () => {
                  const tested = await testEndpoint(api, { base_url: inline.url }, profile);
                  setInlineNote({ text: tested.message, error: !tested.ok });
                }}
              >
                {t("settings.endpoints.test")}
              </OutlineButton>
            </span>
          </div>
        )}
        {saved.map(([entryName, entry]) => {
          const active = current === entryName;
          const rowNote = rowNotes[entryName];
          return (
            <div
              key={entryName}
              className="sb-endpoint"
              data-editing={editing === entryName ? "true" : undefined}
            >
              <button
                type="button"
                className="sb-endpoint-main"
                aria-label={t("settings.endpoints2.edit", {
                  name: entry.display_name || entryName,
                })}
                onClick={() => load(entryName, entry)}
              />
              <div className="sb-endpoint-text">
                <span className="sb-endpoint-name">
                  {entry.display_name || entryName}
                  {active && <Pill tone="accent">{t("settings.endpoints2.active")}</Pill>}
                </span>
                <span className="sb-mono">{entry.base_url}</span>
                <span className="sb-mono sb-faint">
                  {entry.models?.[0] ?? t("settings.endpoints2.noModel")} ·{" "}
                  {entry.key_env ?? t("settings.endpoints2.noKey")}
                </span>
                <Note text={rowNote?.text ?? null} error={rowNote?.error ?? false} />
              </div>
              <span className="sb-endpoint-actions">
                <OutlineButton
                  onClick={async () => {
                    const tested = await testEndpoint(
                      api,
                      {
                        base_url: entry.base_url ?? "",
                        ...(entry.key_env ? { key_env: entry.key_env } : {}),
                      },
                      profile,
                    );
                    setRowNotes((now) => ({
                      ...now,
                      [entryName]: { text: tested.message, error: !tested.ok },
                    }));
                  }}
                >
                  {t("settings.endpoints.test")}
                </OutlineButton>
                {!active && (
                  <OutlineButton
                    icon={<Zap aria-hidden="true" size={13} />}
                    onClick={() => void use(entryName, entry)}
                  >
                    {t("settings.endpoints2.use")}
                  </OutlineButton>
                )}
              </span>
            </div>
          );
        })}
      </div>
      <SectionHeading
        icon={<Plus aria-hidden="true" />}
        title={t(editing === null ? "settings.endpoints2.add" : "settings.endpoints2.editTitle")}
      />
      <div className="sb-form-card">
        <div className="sb-form-grid">
          <div className="sb-field">
            <span>{t("settings.endpoints.name")}</span>
            <TextField
              aria-label={t("settings.endpoints.name")}
              wide
              placeholder="my-server"
              value={name}
              disabled={editing !== null}
              onChange={(event) => setName(event.currentTarget.value)}
            />
          </div>
          <div className="sb-field">
            <span>{t("settings.endpoints2.display")}</span>
            <TextField
              aria-label={t("settings.endpoints2.display")}
              wide
              value={display}
              onChange={(event) => setDisplay(event.currentTarget.value)}
            />
          </div>
        </div>
        <label className="sb-field" htmlFor={urlId}>
          <span>{t("settings.endpoints.url")}</span>
        </label>
        <input
          id={urlId}
          className="settings-input sb-full"
          placeholder="http://127.0.0.1:8000/v1"
          value={url}
          spellCheck={false}
          autoComplete="off"
          onChange={(event) => setUrl(event.currentTarget.value)}
        />
        <div className="sb-field">
          <span>{t("settings.endpoints2.mode")}</span>
          <Segmented<ApiMode>
            label={t("settings.endpoints2.mode")}
            value={mode}
            options={[
              { value: "", label: t("settings.endpoints2.modeAuto") },
              { value: "chat_completions", label: t("settings.endpoints2.modeChat") },
              { value: "responses", label: t("settings.endpoints2.modeResponses") },
              { value: "anthropic_messages", label: t("settings.endpoints2.modeAnthropic") },
            ]}
            onChange={setMode}
          />
        </div>
        <div className="sb-form-grid">
          <div className="sb-field">
            <span>{t("settings.endpoints2.model")}</span>
            {result !== null && result.models.length > 0 ? (
              <CompactSelect
                label={t("settings.endpoints2.model")}
                value={model}
                options={[
                  { value: "", label: t("settings.field.none") },
                  ...result.models.map((m) => ({ value: m, label: m })),
                ]}
                onChange={setModel}
              />
            ) : (
              <TextField
                aria-label={t("settings.endpoints2.model")}
                wide
                placeholder={t("settings.endpoints2.modelHint")}
                value={model}
                onChange={(event) => setModel(event.currentTarget.value)}
              />
            )}
          </div>
          <div className="sb-field">
            <span>{t("settings.endpoints2.context")}</span>
            <TextField
              aria-label={t("settings.endpoints2.context")}
              inputMode="numeric"
              placeholder={t("settings.field.notSet")}
              value={context}
              onChange={(event) => setContext(event.currentTarget.value)}
            />
          </div>
        </div>
        <div className="sb-field">
          <span>{t("settings.endpoints.key")}</span>
          <SecretInput
            label={t("settings.endpoints.key")}
            placeholder={t(
              editing === null ? "settings.endpoints.keyOptional" : "settings.endpoints2.keepKey",
            )}
            value={key}
            onChange={setKey}
          />
        </div>
        <label className="sb-check">
          <input
            type="checkbox"
            checked={useNow}
            onChange={(event) => setUseNow(event.currentTarget.checked)}
          />
          {t("settings.endpoints2.useForNew")}
        </label>
        <div className="sb-form-actions">
          <OutlineButton
            icon={<Zap aria-hidden="true" size={13} />}
            disabled={!/^https?:\/\//i.test(url.trim()) || busy}
            onClick={async () => {
              setBusy(true);
              setResult(
                await testEndpoint(
                  api,
                  { base_url: url.trim(), ...(key.trim() === "" ? {} : { api_key: key.trim() }) },
                  profile,
                ),
              );
              setBusy(false);
            }}
          >
            {t("settings.endpoints.test")}
          </OutlineButton>
          <PrimaryButton
            icon={<Save aria-hidden="true" size={13} />}
            disabled={!valid || busy}
            onClick={async () => {
              setBusy(true);
              const keyEnv = keyEnvFor(name);
              const entry: Record<string, unknown> = { base_url: url.trim() };
              if (key.trim() !== "") entry.key_env = keyEnv;
              if (display.trim() !== "") entry.display_name = display.trim();
              if (mode !== "") entry.api_mode = mode;
              if (model.trim() !== "") entry.models = [model.trim()];
              const ctx = Number(context.trim());
              if (context.trim() !== "" && Number.isInteger(ctx) && ctx > 0)
                entry.context_length = ctx;
              const stored = await rest(api, "PUT", "/api/config", {
                body: { config: patchFor(`providers.${name}`, entry) },
                profile,
              });
              let failure = stored.ok ? null : stored.message;
              if (failure === null && key.trim() !== "") {
                const keyed = await rest(api, "PUT", "/api/env", {
                  body: { key: keyEnv, value: key.trim() },
                  profile,
                });
                failure = keyed.ok ? null : keyed.message;
              }
              if (failure === null && useNow && model.trim() !== "") {
                failure = await chooseProvider(api, profile, name, model.trim());
              }
              setBusy(false);
              if (failure === null) {
                clear();
                reloadConfig();
              }
              setNote(
                failure === null
                  ? { text: t("settings.saved"), error: false }
                  : { text: failure, error: true },
              );
            }}
          >
            {t("settings.save")}
          </PrimaryButton>
          <TextButton onClick={clear}>{t("settings.endpoints2.new")}</TextButton>
        </div>
        {result !== null && <Note text={result.message} error={!result.ok} />}
        <Note text={note?.text ?? null} error={note?.error ?? false} />
      </div>
    </PageBody>
  );
}

// -- local models --------------------------------------------------------------------------------

/** The documented default address of each local server preset our backend knows. */
const LOCAL_URLS: Readonly<Record<string, string>> = {
  ollama: "http://127.0.0.1:11434/v1",
  vllm: "http://127.0.0.1:8000/v1",
  llamacpp: "http://127.0.0.1:8080/v1",
  lmstudio: "http://127.0.0.1:1234/v1",
};

export function LocalModelsPage() {
  const t = useT();
  const { api, profile, focusRow } = useSettings();
  const { options, error } = useModelOptions();
  const [tests, setTests] = useState<Readonly<Record<string, EndpointTestResult>>>({});
  const [notes, setNotes] = useState<Readonly<Record<string, { text: string; error: boolean }>>>(
    {},
  );
  const presets = (options?.providers ?? []).filter((row) => row.name in LOCAL_URLS);
  return (
    <PageBody>
      <div
        data-row="local-models"
        data-highlighted={focusRow === "local-models" ? "true" : undefined}
      >
        <SectionHeading icon={<Zap aria-hidden="true" />} title={t("settings.local.title")} />
        <p className="settings-intro">{t("settings.local.intro")}</p>
        {presets.map((row) => {
          const url = LOCAL_URLS[row.name] as string;
          const tested = tests[row.name];
          const note = notes[row.name];
          return (
            <SettingRow
              key={row.name}
              title={
                <span className="sb-title-pills">
                  {row.label}
                  {tested !== undefined && (
                    <Pill tone={tested.ok ? "accent" : "neutral"}>
                      {t(tested.ok ? "settings.local.running" : "settings.local.notFound")}
                    </Pill>
                  )}
                </span>
              }
              description={
                <>
                  <span className="sb-mono">{url}</span>
                  {note !== undefined && (
                    <span className="sb-status" data-error={note.error ? "true" : undefined}>
                      {note.text}
                    </span>
                  )}
                </>
              }
              control={
                <span className="settings-inline">
                  <OutlineButton
                    onClick={async () => {
                      const result = await testEndpoint(api, { base_url: url }, profile);
                      setTests((now) => ({ ...now, [row.name]: result }));
                      setNotes((now) => ({
                        ...now,
                        [row.name]: { text: result.message, error: !result.ok },
                      }));
                    }}
                  >
                    {t("settings.endpoints.test")}
                  </OutlineButton>
                  <PrimaryButton
                    disabled={tested?.ok !== true || tested.models.length === 0}
                    onClick={async () => {
                      const model = tested?.models[0] ?? "";
                      const failure = await chooseProvider(api, profile, row.name, model);
                      setNotes((now) => ({
                        ...now,
                        [row.name]:
                          failure === null
                            ? { text: t("settings.local.inUse", { model }), error: false }
                            : { text: failure, error: true },
                      }));
                    }}
                  >
                    {t("settings.endpoints2.use")}
                  </PrimaryButton>
                </span>
              }
            />
          );
        })}
        {options !== null && presets.length === 0 && (
          <SettingsEmpty title={t("settings.local.none")} />
        )}
      </div>
      <Note text={error} error />
    </PageBody>
  );
}
