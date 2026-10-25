// @vitest-environment jsdom
// Settings slice A (W52-i): the rail, the moved workspace rows, placeholders and the hand-built
// Model / Passwords pages against a fake REST backend.
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { ThemeProvider } from "../app/theme.tsx";
import { en } from "../i18n/en.ts";
import { makeTranslate } from "../i18n/index.tsx";
import { fakeRest } from "./fakeRest.ts";
import { auxStatus } from "./modelPages.tsx";
import { commaList } from "./pagesA.tsx";
import { SettingsOverlay } from "./SettingsOverlay.tsx";

const SCHEMA = {
  fields: {
    "approvals.mode": { type: "enum", options: ["manual", "off"], description: "", category: "a" },
    "terminal.cwd": { type: "string", description: "working directory", category: "terminal" },
    "browser.vault_sources": { type: "list", description: "", category: "browser" },
    "agent.fast_mode": {
      type: "enum",
      options: ["normal", "fast", "auto"],
      description: "",
      category: "agent",
    },
    "model.context_length": { type: "integer", description: "", category: "model" },
  },
};

function backend(config: Record<string, unknown>) {
  const api = fakeRest();
  api.route("GET", "/api/config/schema", SCHEMA);
  api.route("GET", "/api/config", config);
  api.route("GET", "/api/profiles", { profiles: [{ name: "default" }], current: "default" });
  api.route("PUT", "/api/config", { ok: true });
  api.route("POST", "/api/model/set", { ok: true });
  api.route("GET", "/api/model/options", {
    providers: [
      { name: "alpha", label: "Alpha", configured: true },
      { name: "beta", label: "Beta", configured: false },
    ],
    current: { provider: "alpha", model: "a-1" },
  });
  api.route("GET", "/api/model/auxiliary", {
    tasks: [
      { task: "vision", provider: "auto", model: "", base_url: "", description: "" },
      { task: "title", provider: "beta", model: "b-2", base_url: "", description: "" },
    ],
    main: { provider: "alpha", model: "a-1" },
  });
  return api;
}

