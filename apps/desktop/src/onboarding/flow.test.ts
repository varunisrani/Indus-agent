import { describe, expect, it } from "vitest";
import {
  type FlowEvent,
  type FlowState,
  INITIAL_FLOW,
  isShown,
  keyProviders,
  type ProviderOption,
  reduce,
} from "./flow.ts";

const KEYED: ProviderOption = {
  name: "openrouter",
  label: "OpenRouter",
  auth_type: "api_key",
  configured: false,
  device_login: false,
  family: "vendor",
};
const DEVICE: ProviderOption = {
  ...KEYED,
  name: "acme",
  label: "Acme",
  auth_type: "oauth",
  device_login: true,
};
const LOCAL: ProviderOption = {
  ...KEYED,
  name: "ollama",
  label: "Ollama",
  auth_type: "api_key_optional",
  family: "custom",
};

function run(events: FlowEvent[], start: FlowState = INITIAL_FLOW): FlowState {
  return events.reduce(reduce, start);
}

const loaded = (configured: boolean): FlowEvent => ({
  type: "options-loaded",
  providers: [{ ...KEYED, configured }, DEVICE, LOCAL],
  currentModel: null,
});

describe("onboarding flow", () => {
  // [events, expected step]
  const table: Array<[string, FlowEvent[], string]> = [
    ["configured → hidden", [loaded(true)], "hidden"],
    [
      "a chosen custom endpoint (no key needed) → hidden",
      [
        {
          type: "options-loaded",
          providers: [{ ...KEYED, configured: false }, DEVICE, LOCAL],
          currentModel: "local-model",
          currentProvider: "custom",
        },
      ],
      "hidden",
    ],
    [
      "custom named without a model → picker",
      [
        {
          type: "options-loaded",
          providers: [{ ...KEYED, configured: false }, DEVICE, LOCAL],
          currentModel: null,
          currentProvider: "custom",
        },
      ],
      "picker",
    ],
    ["unconfigured → picker", [loaded(false)], "picker"],
    ["choose later", [loaded(false), { type: "choose-later" }], "dismissed"],
    [
      "dismissed stays dismissed on a new check result",
      [loaded(false), { type: "choose-later" }, loaded(false)],
      "dismissed",
    ],
    [
      "a provider error re-checks",
      [loaded(false), { type: "choose-later" }, { type: "reopen", manual: false }],
      "checking",
    ],
    ["device start", [loaded(false), { type: "pick-device", provider: DEVICE }], "device-starting"],
    [
      "device code shown",
      [
        loaded(false),
        { type: "pick-device", provider: DEVICE },
        {
          type: "device-started",
          sessionId: "s",
          userCode: "ABCD-1234",
          verificationUrl: "https://x",
          intervalS: 5,
        },
      ],
      "device-polling",
    ],
    [
      "pending keeps polling",
      [
        loaded(false),
        { type: "pick-device", provider: DEVICE },
        {
          type: "device-started",
          sessionId: "s",
          userCode: "c",
          verificationUrl: "u",
          intervalS: 5,
        },
        { type: "device-status", status: "pending", message: "" },
      ],
      "device-polling",
    ],
    [
      "approved → confirm model",
      [
        loaded(false),
        { type: "pick-device", provider: DEVICE },
        {
          type: "device-started",
          sessionId: "s",
          userCode: "c",
          verificationUrl: "u",
          intervalS: 5,
        },
        { type: "device-status", status: "approved", message: "" },
      ],
      "confirm-model",
    ],
    [
      "denied → error",
      [
        loaded(false),
        { type: "pick-device", provider: DEVICE },
        {
          type: "device-started",
          sessionId: "s",
          userCode: "c",
          verificationUrl: "u",
          intervalS: 5,
        },
        { type: "device-status", status: "denied", message: "refused" },
      ],
      "error",
    ],
    [
      "start refused → error",
      [
        loaded(false),
        { type: "pick-device", provider: DEVICE },
        { type: "device-failed", message: "no" },
      ],
      "error",
    ],
    ["key form", [loaded(false), { type: "pick-key", provider: KEYED }], "key-form"],
    [
      "key saved → confirm",
      [
        loaded(false),
        { type: "pick-key", provider: KEYED },
        { type: "key-saving" },
        { type: "key-saved" },
      ],
      "confirm-model",
    ],
    [
      "key failed → form",
      [
        loaded(false),
        { type: "pick-key", provider: KEYED },
        { type: "key-saving" },
        { type: "key-failed", message: "bad" },
      ],
      "key-form",
    ],
    [
      "endpoint form",
      [loaded(false), { type: "pick-endpoint", endpoint: "local" }],
      "endpoint-form",
    ],
    [
      "endpoint ok → confirm",
      [
        loaded(false),
        { type: "pick-endpoint", endpoint: "custom" },
        { type: "endpoint-testing" },
        { type: "endpoint-tested", ok: true, models: ["m"], message: "", baseUrl: "http://x" },
      ],
      "confirm-model",
    ],
    [
      "endpoint refused → form",
      [
        loaded(false),
        { type: "pick-endpoint", endpoint: "custom" },
        { type: "endpoint-testing" },
        { type: "endpoint-tested", ok: false, models: [], message: "401", baseUrl: "http://x" },
      ],
      "endpoint-form",
    ],
    [
      "model saved → hidden",
      [
        loaded(false),
        { type: "pick-key", provider: KEYED },
        { type: "key-saving" },
        { type: "key-saved" },
        { type: "model-saving" },
        { type: "model-saved" },
      ],
      "hidden",
    ],
    [
      "model failed → confirm again",
      [
        loaded(false),
        { type: "pick-key", provider: KEYED },
        { type: "key-saving" },
        { type: "key-saved" },
        { type: "model-saving" },
        { type: "model-failed", message: "x" },
      ],
      "confirm-model",
    ],
    [
      "back to the picker",
      [loaded(false), { type: "pick-key", provider: KEYED }, { type: "back" }],
      "picker",
    ],
    [
      "events out of place change nothing",
      [
        loaded(false),
        { type: "key-saved" },
        { type: "device-status", status: "approved", message: "" },
        { type: "model-saved" },
      ],
      "picker",
    ],
    ["first run cannot be closed", [loaded(false), { type: "close" }], "picker"],
  ];

  for (const [name, events, expected] of table) {
    it(name, () => {
      expect(run(events).step.kind).toBe(expected);
    });
  }

  it("a manual open shows the picker even when configured, can be closed, has no choose-later", () => {
    const opened = run([loaded(true), { type: "reopen", manual: true }, loaded(true)]);
    expect(opened.step.kind).toBe("picker");
    expect(opened.manual).toBe(true);
    expect(run([{ type: "choose-later" }], opened).step.kind).toBe("picker");
    expect(run([{ type: "close" }], opened).step.kind).toBe("hidden");
  });

  it("a provider error does not interrupt a flow on screen", () => {
    const inForm = run([loaded(false), { type: "pick-key", provider: KEYED }]);
    expect(run([{ type: "reopen", manual: false }], inForm).step.kind).toBe("key-form");
  });

  it("endpoint models are kept for the confirm step; the endpoint saves under custom", () => {
    const state = run([
      loaded(false),
      { type: "pick-endpoint", endpoint: "local" },
      { type: "endpoint-testing" },
      {
        type: "endpoint-tested",
        ok: true,
        models: ["a", "b"],
        message: "",
        baseUrl: "http://127.0.0.1:1/v1",
      },
    ]);
    expect(state.step).toMatchObject({
      kind: "confirm-model",
      provider: "custom",
      models: ["a", "b"],
      baseUrl: "http://127.0.0.1:1/v1",
    });
    expect(isShown(state)).toBe(true);
  });

  it("the key catalogue is every api_key provider except custom-family endpoints", () => {
    expect(keyProviders([KEYED, DEVICE, LOCAL]).map((p) => p.name)).toEqual(["openrouter"]);
  });
});
