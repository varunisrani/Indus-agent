// Settings › Model (W52-i): the main model (provider, model, Apply, defaults), fallback models,
// the side tasks' models (auxiliary) and the mixture-of-agents presets. Reads `/api/model/options`,
// `/api/model/auxiliary`, the form tree and, for model names, the gateway's `model.options`; writes
// with `POST /api/model/set` and `PUT /api/config` exactly as before.

import { Plus, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { entriesFrom } from "../chat/ModelPicker.tsx";
import { useGatewayConnection } from "../gateway/react.tsx";
import { type MessageKey, useT } from "../i18n/index.tsx";
import { useSettings } from "./context.tsx";
import { LoadError, SchemaFieldRow, SkeletonRows, StatusNote } from "./controlsA.tsx";
import {
  CompactSelect,
  FreeCombo,
  GroupLabel,
  PageBody,
  Pill,
  PrimaryButton,
  type SelectOption,
  SettingRow,
  SettingsEmpty,
  Switch,
  TextButton,
  TextField,
} from "./primitives.tsx";
import { rest, valueAt } from "./settingsApi.ts";

interface ProviderOption {
  readonly name: string;
  readonly label: string;
  readonly configured: boolean;
}

interface OptionsAnswer {
  readonly providers: readonly ProviderOption[];
  readonly current: { provider: string | null; model: string | null };
}

/** `/api/model/options` once per page (null while loading). */
function useProviderOptions() {
  const { api } = useSettings();
  const [options, setOptions] = useState<OptionsAnswer | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  // biome-ignore lint/correctness/useExhaustiveDependencies: tick asks for a reload
  useEffect(() => {
    let live = true;
    setError(null);
    void rest<OptionsAnswer>(api, "GET", "/api/model/options").then((answer) => {
      if (!live) return;
      if (answer.ok) setOptions(answer.data);
      else setError(answer.message);
    });
    return () => {
      live = false;
    };
  }, [api, tick]);
  return { options, error, retry: () => setTick((n) => n + 1) };
}

/** Known model names per provider from the gateway (`model.options`); empty when offline. */
function useModelNames(): ReadonlyMap<string, readonly string[]> {
  const connection = useGatewayConnection();
  const [names, setNames] = useState<ReadonlyMap<string, readonly string[]>>(new Map());
  useEffect(() => {
    if (connection === null) return;
    let live = true;
    const request = connection.request.bind(connection) as unknown as (
      m: string,
      p: object,
    ) => Promise<unknown>;
    request("model.options", {}).then(
      (answer) => {
        if (!live) return;
        const map = new Map<string, string[]>();
        for (const entry of entriesFrom(answer as Parameters<typeof entriesFrom>[0])) {
          map.set(entry.provider, [...(map.get(entry.provider) ?? []), entry.model]);
        }
        setNames(map);
      },
      () => undefined,
    );
    return () => {
      live = false;
    };
  }, [connection]);
  return names;
}

function providerChoices(
  options: OptionsAnswer | null,
  needsKey: string,
  current: string,
  exclude: readonly string[] = [],
): SelectOption[] {
  const rows: SelectOption[] = (options?.providers ?? [])
    .filter((p) => !exclude.includes(p.name))
    .map((p) => ({ value: p.name, label: p.configured ? p.label : `${p.label} · ${needsKey}` }));
  if (current !== "" && !rows.some((row) => row.value === current)) {
    rows.unshift({ value: current, label: current });
  }
  return rows;
}

// -- Model › Main model ----------------------------------------------------------------------------

export function ModelMainPage() {
  const t = useT();
  const { api, config, profile, reloadConfig } = useSettings();
  const { options, error, retry } = useProviderOptions();
  const names = useModelNames();
  const savedProvider =
    options?.current.provider ?? String(valueAt(config, "model.provider") ?? "");
  const savedModel = options?.current.model ?? String(valueAt(config, "model.default") ?? "");
  const [provider, setProvider] = useState<string | null>(null);
  const [model, setModel] = useState<string | null>(null);
  const [state, setState] = useState<"idle" | "busy" | "saved" | { error: string }>("idle");
  const chosenProvider = provider ?? savedProvider ?? "";
  const chosenModel = model ?? savedModel ?? "";
  const changed =
    chosenModel !== "" && (chosenProvider !== savedProvider || chosenModel !== savedModel);

  const apply = async () => {
    setState("busy");
    const answer = await rest(api, "POST", "/api/model/set", {
      body: { scope: "main", provider: chosenProvider, model: chosenModel },
      profile,
    });
    if (!answer.ok) {
      setState({ error: answer.message });
      return;
    }
    setState("saved");
    setProvider(null);
    setModel(null);
    retry();
    reloadConfig();
    window.setTimeout(() => setState((now) => (now === "saved" ? "idle" : now)), 2000);
  };

  if (options === null && error === null) {
    return (
      <PageBody intro={t("settingsA.main.intro")}>
        <SkeletonRows count={3} />
      </PageBody>
    );
  }
  return (
    <PageBody intro={t("settingsA.main.intro")}>
      {error !== null && (
        <LoadError
          message={t("settingsA.main.optionsFailed", { message: error })}
          onRetry={retry}
        />
      )}
      <div className="settingsA-main">
        <SettingRow
          title={t("settingsA.main.provider")}
          rowId="main-provider"
          control={
            options === null ? (
              <TextField
                aria-label={t("settingsA.main.provider")}
                defaultValue={chosenProvider}
                wide
                onBlur={(event) => setProvider(event.currentTarget.value.trim())}
              />
            ) : (
              <CompactSelect
                label={t("settingsA.main.provider")}
                value={chosenProvider}
                width={288}
                options={providerChoices(options, t("settingsA.main.needsKey"), chosenProvider)}
                onChange={(value) => {
                  setProvider(value);
                  setModel("");
                }}
              />
            )
          }
        />
        <SettingRow
          title={t("settingsA.main.model")}
          rowId="main-model"
          control={
            <FreeCombo
              label={t("settingsA.main.model")}
              value={chosenModel}
              suggestions={names.get(chosenProvider) ?? []}
              placeholder={t("settingsA.search")}
              onCommit={(value) => setModel(value.trim())}
            />
          }
        />
        {chosenProvider === "custom" && (
          <>
            <SchemaFieldRow fieldKey="model.base_url" />
            <SchemaFieldRow fieldKey="model.api_mode" />
          </>
        )}
        <div className="settingsA-apply">
          <PrimaryButton disabled={!changed || state === "busy"} onClick={() => void apply()}>
            {state === "busy" ? t("settingsA.main.applying") : t("settingsA.main.apply")}
          </PrimaryButton>
          {state === "saved" && <StatusNote>{t("settings.saved")}</StatusNote>}
          {typeof state === "object" && <StatusNote error>{state.error}</StatusNote>}
        </div>
      </div>
      <GroupLabel>{t("settingsA.main.defaults")}</GroupLabel>
      <SettingRow
        title={t("settingsA.main.reasoning")}
        description={t("settings.notAvailable")}
        control={
          <CompactSelect
            label={t("settingsA.main.reasoning")}
            value="auto"
            disabled
            options={[{ value: "auto", label: t("settingsA.main.reasoningAuto") }]}
            onChange={() => undefined}
          />
        }
      />
      <SchemaFieldRow fieldKey="agent.fast_mode" />
      <SchemaFieldRow fieldKey="model.context_length" />
    </PageBody>
  );
}

// -- Model › Fallback models -------------------------------------------------------------------------

interface Route {
  provider: string;
  model: string;
}

function routesIn(value: unknown): Route[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((v): v is Record<string, unknown> => v !== null && typeof v === "object")
    .map((v) => ({ provider: String(v.provider ?? ""), model: String(v.model ?? "") }));
}

export function ModelFallbacksPage() {
  const t = useT();
  const { config, savePatch } = useSettings();
  const { options } = useProviderOptions();
  const [routes, setRoutes] = useState<Route[] | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const shown = routes ?? routesIn(valueAt(config, "fallback_providers"));

  const write = async (next: Route[]) => {
    setRoutes(next);
    const failure = await (savePatch?.({ fallback_providers: next }) ??
      Promise.resolve("not available"));
    setNote(failure);
  };

  if (config === null) return <SkeletonRows count={2} />;
  const providers = (current: string) =>
    providerChoices(options, t("settingsA.main.needsKey"), current);
  return (
    <PageBody>
      <SettingRow
        wide
        rowId="fallbacks"
        fieldKey="fallback_providers"
        title={t("settingsA.fallback.title")}
        description={t("settingsA.fallback.body")}
        below={
          <div className="settingsA-routes">
            {shown.length === 0 && (
              <p className="settings-row-desc">{t("settingsA.fallback.empty")}</p>
            )}
            {shown.map((route, index) => {
              const n = index + 1;
              const update = (patch: Partial<Route>) =>
                void write(shown.map((r, i) => (i === index ? { ...r, ...patch } : r)));
              return (
                // biome-ignore lint/suspicious/noArrayIndexKey: the position is the identity
                <div key={index} className="settingsA-route">
                  <span className="settingsA-index">{n}</span>
                  <CompactSelect
                    label={t("settingsA.fallback.provider", { n })}
                    value={route.provider}
                    options={[{ value: "", label: "—" }, ...providers(route.provider)]}
                    onChange={(provider) => update({ provider })}
                  />
                  <TextField
                    aria-label={t("settingsA.fallback.model", { n })}
                    defaultValue={route.model}
                    onBlur={(event) => {
                      const next = event.currentTarget.value.trim();
                      if (next !== route.model) update({ model: next });
                    }}
                  />
                  <button
                    type="button"
                    className="settingsA-icon"
                    aria-label={t("settingsA.fallback.remove")}
                    title={t("settingsA.fallback.remove")}
                    onClick={() => void write(shown.filter((_, i) => i !== index))}
                  >
                    <X aria-hidden="true" size={14} />
                  </button>
                </div>
              );
            })}
            <button
              type="button"
              className="settingsA-link"
              onClick={() => void write([...shown, { provider: "", model: "" }])}
            >
              <Plus aria-hidden="true" size={12} />
              {t("settingsA.fallback.add")}
            </button>
            <StatusNote error>{note}</StatusNote>
          </div>
        }
      />
      <SchemaFieldRow fieldKey="fallback.min_switch_reset_s" />
    </PageBody>
  );
}

// -- Model › Auxiliary models ------------------------------------------------------------------------

interface AuxTask {
  readonly task: string;
  readonly provider: string;
  readonly model: string;
  readonly base_url: string;
}

interface AuxAnswer {
  readonly tasks: readonly AuxTask[];
  readonly main: { provider: string; model: string };
}

const EFFORTS = ["", "none", "low", "medium", "high"] as const;

/** The mono status line of one side task. */
export function auxStatus(
  task: Pick<AuxTask, "provider" | "model">,
  effort: string,
  t: (key: MessageKey, vars?: Record<string, string | number>) => string,
): string {
  const base =
    task.provider === "auto" || task.provider === ""
      ? t("settingsA.aux.auto")
      : `${task.provider} · ${task.model === "" ? "—" : task.model}`;
  return effort === "" ? base : `${base} · ${t("settingsA.aux.reasoningLevel", { level: effort })}`;
}

function AuxRow(props: {
  task: AuxTask;
  main: AuxAnswer["main"];
  options: OptionsAnswer | null;
  onChanged: () => void;
}) {
  const t = useT();
  const { api, profile, config, saveField } = useSettings();
  const names = useModelNames();
  const { task } = props;
  const effort = String(valueAt(config, `auxiliary.${task.task}.reasoning_effort`) ?? "");
  const [editing, setEditing] = useState(false);
  const [provider, setProvider] = useState(task.provider);
  const [model, setModel] = useState(task.model);
  const [level, setLevel] = useState(effort);
  const [note, setNote] = useState<string | null>(null);
  const label = (suffix = "") => t(`settingsA.aux.${task.task}${suffix}` as MessageKey);
  const known = [
    "vision",
    "compression",
    "title",
    "approval",
    "review",
    "curator",
    "goal_judge",
    "side_question",
  ].includes(task.task);

  const set = async (body: { provider: string; model: string }) => {
    const answer = await rest(api, "POST", "/api/model/set", {
      body: { scope: "auxiliary", task: task.task, ...body },
      profile,
    });
    return answer.ok ? null : answer.message;
  };

  const applyEdit = async () => {
    let failure = await set({ provider, model });
    if (failure === null && level !== effort && saveField !== undefined) {
      failure = await saveField(`auxiliary.${task.task}.reasoning_effort`, level);
    }
    setNote(failure);
    if (failure === null) {
      setEditing(false);
      props.onChanged();
    }
  };

  return (
    <div className="settingsA-aux" data-row={`aux-${task.task}`}>
      <div className="settingsA-aux-head">
        <div className="settingsA-aux-text">
          <span className="settings-row-label">
            {known ? label() : task.task}
            {known && <Pill>{label(".pill")}</Pill>}
          </span>
          <span className="settingsA-mono">{auxStatus(task, effort, t)}</span>
        </div>
        {!editing && (
          <div className="settingsA-aux-actions">
            <TextButton
              disabled={props.main.model === ""}
              onClick={() =>
                void set({ provider: props.main.provider, model: props.main.model }).then((f) => {
                  setNote(f);
                  if (f === null) props.onChanged();
                })
              }
            >
              {t("settingsA.aux.setMain")}
            </TextButton>
            <button type="button" className="settingsA-link" onClick={() => setEditing(true)}>
              {t("settingsA.aux.change")}
            </button>
          </div>
        )}
      </div>
      {editing && (
        <fieldset
          className="settingsA-aux-edit settingsA-fieldset"
          aria-label={known ? label() : task.task}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.stopPropagation();
              setEditing(false);
            }
          }}
        >
          <div className="settingsA-aux-line">
            <CompactSelect
              label={t("settingsA.main.provider")}
              value={provider}
              options={[
                { value: "auto", label: "auto" },
                ...providerChoices(props.options, t("settingsA.main.needsKey"), provider, ["auto"]),
              ]}
              onChange={setProvider}
            />
            <FreeCombo
              label={t("settingsA.main.model")}
              value={model}
              suggestions={names.get(provider) ?? []}
              onCommit={setModel}
            />
          </div>
          <div className="settingsA-aux-line">
            <span className="settingsA-small">{t("settingsA.aux.reasoning")}</span>
            <CompactSelect
              label={t("settingsA.aux.reasoning")}
              value={level}
              options={EFFORTS.map((e) => ({
                value: e,
                label: e === "" ? t("settingsA.aux.inherit") : e,
              }))}
              onChange={setLevel}
            />
          </div>
          <div className="settingsA-aux-line">
            <PrimaryButton onClick={() => void applyEdit()}>
              {t("settingsA.main.apply")}
            </PrimaryButton>
            <TextButton onClick={() => setEditing(false)}>{t("settingsA.aux.cancel")}</TextButton>
          </div>
        </fieldset>
      )}
      <StatusNote error>{note}</StatusNote>
    </div>
  );
}

