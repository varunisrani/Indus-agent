import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PageHeader } from "../shell/PageHeader.tsx";
import { ProfileScopeProvider } from "../shell/ProfileScope.tsx";
import { FakePtyServer } from "../testing/fakePtyServer.ts";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { testClient } from "../testing/render.tsx";
import { AttachTokenKeeper } from "./attachToken.ts";
import { ChatHost, type ChatHostDeps, PANEL_COLLAPSED_KEY } from "./ChatHost.tsx";
import { attachImage, type ChatRailProps, chooseWorkspace } from "./ChatRail.tsx";
import { browserFeedDeps, FEED_COPY } from "./eventsFeed.ts";
import { browserPtyDeps } from "./ptySocket.ts";
import { COPY } from "./reconnect.ts";
import type { ChatTerminal, TermSize } from "./terminal.ts";

const TOKEN = ["rail", "session", "value"].join("-");

class FakeTerminal implements ChatTerminal {
  readonly written: string[] = [];
  element: HTMLElement | null = null;
  gpu = false;
  disposed = false;
  readonly #data = new Set<(data: string) => void>();

  open(parent: HTMLElement): void {
    const element = document.createElement("div");
    element.dataset.testid = "fake-xterm";
    parent.appendChild(element);
    this.element = element;
  }
  write(text: string): void {
    this.written.push(text);
  }
  onData(listener: (data: string) => void): () => void {
    this.#data.add(listener);
    return () => this.#data.delete(listener);
  }
  onResize(_listener: (size: TermSize) => void): () => void {
    return () => {};
  }
  /** What a person typing into the terminal does. */
  type(data: string): void {
    for (const listener of this.#data) listener(data);
  }
  size(): TermSize {
    return { cols: 100, rows: 30 };
  }
  fit(): void {}
  focus(): void {}
  scrollLines(): void {}
  rowHeight(): number {
    return 16;
  }
  setRendererVisible(visible: boolean): void {
    this.gpu = visible;
  }
  input(): HTMLTextAreaElement | undefined {
    return undefined;
  }
  dispose(): void {
    this.disposed = true;
  }
}

function memoryStorage(): Storage {
  const map = new Map<string, string>();
  return {
    get length() {
      return map.size;
    },
    clear: () => map.clear(),
    getItem: (key) => map.get(key) ?? null,
    key: () => null,
    removeItem: (key) => {
      map.delete(key);
    },
    setItem: (key, value) => {
      map.set(key, value);
    },
  };
}

interface Harness {
  server: FakePtyServer;
  terminal: FakeTerminal;
  mock: MockApi;
  channel(): string;
}

let restoreFetch: () => void = () => {};

function installApi(mock: MockApi, model: string | null) {
  const previous = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    if (url.includes("/api/model/info")) {
      return new Response(JSON.stringify({ model, provider: model ? "fake" : null }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    return mock.fetch(input, init);
  }) as typeof fetch;
  restoreFetch = () => {
    globalThis.fetch = previous;
  };
}

function renderHost(
  options: { rail?: ChatHostDeps["rail"]; model?: string | null; path?: string } = {},
): Harness {
  const server = new FakePtyServer();
  const terminal = new FakeTerminal();
  const mock = createMockApi();
  installApi(mock, options.model === undefined ? "configured-model" : options.model);
  let n = 0;
  const tokens = new AttachTokenKeeper({
    storage: memoryStorage,
    locks: () => undefined,
    mint: () => {
      n += 1;
      return `attach-${n}`;
    },
  });
  const deps: ChatHostDeps = {
    terminal: async () => terminal,
    pty: {
      ...browserPtyDeps,
      socket: server.dial,
      frame: () => {},
      fontsReady: () => Promise.resolve(),
    },
    feed: { ...browserFeedDeps, socket: server.dial },
    tokens,
    ...(options.rail ? { rail: options.rail } : {}),
  };
  render(
    <QueryClientProvider client={testClient()}>
      <MemoryRouter initialEntries={[options.path ?? "/chat"]}>
        <ProfileScopeProvider>
          <PageHeader title="Chat">
            <ChatHost active deps={deps} />
          </PageHeader>
        </ProfileScopeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return {
    server,
    terminal,
    mock,
    channel: () => server.last("/api/pty").query.get("channel") ?? "",
  };
}

async function connected(h: Harness): Promise<void> {
  await waitFor(() => expect(screen.getByTestId("chat-layout").dataset.phase).toBe("open"));
  if (h.server.socketsAt("/api/events").length > 0) return;
  await waitFor(() => expect(h.server.socketsAt("/api/events").length).toBeGreaterThan(0));
}

beforeEach(() => {
  window.__INDUS_SESSION_TOKEN__ = TOKEN;
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  restoreFetch();
});

describe("model badge", () => {
  it("shows the configured model until the feed names the model answering", async () => {
    const h = renderHost();
    await connected(h);
    const badge = await screen.findByTestId("chat-model");
    await waitFor(() => expect(badge.textContent).toContain("configured-model"));
    expect(badge.dataset.source).toBe("configured");

    // The rail listens on the same channel the PTY child publishes on.
    const feedChannel = h.server.last("/api/events").query.get("channel");
    expect(feedChannel).toBe(h.channel());
    act(() => {
      h.server.publish(h.channel(), {
        type: "session.info",
        session_id: "s-live",
        seq: 3,
        model: "fallback-model",
        provider: "backup",
      });
    });
    const live = await screen.findByTestId("chat-model");
    expect(live.dataset.source).toBe("live");
    expect(live.textContent).toContain("fallback-model");
    expect(live.textContent).not.toContain("configured-model");
  });

  it("says so when no model is configured and the feed is silent", async () => {
    const h = renderHost({ model: null });
    await connected(h);
    expect(await screen.findByText("No model configured")).toBeTruthy();
  });
});

describe("session switcher", () => {
  it("lists recent chats and a click resumes in place with a new PTY URL", async () => {
    const h = renderHost();
    await connected(h);
    const first = h.server.last("/api/pty");
    expect(first.query.get("resume")).toBeNull();
    const rail = screen.getByTestId("chat-rail");
    const entry = await within(rail).findByText("Plan the release");
    expect(within(rail).queryByText("Nightly digest")).toBeNull(); // automation stays out
    const xterm = screen.getByTestId("fake-xterm");

    fireEvent.click(entry);
    await waitFor(() => expect(h.server.last("/api/pty")).not.toBe(first));
    const next = h.server.last("/api/pty");
    expect(next.query.get("resume")).toBe("s-chat-2");
    expect(next.query.get("attach")).toBe(first.query.get("attach"));
    expect(next.query.get("channel")).toBe(first.query.get("channel"));
    expect(next.query.get("cwd")).toBeNull();
    expect(first.clientClose?.code).toBe(1000);
    expect(screen.getByTestId("fake-xterm")).toBe(xterm);
    const calls = h.mock.callsTo("GET", "/api/sessions");
    expect(calls.at(-1)?.query.get("order")).toBe("recent");
  });

  it("New chat dials fresh with a new attach token and the remembered workspace", async () => {
    chooseWorkspace("", "/srv/work");
    const h = renderHost();
    await connected(h);
    const first = h.server.last("/api/pty");
    expect(first.query.get("cwd")).toBe("/srv/work");
    fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    await waitFor(() => expect(h.server.last("/api/pty")).not.toBe(first));
    const next = h.server.last("/api/pty");
    expect(next.query.get("fresh")).toBe("1");
    expect(next.query.get("cwd")).toBe("/srv/work");
    expect(next.query.get("attach")).not.toBe(first.query.get("attach"));
  });

  it("a /chat?resume= link opens that session", async () => {
    const h = renderHost({ path: "/chat?resume=s-chat-1" });
    await connected(h);
    expect(h.server.socketsAt("/api/pty")).toHaveLength(1);
    expect(h.server.last("/api/pty").query.get("resume")).toBe("s-chat-1");
  });
});

describe("isolation", () => {
  it("a rail that throws leaves the terminal mounted and connected", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const Broken = (_props: ChatRailProps): never => {
      throw new Error("rail exploded");
    };
    const h = renderHost({ rail: Broken });
    await waitFor(() => expect(screen.getByTestId("chat-layout").dataset.phase).toBe("open"));
    expect(screen.getByTestId("fake-xterm")).toBeTruthy();
    expect(screen.getByText(/Chat details are unavailable/)).toBeTruthy();
    act(() => h.server.pty().output("still here"));
    expect(h.terminal.written.join("")).toContain("still here");
    h.terminal.type("ls\r");
    expect(h.server.pty().typed).toEqual(["ls\r"]);
  });

  it("a 4403 on the feed shows the reload notice and the terminal stays open", async () => {
    const h = renderHost();
    await connected(h);
    act(() => h.server.last("/api/events").serverClose(4403));
    expect(await screen.findByText(FEED_COPY.loginExpired)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reload page" })).toBeTruthy();
    expect(screen.getByTestId("chat-layout").dataset.phase).toBe("open");
    expect(h.server.last("/api/pty").open).toBe(true);
  });
});

describe("rail actions", () => {
  it("the reasoning picker types the TUI's own command", async () => {
    const h = renderHost();
    await connected(h);
    fireEvent.change(screen.getByLabelText("Reasoning effort"), { target: { value: "high" } });
    expect(h.server.pty().typed).toEqual(["/reasoning high\r"]);
  });

  it("copy last answer copies the latest completed reply", async () => {
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const h = renderHost();
    await connected(h);
    const button = screen.getByRole("button", { name: "Copy last answer" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    act(() => {
      h.server.publish(h.channel(), {
        jsonrpc: "2.0",
        method: "message.complete",
        params: {
          session_id: "s1",
          seq: 9,
          turn_id: "t1",
          text: "the final answer",
          reason: "done",
        },
      });
    });
    await waitFor(() => expect(button.disabled).toBe(false));
    fireEvent.click(button);
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("the final answer"));
    expect(await screen.findByText("Last answer copied.")).toBeTruthy();
  });

  it("an image is uploaded through the Files API and attached with /image", async () => {
    const mock = createMockApi({ files: { "/home/user": [] } });
    installApi(mock, null);
    const typed: string[] = [];
    const file = new File([new Uint8Array([1, 2, 3])], "screen shot.png", { type: "image/png" });
    const path = await attachImage(file, (text) => typed.push(text) > 0, 1234);
    expect(path).toBe("/home/user/indus-chat-uploads/1234-screen-shot.png");
    const upload = mock.callsTo("POST", "/api/files/upload")[0];
    const body = upload?.body as Record<string, unknown> | undefined;
    expect(body?.path).toBe(path);
    expect(typed).toEqual([`/image ${path}\r`]);
    await expect(
      attachImage(new File(["x"], "notes.txt", { type: "text/plain" }), () => true),
    ).rejects.toThrow("Only images");
  });
});

describe("terminal overlays", () => {
  it("child exit offers a new session, which dials fresh with a rotated token", async () => {
    const h = renderHost();
    await connected(h);
    const first = h.server.last("/api/pty");
    act(() => h.server.pty().exit());
    expect(await screen.findByText(COPY.exited)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Start new session" }));
    await waitFor(() => expect(h.server.last("/api/pty")).not.toBe(first));
    const next = h.server.last("/api/pty");
    expect(next.query.get("fresh")).toBe("1");
    expect(next.query.get("attach")).not.toBe(first.query.get("attach"));
    await waitFor(() => expect(screen.queryByTestId("chat-overlay")).toBeNull());
  });

  it("typing while reconnecting shows the notice", async () => {
    const h = renderHost();
    await connected(h);
    h.server.autoAccept = false;
    act(() => h.server.last("/api/pty").drop());
    await waitFor(() =>
      expect(screen.getByTestId("chat-layout").dataset.phase).toBe("reconnecting"),
    );
    act(() => h.terminal.type("x"));
    expect(await screen.findByTestId("chat-notice")).toBeTruthy();
    expect(screen.getByText(COPY.reconnecting)).toBeTruthy();
  });
});

describe("side panel (W52-a)", () => {
  it("collapses to a floating panel button and remembers it", async () => {
    window.localStorage.removeItem(PANEL_COLLAPSED_KEY);
    const h = renderHost();
    await connected(h);
    fireEvent.click(screen.getByRole("button", { name: "Collapse side panel" }));
    const panel = () => document.querySelector<HTMLElement>('aside[aria-label="Chat details"]');
    expect(panel()?.hidden).toBe(true);
    expect(window.localStorage.getItem(PANEL_COLLAPSED_KEY)).toBe("1");
    fireEvent.click(screen.getByRole("button", { name: "Show side panel" }));
    expect(panel()?.hidden).toBe(false);
    expect(window.localStorage.getItem(PANEL_COLLAPSED_KEY)).toBeNull();
  });

  it("the badge follows the feed's connection, not the model", async () => {
    const h = renderHost();
    await connected(h);
    await waitFor(() => expect(screen.getByTestId("chat-feed-phase").textContent).toBe("live"));
    act(() => h.server.last("/api/events").serverClose(4403));
    await waitFor(() => expect(screen.getByTestId("chat-feed-phase").textContent).toBe("error"));
  });

  it("the model picker saves the main model and says to start a new chat", async () => {
    const h = renderHost();
    await connected(h);
    const card = await screen.findByTestId("chat-model");
    await waitFor(() => expect(card.textContent).toContain("configured-model"));
    fireEvent.click(within(card).getByRole("button", { name: /switch model/ }));
    const dialog = await screen.findByRole("dialog", { name: "Switch model" });
    await within(dialog).findByRole("button", { name: /Fake provider/ });
    const switchButton = within(dialog).getByRole("button", {
      name: "Switch",
    }) as HTMLButtonElement;
    expect(switchButton.disabled).toBe(true);
    fireEvent.change(within(dialog).getByLabelText("Model id"), {
      target: { value: "bigger-model" },
    });
    expect(switchButton.disabled).toBe(false);
    fireEvent.click(switchButton);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Switch model" })).toBeNull());
    expect(h.mock.callsTo("POST", "/api/model/set")[0]?.body).toEqual({
      scope: "main",
      provider: "fake",
      model: "bigger-model",
    });
    expect(
      await screen.findByText("Model set to bigger-model. Start a new chat to use it."),
    ).toBeTruthy();
  });

  it("the header shows the session title from the feed", async () => {
    const h = renderHost();
    await connected(h);
    act(() => {
      h.server.publish(h.channel(), {
        type: "session.title",
        session_id: "s1",
        seq: 4,
        title: "Cron folder",
      });
    });
    await waitFor(() =>
      expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Cron folder"),
    );
  });

  it("a fresh chat is looked up in the store only after its first answer", async () => {
    const h = renderHost();
    await connected(h);
    act(() => {
      h.server.publish(h.channel(), { type: "session.info", session_id: "s-new", seq: 1 });
    });
    await screen.findByTestId("chat-model");
    await new Promise((done) => setTimeout(done, 50));
    expect(h.mock.callsTo("GET", "/api/sessions/s-new")).toHaveLength(0);
    act(() => {
      h.server.publish(h.channel(), {
        jsonrpc: "2.0",
        method: "message.complete",
        params: { session_id: "s-new", seq: 2, turn_id: "t1", text: "hi", reason: "done" },
      });
    });
    await waitFor(() => expect(h.mock.callsTo("GET", "/api/sessions/s-new").length).toBe(1));
  });
});
