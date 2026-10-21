// The desktop pet (P24-19), two halves:
//
// - `PetOverlay` is the root of the pet window (`?win=pet`, a transparent click-through window over
//   the work area). It shows the picture of the current mood where `roam.ts` puts it, a speech
//   bubble for short status lines, and tells main when the pointer is over the figure, so only the
//   figure takes clicks. A click nudges a sleeping pet awake.
// - `PetHost` lives in an app window. When the daemon has a shown pet (`pet.info`, which sends the
//   pictures only when its revision changed) it asks main to show the overlay with them, hides it
//   otherwise, and turns turn events into bubble lines ("On it…", "All done.", "I need you…").
//
// `pet.info` is not in the generated method catalogue yet (P24-19 wiring): `PetHost` calls it
// through `LooseRpc`, and a daemon that does not serve it simply leaves the pet hidden.

import { type PointerEvent, useEffect, useRef, useState } from "react";
import { desktopBridge } from "../app/bridge.ts";
import type { IndusPetView } from "../global.d.ts";
import { useT } from "../i18n/index.tsx";
import { nudge, type RoamState, startRoam, stepRoam } from "./roam.ts";

/** The figure's size at scale 1 (pictures are drawn square). */
export const PET_SIZE_PX = 96;
/** How often an app window re-reads `pet.info` (cheap when nothing changed). */
export const PET_INFO_POLL_MS = 30_000;

const EMPTY: IndusPetView = {
  visible: false,
  name: null,
  scale: 1,
  revision: null,
  moods: {},
  bubble: null,
};

/** Which picture a moment shows: talking while there is a bubble, else the roam mood. */
export function moodFor(view: IndusPetView, roam: RoamState | null): string {
  const has = (mood: string) => typeof view.moods[mood] === "string";
  const wanted = view.bubble !== null ? "talk" : (roam?.mood ?? "rest");
  if (has(wanted)) return wanted;
  if (has("rest")) return "rest";
  return Object.keys(view.moods)[0] ?? "rest";
}

export function PetOverlay() {
  const t = useT();
  const bridge = desktopBridge();
  const [view, setView] = useState<IndusPetView>(EMPTY);
  const [roam, setRoam] = useState<RoamState | null>(null);
  const over = useRef(false);
  const size = Math.round(PET_SIZE_PX * view.scale);

  useEffect(() => {
    document.documentElement.dataset.window = "pet";
    document.body.style.background = "transparent";
    if (bridge === null) return;
    let live = true;
    const off = bridge.pet.onState((next) => {
      if (live) setView(next);
    });
    void bridge.pet
      .get()
      .then((next) => {
        if (live) setView(next);
      })
      .catch(() => {});
    return () => {
      live = false;
      off();
    };
  }, [bridge]);

  // one animation loop while the pet is visible
  useEffect(() => {
    if (!view.visible) return;
    const area = () => ({ width: window.innerWidth, height: window.innerHeight });
    const sprite = { width: size, height: size };
    let last = performance.now();
    let frame = 0;
    setRoam((current) => current ?? startRoam(area(), sprite, Math.random));
    const tick = (now: number) => {
      const dt = now - last;
      last = now;
      setRoam((current) =>
        current === null ? current : stepRoam(current, area(), sprite, dt, Math.random),
      );
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [view.visible, size]);

  useEffect(() => {
    if (view.bubble !== null) setRoam((current) => (current === null ? current : nudge(current)));
  }, [view.bubble]);

  const report = (next: boolean) => {
    if (over.current === next) return;
    over.current = next;
    bridge?.pet.pointer({ over: next });
  };

  if (!view.visible || roam === null) return null;
  const mood = moodFor(view, roam);
  const picture = view.moods[mood];
  return (
    <main className="pointer-events-none fixed inset-0 select-none" aria-live="polite">
      <div
        className="pointer-events-auto absolute"
        style={{ left: roam.x, top: roam.y, width: size, height: size }}
        data-testid="pet-figure"
        data-mood={mood}
        onPointerEnter={() => report(true)}
        onPointerLeave={() => report(false)}
        onPointerDown={(event: PointerEvent<HTMLDivElement>) => {
          if (event.button === 0)
            setRoam((current) => (current === null ? current : nudge(current)));
        }}
      >
        {view.bubble !== null ? (
          <p
            role="status"
            className="absolute bottom-full left-1/2 mb-1 w-max max-w-56 -translate-x-1/2 rounded-lg border border-line bg-raised px-2 py-1 text-xs text-fg shadow"
          >
            {view.bubble}
          </p>
        ) : null}
        {picture !== undefined ? (
          <img
            src={picture}
            alt={t("pet.label", { name: view.name ?? "" })}
            draggable={false}
            className="h-full w-full object-contain"
            style={{ transform: roam.facing < 0 ? "scaleX(-1)" : undefined }}
          />
        ) : null}
      </div>
    </main>
  );
}

/** The part of a connection `PetHost` needs (methods outside the generated catalogue). */
export interface LooseRpc {
  request(method: string, params: object, options?: { timeoutMs?: number }): Promise<unknown>;
  onEvent?(listener: (event: { type?: string }) => void): () => void;
}

interface PetInfo {
  enabled?: boolean;
  name?: string;
  scale?: number;
  revision?: string;
  unchanged?: boolean;
  moods?: Record<string, string>;
}

export function PetHost(props: { connection: LooseRpc | null; pollMs?: number }) {
  const t = useT();
  const bridge = desktopBridge();
  const { connection } = props;
  const pollMs = props.pollMs ?? PET_INFO_POLL_MS;
  const held = useRef<{ revision: string; moods: Record<string, string> } | null>(null);

  useEffect(() => {
    if (bridge === null || connection === null) return;
    let live = true;
    const refresh = async () => {
      let info: PetInfo;
      try {
        info = (await connection.request("pet.info", {
          ...(held.current === null ? {} : { known_revision: held.current.revision }),
        })) as PetInfo;
      } catch {
        return; // not served here (or offline): leave the overlay as it is
      }
      if (!live) return;
      if (info.enabled !== true || typeof info.revision !== "string") {
        if (held.current !== null) {
          held.current = null;
          await bridge.pet.hide().catch(() => {});
        }
        return;
      }
      const moods = info.unchanged === true ? held.current?.moods : info.moods;
      if (moods === undefined) return;
      held.current = { revision: info.revision, moods };
      await bridge.pet
        .show({ name: info.name ?? "", scale: info.scale ?? 1, revision: info.revision, moods })
        .catch(() => {});
    };
    void refresh();
    const timer = setInterval(() => void refresh(), pollMs);
    const off = connection.onEvent?.((event) => {
      if (held.current === null) return;
      if (event.type === "turn.started") bridge.pet.say({ text: t("pet.say.working") });
      else if (event.type === "turn.done") bridge.pet.say({ text: t("pet.say.done") });
      else if (event.type === "server_request") bridge.pet.say({ text: t("pet.say.waiting") });
    });
    return () => {
      live = false;
      clearInterval(timer);
      off?.();
    };
  }, [bridge, connection, pollMs, t]);

  return null;
}