export function ModelAuxiliaryPage() {
  const t = useT();
  const { api, profile } = useSettings();
  const { options } = useProviderOptions();
  const [aux, setAux] = useState<AuxAnswer | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((n) => n + 1), []);
  // biome-ignore lint/correctness/useExhaustiveDependencies: tick asks for a reload
  useEffect(() => {
    let live = true;
    void rest<AuxAnswer>(api, "GET", "/api/model/auxiliary", { profile }).then((answer) => {
      if (!live) return;
      if (answer.ok) {
        setAux(answer.data);
        setError(null);
      } else setError(answer.message);
    });
    return () => {
      live = false;
    };
  }, [api, profile, tick]);

  const resetAll = async () => {
    if (aux === null) return;
    for (const task of aux.tasks) {
      const answer = await rest(api, "POST", "/api/model/set", {
        body: { scope: "auxiliary", task: task.task, provider: "auto", model: "" },
        profile,
      });
      if (!answer.ok) {
        setError(t("settingsA.aux.failed", { message: answer.message }));
        break;
      }
    }
    reload();
  };

  return (
    <PageBody>
      <div className="settingsA-toolbar">
        <button type="button" className="settingsA-link" onClick={() => void resetAll()}>
          {t("settingsA.aux.resetAll")}
        </button>
      </div>
      <p className="settings-intro">{t("settingsA.aux.intro")}</p>
      {error !== null && <LoadError message={error} onRetry={reload} />}
      {aux === null && error === null && <SkeletonRows count={4} />}
      {aux?.tasks.map((task) => (
        <AuxRow
          key={`${task.task}:${task.provider}:${task.model}`}
          task={task}
          main={aux.main}
          options={options}
          onChanged={reload}
        />
      ))}
    </PageBody>
  );
}

