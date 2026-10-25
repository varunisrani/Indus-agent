// @vitest-environment jsdom
// W57-H1-4: the chat error card's Retry sends the failed turn again through the window's retry
// slot, and Open logs reveals the log folder through the bridge; no Retry where nothing can resend.
import type { ClientEvent, Clock } from "@indus/client";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatView } from "./ChatView.tsx";
import type { ErrorItem } from "./messageStream.ts";
import { TranscriptStoreProvider } from "./transcriptReact.tsx";
import { type TranscriptSource, TranscriptStore } from "./transcriptStore.ts";

const immediate: Clock = {
  now: () => 0,
  setTimeout: (run) => {
    run();
    return 0 as unknown as ReturnType<Clock["setTimeout"]>;
  },
  clearTimeout: () => undefined,
};

function fakeSource() {
  const listeners = new Set<(event: ClientEvent) => void>();
  let seq = 0;
  const source: TranscriptSource & { emit(event: Record<string, unknown>): void } = {
    onEvent: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    request: (async () => ({
      messages: [{ role: "user", text: "Summarise the open issues", row_id: 1 }],
    })) as unknown as TranscriptSource["request"],
    watchSession: async () => undefined,
    unwatchSession: () => undefined,
    emit: (event) => {
      seq += 1;
      for (const listener of listeners) {
        listener({ session_id: "s1", seq, ...event } as unknown as ClientEvent);
      }
    },
  };
  return source;
}

async function failedTurn() {
  const source = fakeSource();
  const store = new TranscriptStore(source, { clock: immediate });
  await act(async () => {
    await store.open("s1");
  });
  act(() => {
    source.emit({ type: "turn.started", turn_id: "t1", generation: 1 });
    source.emit({
      type: "turn.done",
      turn_id: "t1",
      reason: "provider_failed",
      // a provider refusal (HTTP 400) is not marked transient by the backend
      failure: { reason: "HTTP 400 bad request from the model provider", retryable: false },
    });
  });
  return store;
}

afterEach(() => {
  cleanup();
  delete (globalThis as { indusDesktop?: unknown }).indusDesktop;
});

describe("the chat error card's actions", () => {
  it("Retry goes to the retry slot and Open logs to the bridge", async () => {
    const reveal = vi.fn(async () => ({ ok: true as const }));
    (globalThis as { indusDesktop?: unknown }).indusDesktop = { logs: { reveal } };
    const store = await failedTurn();
    const onRetry = vi.fn<(item: ErrorItem) => void>();
    render(
      <MemoryRouter>
        <TranscriptStoreProvider store={store}>
          <ChatView sessionId="s1" onRetry={onRetry} />
        </TranscriptStoreProvider>
      </MemoryRouter>,
    );
    expect(screen.getByRole("alert").getAttribute("data-layer")).toBe("provider");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledOnce();
    expect(onRetry.mock.calls[0]?.[0].turnId).toBe("t1");
    fireEvent.click(screen.getByRole("button", { name: "Open logs" }));
    expect(reveal).toHaveBeenCalledOnce();
  });

  it("a view with no retry slot (the HUD) shows no Retry", async () => {
    const store = await failedTurn();
    render(
      <MemoryRouter>
        <TranscriptStoreProvider store={store}>
          <ChatView sessionId="s1" />
        </TranscriptStoreProvider>
      </MemoryRouter>,
    );
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(screen.getByRole("button", { name: "Open logs" })).toBeTruthy();
  });
});
