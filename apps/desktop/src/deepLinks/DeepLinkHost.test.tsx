// @vitest-environment jsdom
import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router";
import { describe, expect, it } from "vitest";
import { fakeRest } from "../settings/fakeRest.ts";
import { DeepLinkHost } from "./DeepLinkHost.tsx";
import type { LinkPayload } from "./deepLinkActions.ts";

function fakeLinks() {
  const listeners = new Set<(payload: LinkPayload) => void>();
  const order: string[] = [];
  return {
    order,
    links: {
      onLink: (listener: (payload: LinkPayload) => void) => {
        order.push("listen");
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
      ready: () => {
        order.push("ready");
      },
    },
    deliver: (payload: LinkPayload) => {
      for (const listener of listeners) listener(payload);
    },
  };
}

function Where() {
  const location = useLocation();
  return <output data-testid="where">{`${location.pathname}${location.search}`}</output>;
}

function mount(api = fakeRest()) {
  const fake = fakeLinks();
  render(
    <MemoryRouter initialEntries={["/s-1"]}>
      <DeepLinkHost links={fake.links} api={api} />
      <Where />
    </MemoryRouter>,
  );
  return { ...fake, api };
}

describe("deep link host", () => {
  it("listens before it tells main it is ready", () => {
    const { order } = mount();
    expect(order).toEqual(["listen", "ready"]);
  });

  it("open navigates inside the app", async () => {
    const { deliver } = mount();
    await act(async () =>
      deliver({ kind: "open", name: "settings", params: { view: "keyboard" } }),
    );
    expect(screen.getByTestId("where").textContent).toBe("/settings?view=keyboard");
  });

  it("a skill installs only after the person confirms; cancel calls nothing", async () => {
    const api = fakeRest();
    api.route("POST", "/api/skills/hub/install", { ok: true });
    const { deliver } = mount(api);
    await act(async () => deliver({ kind: "skill", name: "team/helper", params: {} }));
    expect(screen.getByText("team/helper")).toBeTruthy();
    expect(api.calls).toEqual([]);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    });
    expect(api.calls).toEqual([]);
    await act(async () => deliver({ kind: "skill", name: "team/helper", params: {} }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Install" }));
    });
    expect(api.calls.map((c) => [c.method, c.path, c.body])).toEqual([
      ["POST", "/api/skills/hub/install", { identifier: "team/helper" }],
    ]);
    expect(screen.getByText("The skill was installed.")).toBeTruthy();
  });

  it("an mcp link with a command is refused without a dialog", async () => {
    const { deliver, api } = mount();
    await act(async () => deliver({ kind: "mcp", name: "x", params: { command: "sh" } }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("alert").textContent).toContain(
      "may not add a server that runs a program",
    );
    expect(api.calls).toEqual([]);
  });

  it("an mcp link adds the https server after confirmation", async () => {
    const api = fakeRest();
    api.route("POST", "/api/mcp/servers", () => ({ status: 201, body: { ok: true } }));
    const { deliver } = mount(api);
    await act(async () =>
      deliver({ kind: "mcp", name: "docs", params: { url: "https://mcp.example/sse" } }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Add the server" }));
    });
    expect(api.calls[0]?.body).toEqual({
      name: "docs",
      url: "https://mcp.example/sse",
      auth: "none",
    });
  });
});