// -- Model › Mixture of Agents ---------------------------------------------------------------------

interface MoaSlot {
  provider: string;
  model: string;
  enabled?: boolean;
  reasoning_effort?: string;
}

interface MoaPreset {
  reference_models: MoaSlot[];
  aggregator?: MoaSlot;
  [extra: string]: unknown;
}

const MOA_PROVIDER = "moa";

function presetsIn(value: unknown): Record<string, MoaPreset> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return {};
  const out: Record<string, MoaPreset> = {};
  for (const [name, raw] of Object.entries(value as Record<string, unknown>)) {
    if (raw === null || typeof raw !== "object") continue;
    const record = raw as Record<string, unknown>;
    out[name] = {
      ...record,
      reference_models: routesIn(record.reference_models).map((slot, i) => ({
        ...((record.reference_models as Record<string, unknown>[])[i] ?? {}),
        ...slot,
      })),
    };
  }
  return out;
}

function SlotEditor(props: {
  slot: MoaSlot;
  options: OptionsAnswer | null;
  label: string;
  onChange: (slot: MoaSlot) => void;
}) {
  const t = useT();
  const names = useModelNames();
  return (
    <div className="settingsA-slot">
      <CompactSelect
        label={`${props.label} · ${t("settingsA.moa.provider")}`}
        value={props.slot.provider}
        options={[
          { value: "", label: "—" },
          ...providerChoices(props.options, t("settingsA.main.needsKey"), props.slot.provider, [
            MOA_PROVIDER,
          ]),
        ]}
        onChange={(provider) => props.onChange({ ...props.slot, provider })}
      />
      <FreeCombo
        label={`${props.label} · ${t("settingsA.moa.model")}`}
        value={props.slot.model}
        suggestions={names.get(props.slot.provider) ?? []}
        onCommit={(model) => props.onChange({ ...props.slot, model })}
      />
    </div>
  );
}

