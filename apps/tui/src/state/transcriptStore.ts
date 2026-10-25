// The finished conversation: messages settled by the turn controller and lines written by the
// event handler. The transcript component renders it; live turn output stays in the turn store.

import { useStore } from "zustand";
import { createStore, type StoreApi } from "zustand/vanilla";
import type { ToolRow, TurnFailureView } from "./turnStore.ts";

export type MessageRole = "user" | "assistant" | "system" | "tool";
export type MessageKind = "text" | "diff" | "failure" | "notice";

export interface TranscriptMessage {
  id: string;
  role: MessageRole;
  text: string;
  kind?: MessageKind;
  tools?: ToolRow[];
  failure?: TurnFailureView;
  /** The model's reasoning for the turn this reply ends, kept behind a collapsed row. */
  reasoning?: string;
  createdAt: number;
}

export type NewMessage = Omit<TranscriptMessage, "id" | "createdAt"> & { createdAt?: number };

export interface TranscriptState {
  messages: TranscriptMessage[];
  append(message: NewMessage): string;
  update(id: string, patch: Partial<Omit<TranscriptMessage, "id">>): void;
  clear(): void;
}

let nextId = 0;

export const transcriptStore: StoreApi<TranscriptState> = createStore<TranscriptState>()((set) => ({
  messages: [],
  append: (message) => {
    nextId += 1;
    const id = `m${nextId}`;
    const full: TranscriptMessage = { createdAt: Date.now(), ...message, id };
    set((s) => ({ messages: [...s.messages, full] }));
    return id;
  },
  update: (id, patch) =>
    set((s) => ({ messages: s.messages.map((m) => (m.id === id ? { ...m, ...patch } : m)) })),
  clear: () => set({ messages: [] }),
}));

export function useTranscript<T>(sel: (s: TranscriptState) => T): T {
  return useStore(transcriptStore, sel);
}

/** Shorthand for a system line. */
export function systemLine(text: string): string {
  return transcriptStore.getState().append({ role: "system", text });
}
