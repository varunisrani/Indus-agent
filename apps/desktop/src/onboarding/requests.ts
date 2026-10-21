// Asking for the onboarding overlay from elsewhere in the window (Settings › Providers, a
// provider error): one tiny in-memory channel per renderer. Nothing is stored.

export interface OnboardingRequest {
  /** Why it opens: an explicit open (closable) or a provider/auth failure (first-run style). */
  readonly reason: "manual" | "provider-error";
}

type Listener = (request: OnboardingRequest) => void;

const listeners = new Set<Listener>();

export function requestOnboarding(request: OnboardingRequest = { reason: "manual" }): void {
  for (const listener of [...listeners]) listener(request);
}

export function onOnboardingRequest(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
