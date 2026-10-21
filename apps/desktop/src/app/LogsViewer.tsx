// The logs viewer (P20-39): the desktop log's recent lines (already redacted by main), a refresh,
// "Reveal log file" and "Copy diagnostics". A panel for Settings → About / Advanced (P20-32 mounts
// it); it needs only the bridge's `logs` and `diagnostics` groups.

import { Copy, FolderOpen, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type { IndusDesktopBridge } from "../global.d.ts";
import { useT } from "../i18n/index.tsx";
import { desktopBridge } from "./bridge.ts";

export const LOGS_VIEWER_LINES = 500;

export interface LogsViewerProps {
  bridge?: Pick<IndusDesktopBridge, "logs" | "diagnostics"> | null;
}

type Notice = "copied" | "copyFailed" | "loadFailed" | null;

export function LogsViewer(props: LogsViewerProps) {
  const t = useT();
  const bridge = props.bridge === undefined ? desktopBridge() : props.bridge;
  const [lines, setLines] = useState<string[]>([]);
  const [notice, setNotice] = useState<Notice>(null);

  const load = useCallback(async () => {
    if (bridge === null) return;
    try {
      const answer = await bridge.logs.recent({ limit: LOGS_VIEWER_LINES });
      setLines(answer.lines);
    } catch {
      setNotice("loadFailed");
    }
  }, [bridge]);

  useEffect(() => {
    void load();
  }, [load]);

  if (bridge === null) return <p className="logs-empty">{t("logs.unavailable")}</p>;

  const copy = async () => {
    try {
      const answer = await bridge.diagnostics.copy();
      setNotice(answer.copied ? "copied" : "copyFailed");
    } catch {
      setNotice("copyFailed");
    }
  };

  return (
    <section className="logs-viewer" aria-label={t("logs.title")}>
      <header className="logs-actions">
        <h2>{t("logs.title")}</h2>
        <button type="button" className="button button-secondary" onClick={() => void load()}>
          <RefreshCw aria-hidden="true" size={14} />
          {t("logs.refresh")}
        </button>
        <button
          type="button"
          className="button button-secondary"
          onClick={() => void bridge.logs.reveal().catch(() => undefined)}
        >
          <FolderOpen aria-hidden="true" size={14} />
          {t("logs.reveal")}
        </button>
        <button type="button" className="button button-secondary" onClick={() => void copy()}>
          <Copy aria-hidden="true" size={14} />
          {t("logs.copyDiagnostics")}
        </button>
      </header>
      {notice !== null && (
        <p className="logs-notice" role="status">
          {notice === "copied"
            ? t("logs.copied")
            : notice === "copyFailed"
              ? t("logs.copyFailed")
              : t("logs.loadFailed")}
        </p>
      )}
      {lines.length === 0 ? (
        <p className="logs-empty">{t("logs.empty")}</p>
      ) : (
        <pre className="logs-lines">{lines.join("\n")}</pre>
      )}
    </section>
  );
}
