import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { renderPage } from "../testing/render.tsx";
import { McpPage, parseEnvLines, splitArgs } from "./McpPage.tsx";

let mock: MockApi;
let restore: () => void;

beforeEach(() => {
  mock = createMockApi();
  mock.state.mcpServers = [
    {
      name: "files",
      transport: "stdio",
      url: null,
      command: "files-server",
      args: ["--root", "/srv"],
      env: { FILES_TOKEN: "****" },
      auth: "none",
      enabled: true,
    },
    {
      name: "remote",
      transport: "http",
      url: "https://mcp.example.org/x",
      command: null,
      args: [],
      env: {},
      auth: "oauth",
      enabled: false,
    },
  ];
  mock.state.mcpTools = {
    files: [
      { name: "read_file", description: "Read a file" },
      { name: "list_dir", description: "List a folder" },
    ],
    remote: "connection refused",
  };
  restore = mock.install();
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  restore();
});

async function openAdd() {
  fireEvent.click(screen.getByRole("button", { name: "Add server" }));
  await screen.findByRole("dialog", { name: "Add MCP server" });
}

describe("mcp page", () => {
  it("shows an empty servers section that points at the catalog", async () => {
    mock.state.mcpServers = [];
    renderPage(<McpPage />, "/mcp");
    expect(
      await screen.findByRole("button", { name: "Browse catalog" }, { timeout: 5_000 }),
    ).toBeTruthy();
    expect(screen.getByText("Your MCP servers (0)")).toBeTruthy();
    expect(screen.getByText("Catalog (0)")).toBeTruthy();
  });

  it("renders the tool list from a test", async () => {
    renderPage(<McpPage />, "/mcp");
    const row = within(await screen.findByTestId("mcp-files"));
    fireEvent.click(row.getByRole("button", { name: "Test" }));
    const tools = await row.findByRole("list", { name: "Server tools" });
    expect(
      within(tools)
        .getAllByRole("listitem")
        .map((li) => li.textContent),
    ).toEqual(["read_file, ", "list_dir"]);
    expect(row.getByTitle("Read a file").textContent).toBe("read_file");
  });

  it("shows a failed test's reason", async () => {
    renderPage(<McpPage />, "/mcp");
    const row = within(await screen.findByTestId("mcp-remote"));
    fireEvent.click(row.getByRole("button", { name: "Test" }));
    expect((await row.findByRole("alert")).textContent).toContain("connection refused");
  });

  it("shows env values only as the server redacted them", async () => {
    renderPage(<McpPage />, "/mcp");
    const row = within(await screen.findByTestId("mcp-files"));
    expect(row.getByText(/1 env var/).getAttribute("title")).toBe("FILES_TOKEN=****");
  });

  it("adds a stdio server with command, args and env", async () => {
    renderPage(<McpPage />, "/mcp");
    await screen.findByTestId("mcp-files");
    await openAdd();
    fireEvent.change(screen.getByLabelText("Transport"), { target: { value: "stdio" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "notes" } });
    fireEvent.change(screen.getByLabelText("Command"), { target: { value: "notes-server" } });
    fireEvent.change(screen.getByLabelText("Arguments"), {
      target: { value: '--dir "My Notes" -v' },
    });
    fireEvent.change(screen.getByLabelText("Environment (KEY=value per line)"), {
      target: { value: "NOTES_MODE=fast\n" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(mock.callsTo("POST", "/api/mcp/servers")).toHaveLength(1));
    expect(mock.callsTo("POST", "/api/mcp/servers")[0]?.body).toEqual({
      name: "notes",
      command: "notes-server",
      args: ["--dir", "My Notes", "-v"],
      env: { NOTES_MODE: "fast" },
    });
    expect(await screen.findByTestId("mcp-notes")).toBeTruthy();
  });

  it("adds an HTTP server and clears stdio fields when switching", async () => {
    renderPage(<McpPage />, "/mcp");
    await screen.findByTestId("mcp-files");
    await openAdd();
    expect((screen.getByLabelText("Transport") as HTMLSelectElement).value).toBe("http");
    fireEvent.change(screen.getByLabelText("Transport"), { target: { value: "stdio" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "web" } });
    fireEvent.change(screen.getByLabelText("Command"), { target: { value: "leftover" } });
    fireEvent.change(screen.getByLabelText("Transport"), { target: { value: "http" } });
    expect(screen.queryByLabelText("Command")).toBeNull();
    fireEvent.change(screen.getByLabelText("URL"), {
      target: { value: "https://mcp.example.org/web" },
    });
    fireEvent.change(screen.getByLabelText("Authentication"), { target: { value: "header" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() =>
      expect(mock.callsTo("POST", "/api/mcp/servers")[0]?.body).toEqual({
        name: "web",
        url: "https://mcp.example.org/web",
        auth: "header",
      }),
    );
  });

  it("refuses a malformed env line before sending", async () => {
    renderPage(<McpPage />, "/mcp");
    await screen.findByTestId("mcp-files");
    await openAdd();
    fireEvent.change(screen.getByLabelText("Transport"), { target: { value: "stdio" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "bad" } });
    fireEvent.change(screen.getByLabelText("Command"), { target: { value: "x" } });
    fireEvent.change(screen.getByLabelText("Environment (KEY=value per line)"), {
      target: { value: "no equals sign" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect((await screen.findByRole("alert")).textContent).toContain("KEY=value");
    expect(mock.callsTo("POST", "/api/mcp/servers")).toHaveLength(0);
    expect(parseEnvLines("A=1\nB=x=y")).toEqual({ A: "1", B: "x=y" });
    expect(splitArgs("a 'b c' \"d\"")).toEqual(["a", "b c", "d"]);
  });

  it("switches a server and removes one after confirming", async () => {
    renderPage(<McpPage />, "/mcp");
    const remote = within(await screen.findByTestId("mcp-remote"));
    fireEvent.click(remote.getByRole("button", { name: "Enable" }));
    await waitFor(() =>
      expect(mock.callsTo("PUT", "/api/mcp/servers/remote/enabled")[0]?.body).toEqual({
        enabled: true,
      }),
    );

    fireEvent.click(remote.getByRole("button", { name: "Remove" }));
    const dialog = within(await screen.findByRole("dialog"));
    expect(mock.callsTo("DELETE", "/api/mcp/servers/remote")).toHaveLength(0);
    fireEvent.click(dialog.getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(screen.queryByTestId("mcp-remote")).toBeNull());
  });
});
