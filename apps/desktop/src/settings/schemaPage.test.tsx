// @vitest-environment jsdom
import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { fakeRest } from "./fakeRest.ts";
import type { PageDef } from "./pages.ts";
import { orderedKeys, parseInput, SchemaPage } from "./schemaPage.tsx";
import type { SchemaFields } from "./search.ts";
import { SECRET_PLACEHOLDER } from "./settingsApi.ts";

const PAGE: PageDef = {
  id: "approvals",
  label: "settings.page.safety.approvals",
  fields: ["command_allowlist"],
  prefixes: ["approvals."],
};

const SCHEMA: SchemaFields = {
  "approvals.mode": {
    type: "enum",
    options: ["manual", "smart", "off"],
    description: "how risky commands are approved",
    category: "approvals",
  },
  "approvals.mcp_reload_confirm": {
    type: "boolean",
    description: "ask before reloading MCP",
    category: "approvals",
  },
  "approvals.smart_policy": {
    type: "string",
    description: "extra rules for the judge",
    category: "approvals",
  },
  "approvals.timeout": { type: "integer", description: "seconds to wait", category: "approvals" },
  command_allowlist: {
    type: "list",
    description: "commands that never need approval",
    category: "command_allowlist",
  },
  "memory.notes_enabled": { type: "boolean", description: "not on this page", category: "memory" },
};

const CONFIG = {
  approvals: { mode: "manual", mcp_reload_confirm: true, smart_policy: "", timeout: 60 },
  command_allowlist: ["ls", "git status"],
};

function mount(profile: string | null = null, config: unknown = CONFIG, schema = SCHEMA) {
  const api = fakeRest();
  api.route("PUT", "/api/config", { ok: true, warnings: [] });
  render(
    <SchemaPage
      page={PAGE}
      schema={schema}
      config={config}
      api={api}
      profile={profile}
      focusField={null}
    />,
  );
  return api;
}

const puts = (api: ReturnType<typeof fakeRest>) =>
  api.calls.filter((c) => c.method === "PUT").map((c) => ({ body: c.body, profile: c.profile }));

describe("schema page", () => {
  it("renders the page's own fields with the control each type asks for", () => {
    mount();
    expect((screen.getByLabelText("Approval mode") as HTMLSelectElement).tagName).toBe("SELECT");
    expect(screen.getByRole("switch", { name: "Confirm MCP reloads" })).toBeTruthy();
    expect((screen.getByLabelText("Smart policy") as HTMLInputElement).type).toBe("text");
    // a list of names is one comma-separated line (W52-i)
    const list = screen.getByLabelText("Command allowlist") as HTMLInputElement;
    expect(list.tagName).toBe("INPUT");
    expect(list.value).toBe("ls, git status");
    expect(screen.queryByText("not on this page")).toBeNull();
  });

  it("each change sends one patch for the chosen profile", async () => {
    const api = mount("work");
    await act(async () => {
      fireEvent.change(screen.getByLabelText("Approval mode"), { target: { value: "smart" } });
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("switch", { name: "Confirm MCP reloads" }));
    });
    const timeout = screen.getByLabelText("Approval timeout");
    fireEvent.change(timeout, { target: { value: "90" } });
    await act(async () => {
      fireEvent.blur(timeout);
    });
    const list = screen.getByLabelText("Command allowlist");
    fireEvent.change(list, { target: { value: "ls, ,  pwd  " } });
    await act(async () => {
      fireEvent.blur(list);
    });
    expect(puts(api)).toEqual([
      { body: { config: { approvals: { mode: "smart" } } }, profile: "work" },
      { body: { config: { approvals: { mcp_reload_confirm: false } } }, profile: "work" },
      { body: { config: { approvals: { timeout: 90 } } }, profile: "work" },
      { body: { config: { command_allowlist: ["ls", "pwd"] } }, profile: "work" },
    ]);
    expect(screen.getAllByText("Saved").length).toBeGreaterThan(0);
  });

  it("an invalid number is not sent", async () => {
    const api = mount();
    const timeout = screen.getByLabelText("Approval timeout");
    fireEvent.change(timeout, { target: { value: "soon" } });
    await act(async () => {
      fireEvent.blur(timeout);
    });
    expect(puts(api)).toEqual([]);
    expect(screen.getByText("That is not a valid value.")).toBeTruthy();
  });

  it("a stored secret is never re-sent; only a new value is", async () => {
    const schema: SchemaFields = {
      "approvals.smart_policy": { type: "string", description: "", category: "approvals" },
    };
    const api = mount(null, { approvals: { smart_policy: SECRET_PLACEHOLDER } }, schema);
    const input = screen.getByLabelText("Smart policy") as HTMLInputElement;
    expect(input.type).toBe("password");
    expect(input.value).toBe("");
    await act(async () => {
      fireEvent.blur(input);
    });
    expect(puts(api)).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Show what you typed" }));
    expect(input.type).toBe("text");
    fireEvent.change(input, { target: { value: "new rule" } });
    await act(async () => {
      fireEvent.blur(input);
    });
    expect(puts(api)).toEqual([
      { body: { config: { approvals: { smart_policy: "new rule" } } }, profile: undefined },
    ]);
  });

  it("a managed install disables the page with the backend's reason", async () => {
    const api = fakeRest();
    api.route("PUT", "/api/config", () => ({
      status: 403,
      body: { detail: { code: "config_managed", message: "This installation is managed." } },
    }));
    render(
      <SchemaPage
        page={PAGE}
        schema={SCHEMA}
        config={CONFIG}
        api={api}
        profile={null}
        focusField={null}
      />,
    );
    await act(async () => {
      fireEvent.change(screen.getByLabelText("Approval mode"), { target: { value: "off" } });
    });
    expect(screen.getAllByText("This installation is managed.").length).toBeGreaterThan(0);
    expect((screen.getByLabelText("Approval mode") as HTMLSelectElement).disabled).toBe(true);
  });

  it("shows the page's ordered fields first, then the rest in schema order (W52-i)", () => {
    expect(
      orderedKeys({ id: "p", label: "settings.page.safety.approvals", order: ["c", "a"] }, [
        "a",
        "b",
        "c",
        "d",
      ]),
    ).toEqual(["c", "a", "b", "d"]);
  });

  it("parses input per type", () => {
    const list = { type: "list", description: "", category: "x" } as const;
    expect(parseInput(list, "ls, git status ,,pwd", ["x"])).toEqual({
      ok: true,
      value: ["ls", "git status", "pwd"],
    });
    expect(parseInput(list, '[{"provider": "a", "model": "b"}]', [{ provider: "x" }])).toEqual({
      ok: true,
      value: [{ provider: "a", model: "b" }],
    });
    expect(parseInput(list, "{", [{ provider: "x" }])).toEqual({ ok: false });
    expect(parseInput({ type: "number", description: "", category: "x" }, "", 1)).toEqual({
      ok: true,
      value: null,
    });
    expect(parseInput({ type: "number", description: "", category: "x" }, "0.5", 1)).toEqual({
      ok: true,
      value: 0.5,
    });
  });
});
