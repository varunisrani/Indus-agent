import { GatewayRpcError } from "@indus/client";
import { ERROR_CODES, type ModelOptionsResult, type ProviderOption } from "@indus/protocol";
import { describe, expect, it, vi } from "vitest";
import type { KeyFlags } from "../cards/keys.ts";
import {
  applyModelChoice,
  disconnected,
  EFFORT_ROWS,
  initialPicker,
  keyFailed,
  keySaved,
  optionsLoaded,
  type PickerState,
  pickerStep,
  SecretBuffer,
  visibleModels,
  visibleProviders,
  windowOf,
} from "./modelPickerState.ts";

const provider = (slug: string, extra: Partial<ProviderOption> = {}) => ({
  slug,
  name: slug[0]?.toUpperCase() + slug.slice(1),
  auth_type: "api_key",
  authenticated: true,
  ...extra,
});

const OPTIONS: ModelOptionsResult = {
  provider: "orbit",
  model: "orbit-large",
  providers: [
    provider("acme", { models: ["acme-1", "acme-2"] }),
    provider("orbit", { models: ["orbit-small", "orbit-large"], current: true }),
    provider("keyless", { authenticated: false }),
    provider("portal", { authenticated: false, auth_type: "oauth" }),
  ] as ProviderOption[],
};

const loaded = (allowGlobal = false): PickerState =>
  optionsLoaded(initialPicker(allowGlobal), OPTIONS);

function drive(state: PickerState, keys: [string, KeyFlags?][], buffer = new SecretBuffer()) {
  let current = state;
  let effect = null as ReturnType<typeof pickerStep>["effect"];
  for (const [input, key] of keys) {
    const result = pickerStep(current, input, key ?? {}, buffer);
    current = result.state;
    effect = result.effect ?? effect;
  }
  return { state: current, effect, buffer };
}

const ENTER: [string, KeyFlags] = ["", { return: true }];
const ESC: [string, KeyFlags] = ["", { escape: true }];
const UP: [string, KeyFlags] = ["", { upArrow: true }];
const DOWN: [string, KeyFlags] = ["", { downArrow: true }];
const ctrl = (letter: string): [string, KeyFlags] => [letter, { ctrl: true }];

describe("provider stage", () => {
  it("starts on the current provider", () => {
    expect(loaded().providerIndex).toBe(1);
    expect(loaded().currentModel).toBe("orbit-large");
  });

  it("arrows move within the list", () => {
    expect(drive(loaded(), [UP, UP, UP]).state.providerIndex).toBe(0);
    expect(drive(loaded(), [DOWN, DOWN, DOWN, DOWN]).state.providerIndex).toBe(3);
  });

  it("typing filters, Backspace and Ctrl+U edit, q is just a letter", () => {
    let { state } = drive(loaded(), [["k"], ["e"]]);
    expect(state.filter).toBe("ke");
    expect(visibleProviders(state).map((p) => p.slug)).toEqual(["keyless"]);
    state = drive(state, [["", { backspace: true }]]).state;
    expect(state.filter).toBe("k");
    state = drive(state, [ctrl("u")]).state;
    expect(state.filter).toBe("");
    const q = drive(loaded(), [["q"]]);
    expect(q.state.filter).toBe("q");
    expect(q.effect).toBeNull();
  });

  it("Esc clears the filter first, then closes", () => {
    const filtered = drive(loaded(), [["a"], ["c"]]);
    const cleared = drive(filtered.state, [ESC]);
    expect(cleared.state.filter).toBe("");
    expect(cleared.state.providerIndex).toBe(0);
    expect(cleared.effect).toBeNull();
    expect(drive(cleared.state, [ESC]).effect).toEqual({ type: "close" });
  });

  it("Enter on a signed-in provider opens its models at the current one", () => {
    const { state } = drive(loaded(), [ENTER]);
    expect(state.stage).toBe("model");
    expect(state.slug).toBe("orbit");
    expect(state.modelIndex).toBe(1);
  });

  it("Enter on a provider without a key opens the key stage; others get a hint", () => {
    expect(drive(loaded(), [DOWN, ENTER]).state.stage).toBe("key");
    const oauth = drive(loaded(), [DOWN, DOWN, ENTER]).state;
    expect(oauth.stage).toBe("provider");
    expect(oauth.note).toContain("indus model");
  });

  it("Ctrl+D opens the disconnect stage only for a signed-in provider", () => {
    expect(drive(loaded(), [ctrl("d")]).state.stage).toBe("disconnect");
    expect(drive(loaded(), [DOWN, ctrl("d")]).state.stage).toBe("provider");
  });

  it("Ctrl+K opens the key stage to replace a key", () => {
    expect(drive(loaded(), [ctrl("k")]).state.stage).toBe("key");
  });

  it("Ctrl+G switches scope only where allowed", () => {
    expect(drive(loaded(), [ctrl("g")]).state.scope).toBe("session");
    expect(drive(loaded(true), [ctrl("g")]).state.scope).toBe("global");
    expect(drive(loaded(true), [ctrl("g"), ctrl("g")]).state.scope).toBe("session");
  });
});

