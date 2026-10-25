// The Messaging page (P24-27): the platforms from `GET /api/messaging/platforms` as a rail, the
// chosen platform's detail beside it — its state, pairing requests and approved users, its
// credential fields (masked: only a preview is ever shown, a blank field keeps what is stored),
// its settings and allowlist — and an action bar (enable switch, Test, Save changes). Values go
// to the daemon through `PUT /api/messaging/platforms/{id}` (credentials into the profile's
// secret store); nothing typed into a credential field is kept after the save. A change that
// needs a restart raises the restart alert (see `manage/common.tsx`). Telegram gets the quick
// setup card. W52-k: list-and-detail look (rail variant), labels from `fieldCopy.ts`.

import { ExternalLink, Plus, Save, Search, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useT } from "../../i18n/index.tsx";
import { Switch } from "../../settings/primitives.tsx";
import { type RestLike, rest } from "../../settings/settingsApi.ts";
import { Button, EmptyState, IconButton, SmallDialog } from "../../ui/components.tsx";
import { desktopBridge } from "../bridge.ts";
import { DetailHead, PanelPill, QuietSearch, TextTabs } from "../kit.tsx";
import {
  LoadState,
  markRestart,
  restartPending,
  type StorageLike,
  sessionStore,
  useApi,
  useRestValue,
} from "../manage/common.tsx";
import {
  DetailColumn,
  DetailSection,
  MasterDetail,
  PageHeader,
  usePlaceholderTerm,
} from "../masterDetail.tsx";
import { fieldCopy, listEntries } from "./fieldCopy.ts";
import "./messaging.css";
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
  docs_url?: string | null;
}

type Tone = "ok" | "warn" | "bad" | "off";

const TONES: Readonly<Record<string, "ok" | "warn" | "bad">> = {
  connected: "ok",
  pending_restart: "warn",
  retrying: "warn",
  daemon_stopped: "warn",
  not_configured: "warn",
  fatal: "bad",
};

/** A platform's status tone (disabled platforms are "off" whatever their state). */
export function toneOf(platform: MessagingPlatform): Tone {
  if (!platform.enabled) return "off";
  return TONES[platform.state] ?? "off";
}

interface PairingPending {
  platform: string;
  request_id: string;
  user_id: string;
  user_name: string | null;
}

interface PairingApproved {
  platform: string;
  user_id: string;
  user_name: string | null;
}

export interface MessagingPageProps {
  api?: RestLike | null;
  profile: string | null;
  /** The daemon instance this window is connected to (the restart alert watches it). */
  instanceId: string | null;
  storage?: StorageLike | null;
  openExternal?: (url: string) => void;
  pollMs?: number;
}

/** A platform's 24px tile: its initial on a tint of its own (no brand art). */
export function PlatformTile(props: { name: string; id: string }) {
  let hash = 0;
  for (const char of props.id) hash = (hash * 31 + char.charCodeAt(0)) % 360;
  return (
    <span
      className="msg-tile"
      aria-hidden="true"
      style={{ "--msg-tile-hue": String(hash) } as React.CSSProperties}
    >
      {props.name.charAt(0).toUpperCase()}
    </span>
  );
}

