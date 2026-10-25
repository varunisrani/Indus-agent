// Asking for the onboarding overlay from elsewhere in the window (Settings › Providers, a
// provider error): one tiny in-memory channel per renderer. Nothing is stored. A second channel
// says the overlay saved a key or a model, so what shows the setup state (the status bar) asks
// the backend again at once.

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

const savedListeners = new Set<() => void>();

/** The overlay saved a key or a model: the provider setup may have changed. */
export function announceProviderSaved(): void {
  for (const listener of [...savedListeners]) listener();
}

export function onProviderSaved(listener: () => void): () => void {
  savedListeners.add(listener);
  return () => {
    savedListeners.delete(listener);
  };
}
