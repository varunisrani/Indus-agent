// A config-backed settings page (P20-32): the fields a page owns, read from the backend's schema
// and form tree, each with the control its type asks for. A change is written at once as a
// one-field patch (`PUT /api/config`, additive), for the profile the "Applies to" chips name.

import { Eye, EyeOff } from "lucide-react";
import { type KeyboardEvent, type ReactNode, useEffect, useId, useRef, useState } from "react";
import { useT } from "../i18n/index.tsx";
import { fieldsOnPage, type PageDef } from "./pages.ts";
import { fieldLabel, type SchemaField, type SchemaFields } from "./search.ts";
import { patchFor, type RestLike, rest, SECRET_PLACEHOLDER, valueAt } from "./settingsApi.ts";

type RowState =
  | { kind: "idle" }
  | { kind: "saving" }
  | { kind: "saved" }
  | { kind: "error"; message: string };

/** The value to send for `text` typed into a field of `type`, or an error key. */
export function parseInput(
  field: SchemaField,
  text: string,
  current: unknown,
): { ok: true; value: unknown } | { ok: false } {
  const trimmed = text.trim();
  switch (field.type) {
    case "integer":
      if (trimmed === "") return { ok: true, value: null };
      return /^-?\d+$/.test(trimmed) ? { ok: true, value: Number(trimmed) } : { ok: false };
    case "number": {
      if (trimmed === "") return { ok: true, value: null };
      const value = Number(trimmed);
      return Number.isFinite(value) ? { ok: true, value } : { ok: false };
    }
    case "list": {
      const structured = Array.isArray(current) && current.some((v) => typeof v !== "string");
      if (structured || trimmed.startsWith("[")) {
        try {
          const value: unknown = JSON.parse(trimmed === "" ? "[]" : trimmed);
          return Array.isArray(value) ? { ok: true, value } : { ok: false };
        } catch {
          return { ok: false };
        }
      }
      return {
        ok: true,
        value: text
          .split("\n")
          .map((line) => line.trim())
          .filter((line) => line !== ""),
      };
    }
    default:
      return { ok: true, value: text };
  }
}

function listText(value: unknown): string {
  if (!Array.isArray(value)) return "";
  if (value.every((v) => typeof v === "string")) return value.join("\n");
  return JSON.stringify(value, null, 2);
}

function inputText(value: unknown): string {
  if (value === null || value === undefined) return "";
  return typeof value === "string" ? value : String(value);
}

export interface FieldRowProps {
  fieldKey: string;
  field: SchemaField;
  value: unknown;
  disabled: boolean;
  highlighted: boolean;
  onSave: (key: string, value: unknown) => Promise<string | null>;
}

