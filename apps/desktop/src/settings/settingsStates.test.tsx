// @vitest-environment jsdom
// Settings pages that showed a state that was not true (W54-D2): password managers "On" with no
// tool installed, the keychain switch ON while secrets sat in files, the main model's own endpoint
// missing from Custom endpoints, and the persona squeezed into a field-sized box.
import { act, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { ThemeProvider } from "../app/theme.tsx";
import type { IndusSecretsPolicyView } from "../global.d.ts";
import type { SettingsBridge } from "./context.tsx";
import { fakeRest } from "./fakeRest.ts";
import { managerRowState } from "./pagesA.tsx";
import { storageSwitchState } from "./pagesB/gateways.tsx";
import { SettingsOverlay } from "./SettingsOverlay.tsx";

const SCHEMA = {
  fields: {
    "browser.vault_sources": { type: "list", description: "", category: "browser" },
  },
};

function backend(config: Record<string, unknown>) {
  const api = fakeRest();
  api.route("GET", "/api/config/schema", SCHEMA);
  api.route("GET", "/api/config", config);
  api.route("GET", "/api/profiles", { profiles: [{ name: "default" }], current: "default" });
  api.route("PUT", "/api/config", { ok: true });
  return api;
}

function Harness(props: {
  initial: string;
  api: ReturnType<typeof fakeRest>;
  bridge: SettingsBridge | null;
}) {
  const [query, setQuery] = useState(props.initial);
  return (
    <ThemeProvider>
      <SettingsOverlay
        query={query}
        onNavigate={setQuery}
        onClose={() => undefined}
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

const ALL = { browser: { vault_sources: ["local", "onepassword", "bitwarden"] } };

describe("password managers (W54-D2-1)", () => {
  it("a manager whose tool is not installed reads Not detected, with no switch", async () => {
    const bridge: SettingsBridge = {
      vault: { detect: async () => ({ checked: true, onepassword: false, bitwarden: true }) },
    };
    render(<Harness initial="view=passwords&page=sources" api={backend(ALL)} bridge={bridge} />);
    await settle();
    const one = document.querySelector('[data-row="vault-onepassword"]') as HTMLElement;
    expect(one.textContent).toContain("Not detected");
    expect(one.textContent).not.toContain("On");
    expect(one.querySelector('[role="switch"]')).toBeNull();
    const bit = document.querySelector('[data-row="vault-bitwarden"]') as HTMLElement;
    expect(bit.textContent).toContain("On");
    expect(screen.getByRole("switch", { name: "Bitwarden" })).toBeTruthy();
    expect(screen.getByRole("switch", { name: "This computer" })).toBeTruthy();
  });

  it("without an answer (remote backend) no manager claims to be On", async () => {
    const bridge: SettingsBridge = {
      vault: { detect: async () => ({ checked: false, onepassword: false, bitwarden: false }) },
    };
    render(<Harness initial="view=passwords&page=sources" api={backend(ALL)} bridge={bridge} />);
    await settle();
    for (const source of ["onepassword", "bitwarden"]) {
      const row = document.querySelector(`[data-row="vault-${source}"]`) as HTMLElement;
      expect(row.querySelector(".settings-pill")).toBeNull();
      expect(row.querySelector('[role="switch"]')).not.toBeNull();
    }
  });

  it("the row state table", () => {
    const found = { checked: true, onepassword: true, bitwarden: false };
    expect(managerRowState("onepassword", true, found)).toEqual({ pill: "on", showSwitch: true });
    expect(managerRowState("onepassword", false, found)).toEqual({
      pill: "off",
      showSwitch: true,
    });
    expect(managerRowState("bitwarden", true, found)).toEqual({
      pill: "missing",
      showSwitch: false,
    });
    expect(managerRowState("bitwarden", true, null)).toEqual({ pill: null, showSwitch: true });
    expect(managerRowState("local", true, null)).toEqual({ pill: "on", showSwitch: true });
  });
});

describe("keychain switch (W54-D2-2)", () => {
  const policy = (over: Partial<IndusSecretsPolicyView>): IndusSecretsPolicyView => ({
    store: "keychain",
    effective: "keychain",
    keychainUsable: true,
    reason: "keychain available",
    ...over,
  });

  it("follows what this run uses, not the stored wish", () => {
    expect(storageSwitchState(null)).toEqual({ checked: false, disabled: true });
    expect(storageSwitchState(policy({}))).toEqual({ checked: true, disabled: false });
    expect(storageSwitchState(policy({ store: "file", effective: "file" }))).toEqual({
      checked: false,
      disabled: false,
    });
    expect(
      storageSwitchState(policy({ effective: "file", keychainUsable: false, reason: "x" })),
    ).toEqual({ checked: false, disabled: true });
  });

  it("no keychain: the switch reads off and the reason is a quiet line, not an error", async () => {
    const getPolicy = vi.fn(async () =>
      policy({
        effective: "file",
        keychainUsable: false,
        reason: "no secret service (backend basic_text)",
      }),
    );
    const bridge: SettingsBridge = {
      secrets: { getPolicy, setPolicy: async () => policy({}) },
    };
    render(<Harness initial="view=gateways" api={backend({})} bridge={bridge} />);
    await settle();
    const toggle = screen.getByRole("switch", {
      name: "Encrypt saved secrets with the OS keychain",
    }) as HTMLButtonElement;
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    expect(toggle.disabled).toBe(true);
    const line = screen.getByTestId("keychain-unusable");
    expect(line.textContent).toContain("no secret service");
    expect(line.getAttribute("data-error")).toBeNull();
  });
});

describe("custom endpoints (W54-D2-8)", () => {
  it("the main model's own endpoint is listed as the active card", async () => {
    const api = backend({
      model: { provider: "custom", base_url: "http://127.0.0.1:9000/v1", default: "fake-model" },
    });
    api.route("POST", "/api/model/test-endpoint", {
      ok: true,
      status: 200,
      models: ["fake-model"],
      message: "The endpoint offers 1 model.",
    });
    render(<Harness initial="view=providers&page=endpoints" api={api} bridge={null} />);
    await settle();
    const card = document.querySelector('[data-endpoint="main-model"]') as HTMLElement;
    expect(card.textContent).toContain("http://127.0.0.1:9000/v1");
    expect(card.textContent).toContain("Active");
    expect(card.textContent).toContain("fake-model");
    expect(document.querySelector(".sb-count")?.textContent).toBe("1");
    // Save it under a name: the form takes its URL and model
    fireEvent.click(
      screen.getByRole("button", { name: "Save the main model endpoint under a name" }),
    );
    expect((screen.getByLabelText("Base URL") as HTMLInputElement).value).toBe(
      "http://127.0.0.1:9000/v1",
    );
  });

  it("a saved entry that is in use is not listed twice", async () => {
    const api = backend({
      model: { provider: "mine", base_url: "http://h/v1", default: "m" },
      providers: { mine: { base_url: "http://h/v1", models: ["m"] } },
    });
    render(<Harness initial="view=providers&page=endpoints" api={api} bridge={null} />);
    await settle();
    expect(document.querySelector('[data-endpoint="main-model"]')).toBeNull();
    expect(document.querySelector(".sb-count")?.textContent).toBe("1");
  });
});

describe("personality (W54-D2-6)", () => {
  it("is a full-width editor under its title, with Save below", async () => {
    const api = backend({});
    api.route("GET", "/api/profiles/default/persona", { content: "Be brief." });
    api.route("PUT", "/api/profiles/default/persona", { ok: true });
    render(<Harness initial="view=chat&page=personality" api={api} bridge={null} />);
    await settle();
    const box = screen.getByLabelText("Personality") as HTMLTextAreaElement;
    expect(box.value).toBe("Be brief.");
    expect(box.closest(".settings-row-below")).not.toBeNull();
    expect(box.closest(".settings-row-control")).toBeNull();
    fireEvent.change(box, { target: { value: "Be kind." } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });
    expect(api.calls.find((c) => c.method === "PUT")?.body).toEqual({ content: "Be kind." });
  });
});

describe("key bindings (W54-D2-9)", () => {
  it("the composer's built-in keys are listed as fixed chips, not as rebindable rows", async () => {
    const snapshot = {
      minimizeToTray: false,
      keepAwake: "while-working" as const,
      quickEntryShortcut: "",
      hudSnapShortcut: "",
      hudToggleShortcut: "",
      notifications: { turnComplete: true, approvals: true, sounds: true },
      keybindings: {},
      chatTextScale: 100,
    };
    const bridge: SettingsBridge = {
      prefs: { get: async () => snapshot, set: async () => snapshot, onChanged: () => () => {} },
    };
    render(<Harness initial="view=keyboard" api={backend({})} bridge={bridge} />);
    await settle();
    const composer = screen.getByRole("region", { name: "Composer" });
    const fixed = Array.from(composer.querySelectorAll('[data-fixed="true"]'));
    expect(fixed.map((row) => row.querySelector(".sb-bind-label")?.textContent)).toEqual([
      "Send the message",
      "New line",
      "Mention files, folders, links",
      "Slash commands",
      "Composer help",
      "Earlier prompts",
      "Close a menu",
    ]);
    for (const row of fixed) expect(row.querySelector("button")).toBeNull();
    expect(fixed[1]?.textContent).toContain("ShiftEnter");
  });
});
