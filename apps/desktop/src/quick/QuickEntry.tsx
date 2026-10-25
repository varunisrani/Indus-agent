// The quick-entry window (P20-35): a one-box composer over every app. It has no connection: the
// text goes to main, which hands it to the app window used last and answers with the outcome. The
// text stays until delivery is confirmed; "not confirmed yet" never invites a blind resend.

import "../ambient/ambient.css";
import { type KeyboardEvent, useEffect, useRef, useState } from "react";
import { desktopBridge } from "../app/bridge.ts";
import { type MessageKey, useT } from "../i18n/index.tsx";

const ERROR_TEXT: Readonly<Record<string, MessageKey>> = {
  empty: "quick.error.empty",
  "no-target": "quick.error.noTarget",
  unconfirmed: "quick.error.unconfirmed",
  failed: "quick.error.failed",
  busy: "quick.error.busy",
};

export function QuickEntry() {
  const t = useT();
  const bridge = desktopBridge();
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState<MessageKey | null>(null);
  const box = useRef<HTMLInputElement>(null);

  useEffect(() => {
    document.documentElement.dataset.window = "quick";
    box.current?.focus();
    if (bridge === null) return;
    return bridge.quick.onShown(() => {
      setProblem(null);
      box.current?.focus();
      box.current?.select();
    });
  }, [bridge]);

  const send = async () => {
    if (bridge === null || sending) return;
    if (text.trim() === "") {
      setProblem("quick.error.empty");
      return;
    }
    setSending(true);
    setProblem(null);
    try {
      const result = await bridge.quick.submit({ text });
      if (result.ok) setText("");
      else setProblem(ERROR_TEXT[result.code] ?? "quick.error.failed");
    } catch {
      setProblem("quick.error.failed");
    } finally {
      setSending(false);
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      bridge?.quick.dismiss();
    } else if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void send();
    }
  };

  return (
    <main
      className="quick"
      aria-label={t("quick.label")}
      data-sending={sending ? "true" : undefined}
    >
      <div className="quick-card">
        <div className="quick-row">
          <span className="quick-prompt" aria-hidden="true">
            ›
          </span>
          <input
            ref={box}
            type="text"
            className="quick-input"
            value={text}
            placeholder={t("quick.placeholder")}
            aria-label={t("quick.label")}
            disabled={sending}
            onChange={(event) => setText(event.target.value)}
            onKeyDown={onKeyDown}
          />
        </div>
        <div className="quick-target">
          <label className="quick-target-label" htmlFor="quick-target">
            {t("quick.sendTo")}
          </label>
          {/* W52-m-2: main delivers to the window used last; other targets need a target field */}
          <select id="quick-target" className="quick-select" value="current" onChange={() => {}}>
            <option value="current">{t("quick.target.current")}</option>
          </select>
          <span className="sr-only" aria-live="polite">
            {sending ? t("quick.sending") : ""}
          </span>
        </div>
        {problem !== null && (
          <p className="quick-error" role="alert">
            {t(problem)}
          </p>
        )}
      </div>
    </main>
  );
}
