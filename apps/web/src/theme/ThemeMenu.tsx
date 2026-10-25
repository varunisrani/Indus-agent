// The theme picker in the sidebar footer: a button naming the current preset that opens a list of
// presets, each with its colour swatch and a one-line description.

import { Check, Palette } from "lucide-react";
import { useCallback, useId, useRef, useState } from "react";
import { cx } from "../ui/cx.ts";
import { useDismiss } from "../ui/useDismiss.ts";
import { PRESETS, type Preset } from "./presets.ts";
import { useTheme } from "./ThemeProvider.tsx";

function Swatch({ preset }: { preset: Preset }) {
  const mid = `color-mix(in srgb, ${preset.ink} 45%, ${preset.canvas})`;
  return (
    <span aria-hidden="true" className="flex h-3.5 shrink-0 border border-line-strong">
      {[preset.canvas, preset.ink, mid].map((colour) => (
        <span key={colour} className="w-2.5" style={{ background: colour }} />
      ))}
    </span>
  );
}

export function ThemeMenu({ compact }: { compact: boolean }) {
  const { preset, choose } = useTheme();
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const menuId = useId();

  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, root, close);

  return (
    <div ref={root} className={cx("relative", compact ? "flex justify-center" : "")}>
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-label={`Theme: ${preset.label}`}
        title={compact ? `Theme: ${preset.label}` : undefined}
        onClick={() => setOpen((was) => !was)}
        className={cx(
          "inline-flex h-8 items-center gap-2 text-ink-muted hover:text-ink",
          compact ? "w-8 justify-center" : "px-1",
        )}
      >
        <Palette aria-hidden="true" className="size-3.5 shrink-0" />
        {compact ? null : (
          <span className="text-[0.75rem] tracking-[0.06em] uppercase">{preset.label}</span>
        )}
      </button>
      {open ? (
        <div
          id={menuId}
          role="menu"
          aria-label="Theme"
          className="fixed bottom-[4.5rem] left-3 z-50 max-h-[calc(100dvh-6rem)] w-[min(24.5rem,calc(100vw-1.5rem))] overflow-y-auto border border-line-strong bg-canvas text-ink shadow-2xl [animation:indus-fade-in_120ms_ease-out]"
        >
          <p className="m-0 border-b border-line px-3 py-2.5 text-[0.75rem] tracking-[0.08em] text-ink-muted">
            Theme
          </p>
          <ul className="m-0 list-none p-0 py-1">
            {PRESETS.map((item) => {
              const active = item.id === preset.id;
              return (
                <li key={item.id}>
                  <button
                    type="button"
                    role="menuitemradio"
                    data-preset={item.id}
                    aria-checked={active}
                    onClick={() => {
                      choose(item.id);
                      setOpen(false);
                    }}
                    className={cx(
                      "flex w-full items-center gap-3 px-3 py-2 text-left hover:bg-hover",
                      active && "bg-selected",
                    )}
                  >
                    <Swatch preset={item} />
                    <span className="flex min-w-0 flex-1 flex-col">
                      <span className="text-[0.75rem] tracking-[0.06em] uppercase">
                        {item.label}
                      </span>{" "}
                      <span className="truncate text-[0.75rem] text-ink-soft">
                        {item.description}
                      </span>
                    </span>
                    {active ? <Check aria-hidden="true" className="size-3.5 shrink-0" /> : null}
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
