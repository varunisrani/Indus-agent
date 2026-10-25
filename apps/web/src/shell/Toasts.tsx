// Short-lived notices. Network failures from the REST client arrive here as one plain sentence;
// the technical details were already written to the console by the client.

import { AlertCircle, CheckCircle2, Info, X } from "lucide-react";
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { onUnreachable } from "../lib/api.ts";

export type ToastTone = "info" | "success" | "error";

interface Toast {
  id: number;
  message: string;
  tone: ToastTone;
}

interface ToastApi {
  show(message: string, tone?: ToastTone): void;
}

const ToastContext = createContext<ToastApi>({ show: () => {} });

export function useToast(): ToastApi {
  return useContext(ToastContext);
}

export const TOAST_MS = 5_000;
export const UNREACHABLE_TOAST = "Backend unreachable. The dashboard server is not answering.";

const TOAST_TONE: Record<ToastTone, string> = {
  info: "border-line-strong border-l-ink",
  success: "border-line-strong border-l-success",
  error: "border-danger/60 border-l-danger",
};

function ToastIcon({ tone }: { tone: ToastTone }) {
  const className = "mt-0.5 size-3.5 shrink-0";
  if (tone === "error")
    return <AlertCircle aria-hidden="true" className={`${className} text-danger`} />;
  if (tone === "success")
    return <CheckCircle2 aria-hidden="true" className={`${className} text-success`} />;
  return <Info aria-hidden="true" className={`${className} text-ink-soft`} />;
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => {
    setToasts((all) => all.filter((t) => t.id !== id));
  }, []);

  const show = useCallback(
    (message: string, tone: ToastTone = "info") => {
      setToasts((all) => {
        // One copy of a message at a time: a burst of failed requests is one notice.
        if (all.some((t) => t.message === message)) return all;
        const id = nextId.current++;
        setTimeout(() => dismiss(id), TOAST_MS);
        return [...all, { id, message, tone }];
      });
    },
    [dismiss],
  );

  useEffect(() => onUnreachable(() => show(UNREACHABLE_TOAST, "error")), [show]);

  const value = useMemo(() => ({ show }), [show]);
  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        aria-live="polite"
        className="pointer-events-none fixed right-4 bottom-4 z-50 flex w-[min(24rem,calc(100vw-2rem))] flex-col gap-2"
      >
        {toasts.map((toast) => (
          <div
            key={toast.id}
            role={toast.tone === "error" ? "alert" : "status"}
            className={`pointer-events-auto flex items-start gap-2.5 border border-l-2 bg-canvas px-3 py-2.5 text-[0.8125rem] text-ink shadow-xl [animation:indus-slide-in_180ms_ease-out] ${TOAST_TONE[toast.tone]}`}
          >
            <ToastIcon tone={toast.tone} />
            <span className="min-w-0 flex-1">{toast.message}</span>
            <button
              type="button"
              aria-label="Dismiss"
              className="text-ink-soft hover:text-ink"
              onClick={() => dismiss(toast.id)}
            >
              <X aria-hidden="true" className="size-3.5" />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

/** A plain sentence for any thrown value. */
export function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong.";
}