describe("model stage", () => {
  const models = () => drive(loaded(), [ENTER]).state;

  it("filters models and Enter goes to the effort stage", () => {
    const { state } = drive(models(), [["s"], ["m"], ENTER]);
    expect(state.stage).toBe("effort");
    expect(state.pendingModel).toBe("orbit-small");
    expect(EFFORT_ROWS[state.effortIndex]?.value).toBe("");
  });

  it("with no listed match Enter takes the typed id", () => {
    const { state } = drive(models(), [["x"], ["9"], ENTER]);
    expect(visibleModels({ ...state, stage: "model" })).toEqual([]);
    expect(state.pendingModel).toBe("x9");
  });

  it("Enter with nothing listed and nothing typed does nothing", () => {
    const empty = { ...models(), slug: "keyless" };
    expect(drive(empty, [ENTER]).state.stage).toBe("model");
  });

  it("Esc clears the filter, then goes back to providers on the same row", () => {
    const filtered = drive(models(), [["s"]]).state;
    const cleared = drive(filtered, [ESC]).state;
    expect(cleared.stage).toBe("model");
    expect(cleared.filter).toBe("");
    const back = drive(cleared, [ESC]).state;
    expect(back.stage).toBe("provider");
    expect(back.providerIndex).toBe(1);
  });
});

describe("effort stage", () => {
  const effort = () => drive(loaded(), [ENTER, UP, ENTER]).state;

  it("Enter applies model, provider, effort and scope", () => {
    const { state, effect } = drive(effort(), [["3"], ENTER]);
    expect(state.working).toBe(true);
    expect(effect).toEqual({
      type: "apply",
      provider: "orbit",
      model: "orbit-small",
      effort: "medium",
      scope: "session",
    });
  });

  it("arrows clamp and Esc goes back to the models", () => {
    const top = drive(effort(), [UP, UP, UP, UP, UP, UP, UP, UP]).state;
    expect(top.effortIndex).toBe(0);
    const back = drive(top, [ESC]).state;
    expect(back.stage).toBe("model");
    expect(back.pendingModel).toBe("");
  });

  it("keys wait while the switch is in flight", () => {
    const busy = drive(effort(), [ENTER]).state;
    expect(drive(busy, [ESC]).state).toBe(busy);
  });
});

describe("key stage", () => {
  const keyStage = () => drive(loaded(), [DOWN, ENTER]).state;

  it("typed text goes to the buffer, never the state", () => {
    const buffer = new SecretBuffer();
    const value = ["k", "e", "y", "-", "1"];
    const { state, effect } = drive(
      keyStage(),
      [...value.map((c): [string] => [c]), ENTER],
      buffer,
    );
    expect(state.keyLength).toBe(5);
    expect(JSON.stringify(state)).not.toContain("key-1");
    expect(effect).toEqual({ type: "saveKey", slug: "keyless" });
    expect(buffer.take()).toBe("key-1");
    expect(buffer.length).toBe(0);
  });

  it("Backspace and Ctrl+U edit; empty Enter does nothing", () => {
    const buffer = new SecretBuffer();
    const { state, effect } = drive(
      keyStage(),
      [["a"], ["b"], ["", { backspace: true }], ctrl("u"), ENTER],
      buffer,
    );
    expect(state.keyLength).toBe(0);
    expect(effect).toBeNull();
  });

  it("Esc goes back and wipes the buffer", () => {
    const buffer = new SecretBuffer();
    const { state } = drive(keyStage(), [["z"], ESC], buffer);
    expect(state.stage).toBe("provider");
    expect(buffer.length).toBe(0);
  });

  it("a saved key opens the models; a failure stays with a note", () => {
    const saved = keySaved(keyStage(), provider("keyless", { models: ["k-1"] }) as ProviderOption);
    expect(saved.stage).toBe("model");
    expect(saved.providers.find((p) => p.slug === "keyless")?.authenticated).toBe(true);
    const failed = keyFailed(keyStage(), "keychain locked");
    expect(failed.stage).toBe("key");
    expect(failed.note).toContain("keychain locked");
  });
});

