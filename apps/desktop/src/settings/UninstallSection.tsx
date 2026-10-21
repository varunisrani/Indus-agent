// Settings → About → Uninstall (P24-34). Main decides what may be offered (`uninstall.summary`):
// when ownership is not confirmed every level is hidden and only the reason shows. Picking a level
// asks once; "Remove everything" also needs the confirmation phrase typed exactly (main checks it
// again). On a yes, main starts the helper and quits the app.

import { useEffect, useState } from "react";
import type {
  IndusDesktopUninstall,
  IndusUninstallLevel,
  IndusUninstallSummary,
} from "../global.d.ts";
import { type MessageKey, useT } from "../i18n/index.tsx";
import { attempt } from "./context.tsx";

const LEVEL_LABELS: Readonly<Record<IndusUninstallLevel, MessageKey>> = {
  gui: "settings.uninstall.level.gui",
  "keep-data": "settings.uninstall.level.keep-data",
  full: "settings.uninstall.level.full",
};

const LEVEL_DESCRIPTIONS: Readonly<Record<IndusUninstallLevel, MessageKey>> = {
  gui: "settings.uninstall.level.gui.desc",
  "keep-data": "settings.uninstall.level.keep-data.desc",
  full: "settings.uninstall.level.full.desc",
};

export function UninstallSection(props: { uninstall: IndusDesktopUninstall | null | undefined }) {
  const t = useT();
  const source = props.uninstall ?? null;
  const [summary, setSummary] = useState<IndusUninstallSummary | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [picked, setPicked] = useState<IndusUninstallLevel | null>(null);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    void attempt(source ? () => source.summary() : null).then((answer) => {
      if (!live) return;
      if (answer.ok) setSummary(answer.value);
      else setProblem(answer.message);
    });
    return () => {
      live = false;
    };
  }, [source]);

  if (summary === null) {
    return (
      <p className="settings-row-desc" aria-busy={problem === null}>
        {problem ?? t("settings.uninstall.checking")}
      </p>
    );
  }
  if (!summary.allowed || summary.levels.length === 0) {
    return (
      <p className="settings-row-desc" data-testid="uninstall-hidden">
        {summary.reason}
      </p>
    );
  }
  const needsPhrase = picked === "full";
  const phraseOk = !needsPhrase || typed.trim() === summary.confirmPhrase;

  const run = async () => {
    if (picked === null || !phraseOk || source === null) return;
    setBusy(true);
    const answer = await attempt(() =>
      source.run(needsPhrase ? { level: picked, typed } : { level: picked }),
    );
    setBusy(false);
    if (!answer.ok) setProblem(answer.message);
    else if (!answer.value.ok) setProblem(answer.value.message);
  };

  return (
    <div className="settings-uninstall">
      <p className="settings-row-desc">{t("settings.uninstall.intro")}</p>
      <ul className="settings-choices">
        {summary.levels.map((level) => (
          <li key={level}>
            <button
              type="button"
              className={`button ${level === "full" ? "button-danger" : "button-secondary"}`}
              aria-pressed={picked === level}
              onClick={() => {
                setPicked(level);
                setTyped("");
                setProblem(null);
              }}
            >
              {t(LEVEL_LABELS[level])}
            </button>
            <p className="settings-row-desc">{t(LEVEL_DESCRIPTIONS[level])}</p>
          </li>
        ))}
      </ul>
      {picked !== null ? (
        <div role="alertdialog" aria-label={t(LEVEL_LABELS[picked])}>
          <p>{t("settings.uninstall.ask", { what: t(LEVEL_LABELS[picked]).toLowerCase() })}</p>
          {needsPhrase ? (
            <label>
              {t("settings.uninstall.typeToConfirm", { phrase: summary.confirmPhrase })}
              <input
                aria-label={t("settings.uninstall.typeToConfirm", {
                  phrase: summary.confirmPhrase,
                })}
                value={typed}
                onChange={(event) => setTyped(event.target.value)}
                autoComplete="off"
                spellCheck={false}
              />
            </label>
          ) : null}
          <span className="settings-inline">
            <button
              type="button"
              className="button button-primary"
              disabled={!phraseOk || busy}
              onClick={() => void run()}
            >
              {t("settings.uninstall.confirm")}
            </button>
            <button type="button" className="button" onClick={() => setPicked(null)}>
              {t("settings.uninstall.cancel")}
            </button>
          </span>
        </div>
      ) : null}
      {problem !== null ? (
        <p role="alert" className="settings-note settings-note-error">
          {t("settings.uninstall.failed", { message: problem })}
        </p>
      ) : null}
    </div>
  );
}
