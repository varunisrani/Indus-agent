// The window's one toast region (W52-g): top centre under the title band, above dialogs. Anything
// in the renderer calls `notify()`; plugins reach it through their toast event. Only the newest
// toast shows; older ones wait behind "Show N more". Info and success close by themselves,
// warnings and errors stay until closed.

import { CircleAlert, CircleCheck, Info, TriangleAlert, X } from "lucide-react";
import { type ReactNode, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { PLUGIN_TOAST_EVENT } from "../contrib/host.ts";
import { useT } from "../i18n/index.tsx";
import "./toasts.css";

export type ToastTone = "info" | "success" | "warning" | "error";

export interface ToastInput {
  /** A stable id replaces an earlier toast with the same id (e.g. one connection warning). */
  readonly id?: string;
  readonly tone?: ToastTone;
  readonly title: string;
  readonly message?: string;
  readonly details?: string;
  /** Auto-close after this long; 0 keeps it. Defaults: info/success 5 s, others stay. */
  readonly timeoutMs?: number;
}

export interface Toast {
  readonly id: string;
  readonly tone: ToastTone;
  readonly title: string;
  readonly message: string | null;
  readonly details: string | null;
  readonly timeoutMs: number;
}

const AUTO_CLOSE_MS = 5_000;

let toasts: readonly Toast[] = [];
let counter = 0;
const listeners = new Set<() => void>();

function emit(next: readonly Toast[]): void {
  toasts = next;
  for (const listener of [...listeners]) listener();
}

/** Show a toast; returns its id. */
export function notify(input: ToastInput): string {
  counter += 1;
  const tone = input.tone ?? "info";
  const toast: Toast = {
    id: input.id ?? `toast-${counter}`,
    tone,
    title: input.title,
    message: input.message ?? null,
    details: input.details ?? null,
    timeoutMs: input.timeoutMs ?? (tone === "info" || tone === "success" ? AUTO_CLOSE_MS : 0),
  };
  emit([...toasts.filter((t) => t.id !== toast.id), toast]);
  return toast.id;
}

export function dismissToast(id: string): void {
  if (toasts.some((t) => t.id === id)) emit(toasts.filter((t) => t.id !== id));
}

export function clearToasts(): void {
  if (toasts.length > 0) emit([]);
}

export function useToasts(): readonly Toast[] {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => toasts,
  );
}

const ICONS: Readonly<Record<ToastTone, ReactNode>> = {
  info: <Info aria-hidden="true" size={16} />,
  success: <CircleCheck aria-hidden="true" size={16} />,
  warning: <TriangleAlert aria-hidden="true" size={16} />,
  error: <CircleAlert aria-hidden="true" size={16} />,
};

/** One toast's card: icon | title, message, details | close. */
export function ToastCard(props: { toast: Toast; onClose: () => void }) {
  const t = useT();
  const { toast } = props;
  const close = useRef(props.onClose);
  close.current = props.onClose;
  // biome-ignore lint/correctness/useExhaustiveDependencies: restart the clock for a new toast only
  useEffect(() => {
    if (toast.timeoutMs <= 0) return;
    const timer = setTimeout(() => close.current(), toast.timeoutMs);
    return () => clearTimeout(timer);
  }, [toast.id, toast.timeoutMs]);
  const urgent = toast.tone === "error" || toast.tone === "warning";
  return (
    <div className="toast" data-tone={toast.tone} role={urgent ? "alert" : "status"}>
      <span className="toast-icon">{ICONS[toast.tone]}</span>
      <div className="toast-text">
        <p className="toast-title">{toast.title}</p>
        {toast.message !== null && <p className="toast-message">{toast.message}</p>}
        {toast.details !== null && (
          <details className="toast-details">
            <summary>{t("toast.details")}</summary>
            <pre>{toast.details}</pre>
          </details>
        )}
      </div>
      <button
        type="button"
        className="toast-close"
        aria-label={t("toast.close")}
        onClick={props.onClose}
      >
        <X aria-hidden="true" size={12} />
      </button>
    </div>
  );
}

const PLUGIN_TONES: Readonly<Record<string, ToastTone>> = {
  info: "info",
  success: "success",
  error: "error",
};

/** The region; mounted once by the shell. */
export function ToastRegion() {
  const t = useT();
  const list = useToasts();
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    const onPlugin = (event: Event) => {
      const detail = (event as CustomEvent<{ text?: unknown; tone?: unknown }>).detail ?? {};
      const text = typeof detail.text === "string" ? detail.text : "";
      if (text === "") return;
      notify({ title: text, tone: PLUGIN_TONES[String(detail.tone)] ?? "info" });
    };
    window.addEventListener(PLUGIN_TOAST_EVENT, onPlugin);
    return () => window.removeEventListener(PLUGIN_TOAST_EVENT, onPlugin);
  }, []);

  useEffect(() => {
    if (list.length <= 1) setExpanded(false);
  }, [list.length]);

  if (list.length === 0) return null;
  const shown = expanded ? [...list].reverse() : list.slice(-1);
  const hiddenCount = list.length - shown.length;
  return (
    <div className="toast-region" aria-live="polite">
      {shown.map((toast) => (
        <ToastCard key={toast.id} toast={toast} onClose={() => dismissToast(toast.id)} />
      ))}
      {list.length > 1 && (
        <div className="toast-stack-bar">
          {hiddenCount > 0 && (
            <button type="button" className="toast-link" onClick={() => setExpanded(true)}>
              {t("toast.more", { count: hiddenCount })}
            </button>
          )}
          <button type="button" className="toast-link" onClick={clearToasts}>
            {t("toast.clear")}
          </button>
        </div>
      )}
    </div>
  );
}
