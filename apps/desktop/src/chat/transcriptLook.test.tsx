// @vitest-environment jsdom
// The W52-g transcript look: tool rows, the error card, toasts and the provider status item.
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { StatusBar } from "../app/StatusBar.tsx";
import { clearToasts, notify, ToastRegion } from "../app/Toasts.tsx";
import { PLUGIN_TOAST_EVENT } from "../contrib/host.ts";
import { providerReady } from "../onboarding/useProviderReady.ts";
import { fakeRest } from "../settings/fakeRest.ts";
import { ErrorCard } from "./ErrorCard.tsx";
import { failureEcho } from "./failure.ts";
import type { ErrorItem, ToolPart } from "./messageStream.ts";
import { ToolRow } from "./ToolRow.tsx";
import { terminalResult, toolOutcome } from "./toolSummary.ts";

afterEach(() => {
  cleanup();
  act(() => clearToasts());
  vi.useRealTimers();
});

function tool(extra: Partial<ToolPart> = {}): ToolPart {
  return {
    type: "tool",
    callId: "c1",
    name: "terminal",
    args: { command: "echo scene-01.fountain" },
    state: "ok",
    progress: null,
    fraction: null,
    result: JSON.stringify({ output: "scene-01.fountain", exit_code: 0, error: null }),
    durationMs: 120,
    ...extra,
  };
}

describe("tool row", () => {
  it("is one plain line with the kind icon, no check mark, and opens to a $ command box", () => {
    const view = render(<ToolRow part={tool()} />);
    const row = view.container.querySelector('[data-ui="transcript-tool-row"]') as HTMLElement;
    expect(row.dataset.state).toBe("ok");
    const toggle = screen.getByRole("button", { name: /Ran echo scene-01\.fountain/ });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(row.querySelector(".lucide-circle-check, .lucide-check-circle-2")).toBeNull();
    expect(row.querySelector(".tool-target")?.tagName).toBe("SPAN");
    fireEvent.click(toggle);
    expect(row.querySelector(".tool-command-text")?.textContent).toBe("echo scene-01.fountain");
    expect(row.querySelector(".tool-exit")?.textContent).toBe("exit 0");
    expect(row.querySelector(".tool-exit")?.hasAttribute("data-zero")).toBe(true);
    expect(screen.getByText("scene-01.fountain")).toBeTruthy();
  });

  it("a refused call is red even without an error sentence", () => {
    const blocked = JSON.stringify({ output: "", exit_code: -1, error: "", status: "blocked" });
    expect(toolOutcome(false, blocked)).toBe("failed");
    expect(toolOutcome(true, blocked)).toBe("failed");
    expect(terminalResult(blocked)).toEqual({
      output: "",
      exitCode: -1,
      error: "",
      blocked: true,
    });
    expect(terminalResult("plain text")).toBeNull();
  });

  it("a settled row can be hidden for this view", () => {
    const view = render(<ToolRow part={tool()} settled />);
    fireEvent.click(screen.getByRole("button", { name: "Hide this step" }));
    expect(view.container.querySelector(".tool-row")).toBeNull();
  });

  it("other tools show argument and output sections", () => {
    render(
      <ToolRow part={tool({ name: "read_file", args: { path: "NOTES.md" }, result: "hi" })} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Read NOTES\.md/ }));
    expect(screen.getByText("Arguments")).toBeTruthy();
    expect(screen.getByText("Output")).toBeTruthy();
  });
});

const failure: ErrorItem = {
  kind: "error",
  key: "error:t1",
  turnId: "t1",
  layer: "provider",
  message: "format_error",
  reason: "provider_failed",
  retryable: true,
  resetsAt: null,
};

describe("error card", () => {
  it("has a headline and one sentence; the raw failure sits in closed details", () => {
    const view = render(
      <ErrorCard item={failure} echo="The model provider could not answer: refused" />,
    );
    const card = screen.getByRole("alert");
    expect(card.textContent).toContain("The model could not answer");
    const details = view.container.querySelector("details") as HTMLDetailsElement;
    expect(details.open).toBe(false);
    expect(details.textContent).toContain("format_error");
    expect(details.textContent).toContain("could not answer: refused");
    expect(screen.queryByText("Send diagnostics")).toBeNull();
    expect(screen.getByRole("button", { name: "Copy error details" })).toBeTruthy();
  });

  it("actions reach the host; dismiss hides the card for this view", () => {
    const onAction = vi.fn();
    render(<ErrorCard item={failure} onAction={onAction} />);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onAction).toHaveBeenCalledWith("retry", failure);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("recognises the backend's own failure replies only", () => {
    expect(failureEcho("The model provider could not answer: demo")).toBe(true);
    expect(failureEcho("The turn stopped because of an internal error: boom")).toBe(true);
    expect(failureEcho("Here is the plan you asked for.")).toBe(false);
    expect(failureEcho("")).toBe(false);
  });
});

describe("toasts", () => {
  it("shows the newest, offers the rest, and closes info toasts by themselves", () => {
    vi.useFakeTimers();
    render(<ToastRegion />);
    act(() => {
      notify({ tone: "error", title: "First" });
      notify({ tone: "info", title: "Second" });
    });
    expect(screen.getByText("Second")).toBeTruthy();
    expect(screen.queryByText("First")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show 1 more" }));
    expect(screen.getByText("First")).toBeTruthy();
    act(() => {
      vi.advanceTimersByTime(5_100);
    });
    expect(screen.queryByText("Second")).toBeNull();
    expect(screen.getByRole("alert").textContent).toContain("First");
  });

  it("routes plugin toasts into the region", () => {
    render(<ToastRegion />);
    act(() => {
      window.dispatchEvent(
        new CustomEvent(PLUGIN_TOAST_EVENT, { detail: { text: "Plugin says hi", tone: "error" } }),
      );
    });
    expect(screen.getByRole("alert").textContent).toContain("Plugin says hi");
  });
});

describe("provider status", () => {
  it("reads the options route like onboarding does", () => {
    expect(providerReady(null)).toBeNull();
    expect(providerReady({ providers: [] })).toBe(false);
    expect(
      providerReady({
        providers: [
          {
            name: "x",
            label: "X",
            auth_type: "api_key",
            configured: true,
            device_login: false,
          },
        ],
      }),
    ).toBe(true);
    expect(providerReady({ providers: [], current: { provider: "custom", model: "m" } })).toBe(
      true,
    );
  });

  it("the status bar says the backend needs setup when no provider is configured", async () => {
    const api = fakeRest();
    api.route("GET", "/api/model/options", { providers: [], current: null });
    render(<StatusBar sessionId={null} connection={null} requests={[]} storage={null} api={api} />);
    expect(await screen.findByRole("button", { name: "Backend needs setup" })).toBeTruthy();
  });
});
