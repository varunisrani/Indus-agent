// The app window's wake word (P24-20 wiring): one `WakeController` per main window over the
// foreground connection, and the hand-off to the composer's dictation (P24-35).
//
// A detection (`wake.detected`) asks the composer on screen to start dictating
// (`WAKE_DICTATE_EVENT`); the composer marks the request taken and, when that dictation ends,
// sends `WAKE_DONE_EVENT`, and the controller listens again (`wake.resume`). No composer on screen
// takes it: the hand-off throws, and the controller resumes listening at once.

import type { LooseRpc } from "../pet/PetOverlay.tsx";
import { WakeController } from "./WakeIndicator.tsx";

export const WAKE_DICTATE_EVENT = "indus-wake-dictate";
export const WAKE_DONE_EVENT = "indus-wake-done";

export interface DictateRequest {
  taken: boolean;
}

/** Ask the composer on screen to dictate; throws when none took the request. */
export function requestDictation(target: EventTarget = window): void {
  const detail: DictateRequest = { taken: false };
  target.dispatchEvent(new CustomEvent<DictateRequest>(WAKE_DICTATE_EVENT, { detail }));
  if (!detail.taken) throw new Error("no composer is on screen to dictate");
}

/** The composer's dictation that a wake hand-off started has ended. */
export function dictationDone(target: EventTarget = window): void {
  target.dispatchEvent(new CustomEvent(WAKE_DONE_EVENT));
}

let current: WakeController | null = null;

/** The window's controller (the Settings switch reads it); null before `installAppWake`. */
export function appWake(): WakeController | null {
  return current;
}

/** Build the window's controller over `rpc()` (the foreground connection, read at each call) and
 * follow `onEvent` for detections. Returns the controller and the undo. */
export function installAppWake(
  rpc: () => LooseRpc,
  target: EventTarget = window,
): { controller: WakeController; dispose: () => void } {
  const controller = new WakeController({
    rpc: {
      request: (method, params, options) => rpc().request(method, params, options),
    },
    handOff: () => requestDictation(target),
  });
  const onDone = () => void controller.captureDone();
  target.addEventListener(WAKE_DONE_EVENT, onDone);
  let detach = controller.attach((listener) => rpc().onEvent?.(listener) ?? (() => {}));
  current = controller;
  return {
    controller,
    dispose: () => {
      detach();
      detach = () => {};
      target.removeEventListener(WAKE_DONE_EVENT, onDone);
      if (current === controller) current = null;
    },
  };
}
