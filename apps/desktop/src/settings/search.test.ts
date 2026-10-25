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
      "browser",
      "passwords",
      "memory",
      "voice",
      "advanced",
      "notifications",
      "providers",
      "gateways",
      "keyboard",
      "tools",
      "sessions",
      "plugins",
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
    // terminal backend fields and the tool output limit live on Advanced (W52-B-3)
    expect(pageForField("terminal.backend")?.page.id).toBe("terminal");
    expect(pageForField("tool_output.max_chars")?.page.id).toBe("output");
    // a page may also show a field owned elsewhere: Output limits shows the checkpoint limit
    const output = VIEWS.find((v) => v.id === "advanced")?.pages.find((p) => p.id === "output");
    expect(
      output && fieldsOnPage(output, ["checkpoints.max_snapshots", "tool_output.max_chars"]),
    ).toEqual(["checkpoints.max_snapshots", "tool_output.max_chars"]);
    expect(pageForField("checkpoints.max_snapshots")?.page.id).toBe("checkpoints");
    // W59-K1-3: and the per-read file limit, still owned by Files & execution
    expect(
      output &&
        fieldsOnPage(output, ["file_read_max_chars", "tool_output.max_chars", "agent.max_turns"]),
    ).toEqual(["file_read_max_chars", "tool_output.max_chars"]);
    expect(pageForField("file_read_max_chars")?.page.id).toBe("files");
    // W59-K1-4: the voice recording rules and the transcription device are up-front rows
    const voice = VIEWS.find((v) => v.id === "voice");
    expect(voice?.pages.find((p) => p.id === "conversation")?.curated).toEqual([
      "voice.record_key",
      "voice.max_seconds",
      "voice.silence_seconds",
      "voice.wait_seconds",
    ]);
    expect(voice?.pages.find((p) => p.id === "transcription")?.curated).toContain(
      "stt.local_device",
    );
    // voice.read_aloud lands on Text to speech, the other voice.* rows on Voice conversation
    expect(pageForField("voice.read_aloud")?.page.id).toBe("speech");
    expect(pageForField("voice.record_key")?.page.id).toBe("conversation");
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
    expect(resolveTarget("view=nope")).toMatchObject({ view: "model", page: "main" });
    expect(resolveTarget("")).toMatchObject({ view: "model", page: "main" });
  });

  it("an old page id still lands: workspace/desktop is Projects & discovery now (W52-i)", () => {
    expect(resolveTarget("view=workspace&page=desktop")).toMatchObject({
      view: "workspace",
      page: "projects",
    });
    expect(resolveTarget("row=default-directory")).toMatchObject({
      view: "workspace",
      page: "projects",
    });
    expect(resolveTarget("row=data-url-limit")).toMatchObject({ view: "workspace", page: "files" });
  });

  it("browser fields are split between Browser profile and Local & private URLs (W52-i)", () => {
    expect(pageForField("browser.real_profile")?.page.id).toBe("profile");
    expect(pageForField("browser.engine")?.page.id).toBe("profile");
    expect(pageForField("browser.route_private_local")?.page.id).toBe("network");
    expect(pageForField("browser.vault_sources")?.page.id).toBe("sources");
    expect(pageForField("security.allow_private_urls")?.page.id).toBe("privacy");
    const network = VIEWS.find((v) => v.id === "browser")?.pages.find((p) => p.id === "network");
    expect(
      network &&
        fieldsOnPage(network, ["security.allow_private_urls", "browser.route_private_local"]),
    ).toEqual(["security.allow_private_urls", "browser.route_private_local"]);
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
    expect(first?.context).toBe("Safety › Privacy & network");
  });

  it("finds hand-built rows by their keywords and labels", () => {
    expect(search(entries, "tray")[0]?.target).toMatchObject({
      view: "appearance",
      page: "window",
      row: "tray",
    });
    expect(search(entries, "keychain")[0]?.target).toMatchObject({
      view: "gateways",
      page: "connection",
    });
    expect(search(entries, "canary")[0]?.target).toMatchObject({
      view: "about",
      page: "version",
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

describe("settings-b rows land on their new pages (W52-j)", () => {
  const entries = buildEntries(SCHEMA, translate);

  it.each([
    ["quick entry", "advanced", "desktop"],
    ["completion sound", "notifications", "sounds"],
    ["archived", "sessions", "archived"],
    ["uninstall", "about", "uninstall"],
    ["wake word", "voice", "conversation"],
    ["global shortcuts", "keyboard", "global"],
    ["local model servers", "providers", "local"],
  ])('"%s" opens %s › %s', (query, view, page) => {
    expect(search(entries, query)[0]?.target).toMatchObject({ view, page });
  });

  it("curated schema labels are searchable by the page's wording and by the key's words", () => {
    expect(search(entries, "persistent memory")[0]?.target.field).toBe("memory.notes_enabled");
  });
});
