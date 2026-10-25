// @vitest-environment jsdom
import type { ClientEvent, Clock } from "@indus/client";
import type { HistoryMessage } from "@indus/protocol";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setRichLoaders } from "./RichBlocks.tsx";
import { resetReadingPositions, TRANSCRIPT_PAGE, Transcript } from "./Transcript.tsx";
import { TranscriptStoreProvider } from "./transcriptReact.tsx";
import { type TranscriptSource, TranscriptStore } from "./transcriptStore.ts";

/** Timers fire at once: every notification is synchronous in these tests. */
const immediate: Clock = {
  now: () => 0,
  setTimeout: (run) => {
    run();
    return 0;
  },
  clearTimeout: () => undefined,
};

function fakeSource(rows: HistoryMessage[]) {
  const listeners = new Set<(event: ClientEvent) => void>();
  let seq = 0;
  const source: TranscriptSource & {
    emit(event: Record<string, unknown>): void;
    rows: HistoryMessage[];
  } = {
    rows,
    onEvent: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    request: (async () => ({ messages: source.rows })) as unknown as TranscriptSource["request"],
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

function conversation(turns: number): HistoryMessage[] {
  const rows: HistoryMessage[] = [];
  for (let i = 0; i < turns; i += 1) {
    rows.push({ role: "user", text: `question ${i}`, row_id: i * 2 + 1 });
    rows.push({ role: "assistant", text: `answer ${i}`, row_id: i * 2 + 2 });
  }
  return rows;
}

async function show(rows: HistoryMessage[], extra: Partial<Parameters<typeof Transcript>[0]> = {}) {
  const source = fakeSource(rows);
  const store = new TranscriptStore(source, { clock: immediate });
  await act(async () => {
    await store.open("s1");
  });
  const view = render(
    <TranscriptStoreProvider store={store}>
      <Transcript sessionId="s1" {...extra} />
    </TranscriptStoreProvider>,
  );
  return { source, store, view };
}

function sizeScroller(scroller: HTMLElement, height: number, client: number) {
  Object.defineProperty(scroller, "scrollHeight", { configurable: true, value: height });
  Object.defineProperty(scroller, "clientHeight", { configurable: true, value: client });
}

beforeEach(() => {
  resetReadingPositions();
  setRichLoaders({
    highlight: async () => null,
    math: async () => null,
    diagram: async () => null,
  });
});

afterEach(() => {
  setRichLoaders(null);
  delete (globalThis as { indusDesktop?: unknown }).indusDesktop;
});

describe("Transcript", () => {
  it("renders only a window of a 2,000-message session and pages older ones in", async () => {
    const { view } = await show(conversation(1_000));
    const rendered = () => view.container.querySelectorAll(".msg").length;
    expect(rendered()).toBe(TRANSCRIPT_PAGE);
    fireEvent.click(screen.getByRole("button", { name: "Show earlier messages" }));
    expect(rendered()).toBe(TRANSCRIPT_PAGE * 2);
    expect(screen.getByText("answer 999")).toBeTruthy();
  });

  it("the timeline has one marker per prompt and jumps outside the window", async () => {
    const { view } = await show(conversation(100));
    const markers = screen.getAllByRole("button", { name: /^Go to prompt:/ });
    expect(markers).toHaveLength(100);
    expect(view.container.querySelector('[data-key="row:1"]')).toBeNull();
    fireEvent.click(markers[0] as HTMLElement);
    expect(view.container.querySelector('[data-key="row:1"]')).not.toBeNull();
  });

  it("stops following when the reader scrolls up and resumes with Scroll to bottom", async () => {
    const { view } = await show(conversation(3));
    const scroller = view.container.querySelector(".transcript-scroll") as HTMLElement;
    sizeScroller(scroller, 1_000, 100);
    scroller.scrollTop = 200;
    fireEvent.scroll(scroller);
    const button = screen.getByRole("button", { name: "Scroll to bottom" });
    fireEvent.click(button);
    expect(scroller.scrollTop).toBe(1_000);
    expect(screen.queryByRole("button", { name: "Scroll to bottom" })).toBeNull();
  });

  it("restores the reading position when the session is shown again", async () => {
    const { store, view } = await show(conversation(3));
    const scroller = view.container.querySelector(".transcript-scroll") as HTMLElement;
    sizeScroller(scroller, 1_000, 100);
    scroller.scrollTop = 300;
    fireEvent.scroll(scroller);
    view.unmount();
    const again = render(
      <TranscriptStoreProvider store={store}>
        <Transcript sessionId="s1" />
      </TranscriptStoreProvider>,
    );
    const next = again.container.querySelector(".transcript-scroll") as HTMLElement;
    expect(next.scrollTop).toBe(300);
    expect(screen.getByRole("button", { name: "Scroll to bottom" })).toBeTruthy();
  });

  it("streams deltas into one reply and shows reasoning only when enabled (live toggle)", async () => {
    const { source, store, view } = await show([]);
    act(() => {
      source.emit({ type: "turn.started", turn_id: "t1", generation: 1 });
      source.emit({ type: "reasoning.delta", turn_id: "t1", text: "pondering" });
      source.emit({ type: "message.delta", turn_id: "t1", text: "Hello " });
      source.emit({ type: "message.delta", turn_id: "t1", text: "there" });
    });
    expect(view.container.querySelectorAll(".msg-assistant")).toHaveLength(1);
    expect(screen.getByText("Hello there")).toBeTruthy();
    expect(screen.queryByText("Thinking")).toBeNull();
    act(() => store.setShowReasoning(true));
    expect(screen.getByText("Thinking")).toBeTruthy();
    expect(screen.getByText("pondering")).toBeTruthy();
    act(() => store.setShowReasoning(false));
    expect(screen.queryByText("pondering")).toBeNull();
  });

  it("puts each prompt first in its turn (the sticky row)", async () => {
    const { view } = await show(conversation(2));
    const turns = view.container.querySelectorAll("section.turn");
    expect(turns).toHaveLength(2);
    expect(turns[0]?.firstElementChild?.classList.contains("msg-user")).toBe(true);
  });

  it("colours tool rows: red only for an explicit failure", async () => {
    const { source, view } = await show([]);
    act(() => {
      source.emit({ type: "turn.started", turn_id: "t1", generation: 1 });
      for (const [callId, result] of [
        ["c1", "exit code 1"],
        ["c2", "Error: permission denied"],
      ] as const) {
        source.emit({
          type: "tool.started",
          turn_id: "t1",
          call_id: callId,
          name: "terminal",
          arguments: { command: "grep x" },
        });
        source.emit({
          type: "tool.completed",
          turn_id: "t1",
          call_id: callId,
          name: "terminal",
          ok: false,
          duration_ms: 5,
          result,
        });
      }
    });
    const rows = [...view.container.querySelectorAll(".tool-row")].map((row) =>
      row.getAttribute("data-state"),
    );
    expect(rows).toEqual(["neutral", "failed"]);
    // the parity flow waits on this hook (W46, RM21): one per tool row
    expect(view.container.querySelectorAll('[data-ui="transcript-tool-row"]').length).toBe(2);
    fireEvent.click(screen.getAllByRole("button", { name: /Ran/ })[0] as HTMLElement);
    expect(screen.getByText("exit code 1")).toBeTruthy();
  });

  it("a failed turn ends in an error card whose actions are reported", async () => {
    const onFailureAction = vi.fn();
    const { source } = await show([], { onFailureAction, canRetry: true });
    act(() => {
      source.emit({ type: "turn.started", turn_id: "t1", generation: 1 });
      source.emit({
        type: "turn.done",
        turn_id: "t1",
        reason: "provider_failed",
        failure: { reason: "rate limit reached", retryable: true },
      });
    });
    expect(screen.getByRole("alert").textContent).toContain("The model provider failed");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onFailureAction).toHaveBeenCalledWith(
      "retry",
      expect.objectContaining({ layer: "provider" }),
    );
  });

  it("links never navigate: a click asks main's opener", async () => {
    const open = vi.fn(async () => ({ ok: true }));
    (globalThis as { indusDesktop?: unknown }).indusDesktop = { external: { open } };
    const { view } = await show([
      { role: "user", text: "where?", row_id: 1 },
      {
        role: "assistant",
        text: "See [the docs](https://example.com/docs) or [this](file:///etc/passwd).",
        row_id: 2,
      },
    ]);
    expect(view.container.querySelector("a[href]")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "the docs" }));
    expect(open).toHaveBeenCalledWith({ url: "https://example.com/docs" });
    expect(screen.queryByRole("button", { name: "this" })).toBeNull();
  });
});