function FieldInput(props: {
  field: PlatformField;
  value: string;
  onChange: (value: string) => void;
  onClear?: () => void;
}) {
  const t = useT();
  const { field } = props;
  const copy = fieldCopy(field.key);
  const secret = field.kind === "credential";
  const placeholder = secret
    ? field.is_set
      ? t("manage.messaging.keep", { preview: field.preview ?? "" })
      : t("manage.messaging.notSet")
    : "";
  const label = copy.label === null ? field.key : t(copy.label);
  const clear =
    props.onClear !== undefined && field.is_set ? (
      <IconButton
        label={t("manage.messaging.clear", { name: label })}
        icon={<Trash2 aria-hidden="true" size={14} />}
        onClick={props.onClear}
      />
    ) : null;
  return (
    <div className="msg-field" data-field={field.key}>
      <div className="msg-field-label">
        <span className={copy.label === null ? "msg-mono" : undefined}>{label}</span>
        {field.is_set && <span className="msg-saved">{t("manage.messaging.savedTag")}</span>}
      </div>
      {copy.help !== null && <p className="msg-field-help">{t(copy.help)}</p>}
      {copy.list && !secret ? (
        <div className="msg-list-field">
          {listEntries(props.value).map((entry, index, all) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: entries are edited in place by position
            <div className="msg-field-row" key={index}>
              <input
                className="msg-input"
                autoComplete="off"
                aria-label={index === 0 ? field.key : `${field.key} ${index + 1}`}
                value={entry}
                onChange={(e) => {
                  const next = [...all];
                  next[index] = e.target.value;
                  props.onChange(next.join(","));
                }}
              />
              {all.length > 1 && (
                <IconButton
                  label={t("manage.messaging.removeEntry", { value: entry })}
                  icon={<X aria-hidden="true" size={14} />}
                  onClick={() => props.onChange(all.filter((_, at) => at !== index).join(","))}
                />
              )}
            </div>
          ))}
          <button
            type="button"
            className="msg-add-another"
            onClick={() => props.onChange(`${props.value},`)}
          >
            <Plus aria-hidden="true" size={12} />
            {t("manage.messaging.addAnother")}
          </button>
        </div>
      ) : (
        <div className="msg-field-row">
          <input
            className="msg-input"
            type={secret ? "password" : "text"}
            autoComplete="off"
            aria-label={field.key}
            placeholder={placeholder}
            value={props.value}
            onChange={(e) => props.onChange(e.target.value)}
          />
          {clear}
        </div>
      )}
    </div>
  );
}

