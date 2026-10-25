// Settings › About (W52-j): Version & updates (name and version, the update card, the update
// channel, a facts card, the desktop log behind a fold) and Uninstall (its own page).

import { ExternalLink, FileText, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import { LogsViewer } from "../../app/LogsViewer.tsx";
import type { IndusUninstallSummary, IndusUpdateStatus } from "../../global.d.ts";
import { type MessageKey, useT } from "../../i18n/index.tsx";
import { BrandLockup } from "../../ui/components.tsx";
import { attempt, useSettings } from "../context.tsx";
import {
  CompactSelect,
  PageBody,
  PrimaryButton,
  SectionHeading,
  SettingRow,
  TextButton,
} from "../primitives.tsx";
import { UninstallSection } from "../UninstallSection.tsx";
import { Note, notAvailable, relativeTime, useUpdates } from "./kit.tsx";

const TITLES: Readonly<Record<IndusUpdateStatus["state"], MessageKey>> = {
  idle: "settings.about2.upToDate",
  checking: "settings.updates.checking",
  available: "settings.about2.available",
  downloading: "settings.about2.available",
  ready: "settings.updates.ready",
  installing: "settings.updates.installing",
  error: "settings.about2.failed",
  manual: "settings.updates.manual",
};

function UpdateCard(props: { updates: ReturnType<typeof useUpdates> }) {
  const t = useT();
  const { bridge } = useSettings();
  const status = props.updates.status;
  const tone =
    status?.state === "error"
      ? "danger"
      : status?.state === "available" ||
          status?.state === "downloading" ||
          status?.state === "ready"
        ? "accent"
        : "neutral";
  const checked = relativeTime(t, status?.checkedAt);
  const external = bridge?.external;
  return (
    <div className="sb-update" data-tone={tone}>
      <p className="sb-update-title">
        {status === null ? "—" : t(TITLES[status.state], { version: status.version ?? "" })}
      </p>
      {status?.state === "error" && status.error !== undefined && (
        <p className="sb-update-body">{status.error}</p>
      )}
      {status?.progress !== undefined && status.state === "downloading" && (
        <progress className="sb-progress" max={1} value={status.progress} />
      )}
      {status?.manualHint !== undefined && <code className="sb-mono">{status.manualHint}</code>}
      {checked !== null && (
        <p className="sb-update-body">{t("settings.about2.checked", { when: checked })}</p>
      )}
      <Note text={notAvailable(t, props.updates.error)} error />
      <div className="sb-update-foot">
        <TextButton
          icon={<RefreshCw aria-hidden="true" size={13} />}
          onClick={() => void props.updates.check()}
        >
          {t("settings.about2.checkNow")}
        </TextButton>
        {status?.state === "ready" && (
          <PrimaryButton onClick={() => void props.updates.apply()}>
            {t("settings.about2.restart")}
          </PrimaryButton>
        )}
        {status?.downloadUrl !== undefined && external && (
          <TextButton
            icon={<ExternalLink aria-hidden="true" size={13} />}
            onClick={() => void attempt(() => external.open({ url: status.downloadUrl as string }))}
          >
            {t("settings.about2.download")}
          </TextButton>
        )}
      </div>
    </div>
  );
}

export function AboutPage() {
  const t = useT();
  const { bridge, focusRow } = useSettings();
  const updates = useUpdates(bridge);
  const [backend, setBackend] = useState<string | null>(null);
  const [summary, setSummary] = useState<IndusUninstallSummary | null>(null);
  const [logsOpen, setLogsOpen] = useState(focusRow === "logs");
  const connection = bridge?.connection;
  const uninstall = bridge?.uninstall;
  useEffect(() => {
    void attempt(connection ? () => connection.get() : null).then((answer) => {
      if (answer.ok) setBackend(answer.value.codeVersion);
    });
  }, [connection]);
  useEffect(() => {
    void attempt(uninstall ? () => uninstall.summary() : null).then((answer) => {
      if (answer.ok) setSummary(answer.value);
    });
  }, [uninstall]);
  const status = updates.status;
  const version = bridge?.facts?.appVersion ?? "—";
  const channel =
    status === null
      ? "—"
      : t(status.channel === "canary" ? "settings.updates.canary" : "settings.updates.stable");
  return (
    <PageBody>
      <div
        className="sb-about"
        data-row="version"
        data-highlighted={focusRow === "version" ? "true" : undefined}
      >
        <div className="sb-hero">
          <BrandLockup className="sb-hero-mark" />
          <span className="sb-hero-name">{t("settings.about2.name")}</span>
          <span className="sb-hero-version">{t("settings.about2.version", { version })}</span>
        </div>
        <SectionHeading
          icon={<RefreshCw aria-hidden="true" />}
          title={t("settings.updates.title")}
        />
        <UpdateCard updates={updates} />
        <SettingRow
          rowId="update-channel"
          highlighted={focusRow === "update-channel"}
          title={t("settings.updates.channel")}
          control={
            <CompactSelect
              label={t("settings.updates.channel")}
              value={status?.channel ?? "stable"}
              disabled={status === null}
              options={[
                { value: "stable", label: t("settings.updates.stable") },
                { value: "canary", label: t("settings.updates.canary") },
              ]}
              onChange={(value) =>
                void updates.setChannel(value === "canary" ? "canary" : "stable")
              }
            />
          }
        />
        <dl className="sb-facts">
          <dt>{t("settings.about2.appVersion")}</dt>
          <dd>{version}</dd>
          <dt>{t("settings.about.backend")}</dt>
          <dd>{backend ?? "—"}</dd>
          <dt>{t("settings.updates.channel")}</dt>
          <dd>{channel}</dd>
          <dt>{t("settings.about2.installType")}</dt>
          <dd>{summary?.installKind ?? "—"}</dd>
          <dt>{t("settings.about2.home")}</dt>
          <dd className="sb-mono">{summary?.home ?? "—"}</dd>
        </dl>
        <div data-row="logs" data-highlighted={focusRow === "logs" ? "true" : undefined}>
          <SectionHeading icon={<FileText aria-hidden="true" />} title={t("settings.logs.title")} />
          <TextButton aria-expanded={logsOpen} onClick={() => setLogsOpen(!logsOpen)}>
            {t(logsOpen ? "settings.about2.hideLines" : "settings.about2.showLines")}
          </TextButton>
          {logsOpen && <LogsViewer />}
        </div>
      </div>
    </PageBody>
  );
}

export function UninstallPage() {
  const { bridge, focusRow } = useSettings();
  return (
    <PageBody>
      <div
        className="sb-about"
        data-row="uninstall"
        data-highlighted={focusRow === "uninstall" ? "true" : undefined}
      >
        <UninstallSection uninstall={bridge?.uninstall} dev={bridge?.facts?.packaged === false} />
      </div>
    </PageBody>
  );
}
