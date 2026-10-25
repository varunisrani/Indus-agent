// The composer's dock (W56-G1 "Floating composer"). Docked, the composer sits under the
// conversation as before. With the setting on, a grip above it lifts it into a card over the
// conversation: drag the grip to move it (the first few pixels of a drag pull it out of the
// dock), press the grip to float or dock it, and use the arrow keys on the grip to move a
// floating one. The card stays inside the chat column. Its place lives in this window only;
// turning the setting off docks it again.

import {
  type KeyboardEvent,
  type PointerEvent,
  type ReactNode,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { useT } from "../i18n/index.tsx";

export interface FloatPlace {
  /** px from the chat column's left edge */
  readonly x: number;
  /** px from the chat column's top edge */
  readonly y: number;
}

interface Size {
  readonly width: number;
  readonly height: number;
}

/** A drag shorter than this is a press, not a move. */
export const DRAG_THRESHOLD = 4;
/** How far one arrow key moves a floating composer. */
export const NUDGE = 16;
/** How far above its dock a pressed grip lifts the composer. */
export const LIFT = 96;

/** `place` moved so a `box` there lies inside `area` (top-left wins when it cannot fit). */
export function clampPlace(place: FloatPlace, box: Size, area: Size): FloatPlace {
  const x = Math.min(Math.max(0, place.x), Math.max(0, area.width - box.width));
  const y = Math.min(Math.max(0, place.y), Math.max(0, area.height - box.height));
  return { x: Math.round(x), y: Math.round(y) };
}

function sameSpot(a: FloatPlace | null, b: FloatPlace | null): boolean {
  return a === b || (a !== null && b !== null && a.x === b.x && a.y === b.y);
}

interface Drag {
  readonly startX: number;
  readonly startY: number;
  readonly fromX: number;
  readonly fromY: number;
  moved: boolean;
}

export function ComposerDock(props: { floatable: boolean; children: ReactNode }) {
  const t = useT();
  const ref = useRef<HTMLDivElement>(null);
  const [place, setPlace] = useState<FloatPlace | null>(null);
  const drag = useRef<Drag | null>(null);
  const swallowClick = useRef(false);
  const floating = props.floatable && place !== null;

  // the setting turned off: dock again
  useEffect(() => {
    if (!props.floatable) setPlace(null);
  }, [props.floatable]);

  /** The dock's box and the chat column's size, relative to the column. */
  const measure = (): { box: Size & FloatPlace; area: Size } | null => {
    const own = ref.current;
    const area = own?.parentElement ?? null;
    if (own === null || area === null) return null;
    const a = area.getBoundingClientRect();
    const b = own.getBoundingClientRect();
    return {
      box: { x: b.left - a.left, y: b.top - a.top, width: b.width, height: b.height },
      area: { width: a.width, height: a.height },
    };
  };

  /** Keep a floating card inside the column (after every move, and when the window resizes). */
  const keepInside = () => {
    const m = measure();
    if (m === null) return;
    setPlace((now) => {
      if (now === null) return now;
      const next = clampPlace(now, m.box, m.area);
      return sameSpot(now, next) ? now : next;
    });
  };
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-checked whenever the place changes
  useLayoutEffect(() => {
    if (floating) keepInside();
  }, [floating, place]);
  useEffect(() => {
    if (!floating) return;
    window.addEventListener("resize", keepInside);
    return () => window.removeEventListener("resize", keepInside);
  });

  const onPointerDown = (event: PointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0) return;
    const m = measure();
    drag.current = {
      startX: event.clientX,
      startY: event.clientY,
      fromX: place?.x ?? m?.box.x ?? 0,
      fromY: place?.y ?? m?.box.y ?? 0,
      moved: false,
    };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  };
  const onPointerMove = (event: PointerEvent<HTMLButtonElement>) => {
    const d = drag.current;
    if (d === null) return;
    const dx = event.clientX - d.startX;
    const dy = event.clientY - d.startY;
    if (!d.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
    d.moved = true;
    setPlace({ x: Math.round(d.fromX + dx), y: Math.round(d.fromY + dy) });
  };
  const onPointerUp = (event: PointerEvent<HTMLButtonElement>) => {
    const d = drag.current;
    drag.current = null;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    // a drag ends with a click on the grip: it must not dock the card it just moved
    if (d?.moved === true) swallowClick.current = true;
  };
  const onClick = () => {
    if (swallowClick.current) {
      swallowClick.current = false;
      return;
    }
    if (floating) {
      setPlace(null);
      return;
    }
    const m = measure();
    setPlace(m === null ? { x: 0, y: 0 } : { x: m.box.x, y: Math.max(0, m.box.y - LIFT) });
  };
  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (!floating || place === null) return;
    const step: Record<string, [number, number]> = {
      ArrowLeft: [-NUDGE, 0],
      ArrowRight: [NUDGE, 0],
      ArrowUp: [0, -NUDGE],
      ArrowDown: [0, NUDGE],
    };
    const delta = step[event.key];
    if (delta === undefined) return;
    event.preventDefault();
    setPlace({ x: place.x + delta[0], y: place.y + delta[1] });
  };

  return (
    <div
      ref={ref}
      className="chat-composer"
      data-floatable={props.floatable ? "true" : undefined}
      data-floating={floating ? "true" : undefined}
      style={floating && place !== null ? { left: place.x, top: place.y } : undefined}
    >
      {props.floatable && (
        <button
          type="button"
          className="composer-grip"
          aria-label={t(floating ? "chat.composer.dock" : "chat.composer.float")}
          title={t(floating ? "chat.composer.dock" : "chat.composer.float")}
          aria-pressed={floating}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={() => {
            drag.current = null;
          }}
          onClick={onClick}
          onKeyDown={onKeyDown}
        >
          <span aria-hidden="true" />
        </button>
      )}
      {props.children}
    </div>
  );
}
