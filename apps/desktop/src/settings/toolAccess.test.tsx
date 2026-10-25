// @vitest-environment jsdom
// W60-L2: Tools & Keys lists the variables our tools declare (set state only), Advanced › Tool
// access starts with the enabled toolsets, and Appearance › Pet lists the pets the store holds.
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { ThemeProvider } from "../app/theme.tsx";
import type { ChatConnection } from "../gateway/connectionStore.ts";
import { GatewayConnectionProvider } from "../gateway/react.tsx";
import { fakeRest } from "./fakeRest.ts";
import { toolsetNames } from "./pagesB/advanced.tsx";
import { toolKeyRows } from "./pagesB/keys.tsx";
import { SettingsOverlay } from "./SettingsOverlay.tsx";

function backend() {
  const api = fakeRest();
  api.route("GET", "/api/config/schema", {
    fields: {
      "prompt.tool_use_enforcement": {
        type: "string",
        description: "tool-use enforcement",
        category: "prompt",
      },
    },
  });
  api.route("GET", "/api/config", { prompt: { tool_use_enforcement: "auto" } });
  api.route("GET", "/api/profiles", { profiles: [{ name: "default" }], current: "default" });
  return api;
}

function Harness(props: { initial: string; api: ReturnType<typeof fakeRest> }) {
  const [query, setQuery] = useState(props.initial);
  return (
    <ThemeProvider>
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

const GROUPS = [
  {
    toolset: "web",
    backend: "brave",
    label: "Brave Search",
    vars: [{ name: "BRAVE_SEARCH_API_KEY", kind: "key", is_set: true }],
  },
  {
    toolset: "browser",
    backend: "firecrawl",
    label: "Firecrawl",
    vars: [
      { name: "FIRECRAWL_API_KEY", kind: "key", is_set: false },
      { name: "FIRECRAWL_API_URL", kind: "url", is_set: false },
    ],
  },
] as const;

describe("tool keys", () => {
  it("groups the declared variables per toolset and merges a name two backends read", () => {
    const rows = toolKeyRows([
      ...GROUPS,
      {
        toolset: "browser",
        backend: "other",
        label: "Other",
        vars: [{ name: "FIRECRAWL_API_KEY", kind: "key", is_set: false }],
      },
    ]);
    expect(rows.map((group) => group.toolset)).toEqual(["web", "browser"]);
    expect(rows[1]?.rows.map((row) => row.name)).toEqual([
      "FIRECRAWL_API_KEY",
      "FIRECRAWL_API_URL",
    ]);
    expect(rows[1]?.rows[0]?.labels).toEqual(["Firecrawl", "Other"]);
  });

  it("shows Set / Not set only, saves through /api/env and keeps other keys below", async () => {
    const api = backend();
    api.route("GET", "/api/tools/keys", { tools: GROUPS });
    api.route("GET", "/api/env", {
      BRAVE_SEARCH_API_KEY: {
        category: "custom",
        description: "",
        advanced: false,
        is_password: true,
        is_set: true,
        redacted_value: "ab…yz",
      },
      MY_TOOL_TOKEN: {
        category: "custom",
        description: "",
        advanced: false,
        is_password: true,
        is_set: true,
        redacted_value: "cd…wx",
      },
    });
    api.route("PUT", "/api/env", { key: "FIRECRAWL_API_URL", is_set: true });
    render(<Harness initial="view=tools" api={api} />);
    await settle();
    expect(screen.getByRole("region", { name: "web tools" })).toBeTruthy();
    expect(screen.getByRole("button", { name: /BRAVE SEARCH API KEY\s*Set/ })).toBeTruthy();
    expect(screen.getAllByText("Not set")).toHaveLength(2);
    // a declared key never shows its preview; an undeclared one keeps its row
    expect(screen.queryByText("ab…yz")).toBeNull();
    expect(screen.getByText("cd…wx")).toBeTruthy();
    expect(screen.getByText("Other keys")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /FIRECRAWL API URL/ }));
    expect(screen.getByText("Firecrawl · FIRECRAWL_API_URL")).toBeTruthy();
    const field = screen.getByRole("textbox", { name: "FIRECRAWL_API_URL" }) as HTMLInputElement;
    fireEvent.change(field, { target: { value: "http://127.0.0.1:3002" } });
    await act(async () => {
      const body = field.closest(".sb-key-body") as HTMLElement;
      fireEvent.click(within(body).getByRole("button", { name: "Save" }));
    });
    expect(api.calls.find((c) => c.method === "PUT" && c.path === "/api/env")?.body).toEqual({
      key: "FIRECRAWL_API_URL",
      value: "http://127.0.0.1:3002",
    });
  });

  it("keeps the empty state when no tool declares a variable and nothing is stored", async () => {
    const api = backend();
    api.route("GET", "/api/tools/keys", { tools: [] });
    api.route("GET", "/api/env", {});
    render(<Harness initial="view=tools" api={api} />);
    await settle();
    expect(screen.getByText("No tool keys yet")).toBeTruthy();
  });
});

