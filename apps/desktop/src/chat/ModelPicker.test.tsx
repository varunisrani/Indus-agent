// @vitest-environment jsdom
import type { ModelOptionsResult } from "@indus/protocol";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import {
  applyModelPick,
  entriesFrom,
  type ModelEntry,
  ModelPicker,
  ModelPrefs,
  orderEntries,
  searchKey,
  searchModels,
} from "./ModelPicker.tsx";
import { FakeConnection, MemoryStorage, rpcError } from "./testFakes.ts";

afterEach(cleanup);

const OPTIONS = {
  provider: "openai",
  model: "gpt-4o",
  providers: [
    {
      slug: "openai",
      name: "OpenAI",
      auth_type: "api_key",
      authenticated: true,
      current: true,
      models: ["gpt-4o", "gpt-4o-mini", "o3"],
    },
    {
      slug: "anthropic",
      name: "Anthropic",
      auth_type: "api_key",
      authenticated: true,
      models: ["claude-sonnet-4-5", "claude-haiku-4-5"],
    },
  ],
} as unknown as ModelOptionsResult;

function daemon(profileDefault: string) {
  let stored = profileDefault;
  const connection = new FakeConnection()
    .on("model.options", () => OPTIONS)
    .on("config.get", (params) => ({
      key: params.key,
      value: params.key === "model.default" ? stored : null,
    }))
    .on("config.set", (params) => {
      if (params.session_id === undefined && params.key === "model") stored = String(params.value);
      return {
        key: params.key,
        value: params.value,
        applied: "applied",
        saved: params.session_id === undefined,
      };
    });
  return { connection, stored: () => stored };
}

const profileWrites = (connection: FakeConnection) =>
  connection.called("config.set").filter((call) => call.params.session_id === undefined);

describe("model search", () => {
  const entries = entriesFrom(OPTIONS);

  it("ignores separators and case", () => {
    expect(searchKey("GPT-4o")).toBe("gpt4o");
    expect(searchModels(entries, "gpt4o").map((e) => e.model)).toEqual(["gpt-4o", "gpt-4o-mini"]);
    expect(searchModels(entries, "claude sonnet").map((e) => e.model)).toEqual([
      "claude-sonnet-4-5",
    ]);
    expect(searchModels(entries, "anthropic haiku").map((e) => e.model)).toEqual([
      "claude-haiku-4-5",
    ]);
  });

  it("puts favourites first", () => {
    const ordered = orderEntries(entries, new Set(["anthropic:claude-haiku-4-5"]));
    expect(ordered[0]?.model).toBe("claude-haiku-4-5");
  });
});

describe("applying a pick", () => {
  const pick = (entry: Pick<ModelEntry, "provider" | "model">) => ({
    provider: entry.provider,
    model: entry.model,
  });

  it("the first pick on a profile without a default writes it once; later picks never", async () => {
    const { connection, stored } = daemon("");
    const prefs = new ModelPrefs(new MemoryStorage());
    const base = {
      request: connection.request,
      sessionId: "s1",
      profile: "work",
      current: null,
      hasHistory: false,
      prefs,
      confirm: async () => true,
    };
    await applyModelPick({ ...base, choice: pick({ provider: "openai", model: "o3" }) });
    expect(profileWrites(connection).map((call) => call.params.value)).toEqual(["openai:o3"]);
    expect(stored()).toBe("openai:o3");
    await applyModelPick({ ...base, choice: pick({ provider: "openai", model: "gpt-4o-mini" }) });
    expect(profileWrites(connection)).toHaveLength(1);
    // every pick applied to the session and became sticky
    expect(
      connection.called("config.set").filter((call) => call.params.session_id === "s1"),
    ).toHaveLength(2);
    expect(prefs.sticky("work")).toEqual({ provider: "openai", model: "gpt-4o-mini" });
  });

  it("a profile that has a default is never written", async () => {
    const { connection } = daemon("openai:gpt-4o");
    const prefs = new ModelPrefs(new MemoryStorage());
    await applyModelPick({
      request: connection.request,
      sessionId: "s1",
      profile: null,
      choice: { provider: "openai", model: "o3" },
      current: null,
      hasHistory: false,
      prefs,
      confirm: async () => true,
    });
    expect(profileWrites(connection)).toEqual([]);
  });

  it("a risky switch asks first and repeats with confirm", async () => {
    const connection = new FakeConnection()
      .on("config.get", () => ({ key: "model.default", value: "openai:gpt-4o" }))
      .on("config.set", (params) => {
        if (params.confirm !== true)
          throw rpcError("CONFIRM_REQUIRED", "this model has a smaller window");
        return { key: "model", value: params.value, applied: "applied", saved: false };
      });
    const asked: string[] = [];
    const result = await applyModelPick({
      request: connection.request,
      sessionId: "s1",
      profile: null,
      choice: { provider: "openai", model: "o3" },
      current: { provider: "openai", model: "gpt-4o" },
      hasHistory: true,
      prefs: new ModelPrefs(new MemoryStorage()),
      confirm: async (message) => {
        asked.push(message);
        return true;
      },
    });
    expect(result.outcome).toBe("applied");
    expect(asked).toEqual(["this model has a smaller window"]);
    expect(connection.called("config.set").at(0)?.params.confirm).toBeUndefined();
    expect(connection.called("config.set").at(1)?.params.confirm).toBe(true);
  });

  it("another provider mid-conversation asks before any call; declining changes nothing", async () => {
    const { connection } = daemon("openai:gpt-4o");
    const result = await applyModelPick({
      request: connection.request,
      sessionId: "s1",
      profile: null,
      choice: { provider: "anthropic", model: "claude-sonnet-4-5" },
      current: { provider: "openai", model: "gpt-4o" },
      hasHistory: true,
      prefs: new ModelPrefs(new MemoryStorage()),
      confirm: async () => false,
    });
    expect(result.outcome).toBe("cancelled");
    expect(connection.called("config.set")).toEqual([]);
  });

  it("applies the model's effort and fast presets", async () => {
    const { connection } = daemon("openai:gpt-4o");
    const prefs = new ModelPrefs(new MemoryStorage());
    prefs.setPreset("openai:o3", { effort: "high", fast: true });
    await applyModelPick({
      request: connection.request,
      sessionId: "s1",
      profile: null,
      choice: { provider: "openai", model: "o3" },
      current: null,
      hasHistory: false,
      prefs,
      confirm: async () => true,
    });
    const sessionSets = connection
      .called("config.set")
      .filter((call) => call.params.session_id === "s1")
      .map((call) => [call.params.key, call.params.value]);
    expect(sessionSets).toEqual([
      ["model", "openai:o3"],
      ["reasoning", "high"],
      ["fast", true],
    ]);
  });

  it("a fresh draft only stores the sticky choice", async () => {
    const { connection } = daemon("openai:gpt-4o");
    const prefs = new ModelPrefs(new MemoryStorage());
    const result = await applyModelPick({
      request: connection.request,
      sessionId: null,
      profile: "p",
      choice: { provider: "openai", model: "o3" },
      current: null,
      hasHistory: false,
      prefs,
      confirm: async () => true,
    });
    expect(result.outcome).toBe("sticky");
    expect(connection.called("config.set")).toEqual([]);
    expect(prefs.sticky("p")).toEqual({ provider: "openai", model: "o3" });
  });
});