/** One setting: label, description and its control; saves itself. */
export function FieldRow(props: FieldRowProps) {
  const t = useT();
  const { fieldKey, field, value } = props;
  const id = useId();
  const secret = value === SECRET_PLACEHOLDER;
  const initial = secret ? "" : field.type === "list" ? listText(value) : inputText(value);
  const [draft, setDraft] = useState(initial);
  const [state, setState] = useState<RowState>({ kind: "idle" });
  const [revealed, setRevealed] = useState(false);
  const [invalid, setInvalid] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => setDraft(initial), [initial]);
  useEffect(() => {
    if (props.highlighted) ref.current?.scrollIntoView?.({ block: "center" });
  }, [props.highlighted]);

  const save = async (next: unknown) => {
    setState({ kind: "saving" });
    const failure = await props.onSave(fieldKey, next);
    setState(failure === null ? { kind: "saved" } : { kind: "error", message: failure });
  };

  const commitText = () => {
    if (secret && draft.trim() === "") return; // an untouched secret is never re-sent
    const before = field.type === "list" ? listText(value) : inputText(value);
    if (!secret && draft === before) return;
    const parsed = parseInput(field, draft, value);
    if (!parsed.ok) {
      setInvalid(true);
      return;
    }
    setInvalid(false);
    void save(parsed.value);
  };

  const onKey = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") commitText();
  };

  let control: ReactNode;
  if (field.type === "boolean") {
    control = (
      <input
        id={id}
        type="checkbox"
        role="switch"
        className="settings-switch"
        checked={value === true}
        aria-checked={value === true}
        disabled={props.disabled}
        onChange={(event) => void save(event.currentTarget.checked)}
      />
    );
  } else if (field.type === "enum") {
    const options = field.options ?? [];
    const current = inputText(value);
    control = (
      <select
        id={id}
        className="settings-input"
        value={current}
        disabled={props.disabled}
        onChange={(event) => void save(event.currentTarget.value)}
      >
        {!options.includes(current) && <option value={current}>{current}</option>}
        {options.map((option) => (
          <option key={option} value={option}>
            {option === "" ? t("settings.field.none") : option}
          </option>
        ))}
      </select>
    );
  } else if (field.type === "list") {
    control = (
      <textarea
        id={id}
        className="settings-input settings-textarea"
        value={draft}
        rows={Math.min(8, Math.max(2, draft.split("\n").length))}
        disabled={props.disabled}
        aria-invalid={invalid}
        placeholder={t("settings.field.listHint")}
        onChange={(event) => setDraft(event.currentTarget.value)}
        onBlur={commitText}
      />
    );
  } else {
    const numeric = field.type === "integer" || field.type === "number";
    control = (
      <span className="settings-inline">
        <input
          id={id}
          className="settings-input"
          type={secret && !revealed ? "password" : "text"}
          inputMode={numeric ? "decimal" : undefined}
          value={draft}
          disabled={props.disabled}
          aria-invalid={invalid}
          placeholder={secret ? t("settings.field.secretStored") : ""}
          autoComplete="off"
          spellCheck={false}
          onChange={(event) => setDraft(event.currentTarget.value)}
          onBlur={commitText}
          onKeyDown={onKey}
        />
        {secret && (
          <button
            type="button"
            className="icon-button"
            aria-label={t(revealed ? "settings.field.hide" : "settings.field.reveal")}
            onClick={() => setRevealed((now) => !now)}
          >
            {revealed ? (
              <EyeOff aria-hidden="true" size={14} />
            ) : (
              <Eye aria-hidden="true" size={14} />
            )}
          </button>
        )}
      </span>
    );
  }

  return (
    <div
      ref={ref}
      className="settings-row"
      data-field={fieldKey}
      data-highlighted={props.highlighted ? "true" : undefined}
    >
      <div className="settings-row-text">
        <label htmlFor={id} className="settings-row-label">
          {fieldLabel(fieldKey)}
        </label>
        {field.description !== "" && <p className="settings-row-desc">{field.description}</p>}
        <code className="settings-row-key">{fieldKey}</code>
      </div>
      <div className="settings-row-control">
        {control}
        {invalid && <span className="settings-row-error">{t("settings.field.invalid")}</span>}
        {state.kind === "saving" && (
          <span className="settings-row-note">{t("settings.saving")}</span>
        )}
        {state.kind === "saved" && <span className="settings-row-note">{t("settings.saved")}</span>}
        {state.kind === "error" && <span className="settings-row-error">{state.message}</span>}
      </div>
    </div>
  );
}

export interface SchemaPageProps {
  page: PageDef;
  schema: SchemaFields;
  /** `GET /api/config` (the form tree). */
  config: unknown;
  api: RestLike | null;
  /** The "Applies to" choice; null = the connection's own profile. */
  profile: string | null;
  focusField: string | null;
  /** The saved value, so the overlay's copy of the form tree stays current. */
  onSaved?: (key: string, value: unknown) => void;
}

export function SchemaPage(props: SchemaPageProps) {
  const t = useT();
  const [managed, setManaged] = useState<string | null>(null);
  const keys = fieldsOnPage(props.page, Object.keys(props.schema));
  if (keys.length === 0) {
    return <p className="settings-empty">{t("settings.page.nothing")}</p>;
  }
  const save = async (key: string, value: unknown): Promise<string | null> => {
    const answer = await rest<{ ok: boolean }>(props.api, "PUT", "/api/config", {
      body: { config: patchFor(key, value) },
      profile: props.profile,
    });
    if (answer.ok) {
      props.onSaved?.(key, value);
      return null;
    }
    if (answer.code === "config_managed") setManaged(answer.message);
    return answer.message || t("settings.saveFailed");
  };
  return (
    <div className="settings-rows">
      {managed !== null && (
        <p className="settings-banner" role="status">
          {managed}
        </p>
      )}
      {keys.map((key) => (
        <FieldRow
          key={`${props.profile ?? ""}:${key}`}
          fieldKey={key}
          field={props.schema[key] as SchemaField}
          value={valueAt(props.config, key)}
          disabled={managed !== null}
          highlighted={props.focusField === key}
          onSave={save}
        />
      ))}
    </div>
  );
}