describe("enabled toolsets", () => {
  it("splits a typed list", () => {
    expect(toolsetNames(" web, file\nbrowser ,, ")).toEqual(["web", "file", "browser"]);
  });

  it("shows the bundle first, saves a list and reports a refusal", async () => {
    const api = backend();
    api.route("GET", "/api/tools/toolsets", {
      toolsets: [],
      selection: { platform: "cli", toolsets: ["indus-cli"], saved: false },
    });
    let refuse = false;
    api.route("PUT", "/api/tools/toolsets", (request) =>
      refuse
        ? {
            status: 400,
            body: {
              detail: {
                code: "unknown_toolset",
                message: "Not a toolset: wbe. Nothing was changed.",
              },
            },
          }
        : {
            status: 200,
            body: {
              ok: true,
              platform: "cli",
              toolsets: (request.body as { toolsets: string[] }).toolsets,
            },
          },
    );
    render(<Harness initial="view=advanced&page=tools" api={api} />);
    await settle();
    const field = screen.getByLabelText("Enabled toolsets") as HTMLInputElement;
    expect(field.value).toBe("indus-cli");
    expect(screen.getByText(/Now: the indus-cli bundle/)).toBeTruthy();
    // the schema rows still follow
    expect(screen.getByLabelText("Tool-use enforcement")).toBeTruthy();

    fireEvent.change(field, { target: { value: "web, file" } });
    await act(async () => {
      fireEvent.blur(field);
    });
    await settle();
    expect(api.calls.find((c) => c.method === "PUT")?.body).toEqual({ toolsets: ["web", "file"] });
    expect(screen.getByText("Saved")).toBeTruthy();

    refuse = true;
    fireEvent.change(field, { target: { value: "web, wbe" } });
    await act(async () => {
      fireEvent.keyDown(field, { key: "Enter" });
    });
    await settle();
    expect(screen.getByText("Not a toolset: wbe. Nothing was changed.")).toBeTruthy();
  });
});

describe("pet page", () => {
  it("lists the pets the store holds and picks one", async () => {
    const calls: Array<{ method: string; params: object }> = [];
    const connection = {
      request: async (method: string, params: object) => {
        calls.push({ method, params });
        if (method === "pet.list") {
          return {
            pets: [
              { slug: "pip", name: "Pip", shown: true },
              { slug: "moss", name: "Moss", shown: false },
            ],
            active: "pip",
            enabled: true,
          };
        }
        return { slug: "moss", name: "Moss", shown: true };
      },
    } as unknown as ChatConnection;
    render(
      <GatewayConnectionProvider value={connection}>
        <Harness initial="view=appearance&page=pet" api={backend()} />
      </GatewayConnectionProvider>,
    );
    await settle();
    const tiles = screen.getAllByRole("radio");
    expect(tiles.map((tile) => tile.textContent)).toEqual(["Pippip", "Mossmoss"]);
    expect(tiles[0]?.getAttribute("aria-checked")).toBe("true");
    expect(screen.queryByText("No pets yet")).toBeNull();
    await act(async () => {
      fireEvent.click(tiles[1] as HTMLElement);
    });
    expect(calls.find((c) => c.method === "pet.select")?.params).toEqual({ slug: "moss" });
  });
});
