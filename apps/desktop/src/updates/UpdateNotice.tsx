// The update and skew notices of an app window (P20-37). An update the person can apply shows
// "Update and restart"; the manual owner shows the command or a download page (opened through the
// audited external opener on this click); a skew (two builds mixed, app behind its runtime, a lazy
// chunk gone) offers reload / update instead of a crash. Settings › About (P20-32) owns the full
// controls; this is the passive surface.

import { useState } from "react";
import { desktopBridge } from "../app/bridge.ts";
import type { IndusUpdateSkew, IndusUpdateStatus } from "../global.d.ts";
import { type MessageKey, useT } from "../i18n/index.tsx";

const SKEW_TEXT: Readonly<Record<IndusUpdateSkew["kind"], MessageKey>> = {
  "mixed-generations": "skew.mixed",
  "app-behind-runtime": "skew.behind",
  "renderer-stale": "skew.stale",
};

export interface UpdateNoticeProps {
  status: IndusUpdateStatus | null;
  skew: IndusUpdateSkew | null;
  onDismissSkew(): void;
}

export function UpdateNotice(props: UpdateNoticeProps) {
  const t = useT();
  const bridge = desktopBridge();
  const [dismissed, setDismissed] = useState<string | null>(null);
  const [applied, setApplied] = useState(false);
  const { status, skew } = props;

  if (skew !== null) {
    return (
      <div className="toast ambient-toast" role="alert">
        <p>{t(SKEW_TEXT[skew.kind])}</p>
        <div className="ambient-actions">
          {skew.kind === "renderer-stale" ? (
            <button
              type="button"
              className="button button-primary"
              onClick={() => window.location.reload()}
            >
              {t("skew.reload")}
            </button>
          ) : (
            <button
              type="button"
              className="button button-primary"
              onClick={() => void bridge?.updates.check().catch(() => {})}
            >
              {t("skew.check")}
            </button>
          )}
          <button type="button" className="button button-secondary" onClick={props.onDismissSkew}>
            {t("update.later")}
          </button>
        </div>
      </div>
    );
  }

  if (status === null) return null;
  if (status.state === "downloading") {
    return (
      <div className="toast ambient-toast" role="status">
        <p>{t("update.downloading", { progress: status.progress ?? 0 })}</p>
        <progress max={100} value={status.progress ?? 0} />
      </div>
    );
  }
  if (status.state === "ready" || status.state === "installing") {
    return (
      <div className="toast ambient-toast" role="status">
        <p>{t("update.restarting")}</p>
      </div>
    );
  }
  if (status.state === "error" && applied) {
    return (
      <div className="toast ambient-toast" role="alert">
        <p>{t("update.failed", { error: status.error ?? "" })}</p>
        <div className="ambient-actions">
          <button
            type="button"
            className="button button-secondary"
            onClick={() => setApplied(false)}
          >
            {t("update.later")}
          </button>
        </div>
      </div>
    );
  }
  const version = status.version ?? "";
  if ((status.state !== "available" && status.state !== "manual") || dismissed === version)
    return null;

  const manual = status.state === "manual";
  return (
    <div className="toast ambient-toast" role="status">
      <p>
        {manual && status.manualHint !== undefined
          ? t("update.manualCommand", { version, command: status.manualHint })
          : t("update.available", { version })}
      </p>
      <div className="ambient-actions">
        {!manual && (
          <button
            type="button"
            className="button button-primary"
            onClick={() => {
              setApplied(true);
              void bridge?.updates.apply().catch(() => {});
            }}
          >
            {t("update.install")}
          </button>
        )}
        {manual && status.downloadUrl !== undefined && (
          <button
            type="button"
            className="button button-primary"
            onClick={() =>
              void bridge?.external.open({ url: status.downloadUrl ?? "" }).catch(() => {})
            }
          >
            {t("update.download")}
          </button>
        )}
        <button
          type="button"
          className="button button-secondary"
          onClick={() => setDismissed(version)}
        >
          {t("update.later")}
        </button>
      </div>
    </div>
  );
}
