// The root error boundary: a render error shows one recovery surface instead of a blank window,
// and is written to the desktop log through main (P20-39's channel; a no-op until it lands).

import { AlertTriangle } from "lucide-react";
import { Component, type ErrorInfo, type ReactNode } from "react";
import { translate } from "../i18n/index.tsx";
import { desktopBridge } from "./bridge.ts";

interface State {
  failed: boolean;
}

export class RootErrorBoundary extends Component<{ children: ReactNode }, State> {
  override state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    try {
      desktopBridge()?.logs.rendererError({
        message: error.message,
        ...(error.stack === undefined
          ? {}
          : { stack: `${error.stack}\n${info.componentStack ?? ""}` }),
        source: `renderer:${new URLSearchParams(window.location.search).get("win") ?? "main"}`,
      });
    } catch {
      // the log is best effort
    }
  }

  override render(): ReactNode {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="boot-overlay" role="alert">
        <div className="boot-card boot-failure">
          <AlertTriangle aria-hidden="true" size={20} />
          <h1>{translate("error.title")}</h1>
          <p>{translate("error.body")}</p>
          <button
            type="button"
            className="button button-primary"
            onClick={() => window.location.reload()}
          >
            {translate("error.reload")}
          </button>
        </div>
      </div>
    );
  }
}
