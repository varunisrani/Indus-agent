// Settings › Voice › Voice conversation (W52-j): the wake word switch under the page's schema rows
// (P24-20, P24-35). The listener's notice shows as a quiet line under the description.

import { useT } from "../../i18n/index.tsx";
import { appWake } from "../../wake/appWake.ts";
import { useWake, type WakeController } from "../../wake/WakeIndicator.tsx";
import { useSettings } from "../context.tsx";
import { SettingRow, Switch } from "../primitives.tsx";

function WakeRow(props: { controller: WakeController }) {
  const t = useT();
  const { focusRow } = useSettings();
  const view = useWake(props.controller);
  const on = view.listening || view.pending;
  return (
    <SettingRow
      rowId="wake-word"
      highlighted={focusRow === "wake-word"}
      title={t("wake.toggle.label")}
      description={
        <>
          {t("wake.toggle.hint", { phrase: view.phrase })}
          {view.pending && <span className="sb-status">{t("wake.toggle.starting")}</span>}
          {view.notice !== null && (
            <span className="sb-status" data-error="true" role="alert">
              {t(view.notice.key, view.notice.vars)}
            </span>
          )}
        </>
      }
      control={
        <Switch
          label={t("wake.toggle.label")}
          checked={on}
          disabled={view.pending}
          onChange={(next) => {
            if (next) void props.controller.start({ consent: true });
            else void props.controller.stop({ persist: true });
          }}
        />
      }
    />
  );
}

export function WakeWordRows() {
  const t = useT();
  const { focusRow } = useSettings();
  const controller = appWake();
  if (controller === null) {
    return (
      <SettingRow
        rowId="wake-word"
        highlighted={focusRow === "wake-word"}
        title={t("wake.toggle.label")}
        description={t("settings.voice.noWindow")}
      />
    );
  }
  return <WakeRow controller={controller} />;
}
