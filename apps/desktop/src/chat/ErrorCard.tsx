// A failed turn: which layer broke, the failure sentence, and that layer's actions.

import { AlertTriangle } from "lucide-react";
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

export function ErrorCard(props: {
  item: ErrorItem;
  onAction?: ((action: FailureAction, item: ErrorItem) => void) | undefined;
}) {
  const { item } = props;
  const t = useT();
  const text = item.message || t(REASON_KEYS[item.reason] ?? "failure.reason.default");
  return (
    <div className="error-card" role="alert" data-layer={item.layer}>
      <AlertTriangle aria-hidden="true" size={16} className="error-icon" />
      <div className="error-body">
        <p className="error-title">{t(LAYER_KEYS[item.layer])}</p>
        <p className="error-text">{text}</p>
        {item.resetsAt !== null && (
          <p className="error-text">{t("failure.resetsAt", { time: item.resetsAt })}</p>
        )}
        <div className="error-actions">
          {failureActions(item.layer, item.retryable).map((action) => (
            <button
              key={action}
              type="button"
              className="button button-secondary"
              onClick={() => props.onAction?.(action, item)}
            >
              {t(ACTION_KEYS[action])}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
