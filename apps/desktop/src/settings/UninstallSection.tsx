// Settings → About → Uninstall (P24-34). Main decides what may be offered (`uninstall.summary`):
// when ownership is not confirmed every level is hidden and only the reason shows. Picking a level
// asks once; "Remove everything" also needs the confirmation phrase typed exactly (main checks it
// again). On a yes, main starts the helper and quits the app.

import { Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import type {
  IndusDesktopUninstall,
  IndusUninstallLevel,
  IndusUninstallSummary,
} from "../global.d.ts";
import { type MessageKey, useT } from "../i18n/index.tsx";
import { Button } from "../ui/components.tsx";
import { attempt } from "./context.tsx";

const LEVEL_LABELS: Readonly<Record<IndusUninstallLevel, MessageKey>> = {
  gui: "settings.uninstall.level.gui",
  "keep-data": "settings.uninstall.level.keep-data",
  full: "settings.uninstall.level.full",
};

/** The CLI's own commands for each level (the same ones the helper would run). */
const MANUAL_COMMANDS: Readonly<Record<IndusUninstallLevel, string>> = {
  gui: "indus uninstall --gui",
  "keep-data": "indus uninstall",
  full: "indus uninstall --full",
};

const LEVEL_DESCRIPTIONS: Readonly<Record<IndusUninstallLevel, MessageKey>> = {
  gui: "settings.uninstall.level.gui.desc",
  "keep-data": "settings.uninstall.level.keep-data.desc",
  full: "settings.uninstall.level.full.desc",
};

/** Install kinds whose reason already says how to remove them (main's managed kinds). */
const MANAGED = new Set(["nix", "docker", "desktop"]);

export function UninstallSection(props: {
  uninstall: IndusDesktopUninstall | null | undefined;
  /** A development run (started from source): said plainly under the reason (W54-D2-4). */
  dev?: boolean;
}) {
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
      <div className="settings-uninstall settings-danger-card">
        <p className="settings-danger-title">{t("settings.uninstall.heading")}</p>
        <p className="settings-row-desc" aria-busy={problem === null}>
          {problem ?? t("settings.uninstall.checking")}
        </p>
      </div>
    );
  }
  if (!summary.allowed || summary.levels.length === 0) {
    return (
      <div className="settings-uninstall settings-danger-card">
        <p className="settings-danger-title">{t("settings.uninstall.heading")}</p>
        <p className="settings-row-desc" data-testid="uninstall-hidden">
          {summary.reason}
        </p>
        {props.dev === true && (
          <p className="settings-row-desc" data-testid="uninstall-dev">
            {t("settings.uninstall.dev")}
          </p>
        )}
        {!MANAGED.has(summary.installKind) && (
          <div className="settings-uninstall-manual" data-testid="uninstall-manual">
            <p className="settings-row-desc">{t("settings.uninstall.manual")}</p>
            <ul className="settings-uninstall-commands">
              {(["gui", "keep-data", "full"] as const).map((level) => (
                <li key={level}>
                  <code className="settings-code">{MANUAL_COMMANDS[level]}</code>
                  <span className="settings-row-desc">{t(LEVEL_LABELS[level])}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
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
    <div className="settings-uninstall settings-danger-card">
      <p className="settings-danger-title">{t("settings.uninstall.heading")}</p>
      <p className="settings-row-desc">{t("settings.uninstall.intro")}</p>
      {picked === null ? (
        <ul className="settings-choices">
          {summary.levels.map((level) => (
            <li key={level}>
              <button
                type="button"
                className="settings-uninstall-option"
                onClick={() => {
                  setPicked(level);
                  setTyped("");
                  setProblem(null);
                }}
              >
                <Trash2 aria-hidden="true" size={14} />
                <span className="settings-uninstall-text">
                  <span className="settings-uninstall-title">{t(LEVEL_LABELS[level])}</span>
                  <span className="settings-row-desc">{t(LEVEL_DESCRIPTIONS[level])}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <div
          role="alertdialog"
          aria-label={t(LEVEL_LABELS[picked])}
          className="settings-uninstall-confirm"
        >
          <p className="settings-danger-title">{t(LEVEL_LABELS[picked])}</p>
          <p className="settings-row-desc">
            {t("settings.uninstall.ask", { what: t(LEVEL_LABELS[picked]).toLowerCase() })}
          </p>
          {needsPhrase ? (
            <label className="settings-uninstall-phrase">
              {t("settings.uninstall.typeToConfirm", { phrase: summary.confirmPhrase })}
              <input
                className="settings-input"
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
            <Button
              variant="danger"
              size="sm"
              disabled={!phraseOk || busy}
              onClick={() => void run()}
            >
              {t("settings.uninstall.confirm")}
            </Button>
            <Button variant="text" size="sm" onClick={() => setPicked(null)}>
              {t("settings.uninstall.cancel")}
            </Button>
          </span>
        </div>
      )}
      {problem !== null ? (
        <p role="alert" className="settings-row-error">
          {t("settings.uninstall.failed", { message: problem })}
        </p>
      ) : null}
    </div>
  );
}