function PlatformDetail(props: {
  platform: MessagingPlatform;
  api: RestLike | null;
  profile: string | null;
  pairing: { pending: PairingPending[]; approved: PairingApproved[] };
  daemonRunning: boolean;
  restartAlert: React.ReactNode;
  telegram: React.ReactNode;
  onPairingChanged: () => void;
  onSaved: (restartNeeded: boolean) => void;
}) {
  const t = useT();
  const { platform } = props;
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [enabled, setEnabled] = useState(platform.enabled);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [revoking, setRevoking] = useState<PairingApproved | null>(null);
  const changed = Object.entries(draft).filter(([key, value]) => {
    const found = platform.fields.find((f) => f.key === key);
    if (found === undefined) return false;
    if (found.kind === "credential") return value.trim() !== "";
    return value !== (found.value ?? "");
  });
  const dirty = changed.length > 0 || enabled !== platform.enabled;
  const tone = toneOf(platform);

  const put = async (body: {
    enabled?: boolean;
    env?: Record<string, string>;
    clear_env?: string[];
  }) => {
    setBusy(true);
    const answer = await rest<{ restart_needed: boolean }>(
      props.api,
      "PUT",
      `/api/messaging/platforms/${encodeURIComponent(platform.id)}`,
      { profile: props.profile, body },
    );
    setBusy(false);
    if (!answer.ok) {
      setNote(answer.message);
      return false;
    }
    setNote(t("manage.messaging.saved"));
    props.onSaved(answer.data.restart_needed);
    return true;
  };

  const save = async () => {
    const body: { enabled?: boolean; env?: Record<string, string> } = {};
    if (enabled !== platform.enabled) body.enabled = enabled;
    if (changed.length > 0) body.env = Object.fromEntries(changed.map(([k, v]) => [k, v.trim()]));
    if (await put(body)) setDraft({}); // credentials typed here are the daemon's now
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

  const pairingCall = async (path: string, body: Record<string, unknown>) => {
    const answer = await rest(props.api, "POST", path, { profile: props.profile, body });
    if (!answer.ok) setNote(answer.message);
    props.onPairingChanged();
  };

  const shownValue = (f: PlatformField) =>
    draft[f.key] ?? (f.kind === "credential" ? "" : (f.value ?? ""));
  const input = (f: PlatformField) => (
    <FieldInput
      key={f.key}
      field={f}
      value={shownValue(f)}
      onChange={(value) => setDraft({ ...draft, [f.key]: value })}
      onClear={() => void put({ clear_env: [f.key] })}
    />
  );
  const required = platform.fields.filter((f) => f.required);
  const recommended = platform.fields.filter((f) => !f.required && f.kind === "policy");
  const advanced = platform.fields.filter((f) => !f.required && f.kind !== "policy");
  const pending = props.pairing.pending.filter((p) => p.platform === platform.id);
  const approved = props.pairing.approved.filter((p) => p.platform === platform.id);

  const actionBar = platform.installed ? (
    <div className="msg-action-bar">
      <span className="msg-enable">
        <Switch
          size="sm"
          label={t("manage.messaging.enabled")}
          checked={enabled}
          onChange={setEnabled}
        />
        {t(enabled ? "manage.messaging.enabledState" : "manage.messaging.disabled")}
      </span>
      <span className="msg-action-end">
        {note !== "" && (
          <span className="msg-note" role="status">
            {note}
          </span>
        )}
        {dirty && <span className="msg-unsaved">{t("manage.messaging.unsaved")}</span>}
        <Button variant="ghost" size="sm" disabled={busy} onClick={() => void test()}>
          {t("manage.messaging.test")}
        </Button>
        <Button
          variant="primary"
          size="sm"
          icon={<Save aria-hidden="true" size={13} />}
          disabled={busy || !dirty}
          onClick={() => void save()}
        >
          {t("manage.messaging.saveChanges")}
        </Button>
      </span>
    </div>
  ) : undefined;

  return (
    <article className="msg-detail" aria-label={platform.name} data-platform={platform.id}>
      <DetailColumn {...(actionBar === undefined ? {} : { footer: actionBar })}>
        {props.restartAlert}
        <DetailHead
          level={2}
          leading={<PlatformTile name={platform.name} id={platform.id} />}
          title={platform.name}
          pills={
            <>
              <PanelPill tone={tone === "ok" ? "good" : tone === "warn" ? "warn" : "muted"} dot>
                {tone === "off"
                  ? t("manage.messaging.disabled")
                  : t("manage.messaging.state", { state: platform.state.replace(/_/g, " ") })}
              </PanelPill>
              {!platform.configured && <PanelPill>{t("manage.messaging.needsSetup")}</PanelPill>}
              {!props.daemonRunning && (
                <PanelPill>{t("manage.messaging.gatewayStopped")}</PanelPill>
              )}
            </>
          }
          description={platform.description}
        />
        {platform.reason !== null && platform.reason !== "" && tone === "bad" && (
          <p className="msg-error" role="alert">
            {platform.reason}
          </p>
        )}
        {!platform.installed ? (
          <p className="msg-muted">
            {t("manage.messaging.notInstalled", { hint: platform.install_hint ?? "" })}
          </p>
        ) : (
          <>
            {pending.length > 0 && (
              <DetailSection title={t("manage.messaging.pending", { count: pending.length })}>
                <ul className="msg-people">
                  {pending.map((p) => (
                    <li key={p.request_id}>
                      <span className="msg-person">
                        <span>{p.user_name ?? p.user_id}</span>
                        <span className="msg-muted">{p.user_id}</span>
                      </span>
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() =>
                          void pairingCall("/api/pairing/approve", {
                            platform: platform.id,
                            request_id: p.request_id,
                          })
                        }
                      >
                        {t("manage.messaging.approve")}
                      </Button>
                    </li>
                  ))}
                </ul>
              </DetailSection>
            )}
            {approved.length > 0 && (
              <DetailSection title={t("manage.messaging.approved", { count: approved.length })}>
                <ul className="msg-people">
                  {approved.map((p) => (
                    <li key={p.user_id}>
                      <span className="msg-person">
                        <span>{p.user_name ?? p.user_id}</span>
                        <span className="msg-muted">{p.user_id}</span>
                      </span>
                      <Button variant="ghost" size="sm" onClick={() => setRevoking(p)}>
                        {t("manage.messaging.revoke")}
                      </Button>
                    </li>
                  ))}
                </ul>
              </DetailSection>
            )}
            {props.telegram !== null && (
              <DetailSection title={t("manage.messaging.quickSetup")}>
                {props.telegram}
              </DetailSection>
            )}
            {required.length > 0 && (
              <DetailSection title={t("manage.messaging.required")}>
                {required.map(input)}
              </DetailSection>
            )}
            {recommended.length > 0 && (
              <DetailSection title={t("manage.messaging.recommended")}>
                {recommended.map(input)}
              </DetailSection>
            )}
            {advanced.length > 0 && (
              <section className="md-section">
                <button
                  type="button"
                  className="md-section-title msg-fold"
                  aria-expanded={advancedOpen}
                  onClick={() => setAdvancedOpen(!advancedOpen)}
                >
                  <span className="msg-caret" data-open={advancedOpen ? "true" : undefined}>
                    ▸
                  </span>
                  {t("manage.messaging.advanced", { count: advanced.length })}
                </button>
                {advancedOpen && advanced.map(input)}
              </section>
            )}
            {platform.docs_url ? (
              <a className="msg-docs" href={platform.docs_url} target="_blank" rel="noreferrer">
                {platform.docs_url}
                <ExternalLink aria-hidden="true" size={12} />
              </a>
            ) : null}
          </>
        )}
      </DetailColumn>
      {revoking !== null && (
        <SmallDialog
          role="alertdialog"
          title={t("manage.messaging.revoke.title")}
          description={t("manage.messaging.revoke.body", {
            name: revoking.user_name ?? revoking.user_id,
            platform: platform.name,
          })}
          onClose={() => setRevoking(null)}
          actions={
            <>
              <Button variant="ghost" size="sm" onClick={() => setRevoking(null)}>
                {t("manage.messaging.cancel")}
              </Button>
              <Button
                variant="danger"
                size="sm"
                onClick={() => {
                  const who = revoking;
                  setRevoking(null);
                  void pairingCall("/api/pairing/revoke", {
                    platform: platform.id,
                    user_id: who.user_id,
                  });
                }}
              >
                {t("manage.messaging.revoke")}
              </Button>
            </>
          }
        />
      )}
    </article>
  );
}

