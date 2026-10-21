// Renderer-side skew (P20-37, R08.14): a lazily loaded chunk that is gone (a 404 after the app or
// its bundle was replaced underneath this window) must become a notice offering reload / update,
// not a crash on the first lazy import. Vite reports a failed dynamic import preload as the
// `vite:preloadError` window event; browsers report the import itself with a few wordings.

import { isNewer } from "../../electron/updates/version.ts";

const CHUNK_FAILURE = [
  /failed to fetch dynamically imported module/i,
  /error loading dynamically imported module/i,
  /importing a module script failed/i,
  /unable to preload css/i,
  /loading (css )?chunk [\w-]+ failed/i,
];

/** True for an error that means "a lazy chunk of this build is missing". */
export function isChunkLoadFailure(error: unknown): boolean {
  if (error === null || error === undefined) return false;
  const name = (error as { name?: unknown }).name;
  if (name === "ChunkLoadError") return true;
  const message =
    typeof error === "string" ? error : String((error as { message?: unknown }).message ?? "");
  return CHUNK_FAILURE.some((pattern) => pattern.test(message));
}

/** A packaged app older than the runtime it attached to. */
export function runtimeAhead(
  appVersion: string,
  runtimeVersion: string | null,
  packaged: boolean,
): boolean {
  return packaged && runtimeVersion !== null && isNewer(runtimeVersion, appVersion);
}

/**
 * Watch this window for missing chunks: `onSkew` once per window; Vite's preload error is
 * consumed (no uncaught error) once reported. Returns the unsubscribe.
 */
export function watchChunkFailures(target: Window, onSkew: () => void): () => void {
  let reported = false;
  const report = () => {
    if (reported) return;
    reported = true;
    onSkew();
  };
  const onPreload = (event: Event) => {
    event.preventDefault();
    report();
  };
  const onRejection = (event: PromiseRejectionEvent) => {
    if (!isChunkLoadFailure(event.reason)) return;
    event.preventDefault();
    report();
  };
  const onError = (event: ErrorEvent) => {
    if (isChunkLoadFailure(event.error ?? event.message)) report();
  };
  target.addEventListener("vite:preloadError", onPreload);
  target.addEventListener("unhandledrejection", onRejection);
  target.addEventListener("error", onError);
  return () => {
    target.removeEventListener("vite:preloadError", onPreload);
    target.removeEventListener("unhandledrejection", onRejection);
    target.removeEventListener("error", onError);
  };
}