describe("disconnect stage", () => {
  const stage = () => drive(loaded(), [ctrl("d")]).state;

  it("y or Enter signs out; n or Esc goes back", () => {
    expect(drive(stage(), [["y"]]).effect).toEqual({ type: "disconnect", slug: "orbit" });
    expect(drive(stage(), [ENTER]).effect).toEqual({ type: "disconnect", slug: "orbit" });
    expect(drive(stage(), [["n"]]).state.stage).toBe("provider");
    expect(drive(stage(), [ESC]).state.stage).toBe("provider");
  });

  it("marks the provider signed out", () => {
    const after = disconnected(stage(), "orbit", true);
    expect(after.stage).toBe("provider");
    expect(after.providers.find((p) => p.slug === "orbit")?.authenticated).toBe(false);
  });
});

describe("loading and errors", () => {
  it("only Esc or Ctrl+C work before the list arrives", () => {
    expect(drive(initialPicker(), [["a"]]).state.filter).toBe("");
    expect(drive(initialPicker(), [ESC]).effect).toEqual({ type: "close" });
  });

  it("keeps the selected row in a 12-row window", () => {
    expect(windowOf(5, 3)).toEqual({ start: 0, end: 5 });
    expect(windowOf(30, 0)).toEqual({ start: 0, end: 12 });
    expect(windowOf(30, 20)).toEqual({ start: 14, end: 26 });
    expect(windowOf(30, 29)).toEqual({ start: 18, end: 30 });
  });
});

describe("applying a choice", () => {
  const confirmRequired = () =>
    new GatewayRpcError(ERROR_CODES.CONFIRM_REQUIRED.code, "orbit-max is marked as expensive");

  it("a deferred reply adds a notice", async () => {
    const call = vi.fn(async () => ({
      key: "model",
      value: "orbit:orbit-large",
      applied: "deferred",
      saved: false,
    }));
    const lines: string[] = [];
    const outcome = await applyModelChoice(
      { call },
      { sessionId: "s-1", provider: "orbit", model: "orbit-large" },
      { confirm: async () => true, systemLine: (t) => lines.push(t) },
    );
    expect(outcome).toBe("deferred");
    expect(call).toHaveBeenCalledWith("config.set", {
      key: "model",
      value: "orbit:orbit-large",
      session_id: "s-1",
    });
    expect(lines[0]).toContain("when the current reply ends");
  });

  it("confirm_required asks, then retries with confirm: true", async () => {
    const call = vi
      .fn()
      .mockRejectedValueOnce(confirmRequired())
      .mockResolvedValueOnce({
        key: "model",
        value: "orbit:orbit-max",
        applied: "now",
        saved: false,
      })
      .mockResolvedValueOnce({ key: "reasoning", value: "high", applied: "now", saved: false });
    const confirm = vi.fn(async () => true);
    const outcome = await applyModelChoice(
      { call },
      { sessionId: "s-1", provider: "orbit", model: "orbit-max", effort: "high" },
      { confirm, systemLine: () => {} },
    );
    expect(outcome).toBe("applied");
    expect(confirm).toHaveBeenCalledWith(expect.objectContaining({ danger: true }));
    expect(call.mock.calls[1]).toEqual([
      "config.set",
      { key: "model", value: "orbit:orbit-max", session_id: "s-1", confirm: true },
    ]);
    expect(call.mock.calls[2]).toEqual([
      "config.set",
      { key: "reasoning", value: "high", session_id: "s-1" },
    ]);
  });

  it("a declined confirmation changes nothing", async () => {
    const call = vi.fn().mockRejectedValueOnce(confirmRequired());
    const lines: string[] = [];
    const outcome = await applyModelChoice(
      { call },
      { sessionId: "s-1", provider: "orbit", model: "orbit-max" },
      { confirm: async () => false, systemLine: (t) => lines.push(t) },
    );
    expect(outcome).toBe("cancelled");
    expect(call).toHaveBeenCalledTimes(1);
    expect(lines).toEqual(["Model unchanged."]);
  });

  it("other failures are reported and no session means no call", async () => {
    const call = vi.fn().mockRejectedValueOnce(new Error("no route"));
    const lines: string[] = [];
    const hooks = { confirm: async () => true, systemLine: (t: string) => lines.push(t) };
    expect(
      await applyModelChoice({ call }, { sessionId: "s-1", provider: "", model: "zz" }, hooks),
    ).toBe("failed");
    expect(lines[0]).toContain("no route");
    expect(
      await applyModelChoice({ call }, { sessionId: null, provider: "", model: "zz" }, hooks),
    ).toBe("failed");
    expect(call).toHaveBeenCalledTimes(1);
  });
});
