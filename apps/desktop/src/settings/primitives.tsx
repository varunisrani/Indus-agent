// Settings page primitives (W51-b, for the wave-52 settings pages). One row grid, one switch, one
// compact select, one text field, headings, pills, cards and the "More settings" fold. They render
// the `settings-*` classes settings.css styles, so pages built before them (customPages `Row`,
// `SchemaPage`) and pages built on them look the same.
//
// Rows keep `data-row` / `data-field` / `data-highlighted`: settings search and deep links find a
// row by them and scroll it into view.

import { ChevronDown, ChevronRight } from "lucide-react";
import {
  type ButtonHTMLAttributes,
  type ReactNode,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { Segmented, TextLink } from "../app/overlay/OverlayFrame.tsx";
import { Button, Kbd } from "../ui/components.tsx";

export { Kbd, Segmented, TextLink };

/** The page's scroll body: the side gutters, an optional intro line, the rows. */
export function PageBody(props: { intro?: ReactNode; children: ReactNode }) {
  return (
    <div className="settings-page-body">
      {props.intro !== undefined && <p className="settings-intro">{props.intro}</p>}
      {props.children}
    </div>
  );
}

/**
 * Label and description on the left, the control right-aligned in a 240–352px column. `wide` puts
 * `control` on the title line and gives `below` the full width (lists, galleries, editors).
 */
export function SettingRow(props: {
  title: ReactNode;
  description?: ReactNode;
  control?: ReactNode;
  below?: ReactNode;
  wide?: boolean;
  rowId?: string;
  fieldKey?: string;
  highlighted?: boolean;
  /** Label the control (the title becomes a `<label for>`). */
  htmlFor?: string;
  /** Shown on hover over the title (e.g. the config key for power users). */
  hint?: string;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (props.highlighted) ref.current?.scrollIntoView?.({ block: "center" });
  }, [props.highlighted]);
  const title =
    props.htmlFor === undefined ? (
      <span className="settings-row-label" title={props.hint}>
        {props.title}
      </span>
    ) : (
      <label className="settings-row-label" htmlFor={props.htmlFor} title={props.hint}>
        {props.title}
      </label>
    );
  return (
    <div
      ref={ref}
      className="settings-row"
      data-wide={props.wide ? "true" : undefined}
      data-row={props.rowId}
      data-field={props.fieldKey}
      data-highlighted={props.highlighted ? "true" : undefined}
    >
      <div className="settings-row-text">
        {title}
        {props.description !== undefined && props.description !== "" && (
          <p className="settings-row-desc">{props.description}</p>
        )}
      </div>
      {props.control !== undefined && <div className="settings-row-control">{props.control}</div>}
      {props.below !== undefined && <div className="settings-row-below">{props.below}</div>}
    </div>
  );
}

/** The pill switch: `role="switch"`, named by `label`. */
export function Switch(props: {
  checked: boolean;
  label: string;
  disabled?: boolean;
  id?: string;
  size?: "md" | "sm";
  onChange: (next: boolean) => void;
}) {
  return (
    <input
      id={props.id}
      type="checkbox"
      role="switch"
      className="settings-switch"
      data-size={props.size ?? "md"}
      aria-label={props.label}
      checked={props.checked}
      aria-checked={props.checked}
      disabled={props.disabled}
      onChange={(event) => props.onChange(event.currentTarget.checked)}
    />
  );
}

export function SwitchRow(props: {
  title: string;
  description?: ReactNode;
  checked: boolean;
  disabled?: boolean;
  rowId?: string;
  highlighted?: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <SettingRow
      title={props.title}
      description={props.description}
      {...(props.rowId === undefined ? {} : { rowId: props.rowId })}
      {...(props.highlighted === undefined ? {} : { highlighted: props.highlighted })}
      control={
        <Switch
          checked={props.checked}
          label={props.title}
          {...(props.disabled === undefined ? {} : { disabled: props.disabled })}
          onChange={props.onChange}
        />
      }
    />
  );
}

export interface SelectOption {
  readonly value: string;
  readonly label: string;
}

/**
 * A compact select sized to its content (or `width`). A real `<select>` under our frame: keyboard
 * and screen readers get the platform control, the closed look is ours (W51-b-7).
 */
