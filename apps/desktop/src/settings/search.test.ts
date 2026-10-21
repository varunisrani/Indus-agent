import { describe, expect, it } from "vitest";
import { translate } from "../i18n/index.tsx";
import { fieldsOnPage, pageForField, resolveTarget, targetQuery, VIEWS } from "./pages.ts";
import { buildEntries, type SchemaFields, search } from "./search.ts";

const SCHEMA: SchemaFields = {
  "model.default": {
    type: "string",
    description: "model id used when nothing else picks one",
    category: "model",
  },
  "approvals.mode": {
    type: "enum",
    options: ["manual", "smart", "off"],
    description: "manual asks before risky commands",
    category: "approvals",
  },
  "security.fake_ip_ranges": {
    type: "list",
    description: "CIDR blocks a local TUN proxy answers DNS with (dialable, not private)",
    category: "security",
  },
  "terminal.cwd": {
    type: "string",
    description: "working directory for commands",
    category: "terminal",
  },
  "terminal.docker_image": {
    type: "string",
    description: "image the Docker sandbox runs",
    category: "terminal",
  },
  "memory.notes_enabled": {
    type: "boolean",
    description: "keep the agent's own notes",
    category: "memory",
  },
  "platforms.telegram.enabled": { type: "boolean", description: "Telegram", category: "platforms" },
};

describe("settings pages", () => {
  it("has the 1.0 views with child pages", () => {
    expect(VIEWS.map((v) => v.id)).toEqual([
      "model",
      "chat",
      "appearance",
      "workspace",
      "safety",
      "memory",
      "advanced",
      "providers",
      "gateways",
      "keyboard",
      "notifications",
      "sessions",
      "about",
    ]);
    for (const view of VIEWS) expect(view.pages.length).toBeGreaterThan(0);
  });

  it("a field belongs to the first page that claims it, and only that page renders it", () => {
    expect(pageForField("terminal.cwd")?.page.id).toBe("projects");
    expect(pageForField("terminal.docker_image")?.page.id).toBe("terminal");
    expect(pageForField("platforms.telegram.enabled")).toBeNull();
    const workspace = VIEWS.find((v) => v.id === "workspace")?.pages[0];
    const advanced = VIEWS.find((v) => v.id === "advanced")?.pages.find((p) => p.id === "terminal");
    const keys = Object.keys(SCHEMA);
    expect(workspace && fieldsOnPage(workspace, keys)).toEqual(["terminal.cwd"]);
    expect(advanced && fieldsOnPage(advanced, keys)).toEqual(["terminal.docker_image"]);
    // a page may also show a field owned elsewhere
    const directory = VIEWS.find((v) => v.id === "sessions")?.pages.find(
      (p) => p.id === "directory",
    );
    expect(directory && fieldsOnPage(directory, keys)).toEqual(["terminal.cwd"]);
  });
});

describe("settings links", () => {
  it("a link naming only a field lands on the owning child page", () => {
    expect(resolveTarget("field=security.fake_ip_ranges")).toEqual({
      view: "safety",
      page: "privacy",
      field: "security.fake_ip_ranges",
      row: null,
    });
  });

  it("a view without a page, an unknown page or an unknown view land on a first page", () => {
    expect(resolveTarget("view=safety")).toMatchObject({ view: "safety", page: "approvals" });
    expect(resolveTarget("view=safety&page=nope")).toMatchObject({
      view: "safety",
      page: "approvals",
    });
    expect(resolveTarget("view=billing")).toMatchObject({ view: "model", page: "main" });
    expect(resolveTarget("")).toMatchObject({ view: "model", page: "main" });
  });

  it("a hand-built row link finds its page; the query round-trips", () => {
    expect(resolveTarget("row=tray")).toMatchObject({
      view: "appearance",
      page: "window",
      row: "tray",
    });
    const query = targetQuery({ view: "memory", page: "context", field: "compression.enabled" });
    expect(resolveTarget(query)).toMatchObject({
      view: "memory",
      page: "context",
      field: "compression.enabled",
    });
  });
});

describe("settings search", () => {
  const entries = buildEntries(SCHEMA, translate);

  it('"proxy" lands on the child page that owns the proxy setting', () => {
    const [first] = search(entries, "proxy");
    expect(first?.target).toEqual({
      view: "safety",
      page: "privacy",
      field: "security.fake_ip_ranges",
      row: null,
    });
    expect(first?.context).toBe("Safety › Privacy and network");
  });

  it("finds hand-built rows by their keywords and labels", () => {
    expect(search(entries, "tray")[0]?.target).toMatchObject({
      view: "appearance",
      page: "window",
      row: "tray",
    });
    expect(search(entries, "keychain")[0]?.target).toMatchObject({
      view: "gateways",
      page: "storage",
    });
    expect(search(entries, "canary")[0]?.target).toMatchObject({
      view: "advanced",
      page: "desktop",
    });
  });

  it("ranks a label match above a description match, and matches every word", () => {
    const hits = search(entries, "notes enabled");
    expect(hits[0]?.target.field).toBe("memory.notes_enabled");
    expect(search(entries, "approvals smart")[0]?.target.field).toBe("approvals.mode");
    expect(search(entries, "zzzz")).toEqual([]);
    expect(search(entries, "   ")).toEqual([]);
  });

  it("fields no page shows are not offered", () => {
    expect(search(entries, "telegram")).toEqual([]);
  });
});
