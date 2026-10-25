// Settings › Providers › API keys and Tools & Keys › Keys (W52-j): one quiet row per stored key
// name (status dot, name, the masked value or "Paste key"); a click opens the row to paste,
// save or remove. Keys go to the backend's secret store and leave the renderer at once.
// W60-L2-1: Tools & Keys first lists the key and address variables our tools declare
// (`GET /api/tools/keys`), grouped by toolset, with "Set" / "Not set" only — never a value or a
// preview; the same row saves or removes one through `/api/env`.

import { ChevronRight, ExternalLink, Search } from "lucide-react";
import { useCallback, useEffect, useId, useState } from "react";
import { useT } from "../../i18n/index.tsx";
import { attempt, useSettings } from "../context.tsx";
import {
  PageBody,
  PrimaryButton,
  SettingRow,
  SettingsEmpty,
  TextButton,
  TextField,
} from "../primitives.tsx";
import { rest } from "../settingsApi.ts";
import { Note, SecretInput } from "./kit.tsx";

export interface EnvRow {
  readonly category: string;
  readonly description: string;
  readonly advanced: boolean;
  readonly is_password: boolean;
  readonly is_set: boolean;
  readonly redacted_value: string | null;
  readonly url?: string | null;
}

/** "API key for OpenRouter" → "OpenRouter"; anything else → the name with spaces. */
export function keyTitle(name: string, row: EnvRow): string {
  const match = /^API key for (.+)$/i.exec(row.description.trim());
  if (match?.[1]) return match[1];
  return name.replace(/_/g, " ");
}

function useEnv() {
  const { api, profile } = useSettings();
  const [rows, setRows] = useState<Readonly<Record<string, EnvRow>> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    void rest<Record<string, EnvRow>>(api, "GET", "/api/env", { profile }).then((answer) => {
      if (answer.ok) setRows(answer.data);
      else setError(answer.message);
    });
  }, [api, profile]);
  useEffect(load, [load]);
  const save = async (name: string, value: string) => {
    const answer = await rest(api, "PUT", "/api/env", { body: { key: name, value }, profile });
    if (answer.ok) load();
    return answer.ok ? null : answer.message;
  };
  const clear = async (name: string) => {
    const answer = await rest(api, "DELETE", "/api/env", { body: { key: name }, profile });
    if (answer.ok) load();
    return answer.ok ? null : answer.message;
  };
  return { rows, error, setError, save, clear };
}

function KeyLine(props: {
  name: string;
  title: string;
  row: EnvRow;
  /** Replaces the masked preview on the right (tool variables show their state only). */
  shown?: string;
  /** Under the name in the open row (the backend that reads it). */
  caption?: string;
  open: boolean;
  highlighted: boolean;
  onToggle: () => void;
  onSave: (name: string, value: string) => Promise<string | null>;
  onClear: (name: string) => Promise<string | null>;
}) {
  const t = useT();
  const { bridge } = useSettings();
  const [value, setValue] = useState("");
  const [note, setNote] = useState<{ text: string; error: boolean } | null>(null);
  const bodyId = useId();
  const external = bridge?.external;
  return (
    <div
      className="sb-key"
      data-open={props.open ? "true" : undefined}
      data-row={`key-${props.name}`}
      data-highlighted={props.highlighted ? "true" : undefined}
    >
      <button
        type="button"
        className="sb-key-head"
        aria-expanded={props.open}
        aria-controls={bodyId}
        onClick={props.onToggle}
      >
        <span className="sb-dot" data-on={props.row.is_set ? "true" : undefined} />
        <span className="sb-key-title">{props.title}</span>
        <span
          className="sb-key-value"
          data-set={props.row.is_set && props.shown === undefined ? "true" : undefined}
        >
          {props.shown ?? (props.row.is_set ? props.row.redacted_value : t("settings.keys2.paste"))}
        </span>
      </button>
      {props.open && (
        <div id={bodyId} className="sb-key-body">
          <span className="sb-mono">
            {props.caption === undefined ? props.name : `${props.caption} · ${props.name}`}
          </span>
          <span className="settings-inline">
            {props.row.is_password ? (
              <SecretInput
                label={props.name}
                value={value}
                onChange={setValue}
                placeholder={t(props.row.is_set ? "settings.keys.replace" : "settings.keys.paste")}
              />
            ) : (
              <TextField
                aria-label={props.name}
                placeholder="https://…"
                value={value}
                onChange={(event) => setValue(event.currentTarget.value)}
              />
            )}
            <PrimaryButton
              disabled={value.trim() === ""}
              onClick={async () => {
                const failure = await props.onSave(props.name, value);
                if (failure === null) setValue(""); // the key leaves the renderer
                setNote(
                  failure === null
                    ? { text: t("settings.saved"), error: false }
                    : { text: failure, error: true },
                );
              }}
            >
              {t("settings.save")}
            </PrimaryButton>
          </span>
          <span className="settings-inline">
            {props.row.is_set && (
              <TextButton
                onClick={async () => {
                  const failure = await props.onClear(props.name);
                  setNote(
                    failure === null
                      ? { text: t("settings.keys.cleared"), error: false }
                      : { text: failure, error: true },
                  );
                }}
              >
                {t("settings.keys.clear")}
              </TextButton>
            )}
            {typeof props.row.url === "string" && props.row.url !== "" && external && (
              <TextButton
                icon={<ExternalLink aria-hidden="true" size={13} />}
                onClick={() => void attempt(() => external.open({ url: props.row.url as string }))}
              >
                {t("settings.keys2.getKey")}
              </TextButton>
            )}
          </span>
          <Note text={note?.text ?? null} error={note?.error ?? false} />
        </div>
      )}
    </div>
  );
}

