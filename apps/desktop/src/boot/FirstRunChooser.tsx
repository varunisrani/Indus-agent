// First run: use an Indus that already runs somewhere, or install one on this computer.

import { Download, Server } from "lucide-react";
import { useT } from "../i18n/index.tsx";

export type FirstRunChoice = "connect-remote" | "install-local";

export function FirstRunChooser(props: {
  onChoose: (choice: FirstRunChoice) => void;
  busy?: boolean;
}) {
  const t = useT();
  return (
    <div className="chooser">
      <h1 id="boot-title">{t("boot.chooser.title")}</h1>
      <p>{t("boot.chooser.body")}</p>
      <div className="chooser-options">
        <button
          type="button"
          className="chooser-option"
          disabled={props.busy === true}
          onClick={() => props.onChoose("connect-remote")}
        >
          <Server aria-hidden="true" size={20} />
          <span className="chooser-label">{t("boot.chooser.remote")}</span>
          <span className="chooser-hint">{t("boot.chooser.remoteHint")}</span>
        </button>
        <button
          type="button"
          className="chooser-option"
          disabled={props.busy === true}
          onClick={() => props.onChoose("install-local")}
        >
          <Download aria-hidden="true" size={20} />
          <span className="chooser-label">{t("boot.chooser.local")}</span>
          <span className="chooser-hint">{t("boot.chooser.localHint")}</span>
        </button>
      </div>
    </div>
  );
}