function RestartAlert(props: { visible: boolean; onRestart: () => void; restarting: boolean }) {
  const t = useT();
  if (!props.visible) return null;
  return (
    <div className="msg-restart" role="status">
      <span>{t("manage.restart.body")}</span>
      <Button variant="secondary" size="sm" onClick={props.onRestart} disabled={props.restarting}>
        {props.restarting ? t("manage.restart.working") : t("manage.restart.now")}
      </Button>
    </div>
  );
}

const FILTERS: readonly ("all" | Tone)[] = ["all", "ok", "warn", "bad", "off"];

export function MessagingPage(props: MessagingPageProps) {
  const t = useT();
  const api = useApi(props.api);
  const storage = props.storage === undefined ? sessionStore() : props.storage;
  const [, bump] = useState(0);
  const [restarting, setRestarting] = useState(false);
  const [chosen, setChosen] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<"all" | Tone>("all");
  const [pairing, setPairing] = useState<{
    pending: PairingPending[];
    approved: PairingApproved[];
  }>({ pending: [], approved: [] });
  const [pairingSerial, setPairingSerial] = useState(0);
  const platforms = useRestValue<{ platforms: MessagingPlatform[]; daemon_running?: boolean }>(
    () => rest(api, "GET", "/api/messaging/platforms", { profile: props.profile }),
    [api, props.profile],
  );

  // biome-ignore lint/correctness/useExhaustiveDependencies: pairingSerial asks for a reload
  useEffect(() => {
    let live = true;
    void rest<{ pending?: PairingPending[]; approved?: PairingApproved[] }>(
      api,
      "GET",
      "/api/pairing",
      { profile: props.profile },
    ).then((answer) => {
      if (live && answer.ok) {
        setPairing({ pending: answer.data.pending ?? [], approved: answer.data.approved ?? [] });
      }
    });
    return () => {
      live = false;
    };
  }, [api, props.profile, pairingSerial]);

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

  const all = platforms.state === "ok" ? platforms.data.platforms : [];
  const terms = useMemo(() => all.map((p) => p.name.toLowerCase()).slice(0, 8), [all]);
  const term = usePlaceholderTerm(terms);
  const tones = new Set(all.map(toneOf));

  return (
    <section className="md-page" aria-labelledby="messaging-title">
      <h1 id="messaging-title" className="sr-only">
        {t("nav.messaging")}
      </h1>
      <PageHeader
        search={
          <QuietSearch
            fill
            label={t("manage.messaging.search")}
            placeholder={
              term === null ? t("manage.messaging.search") : t("manage.messaging.try", { term })
            }
            value={query}
            onChange={setQuery}
          />
        }
        trailing={
          tones.size > 1 ? (
            <TextTabs
              look="page"
              label={t("manage.messaging.filter")}
              value={filter}
              onChange={setFilter}
              options={FILTERS.filter((f) => f === "all" || tones.has(f)).map((f) => ({
                value: f,
                label: t(`manage.messaging.filter.${f}`),
              }))}
            />
          ) : undefined
        }
      />
      <LoadState value={platforms}>
        {(data) => {
          const needle = query.trim().toLowerCase();
          const rows = data.platforms.filter(
            (p) =>
              (filter === "all" || toneOf(p) === filter) &&
              (needle === "" || `${p.name} ${p.id}`.toLowerCase().includes(needle)),
          );
          if (data.platforms.length === 0) {
            return (
              <EmptyState
                look="panel"
                fill
                icon={<Search aria-hidden="true" />}
                title={t("manage.messaging.none")}
              />
            );
          }
          if (rows.length === 0)
            return (
              <EmptyState
                look="panel"
                fill
                icon={<Search aria-hidden="true" />}
                title={t("manage.messaging.noMatch")}
              />
            );
          const selected = rows.find((p) => p.id === chosen) ?? rows[0];
          if (selected === undefined) return null;
          const pendingCount = (id: string) =>
            pairing.pending.filter((p) => p.platform === id).length;
          return (
            <MasterDetail
              id="messaging"
              variant="rail"
              listLabel={t("manage.messaging.platforms")}
              list={
                <ul className="msg-rail">
                  {rows.map((p) => (
                    <li key={p.id}>
                      <button
                        type="button"
                        className="msg-row"
                        aria-current={selected.id === p.id ? "true" : undefined}
                        onClick={() => setChosen(p.id)}
                      >
                        <PlatformTile name={p.name} id={p.id} />
                        <span className="msg-row-name">{p.name}</span>
                        {pendingCount(p.id) > 0 && (
                          <span className="msg-pending">{pendingCount(p.id)}</span>
                        )}
                        <span className="msg-dot" data-tone={toneOf(p)} aria-hidden="true" />
                      </button>
                    </li>
                  ))}
                </ul>
              }
              detail={
                <PlatformDetail
                  key={selected.id}
                  platform={selected}
                  api={api}
                  profile={props.profile}
                  pairing={pairing}
                  daemonRunning={data.daemon_running !== false}
                  restartAlert={
                    <RestartAlert
                      visible={pending}
                      restarting={restarting}
                      onRestart={() => void restart()}
                    />
                  }
                  telegram={
                    selected.id === "telegram" && selected.installed ? (
                      <TelegramQrSetup
                        api={api}
                        platform={selected}
                        profile={props.profile}
                        openExternal={open}
                        onRestartAsked={askRestart}
                        onDone={platforms.reload}
                        {...(props.pollMs === undefined ? {} : { pollMs: props.pollMs })}
                      />
                    ) : null
                  }
                  onPairingChanged={() => setPairingSerial((n) => n + 1)}
                  onSaved={(restartNeeded) => {
                    if (restartNeeded) askRestart();
                    platforms.reload();
                  }}
                />
              }
            />
          );
        }}
      </LoadState>
    </section>
  );
}
