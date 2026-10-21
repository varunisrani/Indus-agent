// The Messaging page (P24-27): one card per platform from `GET /api/messaging/platforms` — its
// state, its credential fields (masked: only a preview is ever shown, a blank field keeps what is
// stored), its settings and allowlist, an enable switch and a Test button. Values go to the daemon
// through `PUT /api/messaging/platforms/{id}` (credentials into the profile's secret store);
// nothing typed into a credential field is kept after the save. A change that needs a restart
// raises the restart banner (see `manage/common.tsx`). Telegram gets the quick setup.

import { useState } from "react";
import { useT } from "../../i18n/index.tsx";
import { type RestLike, rest } from "../../settings/settingsApi.ts";
import { desktopBridge } from "../bridge.ts";
import {
  LoadState,
  markRestart,
  PageFrame,
  RestartBanner,
  restartPending,
  type StorageLike,
  sessionStore,
  useApi,
  useRestValue,
} from "../manage/common.tsx";
import { TelegramQrSetup } from "./TelegramQrSetup.tsx";

export interface PlatformField {
  key: string;
  kind: "credential" | "setting" | "policy" | string;
  required: boolean;
  is_set: boolean;
  is_secret: boolean;
  preview: string | null;
  value: string | null;
}

export interface MessagingPlatform {
  id: string;
  name: string;
  description: string;
  installed: boolean;
  install_hint: string | null;
  enabled: boolean;
  configured: boolean;
  state: string;
  reason: string | null;
  fields: PlatformField[];
}

const TONES: Readonly<Record<string, "ok" | "warn" | "bad">> = {
  connected: "ok",
  pending_restart: "warn",
  retrying: "warn",
  daemon_stopped: "warn",
  not_configured: "warn",
  fatal: "bad",
};

export interface MessagingPageProps {
  api?: RestLike | null;
  profile: string | null;
  /** The daemon instance this window is connected to (the restart banner watches it). */
  instanceId: string | null;
  storage?: StorageLike | null;
  openExternal?: (url: string) => void;
  pollMs?: number;
}

function PlatformCard(props: {
  platform: MessagingPlatform;
  api: RestLike | null;
  profile: string | null;
  onSaved: (restartNeeded: boolean) => void;
}) {
  const t = useT();
  const { platform } = props;
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [enabled, setEnabled] = useState(platform.enabled);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const changed = Object.entries(draft).filter(([key, value]) => {
    const field = platform.fields.find((f) => f.key === key);
    if (field === undefined) return false;
    if (field.kind === "credential") return value.trim() !== "";
    return value !== (field.value ?? "");
  });

  const save = async () => {
    setBusy(true);
    const body: { enabled?: boolean; env?: Record<string, string> } = {};
    if (enabled !== platform.enabled) body.enabled = enabled;
    if (changed.length > 0) body.env = Object.fromEntries(changed.map(([k, v]) => [k, v.trim()]));
    const answer = await rest<{ restart_needed: boolean }>(
      props.api,
      "PUT",
      `/api/messaging/platforms/${encodeURIComponent(platform.id)}`,
      { profile: props.profile, body },
    );
    setBusy(false);
    if (!answer.ok) {
      setNote(answer.message);
      return;
    }
    setDraft({}); // credentials typed here are the daemon's now
    setNote(t("manage.messaging.saved"));
    props.onSaved(answer.data.restart_needed);
  };

  const test = async () => {
    setBusy(true);
    const answer = await rest<{ ok: boolean; message: string }>(
      props.api,
      "POST",
      `/api/messaging/platforms/${encodeURIComponent(platform.id)}/test`,
      { profile: props.profile },
    );
    setBusy(false);
    setNote(answer.ok ? answer.data.message : answer.message);
  };

  return (
    <article className="manage-card" aria-label={platform.name} data-platform={platform.id}>
      <div className="manage-inline">
        <h2>{platform.name}</h2>
        <span className="manage-badge" data-tone={TONES[platform.state]}>
          {t("manage.messaging.state", { state: platform.state.replace(/_/g, " ") })}
        </span>
      </div>
      {platform.description !== "" && <p className="manage-muted">{platform.description}</p>}
      {!platform.installed ? (
        <p className="manage-muted">
          {t("manage.messaging.notInstalled", { hint: platform.install_hint ?? "" })}
        </p>
      ) : (
        <>
          {platform.fields.map((field) => (
            <label className="manage-field" key={field.key}>
              {field.key}
              <input
                type={field.kind === "credential" ? "password" : "text"}
                autoComplete="off"
                aria-label={field.key}
                placeholder={
                  field.kind === "credential"
                    ? field.is_set
                      ? t("manage.messaging.keep", { preview: field.preview ?? "" })
                      : t("manage.messaging.notSet")
                    : ""
                }
                value={draft[field.key] ?? (field.kind === "credential" ? "" : (field.value ?? ""))}
                onChange={(e) => setDraft({ ...draft, [field.key]: e.target.value })}
              />
            </label>
          ))}
          <label className="manage-inline">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
            />
            {t("manage.messaging.enabled")}
          </label>
          <div className="manage-inline">
            <button
              type="button"
              className="button button-primary"
              disabled={busy || (changed.length === 0 && enabled === platform.enabled)}
              onClick={() => void save()}
            >
              {t("manage.save")}
            </button>
            <button
              type="button"
              className="button button-secondary"
              disabled={busy}
              onClick={() => void test()}
            >
              {t("manage.messaging.test")}
            </button>
          </div>
        </>
      )}
      {note !== "" && (
        <p className="manage-muted" role="status">
          {note}
        </p>
      )}
    </article>
  );
}

export function MessagingPage(props: MessagingPageProps) {
  const t = useT();
  const api = useApi(props.api);
  const storage = props.storage === undefined ? sessionStore() : props.storage;
  const [, bump] = useState(0);
  const [restarting, setRestarting] = useState(false);
  const platforms = useRestValue<{ platforms: MessagingPlatform[] }>(
    () => rest(api, "GET", "/api/messaging/platforms", { profile: props.profile }),
    [api, props.profile],
  );
  const pending = restartPending(storage, props.profile, props.instanceId);
  const askRestart = () => {
    markRestart(storage, props.profile, props.instanceId);
    bump((n) => n + 1);
  };
  const restart = async () => {
    setRestarting(true);
    await rest(api, "POST", "/api/daemon/restart", { profile: props.profile });
    setRestarting(false);
  };
  const open =
    props.openExternal ?? ((url: string) => void desktopBridge()?.external.open({ url }));

  return (
    <PageFrame title={t("nav.messaging")} id="messaging">
      <RestartBanner visible={pending} restarting={restarting} onRestart={() => void restart()} />
      <LoadState value={platforms}>
        {(data) => (
          <div className="manage-grid">
            {data.platforms.map((platform) => (
              <div key={platform.id} className="manage-body">
                <PlatformCard
                  platform={platform}
                  api={api}
                  profile={props.profile}
                  onSaved={(restartNeeded) => {
                    if (restartNeeded) askRestart();
                    platforms.reload();
                  }}
                />
                {platform.id === "telegram" && platform.installed && (
                  <TelegramQrSetup
                    api={api}
                    platform={platform}
                    profile={props.profile}
                    openExternal={open}
                    onRestartAsked={askRestart}
                    onDone={platforms.reload}
                    {...(props.pollMs === undefined ? {} : { pollMs: props.pollMs })}
                  />
                )}
              </div>
            ))}
            {data.platforms.length === 0 && (
              <p className="manage-muted">{t("manage.messaging.none")}</p>
            )}
          </div>
        )}
      </LoadState>
    </PageFrame>
  );
}
