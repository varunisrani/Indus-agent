// Overlays: one blocking card at a time (more wait in line) plus independent full-pane views.
// "Blocked" means something that owns the keyboard is open, so the composer stops taking keys.

import { useStore } from "zustand";
import { createStore, type StoreApi } from "zustand/vanilla";

export type CardKind =
  | "approval"
  | "confirm"
  | "pager"
  | "clarify"
  | "sudo"
  | "secret"
  | "modelPicker"
  | "sessionPicker";

export interface Card {
  id: string;
  kind: CardKind;
  /** The server request the card answers, when it answers one. */
  requestId?: string;
  payload: unknown;
  ownsInput: boolean;
  /** The turn the card belongs to; flow cards close when that turn goes idle. */
  turnId?: string;
}

/** Cards that belong to a turn's flow; the pickers the user opened are not among them. */
export const FLOW_CARD_KINDS: ReadonlySet<CardKind> = new Set([
  "approval",
  "confirm",
  "pager",
  "clarify",
  "sudo",
  "secret",
]);

export interface OverlayState {
  card: Card | null;
  views: string[];
  /** Cards waiting for the open one to close, oldest first. */
  waiting: Card[];
  open(card: Card): void;
  close(id: string): void;
  blocked(): boolean;
  openView(name: string): void;
  closeView(name: string): void;
  /** Close every card answering `requestId` (open or waiting); true when one was found. */
  closeByRequest(requestId: string): boolean;
  /** Turn idle: drop flow cards of that turn (all flow cards when `turnId` is absent). */
  closeFlowCards(turnId?: string): void;
}

function nextVisible(state: { card: Card | null; waiting: Card[] }): {
  card: Card | null;
  waiting: Card[];
} {
  if (state.card !== null) return state;
  const [first, ...rest] = state.waiting;
  return { card: first ?? null, waiting: rest };
}

function overlayData(): Pick<OverlayState, "card" | "views" | "waiting"> {
  return { card: null, views: [], waiting: [] };
}

export const overlayStore: StoreApi<OverlayState> = createStore<OverlayState>()((set, get) => ({
  ...overlayData(),
  open: (card) =>
    set((s) => {
      if (s.card?.id === card.id) return { card };
      if (s.card === null) return { card };
      const waiting = s.waiting.filter((c) => c.id !== card.id);
      return { waiting: [...waiting, card] };
    }),
  close: (id) =>
    set((s) => {
      if (s.card?.id === id) return nextVisible({ card: null, waiting: s.waiting });
      return { waiting: s.waiting.filter((c) => c.id !== id) };
    }),
  blocked: () => {
    const s = get();
    return (s.card?.ownsInput ?? false) || s.views.length > 0;
  },
  openView: (name) => set((s) => (s.views.includes(name) ? s : { views: [...s.views, name] })),
  closeView: (name) => set((s) => ({ views: s.views.filter((v) => v !== name) })),
  closeByRequest: (requestId) => {
    const s = get();
    const hit = s.card?.requestId === requestId || s.waiting.some((c) => c.requestId === requestId);
    if (!hit) return false;
    const waiting = s.waiting.filter((c) => c.requestId !== requestId);
    const card = s.card?.requestId === requestId ? null : s.card;
    set(nextVisible({ card, waiting }));
    return true;
  },
  closeFlowCards: (turnId) =>
    set((s) => {
      const drop = (c: Card): boolean =>
        FLOW_CARD_KINDS.has(c.kind) &&
        (turnId === undefined || c.turnId === undefined || c.turnId === turnId);
      const waiting = s.waiting.filter((c) => !drop(c));
      const card = s.card !== null && drop(s.card) ? null : s.card;
      return nextVisible({ card, waiting });
    }),
}));

export function useOverlay<T>(sel: (s: OverlayState) => T): T {
  return useStore(overlayStore, sel);
}

export function resetOverlay(): void {
  overlayStore.setState(overlayData());
}
