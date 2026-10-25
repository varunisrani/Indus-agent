// @vitest-environment jsdom
import { act, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { ThemeProvider } from "../app/theme.tsx";
import type { IndusProfileState } from "../global.d.ts";
import type { SettingsBridge } from "./context.tsx";
import { DISPLAY_DEFAULTS } from "./displayPrefs.ts";
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
    expect((screen.getByLabelText("Approval mode") as HTMLSelectElement).value).toBe("manual");
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
    expect(hit?.textContent).toContain("Safety › Privacy & network");
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
    expect((screen.getByLabelText("Approval mode") as HTMLSelectElement).value).toBe("off");
    await act(async () => {
      fireEvent.change(screen.getByLabelText("Approval mode"), { target: { value: "manual" } });
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
    // rows are closed until clicked (W52-j); the row is named by the provider
    fireEvent.click(screen.getByRole("button", { name: /OpenRouter/ }));
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
      ...DISPLAY_DEFAULTS,
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

  it("About → Licences shows the packaged notices as plain text (W46, F-2)", async () => {
    const text = "# Third-party notices\n\n<b>some-lib</b> MIT License\n";
    const bridge: SettingsBridge = { about: { notices: async () => ({ text }) } };
    render(<Harness initial="view=about" api={backend(["default"])} bridge={bridge} />);
    await settle();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Licences" }));
    });
    await settle();
    expect(screen.getByTestId("query").textContent).toBe("view=about&page=licences");
    const block = document.querySelector('[data-ui="settings-licences-text"]') as HTMLElement;
    expect(block.textContent).toBe(text);
    expect(block.querySelector("b")).toBeNull(); // never parsed as markup
  });

  it("Licences without a notices file (a dev run or an old main) says why (W46, F-2)", async () => {
    render(<Harness initial="view=about&page=licences" api={backend(["default"])} bridge={null} />);
    await settle();
    expect(screen.getByText(/The notices file ships inside the installed app/)).toBeTruthy();
    const empty: SettingsBridge = { about: { notices: async () => ({ text: null }) } };
    render(
      <Harness initial="view=about&page=licences" api={backend(["default"])} bridge={empty} />,
    );
    await settle();
    expect(screen.getAllByText(/The notices file ships inside the installed app/)).toHaveLength(2);
  });

  it('search "licenses" finds the licences page', async () => {
    render(<Harness initial="" api={backend(["default"])} bridge={null} />);
    await settle();
    fireEvent.change(screen.getByRole("searchbox", { name: "Search settings" }), {
      target: { value: "licenses" },
    });
    const hit = screen
      .getAllByRole("button")
      .find((b) => b.textContent?.includes("Third-party licences"));
    await act(async () => {
      fireEvent.click(hit as HTMLElement);
    });
    await settle();
    expect(screen.getByTestId("query").textContent).toBe("view=about&page=licences&row=licences");
  });
});

