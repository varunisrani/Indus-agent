// @vitest-environment jsdom
// The renderer-only display preferences (W55-F3): the Settings rows write desktop prefs, PrefsEffects
// publishes them, and the transcript, tool rows, chat view and sidebar draw with them. Rows with no
// feature behind them stay disabled.
import type { ClientEvent, Clock } from "@indus/client";
import type { HistoryMessage } from "@indus/protocol";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ThemeProvider } from "../app/theme.tsx";
import { ChatView } from "../chat/ChatView.tsx";
import type { ToolPart } from "../chat/messageStream.ts";
import { setRichLoaders } from "../chat/RichBlocks.tsx";
import { ToolRow } from "../chat/ToolRow.tsx";
import { resetReadingPositions, Transcript } from "../chat/Transcript.tsx";
import { TranscriptStoreProvider } from "../chat/transcriptReact.tsx";
import { type TranscriptSource, TranscriptStore } from "../chat/transcriptStore.ts";
import { en } from "../i18n/en.ts";
import { makeTranslate } from "../i18n/index.tsx";
import { rowDetails } from "../sidebar/Sidebar.tsx";
import type { SessionListState, SessionRow } from "../sidebar/sessionList.ts";
import type { SettingsBridge } from "./context.tsx";
import {
  DISPLAY_DEFAULTS,
  type DisplayPrefs,
  displayPrefsKnown,
  getDisplayPrefs,
  publishDisplayPrefs,
  resetDisplayPrefs,
} from "./displayPrefs.ts";
import { fakeRest } from "./fakeRest.ts";
import { BUBBLE_FILL_VAR, PrefsEffects } from "./PrefsEffects.tsx";
import { lastChatTarget } from "./ReopenLastChat.tsx";
import { SettingsOverlay } from "./SettingsOverlay.tsx";

const t = makeTranslate(en);

function prefsBridge(overrides: Partial<DisplayPrefs> = {}) {
  let state = {
    minimizeToTray: false,
    keepAwake: "while-working" as const,
    quickEntryShortcut: "",
    hudSnapShortcut: "",
    hudToggleShortcut: "",
    notifications: { turnComplete: true, approvals: true, sounds: true },
    keybindings: {},
    chatTextScale: 110,
    ...DISPLAY_DEFAULTS,
    ...overrides,
  };
  const set = vi.fn(async (patch: Record<string, unknown>) => {
    state = { ...state, ...patch } as typeof state;
    return state;
  });
  const bridge = {
    prefs: { get: async () => state, set, onChanged: () => () => undefined },
  } as unknown as SettingsBridge;
  return { set, bridge };
}

function backend() {
  const api = fakeRest();
  api.route("GET", "/api/config/schema", { fields: {} });
  api.route("GET", "/api/config", {});
  api.route("GET", "/api/profiles", { profiles: [{ name: "default" }], current: "default" });
  return api;
}

function Harness(props: { initial: string; bridge: SettingsBridge }) {
  const [query, setQuery] = useState(props.initial);
  return (
    <ThemeProvider>
      <SettingsOverlay
        query={query}
        onNavigate={setQuery}
        onClose={() => undefined}
        api={backend()}
        bridge={props.bridge}
        saveKey={null}
      />
    </ThemeProvider>
  );
}

async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

function control(name: string): HTMLInputElement {
  return screen.getByRole("switch", { name }) as HTMLInputElement;
}

beforeEach(() => {
  resetDisplayPrefs();
  resetReadingPositions();
  setRichLoaders({
    highlight: async () => null,
    math: async () => null,
    diagram: async () => null,
  });
});

afterEach(() => {
  cleanup();
  setRichLoaders(null);
  resetDisplayPrefs();
  document.documentElement.style.removeProperty(BUBBLE_FILL_VAR);
  delete document.documentElement.dataset.tabStrip;
});