describe("TranscriptStore history before the connection is ready", () => {
  it("a session opened while the connection dials loads its history once it is ready", async () => {
    const listeners = new Set<() => void>();
    let ready = false;
    let calls = 0;
    const source: TranscriptSource = {
      onEvent: () => () => undefined,
      request: (async () => {
        calls += 1;
        if (!ready) throw new Error("not connected");
        return { messages: conversation(1) };
      }) as unknown as TranscriptSource["request"],
      watchSession: async () => undefined,
      unwatchSession: () => undefined,
      subscribe: (listener) => {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
      getState: () => ({ ready }) as ReturnType<NonNullable<TranscriptSource["getState"]>>,
    };
    const store = new TranscriptStore(source, { clock: immediate });
    await store.open("s1");
    expect(calls).toBe(1);
    expect(store.get("s1").items.length).toBe(0);
    ready = true;
    for (const listener of listeners) listener();
    await vi.waitFor(() => expect(store.get("s1").items.length).toBe(2));
    expect(calls).toBe(2);
    // a later state change while still ready loads nothing again
    for (const listener of listeners) listener();
    expect(calls).toBe(2);
    store.dispose();
  });
});

describe("reply actions (W59-K1-5)", () => {
  it("a settled reply shows its stored time and copies its text", async () => {
    const at = new Date(2026, 9, 25, 9, 41).getTime();
    const rows = [
      { role: "user", text: "question", row_id: 1, at: at - 60_000 },
      { role: "assistant", text: "the answer", row_id: 2, at },
    ] as unknown as HistoryMessage[];
    const writes: string[] = [];
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: async (text: string) => void writes.push(text) },
    });
    const { view } = await show(rows);
    const reply = view.container.querySelector(".msg-assistant") as HTMLElement;
    const time = reply.querySelector(".msg-actions time") as HTMLTimeElement;
    expect(time.dateTime).toBe(new Date(at).toISOString());
    const prompt = view.container.querySelector(".msg-user time") as HTMLTimeElement;
    expect(prompt.dateTime).toBe(new Date(at - 60_000).toISOString());
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Copy reply" }));
    });
    expect(writes).toEqual(["the answer"]);
    expect(screen.getByRole("button", { name: "Copied" })).toBeTruthy();
  });

  it("a reply without a stored time still offers copy; a streaming one shows nothing", async () => {
    const { view, source } = await show([
      { role: "user", text: "q", row_id: 1 },
      { role: "assistant", text: "a", row_id: 2 },
    ]);
    expect(view.container.querySelector(".msg-actions time")).toBeNull();
    expect(screen.getByRole("button", { name: "Copy reply" })).toBeTruthy();
    await act(async () => {
      source.emit({ type: "turn.started", turn_id: "t9" });
      source.emit({ type: "message.delta", turn_id: "t9", text: "partial" });
    });
    expect(screen.getAllByRole("button", { name: "Copy reply" })).toHaveLength(1);
  });
});