function Harness(props: { initial: string; api: ReturnType<typeof fakeRest> }) {
  const [query, setQuery] = useState(props.initial);
  return (
    <ThemeProvider>
      <output data-testid="query">{query}</output>
      <SettingsOverlay
        query={query}
        onNavigate={setQuery}
        onClose={() => undefined}
        api={props.api}
        bridge={null}
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

function writes(api: ReturnType<typeof fakeRest>, method: "PUT" | "POST", path: string) {
  return api.calls.filter((c) => c.method === method && c.path === path).map((c) => c.body);
}

describe("settings slice A", () => {
  it("the rail has Browser and Passwords & Logins after Safety, icons per page, a footer", async () => {
    render(<Harness initial="view=model" api={backend({})} />);
    await settle();
    const nav = screen.getByRole("navigation", { name: "Settings sections" });
    const groups = Array.from(nav.querySelectorAll(".settings-view")).map((b) => b.textContent);
    expect(groups.slice(0, 7)).toEqual([
      "Model",
      "Chat",
      "Appearance",
      "Workspace",
      "Safety",
      "Browser",
      "Passwords & Logins",
    ]);
    const children = Array.from(nav.querySelectorAll(".settings-page-link"));
    expect(children.map((b) => b.textContent)).toEqual([
      "Main model",
      "Fallback models",
      "Auxiliary models",
      "Mixture of Agents",
    ]);
    for (const child of children) expect(child.querySelector("svg")).not.toBeNull();
    for (const name of ["Export settings", "Import settings", "Reset to defaults"]) {
      expect((within(nav).getByRole("button", { name }) as HTMLButtonElement).disabled).toBe(true);
    }
  });

  it("an old workspace/desktop link lands on Projects & discovery with the folder row", async () => {
    render(
      <Harness initial="view=workspace&page=desktop" api={backend({ terminal: { cwd: "." } })} />,
    );
    await settle();
    expect(screen.getByRole("list", { name: "Where you are in Settings" }).textContent).toContain(
      "Projects & discovery",
    );
    expect(document.querySelector('[data-row="default-directory"] input')).not.toBeNull();
    expect((screen.getByLabelText("Working directory") as HTMLInputElement).value).toBe(".");
  });

  it("reference rows without a feature are shown disabled with the not-available caption", async () => {
    render(<Harness initial="view=appearance&page=general" api={backend({})} />);
    await settle();
    for (const name of ["Intro splash", "Model pricing", "Guided tours"]) {
      expect((screen.getByRole("switch", { name }) as HTMLInputElement).disabled).toBe(true);
    }
    expect(screen.getAllByText("Not available in this build yet.").length).toBeGreaterThan(4);
  });

  it("a password manager's switch edits browser.vault_sources", async () => {
    const api = backend({ browser: { vault_sources: ["local", "onepassword", "bitwarden"] } });
    render(<Harness initial="view=passwords&page=sources" api={api} />);
    await settle();
    await act(async () => {
      fireEvent.click(screen.getByRole("switch", { name: "Bitwarden" }));
    });
    expect(writes(api, "PUT", "/api/config")).toEqual([
      { config: { browser: { vault_sources: ["local", "onepassword"] } } },
    ]);
  });

  it("saved credentials is an empty state with a disabled Add (no vault API yet)", async () => {
    render(<Harness initial="view=passwords" api={backend({})} />);
    await settle();
    expect(screen.getByText("Nothing saved yet")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Add" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("main model: Apply posts the new pair; Defaults keep their schema rows", async () => {
    const api = backend({ model: { provider: "alpha", default: "a-1" } });
    render(<Harness initial="view=model&page=main" api={api} />);
    await settle();
    const apply = screen.getByRole("button", { name: "Apply" }) as HTMLButtonElement;
    expect(apply.disabled).toBe(true);
    const model = screen.getByRole("combobox", { name: "Model" });
    fireEvent.change(model, { target: { value: "a-2" } });
    fireEvent.blur(model);
    expect(apply.disabled).toBe(false);
    await act(async () => {
      fireEvent.click(apply);
    });
    expect(writes(api, "POST", "/api/model/set")).toEqual([
      { scope: "main", provider: "alpha", model: "a-2" },
    ]);
    expect(screen.getByLabelText("Speed")).toBeTruthy();
    expect(screen.getByLabelText("Main model context window (override)")).toBeTruthy();
  });

  it("side tasks: status lines and Set to main", async () => {
    const api = backend({});
    render(<Harness initial="view=model&page=auxiliary" api={api} />);
    await settle();
    expect(screen.getByText("auto · use main model")).toBeTruthy();
    expect(screen.getByText("beta · b-2")).toBeTruthy();
    const row = document.querySelector('[data-row="aux-title"]') as HTMLElement;
    await act(async () => {
      fireEvent.click(within(row).getByRole("button", { name: "Set to main" }));
    });
    expect(writes(api, "POST", "/api/model/set")).toEqual([
      { scope: "auxiliary", task: "title", provider: "alpha", model: "a-1" },
    ]);
  });

  it("fallbacks: adding a route writes the whole list", async () => {
    const api = backend({ fallback_providers: [{ provider: "alpha", model: "a-1" }] });
    render(<Harness initial="view=model&page=fallbacks" api={api} />);
    await settle();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Add fallback" }));
    });
    expect(writes(api, "PUT", "/api/config")).toEqual([
      {
        config: {
          fallback_providers: [
            { provider: "alpha", model: "a-1" },
            { provider: "", model: "" },
          ],
        },
      },
    ]);
  });

  it("mixture of agents: a reference edit writes the preset; Set default writes the name", async () => {
    const api = backend({
      moa: {
        default_preset: "one",
        presets: {
          one: { reference_models: [{ provider: "alpha", model: "a-1" }] },
          two: { reference_models: [{ provider: "beta", model: "b-1" }] },
        },
      },
    });
    render(<Harness initial="view=model&page=moa" api={api} />);
    await settle();
    await act(async () => {
      fireEvent.click(screen.getByRole("switch", { name: "Use reference 1" }));
    });
    fireEvent.change(screen.getByLabelText("Preset"), { target: { value: "two" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Set default" }));
    });
    expect(writes(api, "PUT", "/api/config")).toEqual([
      {
        config: {
          moa: {
            presets: {
              one: { reference_models: [{ provider: "alpha", model: "a-1", enabled: false }] },
            },
          },
        },
      },
      { config: { moa: { default_preset: "two" } } },
    ]);
  });

  it("helpers: comma lists and the side task status line", () => {
    expect(commaList(" a, b ,, c\nd ")).toEqual(["a", "b", "c", "d"]);
    const t = makeTranslate(en);
    expect(auxStatus({ provider: "auto", model: "" }, "", t)).toBe("auto · use main model");
    expect(auxStatus({ provider: "beta", model: "b-2" }, "high", t)).toBe(
      "beta · b-2 · Reasoning high",
    );
  });
});
