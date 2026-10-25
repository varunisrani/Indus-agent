// Small pieces the slice A pages share (W52-i), on the W51-b primitives: a schema row inside a
// hand-built page, placeholder rows for reference settings we have no feature behind, the slider,
// skeleton rows and a status note.

import type { ReactNode } from "react";
import { Segmented } from "../app/overlay/OverlayFrame.tsx";
import { useT } from "../i18n/index.tsx";
import { useSettings } from "./context.tsx";
import { SettingRow, Switch } from "./primitives.tsx";
import { FieldRow } from "./schemaPage.tsx";
import type { SchemaField } from "./search.ts";
import { valueAt } from "./settingsApi.ts";
import "./settingsA.css";

/** Skeleton rows that keep the page's rhythm while it loads. */
export function SkeletonRows(props: { count?: number }) {
  const rows = Array.from({ length: props.count ?? 4 }, (_, i) => i);
  return (
    <div className="settingsA-skeleton" aria-hidden="true">
      {rows.map((i) => (
        <div key={i} className="settingsA-skeleton-row">
          <div>
            <span className="settingsA-bar" data-size="title" />
            <span className="settingsA-bar" data-size="caption" />
          </div>
          <span className="settingsA-bar" data-size="control" />
        </div>
      ))}
    </div>
  );
}

/** One schema field rendered on a hand-built page (search and links land on it too). */
export function SchemaFieldRow(props: { fieldKey: string; field?: SchemaField }) {
  const { schema, config, saveField, focusField } = useSettings();
  const field = props.field ?? schema?.[props.fieldKey];
  if (field === undefined || config === null) {
    return schema === null || config === null ? <SkeletonRows count={1} /> : null;
  }
  return (
    <FieldRow
      fieldKey={props.fieldKey}
      field={field}
      value={valueAt(config, props.fieldKey)}
      disabled={saveField === undefined}
      highlighted={focusField === props.fieldKey}
      onSave={async (key, value) =>
        saveField === undefined ? "not available" : await saveField(key, value)
      }
    />
  );
}

/** A reference setting with no feature behind it in this build: shown, off and disabled. */
export function PlaceholderSwitchRow(props: { title: string; checked?: boolean }) {
  const t = useT();
  return (
    <SettingRow
      title={props.title}
      description={t("settings.notAvailable")}
      control={
        <Switch checked={props.checked ?? false} label={props.title} disabled onChange={noop} />
      }
    />
  );
}

/** A reference choice with no feature behind it: the segmented control, disabled. */
export function PlaceholderSegmentedRow(props: {
  title: string;
  value: string;
  options: ReadonlyArray<{ value: string; label: string }>;
}) {
  const t = useT();
  return (
    <SettingRow
      title={props.title}
      description={t("settings.notAvailable")}
      control={
        <fieldset className="settingsA-fieldset" disabled>
          <Segmented
            label={props.title}
            value={props.value}
            options={props.options}
            onChange={noop}
          />
        </fieldset>
      }
    />
  );
}

/** Track 160px, accent fill, value at the right. */
export function Slider(props: {
  label: string;
  value: number;
  min?: number;
  max?: number;
  disabled?: boolean;
  format?: (value: number) => string;
  onChange: (value: number) => void;
}) {
  const min = props.min ?? 0;
  const max = props.max ?? 100;
  const share = max === min ? 0 : ((props.value - min) / (max - min)) * 100;
  return (
    <span className="settingsA-slider">
      <input
        type="range"
        aria-label={props.label}
        min={min}
        max={max}
        value={props.value}
        disabled={props.disabled}
        style={{ "--settingsA-fill": `${share}%` } as React.CSSProperties}
        onChange={(event) => props.onChange(Number(event.currentTarget.value))}
      />
      <output>{props.format ? props.format(props.value) : String(props.value)}</output>
    </span>
  );
}

/** A 12px status line under a control: neutral or danger. */
export function StatusNote(props: { children: ReactNode; error?: boolean }) {
  if (props.children === null || props.children === "" || props.children === undefined) {
    return null;
  }
  return (
    <p className={props.error ? "settings-row-error" : "settings-row-note"} role="status">
      {props.children}
    </p>
  );
}

/** A danger caption with a Retry text button (a page whose data could not be read). */
export function LoadError(props: { message: string; onRetry: () => void }) {
  const t = useT();
  return (
    <p className="settingsA-load-error" role="alert">
      <span>{props.message}</span>
      <button type="button" className="settingsA-link" onClick={props.onRetry}>
        {t("settingsA.retry")}
      </button>
    </p>
  );
}

function noop(): void {}
