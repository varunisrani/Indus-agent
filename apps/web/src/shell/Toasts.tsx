// Short-lived notices. Network failures from the REST client arrive here as one plain sentence;
// the technical details were already written to the console by the client.

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
        className="pointer-events-none fixed right-4 bottom-4 z-50 flex flex-col gap-2"
      >
        {toasts.map((toast) => (
          <div
            key={toast.id}
            role={toast.tone === "error" ? "alert" : "status"}
            className={`pointer-events-auto flex items-start gap-3 rounded-md border bg-raised px-3 py-2 text-sm shadow ${
              toast.tone === "error"
                ? "border-danger text-danger"
                : toast.tone === "success"
                  ? "border-ok text-fg"
                  : "border-line text-fg"
            }`}
          >
            <span>{toast.message}</span>
            <button
              type="button"
              aria-label="Dismiss"
              className="text-muted"
              onClick={() => dismiss(toast.id)}
            >
              ×
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
