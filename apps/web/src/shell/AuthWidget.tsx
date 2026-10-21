// The signed-in person in the sidebar footer (P18-28). It asks the identity route on mount; the
// loopback stand-in identity (or a 401/403) means there is nothing to sign out of, so it renders
// nothing. It also routes back, once, to where a gated 401 sent the browser away from
// (`indus.lastLocation`).

import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router";
import { ApiError } from "../lib/api.ts";
import {
  type AuthIdentity,
  fetchIdentity,
  fetchProviders,
  isLocalIdentity,
  shortUserId,
  signOut,
  takeLastLocation,
} from "../lib/auth.ts";
import { authRequired } from "../lib/bootstrap.ts";

type WidgetState =
  | { kind: "loading" }
  | { kind: "hidden" }
  | { kind: "unavailable" }
  | { kind: "signed-in"; identity: AuthIdentity; providerLabel: string };

function useIdentity(): WidgetState {
  const [state, setState] = useState<WidgetState>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const identity = await fetchIdentity();
        if (cancelled) return;
        if (isLocalIdentity(identity)) {
          setState({ kind: "hidden" });
          return;
        }
        let providerLabel = identity.provider;
        try {
          const listed = await fetchProviders();
          const match = listed.providers.find((p) => p.name === identity.provider);
          if (match?.display_name) providerLabel = match.display_name;
        } catch {
          // the provider key is a fine label when the list cannot be read
        }
        if (!cancelled) setState({ kind: "signed-in", identity, providerLabel });
      } catch (error) {
        if (cancelled) return;
        const status = error instanceof ApiError ? error.status : -1;
        setState(status === 401 || status === 403 ? { kind: "hidden" } : { kind: "unavailable" });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return state;
}

/** Go back to the remembered location once, right after a sign-in. */
function useReturnAfterSignIn(signedIn: boolean): void {
  const navigate = useNavigate();
  const location = useLocation();
  const done = useRef(false);
  useEffect(() => {
    if (!signedIn || done.current) return;
    done.current = true;
    const target = takeLastLocation();
    if (target && target !== `${location.pathname}${location.search}`) {
      navigate(target, { replace: true });
    }
  }, [signedIn, navigate, location.pathname, location.search]);
}

export function AuthWidget() {
  const state = useIdentity();
  const [signingOut, setSigningOut] = useState(false);
  const [signOutFailed, setSignOutFailed] = useState(false);
  useReturnAfterSignIn(state.kind === "signed-in");

  if (state.kind === "hidden") return null;
  if (state.kind === "loading") {
    // Gated mode reserves the row so the footer does not jump; loopback usually renders nothing.
    return authRequired() ? <div className="h-9 px-2" aria-busy="true" /> : null;
  }
  if (state.kind === "unavailable") {
    return (
      <p className="px-2 text-xs text-muted" role="status">
        Sign-in status unavailable
      </p>
    );
  }

  const { identity, providerLabel } = state;
  const shown = shortUserId(identity.user_id);
  const onSignOut = async () => {
    setSigningOut(true);
    setSignOutFailed(false);
    try {
      await signOut();
    } catch {
      setSignOutFailed(true);
      setSigningOut(false);
    }
  };

  return (
    <section
      className="flex items-center justify-between gap-2 px-2 text-xs"
      aria-label={`Signed in as ${identity.display_name || shown}`}
    >
      <div className="flex min-w-0 flex-col">
        {identity.display_name ? (
          <span className="truncate text-fg">{identity.display_name}</span>
        ) : null}
        <span className="truncate font-mono text-fg" title={identity.user_id}>
          {shown}
        </span>
        <span className="truncate text-muted">via {providerLabel}</span>
        {signOutFailed ? (
          <span className="text-danger" role="alert">
            Could not sign out
          </span>
        ) : null}
      </div>
      <button
        type="button"
        className="shrink-0 rounded-md border border-line px-2 py-1 text-xs hover:bg-code disabled:opacity-60"
        onClick={onSignOut}
        disabled={signingOut}
      >
        Sign out
      </button>
    </section>
  );
}
