// Settings › Notifications (W52-j): Desktop alerts (a master switch over the kinds of OS
// notification the app raises — approvals, questions, replies, failed turns and plugin messages
// since W59-K1-1 — and a test notification) and Sounds (our one completion chime, with a preview).

import { Bell, Play } from "lucide-react";
import { useState } from "react";
import { playChime } from "../../ambient/sound.ts";
import { useT } from "../../i18n/index.tsx";
import { attempt, usePrefs, useSettings } from "../context.tsx";
import { CompactSelect, OutlineButton, PageBody, SettingRow, Switch } from "../primitives.tsx";
import { Note, notAvailable } from "./kit.tsx";

/** One switch per kind of OS notification the app raises (W59-K1-1), in the order they matter. */
const ALERT_ROWS = [
  {
    id: "approvals",
    row: "notify-approvals",
    label: "settings.notify.approvalNeeded",
    body: "settings.notify.approvalNeededBody",
  },
  {
    id: "input",
    row: "notify-input",
    label: "settings.notify.inputNeeded",
    body: "settings.notify.inputNeededBody",
  },
  {
    id: "turnComplete",
    row: "notify-turn",
    label: "settings.notify.replyReady",
    body: "settings.notify.replyReadyBody",
  },
  {
    id: "turnFailed",
    row: "notify-failed",
    label: "settings.notify.turnFailed",
    body: "settings.notify.turnFailedBody",
  },
  {
    id: "plugins",
    row: "notify-plugins",
    label: "settings.notify.plugins",
    body: "settings.notify.pluginsBody",
  },
] as const;

type AlertKind = (typeof ALERT_ROWS)[number]["id"];

export function AlertsPage() {
  const t = useT();
  const { bridge, focusRow } = useSettings();
  const { prefs, error, update } = usePrefs(bridge);
  const [tested, setTested] = useState<{ text: string; error: boolean } | null>(null);
  const kinds = prefs?.notifications;
  // the master switch is a view over every kind (W52-B-10): on while any one is on
  const allOn = kinds === undefined ? true : ALERT_ROWS.some((row) => kinds[row.id]);
  const everyKind = (on: boolean) =>
    Object.fromEntries(ALERT_ROWS.map((row) => [row.id, on])) as Record<AlertKind, boolean>;
  const rows = ALERT_ROWS;
  const notifications = bridge?.notifications;
  return (
    <PageBody intro={t("settings.notify.intro")}>
      <SettingRow
        rowId="notify-all"
        title={t("settings.notify.all")}
        description={t("settings.notify.allBody")}
        control={
          <Switch
            label={t("settings.notify.all")}
            checked={allOn}
            disabled={prefs === null}
            onChange={(on) => void update({ notifications: everyKind(on) })}
          />
        }
      />
      {rows.map((row) => (
        <SettingRow
          key={row.id}
          rowId={row.row}
          highlighted={focusRow === row.row}
          title={t(row.label)}
          description={t(row.body)}
          control={
            <Switch
              label={t(row.label)}
              checked={kinds?.[row.id] ?? true}
              disabled={prefs === null || !allOn}
              onChange={(checked) => void update({ notifications: { [row.id]: checked } })}
            />
          }
        />
      ))}
      <Note text={notAvailable(t, error)} error />
      <div className="sb-after-rows" data-row="notify-test">
        <OutlineButton
          icon={<Bell aria-hidden="true" size={14} />}
          disabled={notifications === undefined}
          onClick={async () => {
            const answer = await attempt(
              notifications
                ? () =>
                    notifications.show({
                      title: t("app.name"),
                      body: t("settings.notify.testBody"),
                    })
                : null,
            );
            setTested(
              answer.ok
                ? {
                    text: t(
                      answer.value.shown ? "settings.notify.sent" : "settings.notify.notShown",
                    ),
                    error: !answer.value.shown,
                  }
                : { text: answer.message, error: true },
            );
          }}
        >
          {t("settings.notify.test")}
        </OutlineButton>
        <Note text={tested?.text ?? null} error={tested?.error ?? false} />
        <p className="sb-footnote">{t("settings.notify.footnote")}</p>
      </div>
    </PageBody>
  );
}

export function SoundsPage() {
  const t = useT();
  const { bridge, focusRow } = useSettings();
  const { prefs, error, update } = usePrefs(bridge);
  const on = prefs?.notifications.sounds ?? true;
  return (
    <PageBody>
      <SettingRow
        rowId="notify-sounds"
        highlighted={focusRow === "notify-sounds"}
        title={t("settings.sounds.title")}
        description={t("settings.sounds.body")}
        control={
          <>
            <CompactSelect
              label={t("settings.sounds.title")}
              value={on ? "chime" : "off"}
              disabled={prefs === null}
              options={[
                { value: "off", label: t("settings.sounds.off") },
                { value: "chime", label: t("settings.sounds.chime") },
              ]}
              onChange={(value) => void update({ notifications: { sounds: value === "chime" } })}
            />
            <OutlineButton
              icon={<Play aria-hidden="true" size={13} />}
              disabled={!on}
              onClick={() => playChime()}
            >
              {t("settings.sounds.preview")}
            </OutlineButton>
            <Note text={notAvailable(t, error)} error />
          </>
        }
      />
    </PageBody>
  );
}
