// A failed turn (W52-g look): one tinted card with a headline and one sentence for the layer that
// broke, a closed "Details" holding the raw failure (the backend's sentence and its code), round
// action pills for that layer, and a dismiss X that hides the card for this view only.

import { ClipboardCopy, Cpu, FileText, KeyRound, Plus, RefreshCw, Settings, X } from "lucide-react";
import { type ReactNode, useState } from "react";
import { copyText } from "../app/clipboard.ts";
import { type MessageKey, useT } from "../i18n/index.tsx";
import { type FailureAction, type FailureLayer, failureActions } from "./failure.ts";
import type { ErrorItem } from "./messageStream.ts";

const REASON_KEYS: Readonly<Record<string, MessageKey>> = {
  max_iterations: "failure.reason.max_iterations",
  budget_exhausted: "failure.reason.budget_exhausted",
  invalid_tool_strikes: "failure.reason.invalid_tool_strikes",
  compression_exhausted: "failure.reason.compression_exhausted",
  guardrail_halt: "failure.reason.guardrail_halt",
  persistence_failed: "failure.reason.persistence_failed",
  provider_failed: "failure.reason.provider_failed",
};

const HEADLINE_KEYS: Readonly<Record<FailureLayer, MessageKey>> = {
  provider: "failure.headline.provider",
  endpoint: "failure.headline.endpoint",
  streaming: "failure.headline.streaming",
  auth: "failure.headline.auth",
  daemon: "failure.headline.daemon",
  runtime: "failure.headline.runtime",
  disk: "failure.headline.disk",
};

const BODY_KEYS: Readonly<Record<FailureLayer, MessageKey>> = {
  provider: "failure.body.provider",
  endpoint: "failure.body.endpoint",
  streaming: "failure.body.streaming",
  auth: "failure.body.auth",
  daemon: "failure.body.daemon",
  runtime: "failure.body.runtime",
  disk: "failure.body.disk",
};

const LAYER_KEYS: Readonly<Record<FailureLayer, MessageKey>> = {
  provider: "failure.layer.provider",
  endpoint: "failure.layer.endpoint",
  streaming: "failure.layer.streaming",
  auth: "failure.layer.auth",
  daemon: "failure.layer.daemon",
  runtime: "failure.layer.runtime",
  disk: "failure.layer.disk",
};

const ACTION_KEYS: Readonly<Record<FailureAction, MessageKey>> = {
  retry: "failure.action.retry",
  "choose-model": "failure.action.choose-model",
  "open-settings": "failure.action.open-settings",
  "sign-in": "failure.action.sign-in",
  reconnect: "failure.action.reconnect",
  "open-logs": "failure.action.open-logs",
  "new-session": "failure.action.new-session",
};

const ACTION_ICONS: Readonly<Record<FailureAction, ReactNode>> = {
  retry: <RefreshCw aria-hidden="true" size={12} />,
  "choose-model": <Cpu aria-hidden="true" size={12} />,
  "open-settings": <Settings aria-hidden="true" size={12} />,
  "sign-in": <KeyRound aria-hidden="true" size={12} />,
  reconnect: <RefreshCw aria-hidden="true" size={12} />,
  "open-logs": <FileText aria-hidden="true" size={12} />,
  "new-session": <Plus aria-hidden="true" size={12} />,
};

/** The text "Copy error details" puts on the clipboard. */
export function errorDetails(item: ErrorItem, said: readonly string[]): string {
  return [`layer: ${item.layer}`, `reason: ${item.reason || "-"}`, ...said].join("\n");
}

export function ErrorCard(props: {
  item: ErrorItem;
  /** The failure sentence the turn's reply carried (hidden above the card, shown in details). */
  echo?: string | null;
  onAction?: ((action: FailureAction, item: ErrorItem) => void) | undefined;
  /** The host can send the failed turn again (W57-H1-4); without it there is no Retry. */
  canRetry?: boolean | undefined;
}) {
  const { item } = props;
  const t = useT();
  const [dismissed, setDismissed] = useState(false);
  if (dismissed) return null;
  const said = [props.echo ?? "", item.message].filter((line) => line.trim() !== "");
  const fallback = t(REASON_KEYS[item.reason] ?? "failure.reason.default");
  const details = said.length > 0 ? said : [fallback];
  const actions = failureActions(
    item.layer,
    props.canRetry === true && props.onAction !== undefined,
  );
  return (
    <div className="error-card" role="alert" data-layer={item.layer}>
      <div className="error-head">
        <p className="error-title">{t(HEADLINE_KEYS[item.layer])}</p>
        <button
          type="button"
          className="error-dismiss"
          aria-label={t("failure.dismiss")}
          onClick={() => setDismissed(true)}
        >
          <X aria-hidden="true" size={14} />
        </button>
      </div>
      <p className="error-text">{t(BODY_KEYS[item.layer])}</p>
      {item.resetsAt !== null && (
        <p className="error-text">{t("failure.resetsAt", { time: item.resetsAt })}</p>
      )}
      <details className="error-details">
        <summary>{t("failure.details")}</summary>
        <pre>
          {t(LAYER_KEYS[item.layer])}
          {"\n"}
          {details.join("\n")}
          {item.reason !== "" ? `\n(${item.reason})` : ""}
        </pre>
      </details>
      <div className="error-actions">
        {actions.map((action) => (
          <button
            key={action}
            type="button"
            className="error-pill"
            onClick={() => props.onAction?.(action, item)}
          >
            {ACTION_ICONS[action]}
            {t(ACTION_KEYS[action])}
          </button>
        ))}
        <button
          type="button"
          className="error-pill"
          onClick={() => void copyText(errorDetails(item, details))}
        >
          <ClipboardCopy aria-hidden="true" size={12} />
          {t("failure.copyDetails")}
        </button>
      </div>
    </div>
  );
}
