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
  const box = useRef<HTMLTextAreaElement>(null);

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

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      bridge?.quick.dismiss();
    } else if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void send();
    }
  };

  return (
    <main className="quick" aria-label={t("quick.label")}>
      <textarea
        ref={box}
        value={text}
        rows={2}
        placeholder={t("quick.placeholder")}
        aria-label={t("quick.label")}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={onKeyDown}
      />
      {problem !== null && (
        <p className="quick-error" role="alert">
          {t(problem)}
        </p>
      )}
      <div className="quick-footer">
        <span className="quick-hint">{sending ? t("quick.sending") : t("quick.hint")}</span>
        <button
          type="button"
          className="button button-primary"
          disabled={sending}
          onClick={() => void send()}
        >
          {t("quick.send")}
        </button>
      </div>
    </main>
  );
}
