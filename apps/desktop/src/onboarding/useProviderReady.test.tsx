// @vitest-environment jsdom
import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { fakeRest } from "../settings/fakeRest.ts";
import { announceProviderSaved } from "./requests.ts";
import { providerReady, useProviderReady } from "./useProviderReady.ts";

const options = (provider: string | null, model: string | null) => ({
  providers: [
    {
      name: "openrouter",
      label: "OpenRouter",
      auth_type: "api_key",
      configured: false,
      device_login: false,
    },
  ],
  current: { provider, model },
});

async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

describe("provider readiness", () => {
  it("unknown without an answer; set up by a configured provider or a chosen endpoint", () => {
    expect(providerReady(null)).toBeNull();
    expect(providerReady(options(null, null))).toBe(false);
    expect(providerReady(options("custom", "fake-model"))).toBe(true);
  });

  it("asks again at once when onboarding saved a model (a first run that finished)", async () => {
    const api = fakeRest();
    api.route("GET", "/api/model/options", options(null, null));
    const { result } = renderHook(() => useProviderReady(api));
    await settle();
    expect(result.current).toBe(false);

    api.route("GET", "/api/model/options", options("custom", "fake-model"));
    await act(async () => {
      announceProviderSaved();
    });
    await settle();
    expect(result.current).toBe(true);
  });
});