describe("settings-b pages (W52-j)", () => {
  function prefsBridge(overrides: Record<string, unknown> = {}) {
    let state = {
      minimizeToTray: false,
      keepAwake: "while-working" as const,
      quickEntryShortcut: "CommandOrControl+Shift+Space",
      hudSnapShortcut: "CommandOrControl+Shift+G",
      hudToggleShortcut: "CommandOrControl+Shift+H",
      notifications: { turnComplete: true, approvals: true, sounds: true },
      keybindings: {},
      chatTextScale: 110,
      ...DISPLAY_DEFAULTS,
      ...overrides,
    };
    const set = vi.fn(async (patch: Record<string, unknown>) => {
      const notifications = {
        ...state.notifications,
        ...((patch.notifications as object | undefined) ?? {}),
      };
      state = { ...state, ...patch, notifications } as typeof state;
      return state;
    });
    return { set, prefs: { get: async () => state, set, onChanged: () => () => undefined } };
  }

  it("the notifications master switch writes both kinds; off dims the two rows", async () => {
    const fake = prefsBridge();
    const bridge = { prefs: fake.prefs } as unknown as SettingsBridge;
    render(<Harness initial="view=notifications" api={backend(["default"])} bridge={bridge} />);
    await settle();
    await act(async () => {
      fireEvent.click(screen.getByRole("switch", { name: "Enable notifications" }));
    });
    expect(fake.set).toHaveBeenLastCalledWith({
      notifications: { turnComplete: false, approvals: false },
    });
    await settle();
    expect(
      (screen.getByRole("switch", { name: "Approval needed" }) as HTMLInputElement).disabled,
    ).toBe(true);
  });

  it("the completion sound select maps to the sounds preference", async () => {
    const fake = prefsBridge();
    const bridge = { prefs: fake.prefs } as unknown as SettingsBridge;
    render(
      <Harness
        initial="view=notifications&page=sounds"
        api={backend(["default"])}
        bridge={bridge}
      />,
    );
    await settle();
    const select = screen.getByLabelText("Completion sound") as HTMLSelectElement;
    expect(select.value).toBe("chime");
    await act(async () => {
      fireEvent.change(select, { target: { value: "off" } });
    });
    expect(fake.set).toHaveBeenLastCalledWith({ notifications: { sounds: false } });
  });

  it("Local models: Test asks the preset's address, then Use sets the main model", async () => {
    const api = backend(["default"]);
    api.route("GET", "/api/model/options", {
      providers: [
        {
          name: "ollama",
          label: "Ollama (local)",
          auth_type: "api_key_optional",
          configured: false,
          device_login: false,
        },
        {
          name: "openai",
          label: "OpenAI",
          auth_type: "api_key",
          configured: false,
          device_login: false,
        },
      ],
      current: { provider: null, model: null },
    });
    api.route("POST", "/api/model/test-endpoint", {
      ok: true,
      status: 200,
      models: ["llama3"],
      message: "The endpoint offers 1 model.",
    });
    api.route("POST", "/api/model/set", { ok: true });
    render(<Harness initial="view=providers&page=local" api={api} bridge={null} />);
    await settle();
    expect(screen.queryByText("OpenAI")).toBeNull();
    const use = screen.getByRole("button", { name: "Use" }) as HTMLButtonElement;
    expect(use.disabled).toBe(true);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Test" }));
    });
    await settle();
    expect(api.calls.find((c) => c.path === "/api/model/test-endpoint")?.body).toEqual({
      base_url: "http://127.0.0.1:11434/v1",
    });
    expect(screen.getByText("Running")).toBeTruthy();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Use" }));
    });
    await settle();
    expect(api.calls.find((c) => c.path === "/api/model/set")?.body).toEqual({
      scope: "main",
      provider: "ollama",
      model: "llama3",
    });
  });

  it("a key row opens, and Remove clears a stored key", async () => {
    const api = backend(["default"]);
    api.route("GET", "/api/env", {
      MY_TOOL_TOKEN: {
        category: "custom",
        description: "",
        advanced: false,
        is_password: true,
        is_set: true,
        redacted_value: "ab…yz",
      },
    });
    api.route("DELETE", "/api/env", { ok: true });
    render(<Harness initial="view=tools" api={api} bridge={null} />);
    await settle();
    expect(screen.getByText("ab…yz")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /MY TOOL TOKEN/ }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    });
    expect(api.calls.find((c) => c.method === "DELETE")?.body).toEqual({ key: "MY_TOOL_TOKEN" });
  });

  it("a curated page shows its rows in order and folds the rest; a link inside opens the fold", async () => {
    const api = fakeRest();
    api.route("GET", "/api/config/schema", {
      fields: {
        "memory.notes_enabled": { type: "boolean", description: "notes", category: "memory" },
        "memory.provider": { type: "string", description: "provider", category: "memory" },
        "memory.review_interval": { type: "integer", description: "review", category: "memory" },
      },
    });
    api.route("GET", "/api/config", { memory: { notes_enabled: true, provider: "" } });
    api.route("GET", "/api/profiles", { profiles: [{ name: "default" }], current: "default" });
    const { unmount } = render(<Harness initial="view=memory" api={api} bridge={null} />);
    await settle();
    expect(screen.getByRole("switch", { name: "Persistent memory" })).toBeTruthy();
    expect((screen.getByLabelText("Memory provider") as HTMLInputElement).placeholder).toBe(
      "Built-in only",
    );
    expect(screen.queryByLabelText("Review interval")).toBeNull();
    unmount();
    render(<Harness initial="view=memory&field=memory.review_interval" api={api} bridge={null} />);
    await settle();
    expect(screen.getByLabelText("Review interval")).toBeTruthy();
  });
});