describe("the picker", () => {
  it("risky model shows the confirm and switches on Switch", async () => {
    const connection = new FakeConnection()
      .on("model.options", () => OPTIONS)
      .on("config.get", () => ({ key: "model.default", value: "openai:gpt-4o" }))
      .on("config.set", (params) => {
        if (params.confirm !== true) throw rpcError("CONFIRM_REQUIRED", "o3 costs more per call");
        return { key: "model", value: params.value, applied: "applied", saved: false };
      });
    render(
      <ModelPicker
        sessionId="s1"
        profile={null}
        connection={connection}
        prefs={new ModelPrefs(new MemoryStorage())}
        current={{ provider: "openai", model: "gpt-4o" }}
      />,
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "gpt-4o" }));
    });
    fireEvent.change(screen.getByLabelText("Search models"), { target: { value: "o3" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^o3/ }));
    });
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog.textContent).toContain("o3 costs more per call");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Switch" }));
    });
    expect(connection.called("config.set").map((call) => call.params.confirm)).toEqual([
      undefined,
      true,
    ]);
    expect(screen.getByRole("button", { name: "o3" })).toBeTruthy();
  });

  it("search gpt4o lists gpt-4o", async () => {
    const connection = new FakeConnection().on("model.options", () => OPTIONS);
    render(
      <ModelPicker
        sessionId="s1"
        profile={null}
        connection={connection}
        prefs={new ModelPrefs(new MemoryStorage())}
        open
      />,
    );
    await act(async () => {});
    fireEvent.change(screen.getByLabelText("Search models"), { target: { value: "gpt4o" } });
    const options = screen.getAllByRole("listitem").map((option) => option.textContent ?? "");
    expect(options.some((text) => text.includes("gpt-4o"))).toBe(true);
    expect(options.some((text) => text.includes("claude"))).toBe(false);
  });

  it("starring a model keeps it first", async () => {
    const storage = new MemoryStorage();
    const connection = new FakeConnection().on("model.options", () => OPTIONS);
    render(
      <ModelPicker
        sessionId="s1"
        profile={null}
        connection={connection}
        prefs={new ModelPrefs(storage)}
        open
      />,
    );
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "Add claude-haiku-4-5 to favourites" }));
    expect(screen.getAllByRole("listitem")[0]?.textContent).toContain("claude-haiku-4-5");
    expect(new ModelPrefs(storage).favourites().has("anthropic:claude-haiku-4-5")).toBe(true);
  });
});

describe("the composer chips (W51-b)", () => {
  it("names the configured model before the picker is ever opened", async () => {
    const connection = new FakeConnection().on("model.options", () => OPTIONS);
    render(
      <ModelPicker
        sessionId="s1"
        profile={null}
        connection={connection}
        prefs={new ModelPrefs(new MemoryStorage())}
      />,
    );
    expect(await screen.findByRole("button", { name: "gpt-4o" })).toBeTruthy();
    expect(screen.queryByText("Choose a model")).toBeNull();
  });

  it("the effort chip stores the model's preset and tells the session", async () => {
    const connection = new FakeConnection()
      .on("model.options", () => OPTIONS)
      .on("config.set", () => ({
        key: "reasoning",
        value: "high",
        applied: "applied",
        saved: false,
      }));
    const prefs = new ModelPrefs(new MemoryStorage());
    render(<ModelPicker sessionId="s1" profile={null} connection={connection} prefs={prefs} />);
    fireEvent.click(await screen.findByRole("button", { name: "Reasoning effort: Auto" }));
    await act(async () => {
      fireEvent.click(screen.getByRole("menuitemradio", { name: "High" }));
    });
    expect(prefs.preset("openai:gpt-4o").effort).toBe("high");
    expect(connection.called("config.set").map((call) => call.params.key)).toEqual(["reasoning"]);
    expect(screen.getByRole("button", { name: "Reasoning effort: High" })).toBeTruthy();
  });
});