describe("Settings rows (W55-F3)", () => {
  it("Chat display: the renderer rows are live and write desktop prefs; the rest stay disabled", async () => {
    const { set, bridge } = prefsBridge();
    render(<Harness initial="view=appearance&page=chat-display" bridge={bridge} />);
    await settle();
    expect(control("Hide thread timeline bars").disabled).toBe(false);
    await act(async () => {
      fireEvent.click(control("Hide thread timeline bars"));
    });
    expect(set).toHaveBeenLastCalledWith({ hideTimeline: true });
    await act(async () => {
      fireEvent.click(control("Collapse thinking by default"));
    });
    expect(set).toHaveBeenLastCalledWith({ collapseThinking: true });
    await act(async () => {
      fireEvent.click(screen.getByRole("radio", { name: "Product" }));
    });
    expect(set).toHaveBeenLastCalledWith({ toolCallDisplay: "product" });
    await act(async () => {
      fireEvent.click(screen.getByRole("radio", { name: "Right-to-left" }));
    });
    expect(set).toHaveBeenLastCalledWith({ textDirection: "rtl" });
    for (const name of ["Message reactions", "Vibe hearts", "Hide code diffs"]) {
      expect(control(name).disabled).toBe(true);
    }
  });

  it("Message bubble saves once the slider settles", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { set, bridge } = prefsBridge();
      render(<Harness initial="view=appearance&page=chat-display" bridge={bridge} />);
      await settle();
      const slider = screen.getByRole("slider", { name: "Message bubble" }) as HTMLInputElement;
      expect(slider.disabled).toBe(false);
      fireEvent.change(slider, { target: { value: "20" } });
      fireEvent.change(slider, { target: { value: "40" } });
      expect(set).not.toHaveBeenCalled();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(300);
      });
      expect(set).toHaveBeenCalledTimes(1);
      expect(set).toHaveBeenLastCalledWith({ bubbleTransparency: 40 });
    } finally {
      vi.useRealTimers();
    }
  });

  it("General and Window & layout: live rows next to the disabled ones", async () => {
    const { set, bridge } = prefsBridge();
    const view = render(<Harness initial="view=appearance&page=general" bridge={bridge} />);
    await settle();
    expect(control("Intro splash").checked).toBe(true);
    await act(async () => {
      fireEvent.click(control("Reopen last chat on launch"));
    });
    expect(set).toHaveBeenLastCalledWith({ reopenLastChat: true });
    for (const name of ["Model pricing", "In-app tips", "Guided tours"]) {
      expect(control(name).disabled).toBe(true);
    }
    view.unmount();
    render(<Harness initial="view=appearance&page=window" bridge={bridge} />);
    await settle();
    await act(async () => {
      fireEvent.click(screen.getByRole("radio", { name: "Detailed" }));
    });
    expect(set).toHaveBeenLastCalledWith({ sessionDensity: "detailed" });
    await act(async () => {
      fireEvent.click(screen.getByRole("radio", { name: "Never" }));
    });
    expect(set).toHaveBeenLastCalledWith({ tabStrip: "never" });
    await act(async () => {
      fireEvent.click(control("Chat backdrop"));
    });
    expect(set).toHaveBeenLastCalledWith({ chatBackdrop: true });
    // W56-G1: the last three rows of the page are live too
    expect(screen.getByRole("radio", { name: "Left" }).closest("fieldset")?.disabled).toBe(false);
    await act(async () => {
      fireEvent.click(screen.getByRole("radio", { name: "Left" }));
    });
    expect(set).toHaveBeenLastCalledWith({ appActionsSide: "left" });
    expect(control("File browser").checked).toBe(false);
    await act(async () => {
      fireEvent.click(control("File browser"));
    });
    expect(set).toHaveBeenLastCalledWith({ fileBrowser: true });
    expect(control("Floating composer").checked).toBe(false);
    await act(async () => {
      fireEvent.click(control("Floating composer"));
    });
    expect(set).toHaveBeenLastCalledWith({ floatingComposer: true });
  });
});

describe("PrefsEffects", () => {
  it("publishes the display prefs and mirrors the CSS-driven ones on the root", async () => {
    const { bridge } = prefsBridge({
      bubbleTransparency: 30,
      tabStrip: "auto",
      hideTimeline: true,
    });
    expect(displayPrefsKnown()).toBe(false);
    render(
      <MemoryRouter>
        <PrefsEffects bridge={bridge} />
      </MemoryRouter>,
    );
    await settle();
    expect(displayPrefsKnown()).toBe(true);
    expect(getDisplayPrefs().hideTimeline).toBe(true);
    const root = document.documentElement;
    expect(root.style.getPropertyValue(BUBBLE_FILL_VAR)).toBe("70%");
    expect(root.dataset.tabStrip).toBe("auto");
  });
});