export function CompactSelect(props: {
  value: string;
  options: readonly SelectOption[];
  onChange: (value: string) => void;
  id?: string;
  label?: string;
  disabled?: boolean;
  width?: number;
}) {
  return (
    <span
      className="settings-select"
      style={props.width === undefined ? undefined : { width: props.width }}
    >
      <select
        id={props.id}
        className="settings-input"
        aria-label={props.label}
        value={props.value}
        disabled={props.disabled}
        onChange={(event) => props.onChange(event.currentTarget.value)}
      >
        {props.options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      <ChevronDown aria-hidden="true" className="settings-select-caret" />
    </span>
  );
}

/** The 214px text field (commit on blur or Enter is the caller's). */
export function TextField(
  props: Omit<React.InputHTMLAttributes<HTMLInputElement>, "className"> & { wide?: boolean },
) {
  const { wide, ...rest } = props;
  return (
    <input
      type="text"
      autoComplete="off"
      spellCheck={false}
      {...rest}
      className="settings-input"
      data-wide={wide ? "true" : undefined}
    />
  );
}

/** A text field with a suggestion list (long option sets such as voices or models). */
export function FreeCombo(props: {
  value: string;
  suggestions: readonly string[];
  onCommit: (value: string) => void;
  label: string;
  placeholder?: string;
  disabled?: boolean;
}) {
  const [draft, setDraft] = useState(props.value);
  const [open, setOpen] = useState(false);
  const listId = useId();
  useEffect(() => setDraft(props.value), [props.value]);
  const needle = draft.trim().toLowerCase();
  const shown = props.suggestions
    .filter((s) => needle === "" || s.toLowerCase().includes(needle))
    .slice(0, 50);
  const commit = (value: string) => {
    setOpen(false);
    if (value !== props.value) props.onCommit(value);
  };
  return (
    <span className="settings-combo">
      <input
        type="text"
        role="combobox"
        aria-label={props.label}
        aria-expanded={open}
        aria-controls={listId}
        aria-autocomplete="list"
        className="settings-input"
        value={draft}
        placeholder={props.placeholder}
        disabled={props.disabled}
        autoComplete="off"
        spellCheck={false}
        onFocus={() => setOpen(true)}
        onChange={(event) => {
          setDraft(event.currentTarget.value);
          setOpen(true);
        }}
        onBlur={() => commit(draft)}
        onKeyDown={(event) => {
          if (event.key === "Enter") commit(draft);
          if (event.key === "Escape") setOpen(false);
        }}
      />
      <ChevronDown aria-hidden="true" className="settings-select-caret" />
      {open && shown.length > 0 && (
        <div id={listId} role="listbox" className="settings-combo-list">
          {shown.map((s) => (
            <div
              key={s}
              role="option"
              tabIndex={-1}
              aria-selected={s === draft}
              onMouseDown={(event) => {
                event.preventDefault();
                setDraft(s);
                commit(s);
              }}
            >
              {s}
            </div>
          ))}
        </div>
      )}
    </span>
  );
}

/** A heading inside a page: icon, title, optional count pill and right-hand content. */
export function SectionHeading(props: {
  icon?: ReactNode;
  title: ReactNode;
  count?: number;
  aside?: ReactNode;
}) {
  return (
    <div className="settings-section-heading">
      {props.icon}
      <h3>{props.title}</h3>
      {props.count !== undefined && <Pill>{props.count}</Pill>}
      {props.aside !== undefined && <span className="settings-section-aside">{props.aside}</span>}
    </div>
  );
}

/** A quiet label between groups of rows ("Defaults"). */
export function GroupLabel(props: { children: ReactNode }) {
  return <p className="settings-group-label">{props.children}</p>;
}

export function SettingsEmpty(props: { title: ReactNode; body?: ReactNode }) {
  return (
    <div className="settings-empty-state">
      <p className="settings-empty-title">{props.title}</p>
      {props.body !== undefined && <p className="settings-empty-body">{props.body}</p>}
    </div>
  );
}

export function Pill(props: { children: ReactNode; tone?: "neutral" | "accent" | "danger" }) {
  return (
    <span className="settings-pill" data-tone={props.tone ?? "neutral"}>
      {props.children}
    </span>
  );
}

/** A soft accent-tinted panel (recommended choice, banners). */
export function TintCard(props: { children: ReactNode; selected?: boolean }) {
  return (
    <div className="settings-tint-card" data-selected={props.selected ? "true" : undefined}>
      {props.children}
    </div>
  );
}

/** A red-edged panel for destructive or failed things (uninstall, errors). */
export function DangerCard(props: { title?: ReactNode; children: ReactNode }) {
  return (
    <div className="settings-danger-card">
      {props.title !== undefined && <p className="settings-danger-title">{props.title}</p>}
      {props.children}
    </div>
  );
}

type ButtonProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, "className"> & {
  icon?: ReactNode;
};

/** Quiet text action (no frame). */
export function TextButton(props: ButtonProps) {
  return <Button {...props} variant="text" size="sm" />;
}

export function OutlineButton(props: ButtonProps) {
  return <Button {...props} variant="outline" size="sm" />;
}

export function PrimaryButton(props: ButtonProps) {
  return <Button {...props} variant="primary" size="sm" />;
}

/**
 * "More settings": a fold at the end of a curated page holding the rest of its fields, so every
 * setting stays reachable. It opens itself when a search hit or link points inside it.
 */
export function MoreFields(props: { label: string; children: ReactNode; open?: boolean }) {
  const [open, setOpen] = useState(props.open === true);
  useEffect(() => {
    if (props.open) setOpen(true);
  }, [props.open]);
  const bodyId = useId();
  return (
    <div className="settings-more">
      <button
        type="button"
        className="settings-more-toggle"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setOpen(!open)}
      >
        {open ? (
          <ChevronDown aria-hidden="true" size={13} />
        ) : (
          <ChevronRight aria-hidden="true" size={13} />
        )}
        {props.label}
      </button>
      <div id={bodyId} hidden={!open}>
        {open && props.children}
      </div>
    </div>
  );
}