function KeyList(props: {
  entries: ReadonlyArray<readonly [string, EnvRow]>;
  titleOf: (name: string, row: EnvRow) => string;
  shownOf?: (name: string, row: EnvRow) => string;
  captionOf?: (name: string) => string | undefined;
  onSave: (name: string, value: string) => Promise<string | null>;
  onClear: (name: string) => Promise<string | null>;
}) {
  const { focusRow } = useSettings();
  const [open, setOpen] = useState<string | null>(
    focusRow?.startsWith("key-") ? focusRow.slice(4) : null,
  );
  return (
    <div className="sb-keys">
      {props.entries.map(([name, row]) => (
        <KeyLine
          key={name}
          name={name}
          title={props.titleOf(name, row)}
          row={row}
          {...(props.shownOf === undefined ? {} : { shown: props.shownOf(name, row) })}
          {...(props.captionOf?.(name) === undefined
            ? {}
            : { caption: props.captionOf(name) as string })}
          open={open === name}
          highlighted={focusRow === `key-${name}`}
          onToggle={() => setOpen((now) => (now === name ? null : name))}
          onSave={props.onSave}
          onClear={props.onClear}
        />
      ))}
    </div>
  );
}

export function ProviderKeysPage() {
  const t = useT();
  const { go, focusRow } = useSettings();
  const env = useEnv();
  const [needle, setNeedle] = useState("");
  // one row per provider: names that share a title (GEMINI / GOOGLE) show the set one, else the first
  const byTitle = new Map<string, readonly [string, EnvRow]>();
  for (const entry of Object.entries(env.rows ?? {})) {
    const [name, row] = entry;
    if (!row.is_password || row.category !== "provider") continue;
    const title = keyTitle(name, row);
    const held = byTitle.get(title);
    if (held === undefined || (!held[1].is_set && row.is_set)) byTitle.set(title, entry);
  }
  const entries = [...byTitle.values()].filter(([name, row]) => {
    const q = needle.trim().toLowerCase();
    return (
      q === "" || name.toLowerCase().includes(q) || keyTitle(name, row).toLowerCase().includes(q)
    );
  });
  return (
    <PageBody>
      <div data-row="api-keys" data-highlighted={focusRow === "api-keys" ? "true" : undefined}>
        <button
          type="button"
          className="sb-link-row"
          onClick={() => go?.({ view: "providers", page: "endpoints" })}
        >
          <span className="sb-link-row-text">
            <span className="settings-row-label">{t("settings.keys2.endpointRow")}</span>
            <span className="settings-row-desc">{t("settings.keys2.endpointRowBody")}</span>
          </span>
          <ChevronRight aria-hidden="true" size={14} />
        </button>
      </div>
      <label className="sb-search">
        <Search aria-hidden="true" size={13} />
        <input
          type="search"
          aria-label={t("settings.keys2.search")}
          placeholder={t("settings.keys2.search")}
          value={needle}
          onChange={(event) => setNeedle(event.currentTarget.value)}
        />
      </label>
      <KeyList entries={entries} titleOf={keyTitle} onSave={env.save} onClear={env.clear} />
      {env.rows !== null && entries.length === 0 && (
        <SettingsEmpty title={t("settings.keys2.noMatch")} />
      )}
      <Note text={env.error} error />
    </PageBody>
  );
}

/** One backend of `GET /api/tools/keys` (W60-L2-1): names and set state only. */
export interface ToolKeyGroup {
  readonly toolset: string;
  readonly backend: string;
  readonly label: string;
  readonly vars: ReadonlyArray<{
    readonly name: string;
    readonly kind: "key" | "url";
    readonly is_set: boolean;
  }>;
}

/** The declared variables per toolset, in the backend's order; a name shared by two backends
 * shows once (under the first) with both labels as its caption. */
