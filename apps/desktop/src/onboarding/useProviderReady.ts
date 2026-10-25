// Whether a model provider is set up (W52-g): asks `GET /api/model/options` once per mount, again
// whenever onboarding is asked for (it may have changed the answer) and at once when the overlay
// saved a key or a model (a first run that finished), using the same rule as the overlay. `null` while unknown (no bridge, backend not answering): callers then
// show nothing rather than a false "needs setup".

import { useEffect, useState } from "react";
import { bridgeRest, type RestLike, rest } from "../settings/settingsApi.ts";
import { anyConfigured, endpointChosen, type ProviderOption } from "./flow.ts";
import { onOnboardingRequest, onProviderSaved } from "./requests.ts";

interface Options {
  providers?: ProviderOption[];
  current?: { model?: string | null; provider?: string | null } | null;
}

/** The answer of the options route → set up or not; null when it cannot tell. */
export function providerReady(data: Options | null): boolean | null {
  if (data === null || !Array.isArray(data.providers)) return null;
  return (
    anyConfigured(data.providers) ||
    endpointChosen({
      currentProvider: data.current?.provider ?? null,
      currentModel: data.current?.model ?? null,
    })
  );
}

/** How long after an onboarding request to ask again (the overlay saves in the meantime). */
const RECHECK_MS = 4_000;
/** While the backend is still starting, ask again this often, a few times. */
const RETRY_MS = 5_000;
const RETRIES = 6;

export function useProviderReady(api?: RestLike | null): boolean | null {
  const target = api === undefined ? bridgeRest() : api;
  const [ready, setReady] = useState<boolean | null>(null);
  const [tick, setTick] = useState(0);
  const [tries, setTries] = useState(0);

  useEffect(
    () =>
      onOnboardingRequest(() => {
        setTimeout(() => setTick((n) => n + 1), RECHECK_MS);
      }),
    [],
  );
  useEffect(() => onProviderSaved(() => setTick((n) => n + 1)), []);

  // biome-ignore lint/correctness/useExhaustiveDependencies: tick asks again
  useEffect(() => {
    if (target === null) return;
    let live = true;
    let retry: ReturnType<typeof setTimeout> | null = null;
    void rest<Options>(target, "GET", "/api/model/options").then((answer) => {
      if (!live) return;
      const value = answer.ok ? providerReady(answer.data) : null;
      setReady(value);
      if (value === null && tries < RETRIES) {
        retry = setTimeout(() => setTries((n) => n + 1), RETRY_MS);
      }
    });
    return () => {
      live = false;
      if (retry !== null) clearTimeout(retry);
    };
  }, [target, tick, tries]);

  return ready;
}