export function ModelMoaPage() {
  const t = useT();
  const { config, savePatch, focusRow } = useSettings();
  const { options } = useProviderOptions();
  const stored = presetsIn(valueAt(config, "moa.presets"));
  const defaultName = String(valueAt(config, "moa.default_preset") ?? "");
  const [chosen, setChosen] = useState<string | null>(null);
  const [draftName, setDraftName] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const names = Object.keys(stored);
  const current =
    chosen !== null && chosen in stored
      ? chosen
      : names.includes(defaultName)
        ? defaultName
        : names[0];
  const preset = current === undefined ? undefined : stored[current];

  const write = async (patch: Record<string, unknown>) => {
    setNote((await savePatch?.({ moa: patch })) ?? null);
  };
  const writePreset = (next: MoaPreset) => {
    if (current === undefined) return;
    void write({ presets: { [current]: next } });
  };

  if (config === null) return <SkeletonRows count={3} />;
  const newName = draftName.trim();
  const canAdd = newName !== "" && !(newName in stored) && newName !== MOA_PROVIDER;
  return (
    <PageBody intro={t("settingsA.moa.intro")}>
      <div
        className="settingsA-moa"
        data-row="moa-presets"
        data-highlighted={focusRow === "moa-presets" ? "true" : undefined}
      >
        {preset !== undefined && current !== undefined && (
          <CompactSelect
            label={t("settingsA.moa.preset")}
            value={current}
            options={names.map((name) => ({ value: name, label: name }))}
            onChange={setChosen}
          />
        )}
        <div className="settingsA-moa-toolbar">
          {preset !== undefined && (
            <>
              <span className="settingsA-chip" title={t("settings.notAvailable")}>
                {t("settingsA.moa.enabled")}
                <Switch
                  size="sm"
                  checked
                  disabled
                  label={t("settingsA.moa.enabled")}
                  onChange={() => undefined}
                />
              </span>
              <TextButton
                disabled={current === defaultName}
                onClick={() => void write({ default_preset: current })}
              >
                {t("settingsA.moa.setDefault")}
              </TextButton>
              {/* the config write is additive: a preset cannot be removed from here yet */}
              <TextButton disabled title={t("settings.notAvailable")}>
                {t("settingsA.moa.delete")}
              </TextButton>
            </>
          )}
          <TextField
            aria-label={t("settingsA.moa.newPreset")}
            placeholder={t("settingsA.moa.newPreset")}
            value={draftName}
            onChange={(event) => setDraftName(event.currentTarget.value)}
          />
          <button
            type="button"
            className="settingsA-link"
            disabled={!canAdd}
            onClick={() => {
              void write({
                presets: { [newName]: { reference_models: [{ provider: "", model: "" }] } },
              });
              setChosen(newName);
              setDraftName("");
            }}
          >
            {t("settingsA.moa.addPreset")}
          </button>
        </div>
        {preset === undefined ? (
          <SettingsEmpty title={t("settingsA.moa.none")} />
        ) : (
          <>
            <p className="settingsA-small">
              {t("settingsA.moa.default")}{" "}
              <code className="settingsA-mono">{defaultName || "—"}</code>
            </p>
            {preset.reference_models.map((slot, index) => {
              const n = index + 1;
              const replace = (next: MoaSlot | null) =>
                writePreset({
                  ...preset,
                  reference_models:
                    next === null
                      ? preset.reference_models.filter((_, i) => i !== index)
                      : preset.reference_models.map((s, i) => (i === index ? next : s)),
                });
              return (
                // biome-ignore lint/suspicious/noArrayIndexKey: the position is the identity
                <div key={index} className="settingsA-moa-block">
                  <div className="settingsA-moa-head">
                    <span className="settings-row-label">
                      {t("settingsA.moa.reference", { n })}
                      <Pill>{t("settingsA.moa.referencePill")}</Pill>
                    </span>
                    <span className="settingsA-mono">{`${slot.provider || "—"} · ${slot.model || "—"}`}</span>
                  </div>
                  <div className="settingsA-moa-body">
                    <SlotEditor
                      slot={slot}
                      options={options}
                      label={t("settingsA.moa.reference", { n })}
                      onChange={replace}
                    />
                    <Switch
                      checked={slot.enabled !== false}
                      label={t("settingsA.moa.referenceOn", { n })}
                      onChange={(enabled) => replace({ ...slot, enabled })}
                    />
                  </div>
                  <TextButton
                    disabled={preset.reference_models.length <= 1}
                    onClick={() => replace(null)}
                  >
                    {t("settingsA.moa.remove")}
                  </TextButton>
                </div>
              );
            })}
            <button
              type="button"
              className="settingsA-link settingsA-center"
              onClick={() =>
                writePreset({
                  ...preset,
                  reference_models: [...preset.reference_models, { provider: "", model: "" }],
                })
              }
            >
              {t("settingsA.moa.addReference")}
            </button>
            <div className="settingsA-moa-block">
              <div className="settingsA-moa-head">
                <span className="settings-row-label">
                  {t("settingsA.moa.aggregator")}
                  <Pill>{t("settingsA.moa.aggregatorPill")}</Pill>
                </span>
                <span className="settingsA-mono">{`${preset.aggregator?.provider || "—"} · ${preset.aggregator?.model || "—"}`}</span>
              </div>
              <SlotEditor
                slot={preset.aggregator ?? { provider: "", model: "" }}
                options={options}
                label={t("settingsA.moa.aggregator")}
                onChange={(aggregator) => writePreset({ ...preset, aggregator })}
              />
            </div>
          </>
        )}
        <StatusNote error>{note}</StatusNote>
      </div>
      <SchemaFieldRow fieldKey="moa.reference_timeout" />
      <SchemaFieldRow fieldKey="moa.privacy_filter" />
    </PageBody>
  );
}
