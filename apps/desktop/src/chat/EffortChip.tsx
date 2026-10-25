// The composer's reasoning-effort chip (W51-b-6, menu W52-h): shows the current model's effort
// preset and sets it from a small menu — a Thinking switch (off = `none`) and the effort levels.
// The caller applies the choice (preset store, `config.set reasoning`). Picking a level closes the
// menu; the switch keeps it open.

import { useCallback, useRef, useState } from "react";
import type { MessageKey } from "../i18n/index.tsx";
import { useT } from "../i18n/index.tsx";
import { Chip, MenuLabel, MenuPanel, MenuRow, MenuSeparator } from "../ui/components.tsx";

const LEVELS: ReadonlyArray<{ value: string | null; short: MessageKey; long: MessageKey }> = [
  { value: null, short: "effort.auto", long: "effort.autoLong" },
  { value: "none", short: "effort.none", long: "effort.noneLong" },
  { value: "minimal", short: "effort.minimal", long: "effort.minimalLong" },
  { value: "low", short: "effort.low", long: "effort.lowLong" },
  { value: "medium", short: "effort.medium", long: "effort.mediumLong" },
  { value: "high", short: "effort.high", long: "effort.highLong" },
  { value: "xhigh", short: "effort.xhigh", long: "effort.xhighLong" },
  { value: "max", short: "effort.max", long: "effort.maxLong" },
  { value: "ultra", short: "effort.ultra", long: "effort.ultraLong" },
];

/** The levels the menu lists under "Effort" (Off is the Thinking switch). */
const LISTED = LEVELS.filter((level) => level.value !== "none");

export const DEFAULT_THINKING_EFFORT = "medium";

export function effortShort(value: string | null): MessageKey {
  return LEVELS.find((level) => level.value === value)?.short ?? "effort.auto";
}

/** The menu body: Options (Thinking switch) and the Effort list. Shared with the model menu. */
export function EffortOptions(props: {
  effort: string | null;
  onPick: (value: string | null) => void;
  /** Called after a level is picked (the switch never closes). */
  onPicked?: () => void;
}) {
  const t = useT();
  const lastOn = useRef<string>(DEFAULT_THINKING_EFFORT);
  if (props.effort !== null && props.effort !== "none") lastOn.current = props.effort;
  const thinking = props.effort !== "none";
  return (
    <>
      <MenuLabel>{t("effort.options")}</MenuLabel>
      <MenuRow
        kind="switch"
        label={t("effort.thinking")}
        checked={thinking}
        onSelect={() => props.onPick(thinking ? "none" : lastOn.current)}
      />
      <MenuSeparator />
      <MenuLabel>{t("effort.effort")}</MenuLabel>
      {LISTED.map((level) => (
        <MenuRow
          key={level.value ?? "auto"}
          kind="radio"
          label={t(level.long)}
          checked={level.value === props.effort}
          onSelect={() => {
            props.onPick(level.value);
            props.onPicked?.();
          }}
        />
      ))}
    </>
  );
}

export function EffortChip(props: {
  effort: string | null;
  onPick: (value: string | null) => void;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);
  return (
    <div className="desk-menu-root" ref={trigger}>
      <Chip
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t("effort.label", { level: t(effortShort(props.effort)) })}
        onClick={() => setOpen(!open)}
      >
        {t(effortShort(props.effort))}
      </Chip>
      {open && (
        <MenuPanel
          label={t("effort.title")}
          onClose={close}
          keepOpenFor={trigger}
          className="effort-menu"
        >
          <EffortOptions effort={props.effort} onPick={props.onPick} onPicked={close} />
        </MenuPanel>
      )}
    </div>
  );
}
