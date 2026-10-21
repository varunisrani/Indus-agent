// @vitest-environment jsdom
import { act, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { ThemeProvider } from "../app/theme.tsx";
import type { IndusProfileState } from "../global.d.ts";
import type { SettingsBridge } from "./context.tsx";
import { fakeRest } from "./fakeRest.ts";
import { SettingsOverlay } from "./SettingsOverlay.tsx";

const SCHEMA = {
  fields: {
    "approvals.mode": {
      type: "enum",
      options: ["manual", "off"],
      description: "approval mode",
      category: "approvals",
    },
    "security.fake_ip_ranges": {
      type: "list",
      description: "CIDR blocks a local TUN proxy answers DNS with",
      category: "security",
    },
  },
};

function backend(profiles: string[]) {
  const api = fakeRest();
  api.route("GET", "/api/config/schema", SCHEMA);
  api.route("GET", "/api/config", (request) => ({
    status: 200,
    body: { approvals: { mode: request.profile === "work" ? "off" : "manual" } },
  }));
  api.route("GET", "/api/profiles", {
    profiles: profiles.map((name) => ({ name })),
    current: profiles[0],
  });
  api.route("PUT", "/api/config", { ok: true });
  return api;
}

function profileBridge(initial: string) {
  const listeners = new Set<(state: IndusProfileState) => void>();
  return {
    bridge: {
      profile: {
        get: async () => ({ active: initial, defaultProfile: initial, launch: initial }),
        set: async () => ({ active: initial, defaultProfile: initial, launch: initial }),
        setDefault: async () => ({ active: initial, defaultProfile: initial, launch: initial }),
        prepareChange: async () => ({ ok: true as const }),
        commitChange: async () => ({ active: initial, defaultProfile: initial, launch: initial }),
        onChanged: (listener: (state: IndusProfileState) => void) => {
          listeners.add(listener);
          return () => listeners.delete(listener);
        },
      },
    } satisfies SettingsBridge,
    emit: (state: IndusProfileState) => {
      for (const listener of listeners) listener(state);
    },
  };
}

function Harness(props: {
  initial: string;
  api: ReturnType<typeof fakeRest>;
  bridge: SettingsBridge | null;
  onClose?: () => void;
}) {
  const [query, setQuery] = useState(props.initial);
  return (
    <ThemeProvider>
      <output data-testid="query">{query}</output>
      <SettingsOverlay
        query={query}
        onNavigate={setQuery}
        onClose={props.onClose ?? (() => undefined)}
        api={props.api}
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

describe("settings overlay", () => {
  it("shows a breadcrumb and the page a link names", async () => {
    render(
      <Harness initial="view=safety&page=approvals" api={backend(["default"])} bridge={null} />,
    );
    await settle();
    const crumbs = screen.getByRole("list", { name: "Where you are in Settings" });
    expect(crumbs.textContent).toContain("Settings");
    expect(crumbs.textContent).toContain("Safety");
    expect(crumbs.textContent).toContain("Approvals");
    expect((screen.getByLabelText("Mode") as HTMLSelectElement).value).toBe("manual");
  });

  it('search "proxy" opens the owning child page and outlines the field', async () => {
    render(<Harness initial="" api={backend(["default"])} bridge={null} />);
    await settle();
    fireEvent.change(screen.getByRole("searchbox", { name: "Search settings" }), {
      target: { value: "proxy" },
    });
    const hit = screen
      .getAllByRole("button")
      .find((b) => b.textContent?.includes("Fake ip ranges"));
    expect(hit?.textContent).toContain("Safety › Privacy and network");
    await act(async () => {
      fireEvent.click(hit as HTMLElement);
    });
    await settle();
    expect(screen.getByTestId("query").textContent).toBe(
      "view=safety&page=privacy&field=security.fake_ip_ranges",
    );
    const row = document.querySelector('[data-field="security.fake_ip_ranges"]');
    expect(row?.getAttribute("data-highlighted")).toBe("true");
  });

  it("no chips with one profile; with two the chosen one scopes every request", async () => {
    const single = backend(["default"]);
    const { unmount } = render(<Harness initial="view=safety" api={single} bridge={null} />);
    await settle();
    expect(screen.queryByText("Applies to")).toBeNull();
    expect(single.calls.every((c) => c.profile === undefined)).toBe(true);
    unmount();

    const api = backend(["default", "work"]);
    render(<Harness initial="view=safety" api={api} bridge={null} />);
    await settle();
    expect(screen.getByText("Applies to")).toBeTruthy();
    await act(async () => {
      fireEvent.click(screen.getByRole("radio", { name: "work" }));
    });
    await settle();
    expect((screen.getByLabelText("Mode") as HTMLSelectElement).value).toBe("off");
    await act(async () => {
      fireEvent.change(screen.getByLabelText("Mode"), { target: { value: "manual" } });
    });
    const put = api.calls.find((c) => c.method === "PUT");
    expect(put?.profile).toBe("work");
    expect(screen.getByText("You are changing the settings of work.")).toBeTruthy();
  });

  it("the chip resets when the active profile changes", async () => {
    const api = backend(["default", "work", "lab"]);
    const fake = profileBridge("default");
    render(<Harness initial="view=memory" api={api} bridge={fake.bridge} />);
    await settle();
    await act(async () => {
      fireEvent.click(screen.getByRole("radio", { name: "work" }));
    });
    expect(screen.getByRole("radio", { name: "work" }).getAttribute("aria-checked")).toBe("true");
    await act(async () => {
      fake.emit({ active: "lab", defaultProfile: "default", launch: "default" });
    });
    await settle();
    expect(screen.getByRole("radio", { name: "lab" }).getAttribute("aria-checked")).toBe("true");
    expect(screen.getByRole("radio", { name: "work" }).getAttribute("aria-checked")).toBe("false");
    const last = api.calls.filter((c) => c.path === "/api/config").at(-1);
    expect(last?.profile).toBeUndefined();
  });

  it("a custom endpoint's Test calls the backend route and shows what it found", async () => {
    const api = backend(["default"]);
    api.route("POST", "/api/model/test-endpoint", (request) => ({
      status: 200,
      body: {
        ok: true,
        status: 200,
        models: ["llama-3", "qwen"],
        message: `The endpoint offers 2 models.`,
        echo: request.body,
      },
    }));
    render(<Harness initial="view=providers&page=endpoints" api={api} bridge={null} />);
    await settle();
    fireEvent.change(screen.getByLabelText("Base URL"), {
      target: { value: "http://127.0.0.1:8000/v1" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Test" }));
    });
    await settle();
    const call = api.calls.find((c) => c.path === "/api/model/test-endpoint");
    expect(call?.method).toBe("POST");
    expect(call?.body).toEqual({ base_url: "http://127.0.0.1:8000/v1" });
    expect(screen.getByText("The endpoint offers 2 models.")).toBeTruthy();
  });

  it("an API key is saved through the backend and cleared from the field", async () => {
    const api = backend(["default"]);
    api.route("GET", "/api/env", {
      OPENROUTER_API_KEY: {
        category: "provider",
        description: "API key for OpenRouter",
        advanced: false,
        is_password: true,
        is_set: false,
        redacted_value: null,
      },
    });
    api.route("PUT", "/api/env", { key: "OPENROUTER_API_KEY", is_set: true });
    render(<Harness initial="view=providers&page=keys" api={api} bridge={null} />);
    await settle();
    const value = ["test", "key", String(Date.now())].join("-");
    const input = screen.getByLabelText("OPENROUTER_API_KEY") as HTMLInputElement;
    fireEvent.change(input, { target: { value } });
    await act(async () => {
      fireEvent.click(screen.getAllByRole("button", { name: "Save" })[0] as HTMLElement);
    });
    await settle();
    const put = api.calls.find((c) => c.method === "PUT" && c.path === "/api/env");
    expect(put?.body).toEqual({ key: "OPENROUTER_API_KEY", value });
    expect(input.value).toBe("");
    expect(JSON.stringify(window.localStorage)).not.toContain(value);
  });

  it("desktop-held rows read and write the preferences in main", async () => {
    const set = vi.fn(async (patch: Record<string, unknown>) => ({
      minimizeToTray: patch.minimizeToTray === true,
      keepAwake: "while-working" as const,
      quickEntryShortcut: "CommandOrControl+Shift+Space",
      hudSnapShortcut: "CommandOrControl+Shift+G",
      hudToggleShortcut: "CommandOrControl+Shift+H",
      notifications: { turnComplete: true, approvals: true, sounds: true },
      keybindings: {},
      chatTextScale: 110,
    }));
    const bridge: SettingsBridge = {
      prefs: {
        get: async () => set({}),
        set,
        onChanged: () => () => undefined,
      },
    };
    render(<Harness initial="row=tray" api={backend(["default"])} bridge={bridge} />);
    await settle();
    const toggle = screen.getByRole("switch", { name: "Minimise to the tray" });
    expect(toggle.closest("[data-row]")?.getAttribute("data-highlighted")).toBe("true");
    await act(async () => {
      fireEvent.click(toggle);
    });
    expect(set).toHaveBeenLastCalledWith({ minimizeToTray: true });
  });

  it("main channels that have not landed say so instead of breaking the page", async () => {
    const bridge: SettingsBridge = {
      updates: {
        status: async () => {
          throw new Error("not-available: indus:updates:status is not available in this build yet");
        },
        check: async () => {
          throw new Error("not-available: x");
        },
        apply: async () => ({ accepted: false }),
        setChannel: async () => {
          throw new Error("not-available: x");
        },
        onProgress: () => () => undefined,
        onSkew: () => () => undefined,
      },
    };
    render(<Harness initial="view=about" api={backend(["default"])} bridge={bridge} />);
    await settle();
    expect(screen.getByText("Not available in this build yet.")).toBeTruthy();
  });
});
