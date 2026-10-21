// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { FakeConnection, MemoryStorage } from "../chat/testFakes.ts";
import type { OpenServerRequest } from "../gateway/serverRequests.ts";
import {
  cacheHitRate,
  contextShare,
  foldFacts,
  ITEMS_KEY,
  NO_FACTS,
  type SessionFacts,
  StatusBar,
  tokensPerSecond,
} from "./StatusBar.tsx";

afterEach(cleanup);

function connectionWithYolo() {
  let yolo = false;
  return new FakeConnection({ codeVersion: "0.9.1" })
    .on("config.get", () => ({ key: "yolo", value: yolo }))
    .on("config.set", (params) => {
      yolo = params.value === true;
      return { key: "yolo", value: yolo, applied: "applied", saved: false };
    });
}

const usage = (numbers: Record<string, number>) =>
  ({ type: "session.usage", session_id: "s1", seq: 1, usage: numbers }) as never;

describe("session facts", () => {
  it("tokens/s is the mean over the last 10 model calls", () => {
    let facts: SessionFacts = NO_FACTS;
    let now = 0;
    facts = foldFacts(
      facts,
      { type: "turn.started", session_id: "s1", seq: 1, turn_id: "t", generation: 1 } as never,
      now,
    );
    facts = foldFacts(facts, usage({ api_calls: 0, output_tokens: 0 }), now);
    for (let call = 1; call <= 12; call += 1) {
      now += 1000;
      facts = foldFacts(
        facts,
        usage({ api_calls: call, output_tokens: call * (call <= 2 ? 10 : 50) }),
        now,
      );
    }
    expect(facts.samples).toHaveLength(10);
    // the first two calls (slow) fell out of the window
    const rate = tokensPerSecond(facts.samples) ?? 0;
    expect(rate).toBeGreaterThan(40);
  });

  it("cache hit rate and context share", () => {
    expect(cacheHitRate({ cache_read_tokens: 300, input_tokens: 100 })).toBeCloseTo(0.75);
    expect(cacheHitRate({ input_tokens: 0 })).toBeNull();
    expect(contextShare({ context_tokens: 5000, context_window: 20000 })).toBeCloseTo(0.25);
    expect(contextShare({ context_tokens: 5000 })).toBeNull();
  });
});

describe("status bar", () => {
  it("toggles yolo for the session only", async () => {
    const connection = connectionWithYolo();
    render(
      <StatusBar
        sessionId="s1"
        connection={connection}
        requests={[]}
        storage={new MemoryStorage()}
      />,
    );
    const toggle = await screen.findByRole("switch", { name: "Auto-approve" });
    await act(async () => {});
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    await act(async () => {
      fireEvent.click(toggle);
    });
    expect(connection.called("config.set")[0]?.params).toEqual({
      key: "yolo",
      value: true,
      session_id: "s1",
    });
    expect(toggle.getAttribute("aria-checked")).toBe("true");
    expect(toggle.getAttribute("title")).toContain("Hard safety limits still apply");
  });

  it("shows the context meter with a breakdown popover", async () => {
    const connection = connectionWithYolo();
    render(
      <StatusBar
        sessionId="s1"
        connection={connection}
        requests={[]}
        storage={new MemoryStorage()}
      />,
    );
    act(() => {
      connection.emit("s1", {
        type: "session.usage",
        usage: {
          api_calls: 2,
          input_tokens: 1200,
          output_tokens: 300,
          cache_read_tokens: 4000,
          context_tokens: 32000,
          context_window: 128000,
        },
      });
    });
    expect(screen.getByText("25%")).toBeTruthy();
    fireEvent.click(screen.getByText("25%"));
    const popover = screen.getByRole("dialog", { name: "Context breakdown" });
    expect(popover.textContent).toContain("32000 / 128000");
    expect(popover.textContent).toContain("4000");
  });

  it("shows workspace, model, approvals and version; Reconnect reaches the store", async () => {
    const connection = connectionWithYolo();
    const approval = { id: "r1", sessionId: "s1", kind: "approval" } as OpenServerRequest;
    render(
      <StatusBar
        sessionId="s1"
        connection={connection}
        requests={[approval]}
        storage={new MemoryStorage()}
      />,
    );
    act(() => {
      connection.emit("s1", {
        type: "session.info",
        model: "gpt-4o",
        provider: "openai",
        cwd: "/home/me/project",
        reasoning_effort: "high",
      });
    });
    expect(screen.getByText("/home/me/project")).toBeTruthy();
    expect(screen.getByText("gpt-4o · high")).toBeTruthy();
    expect(screen.getByText("1 to approve")).toBeTruthy();
    expect(screen.getByText("v0.9.1")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Background service" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Reconnect" }));
    expect(connection.reconnects).toBe(1);
  });

  it("items can be hidden and shown, and the choice is kept", () => {
    const storage = new MemoryStorage();
    const connection = connectionWithYolo();
    render(<StatusBar sessionId="s1" connection={connection} requests={[]} storage={storage} />);
    expect(screen.getByText("v0.9.1")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Choose status items" }));
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: /Version/ }));
    expect(screen.queryByText("v0.9.1")).toBeNull();
    expect(JSON.parse(storage.getItem(ITEMS_KEY) ?? "[]")).not.toContain("version");
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: /Tokens per second/ }));
    expect(JSON.parse(storage.getItem(ITEMS_KEY) ?? "[]")).toContain("speed");
    cleanup();
    render(<StatusBar sessionId="s1" connection={connection} requests={[]} storage={storage} />);
    expect(screen.queryByText("v0.9.1")).toBeNull();
  });

  it("without a session there is no yolo toggle; without a connection the daemon item says so", () => {
    render(
      <StatusBar sessionId={null} connection={null} requests={[]} storage={new MemoryStorage()} />,
    );
    expect(screen.queryByRole("switch")).toBeNull();
    expect(screen.getByText("Not connected")).toBeTruthy();
  });
});