// -- the transcript and the chat view --------------------------------------------------------------

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
  const source: TranscriptSource & { emit(event: Record<string, unknown>): void } = {
    onEvent: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    request: (async () => ({ messages: rows })) as unknown as TranscriptSource["request"],
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

async function storeWith(rows: HistoryMessage[]) {
  const source = fakeSource(rows);
  const store = new TranscriptStore(source, { clock: immediate });
  await act(async () => {
    await store.open("s1");
  });
  return { source, store };
}

const TWO_TURNS: HistoryMessage[] = [
  { role: "user", text: "first", row_id: 1 },
  { role: "assistant", text: "one", row_id: 2 },
  { role: "user", text: "second", row_id: 3 },
  { role: "assistant", text: "two", row_id: 4 },
];

describe("the transcript draws with the display prefs", () => {
  it("hides the timeline and sets the text direction on prompts and replies", async () => {
    const { store } = await storeWith(TWO_TURNS);
    const view = render(
      <TranscriptStoreProvider store={store}>
        <Transcript sessionId="s1" />
      </TranscriptStoreProvider>,
    );
    expect(view.container.querySelector("nav.timeline")).not.toBeNull();
    expect(view.container.querySelector(".msg-bubble")?.getAttribute("dir")).toBe("auto");
    act(() =>
      publishDisplayPrefs({ ...DISPLAY_DEFAULTS, hideTimeline: true, textDirection: "rtl" }),
    );
    expect(view.container.querySelector("nav.timeline")).toBeNull();
    expect(view.container.querySelector(".msg-bubble")?.getAttribute("dir")).toBe("rtl");
    expect(view.container.querySelector(".msg-text")?.getAttribute("dir")).toBe("rtl");
  });

  it("collapse thinking keeps streamed reasoning closed", async () => {
    const { source, store } = await storeWith([]);
    act(() => store.setShowReasoning(true));
    const view = render(
      <TranscriptStoreProvider store={store}>
        <Transcript sessionId="s1" />
      </TranscriptStoreProvider>,
    );
    act(() => {
      source.emit({ type: "turn.started", turn_id: "t1", generation: 1 });
      source.emit({ type: "reasoning.delta", turn_id: "t1", text: "pondering" });
    });
    const details = () => view.container.querySelector("details.reasoning") as HTMLDetailsElement;
    expect(details().open).toBe(true);
    act(() => publishDisplayPrefs({ ...DISPLAY_DEFAULTS, collapseThinking: true }));
    expect(details().open).toBe(false);
  });

  it("product tool display leaves the argument payload out", () => {
    const part: ToolPart = {
      type: "tool",
      callId: "c1",
      name: "read_file",
      args: { path: "NOTES.md" },
      state: "ok",
      progress: null,
      fraction: null,
      result: "hello",
      durationMs: 10,
    };
    render(<ToolRow part={part} />);
    fireEvent.click(screen.getByRole("button", { name: /Read NOTES\.md/ }));
    expect(screen.getByText("Arguments")).toBeTruthy();
    act(() => publishDisplayPrefs({ ...DISPLAY_DEFAULTS, toolCallDisplay: "product" }));
    expect(screen.queryByText("Arguments")).toBeNull();
    expect(screen.getByText("hello")).toBeTruthy();
  });

  it("the intro splash and the backdrop follow their switches", async () => {
    const { store } = await storeWith(TWO_TURNS);
    const shown = (sessionId: string | null) =>
      render(
        <MemoryRouter>
          <TranscriptStoreProvider store={store}>
            <ChatView sessionId={sessionId} />
          </TranscriptStoreProvider>
        </MemoryRouter>,
      );
    const empty = shown(null);
    expect(empty.container.querySelector(".desk-wordmark")).not.toBeNull();
    act(() => publishDisplayPrefs({ ...DISPLAY_DEFAULTS, introSplash: false }));
    expect(empty.container.querySelector(".desk-wordmark")).toBeNull();
    empty.unmount();
    const chat = shown("s1");
    expect(chat.container.querySelector(".chat-backdrop")).toBeNull();
    act(() => publishDisplayPrefs({ ...DISPLAY_DEFAULTS, chatBackdrop: true }));
    expect(chat.container.querySelector(".chat-backdrop")?.getAttribute("aria-hidden")).toBe(
      "true",
    );
  });
});

// -- sidebar density and the cold-start reopen -------------------------------------------------------

function row(extra: Partial<SessionRow> = {}): SessionRow {
  return {
    id: "s1",
    durableId: "s1",
    title: "Fix the build",
    preview: "the CI job fails on lint",
    source: null,
    lastActive: 2,
    startedAt: 1,
    active: false,
    pinned: false,
    archived: false,
    branch: false,
    cwd: "/home/me/indus",
    gitBranch: "main",
    messageCount: 12,
    ids: ["s1"],
    ...extra,
  };
}

describe("session list density", () => {
  it("compact adds nothing, comfortable one summary line, detailed the summary and the facts", () => {
    expect(rowDetails(t, row(), "compact")).toEqual([]);
    expect(rowDetails(t, row(), "comfortable")).toEqual(["the CI job fails on lint"]);
    expect(rowDetails(t, row(), "detailed")).toEqual([
      "the CI job fails on lint",
      "12 messages · main · indus",
    ]);
    // a title that is the preview: the size stands in for the summary
    expect(rowDetails(t, row({ title: null, messageCount: 1 }), "comfortable")).toEqual([
      "1 message",
    ]);
    expect(
      rowDetails(
        t,
        row({ preview: null, messageCount: 0, cwd: null, gitBranch: null }),
        "detailed",
      ),
    ).toEqual([]);
  });
});

describe("reopen last chat", () => {
  function list(extra: Partial<SessionListState> = {}): SessionListState {
    return {
      rows: [],
      view: "live",
      loading: false,
      error: null,
      total: null,
      running: new Set(),
      ...extra,
    };
  }

  it("waits for the list, skips archived rows, and gives up quietly on an empty list", () => {
    expect(lastChatTarget(null, false)).toBeNull();
    expect(lastChatTarget(list(), false)).toBeNull();
    expect(lastChatTarget(list({ loading: true }), true)).toBeNull();
    expect(lastChatTarget(list({ error: "status 503" }), true)).toBeNull();
    expect(lastChatTarget(list(), true)).toBe("none");
    expect(
      lastChatTarget(
        list({ rows: [row({ id: "old", archived: true }), row({ id: "s2" })] }),
        false,
      ),
    ).toBe("s2");
  });
});