export function toolKeyRows(groups: readonly ToolKeyGroup[]): Array<{
  toolset: string;
  rows: Array<{ name: string; kind: "key" | "url"; is_set: boolean; labels: string[] }>;
}> {
  const out: Array<{
    toolset: string;
    rows: Array<{ name: string; kind: "key" | "url"; is_set: boolean; labels: string[] }>;
  }> = [];
  const seen = new Map<string, { labels: string[] }>();
  for (const group of groups) {
    let bucket = out.find((entry) => entry.toolset === group.toolset);
    if (bucket === undefined) {
      bucket = { toolset: group.toolset, rows: [] };
      out.push(bucket);
    }
    for (const item of group.vars) {
      const held = seen.get(item.name);
      if (held !== undefined) {
        if (!held.labels.includes(group.label)) held.labels.push(group.label);
        continue;
      }
      const row = { ...item, labels: [group.label] };
      seen.set(item.name, row);
      bucket.rows.push(row);
    }
  }
  return out;
}

function useToolKeys(env: ReturnType<typeof useEnv>) {
  const { api, profile } = useSettings();
  const [groups, setGroups] = useState<readonly ToolKeyGroup[] | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const load = useCallback(() => {
    void rest<{ tools: ToolKeyGroup[] }>(api, "GET", "/api/tools/keys", { profile }).then(
      (answer) => {
        if (answer.ok) setGroups(answer.data.tools);
        // a backend without the route (an older one) declares nothing: no error to show
        else if (answer.status === 404) setGroups([]);
        else setFailed(answer.message);
      },
    );
  }, [api, profile]);
  // biome-ignore lint/correctness/useExhaustiveDependencies: a key list change reloads the states
  useEffect(load, [load, env.rows]);
  return { groups, failed };
}

/** Tools & Keys › Keys: the variables our tools read, the other keys a person added for tools
 * and skills, and the add form. */
export function ToolKeysPage() {
  const t = useT();
  const { focusRow } = useSettings();
  const env = useEnv();
  const tools = useToolKeys(env);
  const [newName, setNewName] = useState("");
  const [newValue, setNewValue] = useState("");
  const declared = toolKeyRows(tools.groups ?? []);
  const declaredNames = new Set(declared.flatMap((group) => group.rows.map((row) => row.name)));
  const entries = Object.entries(env.rows ?? {}).filter(
    ([name, row]) => row.is_password && row.category !== "provider" && !declaredNames.has(name),
  );
  const nameId = useId();
  return (
    <PageBody intro={t("settings.toolKeys.intro")}>
      {declared.map((group) => (
        <section
          key={group.toolset}
          className="sb-key-group"
          aria-label={t("settings.toolKeys.group", { toolset: group.toolset })}
          data-row={`tool-keys-${group.toolset}`}
        >
          <h3 className="sb-caps">{t("settings.toolKeys.group", { toolset: group.toolset })}</h3>
          <KeyList
            entries={group.rows.map(
              (row) =>
                [
                  row.name,
                  {
                    category: "tool",
                    description: "",
                    advanced: false,
                    is_password: row.kind === "key",
                    is_set: row.is_set,
                    redacted_value: null,
                  },
                ] as const,
            )}
            titleOf={(name) => name.replace(/_/g, " ")}
            shownOf={(_, row) =>
              t(row.is_set ? "settings.toolKeys.set" : "settings.toolKeys.unset")
            }
            captionOf={(name) => group.rows.find((row) => row.name === name)?.labels.join(", ")}
            onSave={env.save}
            onClear={env.clear}
          />
        </section>
      ))}
      <Note text={tools.failed} error />
      {declared.length > 0 && entries.length > 0 && (
        <h3 className="sb-caps sb-key-group">{t("settings.toolKeys.others")}</h3>
      )}
      <div data-row="tool-keys" data-highlighted={focusRow === "tool-keys" ? "true" : undefined}>
        <KeyList
          entries={entries}
          titleOf={(name) => name.replace(/_/g, " ")}
          onSave={env.save}
          onClear={env.clear}
        />
      </div>
      {env.rows !== null &&
        (tools.groups !== null || tools.failed !== null) &&
        declared.length === 0 &&
        entries.length === 0 && (
          <SettingsEmpty
            title={t("settings.toolKeys.none")}
            body={t("settings.toolKeys.noneBody")}
          />
        )}
      <SettingRow
        title={t("settings.keys.other")}
        description={t("settings.keys.otherBody")}
        htmlFor={nameId}
        control={
          <div className="sb-stack">
            <TextField
              id={nameId}
              placeholder="EXAMPLE_API_KEY"
              value={newName}
              onChange={(event) => setNewName(event.currentTarget.value.toUpperCase())}
            />
            <span className="settings-inline">
              <SecretInput
                label={t("settings.keys.value")}
                value={newValue}
                onChange={setNewValue}
              />
              <PrimaryButton
                disabled={!/^[A-Z][A-Z0-9_]*$/.test(newName) || newValue.trim() === ""}
                onClick={async () => {
                  const failure = await env.save(newName, newValue);
                  if (failure === null) {
                    setNewName("");
                    setNewValue("");
                  } else env.setError(failure);
                }}
              >
                {t("settings.save")}
              </PrimaryButton>
            </span>
          </div>
        }
      />
      <Note text={env.error} error />
    </PageBody>
  );
}
