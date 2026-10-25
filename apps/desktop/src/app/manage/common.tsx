// Shared pieces of the management pages (P24-27, P24-28): loading a REST value, the page frame,
// the restart banner and its mark.
//
// The restart banner: a change the daemon picks up only on restart (a messaging credential, an
// allowlist) leaves a mark `{instanceId}` — the daemon instance that was live when the change was
// saved — in this window's session storage, per profile. The banner shows while the window's
// connection still reports that instance; a reconnect to the same instance keeps it, a new
// instance id (the daemon restarted) clears it. "Restart now" asks the daemon to restart.

import { type DependencyList, type ReactNode, useCallback, useEffect, useState } from "react";
import { useT } from "../../i18n/index.tsx";
import { bridgeRest, type RestAnswer, type RestLike } from "../../settings/settingsApi.ts";
import { Loader } from "../Loader.tsx";
import "./manage.css";

export type Loaded<T> =
  | { readonly state: "loading" }
  | { readonly state: "ok"; readonly data: T }
  | { readonly state: "error"; readonly code: string; readonly message: string };

/** Load a REST value; `reload()` loads it again. A late answer after unmount is dropped. */
export function useRestValue<T>(
  load: () => Promise<RestAnswer<T>>,
  deps: DependencyList,
): Loaded<T> & { reload: () => void } {
  const [value, setValue] = useState<Loaded<T>>({ state: "loading" });
  const [serial, setSerial] = useState(0);
  // biome-ignore lint/correctness/useExhaustiveDependencies: the caller names what the load reads
  useEffect(() => {
    let live = true;
    setValue((current) => (current.state === "ok" ? current : { state: "loading" }));
    void load().then((answer) => {
      if (!live) return;
      setValue(
        answer.ok
          ? { state: "ok", data: answer.data }
          : { state: "error", code: answer.code, message: answer.message },
      );
    });
    return () => {
      live = false;
    };
  }, [...deps, serial]);
  const reload = useCallback(() => setSerial((n) => n + 1), []);
  return { ...value, reload };
}

/** The REST proxy of this window (tests pass a fake). */
export function useApi(api?: RestLike | null): RestLike | null {
  return api === undefined ? bridgeRest() : api;
}

export function PageFrame(props: {
  title: string;
  id: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="manage-page" aria-labelledby={`${props.id}-title`}>
      <header className="manage-header">
        <h1 id={`${props.id}-title`}>{props.title}</h1>
        {props.actions !== undefined && <div className="manage-actions">{props.actions}</div>}
      </header>
      <div className="manage-body">{props.children}</div>
    </section>
  );
}

export function LoadState<T>(props: {
  value: Loaded<T> & { reload: () => void };
  children: (data: T) => ReactNode;
}) {
  const t = useT();
  const { value } = props;
  if (value.state === "loading") return <Loader />;
  if (value.state === "error") {
    return (
      <div className="manage-error" role="alert">
        <p>
          {value.code === "not-connected"
            ? t("manage.notConnected")
            : value.message || t("manage.failed")}
        </p>
        <button type="button" className="button button-secondary" onClick={value.reload}>
          {t("manage.retry")}
        </button>
      </div>
    );
  }
  return <>{props.children(value.data)}</>;
}

// -- the restart mark ----------------------------------------------------------------------------

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export function restartKey(profile: string | null): string {
  return `indus.manage.restart.${profile ?? "default"}`;
}

export function sessionStore(): StorageLike | null {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

/** Remember that `profile`'s daemon needs a restart; `instanceId` is the instance live now. */
export function markRestart(
  storage: StorageLike | null,
  profile: string | null,
  instanceId: string | null,
): void {
  try {
    storage?.setItem(restartKey(profile), JSON.stringify({ instanceId: instanceId ?? "" }));
  } catch {
    // storage full or blocked: the banner just will not survive a reload
  }
}

/**
 * Is a restart still owed? True while the connection reports the instance that was live at the
 * change (or no instance yet); a different instance id clears the mark.
 */
export function restartPending(
  storage: StorageLike | null,
  profile: string | null,
  instanceId: string | null,
): boolean {
  let raw: string | null = null;
  try {
    raw = storage?.getItem(restartKey(profile)) ?? null;
  } catch {
    raw = null;
  }
  if (raw === null) return false;
  let marked = "";
  try {
    marked = String((JSON.parse(raw) as { instanceId?: unknown }).instanceId ?? "");
  } catch {
    marked = "";
  }
  if (instanceId === null || instanceId === "") return true; // no instance known yet: keep it
  if (marked === "") {
    markRestart(storage, profile, instanceId); // saved before any instance was seen: adopt this one
    return true;
  }
  if (instanceId === marked) return true;
  try {
    storage?.removeItem(restartKey(profile));
  } catch {
    // ignore
  }
  return false;
}

export function RestartBanner(props: {
  visible: boolean;
  onRestart: () => void;
  restarting: boolean;
}) {
  const t = useT();
  if (!props.visible) return null;
  return (
    <div className="manage-banner" role="status">
      <span>{t("manage.restart.body")}</span>
      <button
        type="button"
        className="button button-primary"
        onClick={props.onRestart}
        disabled={props.restarting}
      >
        {props.restarting ? t("manage.restart.working") : t("manage.restart.now")}
      </button>
    </div>
  );
}

/** "2 min ago" style time from unix seconds or an ISO string. */
export function when(value: number | string | null | undefined, now = Date.now()): string {
  if (value === null || value === undefined || value === "") return "—";
  const ms = typeof value === "number" ? (value > 1e12 ? value : value * 1000) : Date.parse(value);
  if (!Number.isFinite(ms)) return "—";
  const seconds = Math.round((now - ms) / 1000);
  if (seconds < 0) return new Date(ms).toLocaleString();
  if (seconds < 60) return `${seconds} s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86_400) return `${Math.round(seconds / 3600)} h ago`;
  return new Date(ms).toLocaleDateString();
}
