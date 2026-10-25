// Webhooks (P24-27; W52-l look): the routes the daemon's webhook receiver serves (P15-32) as an
// overlay panel — list, create, enable/disable, delete. Routes defined in config.yaml are shown
// read-only. While the receiver is off a banner offers to turn it on (it takes effect after a
// gateway restart, which the next banner offers). The signing secret exists in exactly one answer
// (the create) and lives only in this component's state until the dialog closes; it is never
// written to storage or a log.

import { AlertTriangle, Copy, Globe, Plus } from "lucide-react";
import { type ReactNode, useState } from "react";
import { useT } from "../../i18n/index.tsx";
import { Switch } from "../../settings/primitives.tsx";
import { type RestLike, rest } from "../../settings/settingsApi.ts";
import { Button, IconButton, SmallDialog } from "../../ui/components.tsx";
import { copyText } from "../clipboard.ts";
import {
  Banner,
  CapsLabel,
  DetailHead,
  Field,
  InlineError,
  MetaGrid,
  PanelBlock,
  PanelPill,
  PanelRow,
} from "../kit.tsx";
import { Loader } from "../Loader.tsx";
import { useApi, useRestValue, when } from "../manage/common.tsx";
import { ListDetail, OverlayCard } from "../overlay/OverlayFrame.tsx";
import "./webhooks.css";

export const WEBHOOK_AUTHS = ["auto", "github", "gitlab", "hmac", "hmac_ts"] as const;

export interface WebhookRoute {
  name: string;
  source: "subscribed" | "config" | string;
  url: string;
  auth: string;
  events: string[];
  prompt: string;
  deliver: string | null;
  deliver_only?: boolean;
  description: string;
  created_at: string | null;
  enabled: boolean;
  secret_set: boolean;
}

interface WebhooksAnswer {
  receiver_enabled: boolean;
  base_url: string;
  routes: WebhookRoute[];
}

export interface DeliveryTarget {
  id: string;
  label: string;
  kind: string;
  available: boolean;
}

function CopyRow(props: { label: string; value: string; copyLabel: string; testId?: string }) {
  return (
    <div className="wh-copy-row">
      <span className="wh-copy-label">{props.label}</span>
      <span className="wh-copy-box">
        <code data-testid={props.testId}>{props.value}</code>
        <IconButton
          label={props.copyLabel}
          icon={<Copy aria-hidden="true" size={13} />}
          onClick={() => void copyText(props.value)}
        />
      </span>
    </div>
  );
}

function NewSubscription(props: {
  api: RestLike | null;
  profile: string | null;
  targets: readonly DeliveryTarget[];
  onCreated: () => void;
  onClose: () => void;
}) {
  const t = useT();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [prompt, setPrompt] = useState("");
  const [events, setEvents] = useState("");
  const [auth, setAuth] = useState<(typeof WEBHOOK_AUTHS)[number]>("auto");
  const [deliver, setDeliver] = useState("");
  const [deliverOnly, setDeliverOnly] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [created, setCreated] = useState<{ name: string; url: string; secret: string } | null>(
    null,
  );
  const create = async () => {
    setBusy(true);
    setError("");
    const body = {
      name: name.trim(),
      events: events
        .split(",")
        .map((e) => e.trim())
        .filter((e) => e !== ""),
      prompt,
      auth,
      ...(description.trim() === "" ? {} : { description: description.trim() }),
      ...(deliver === "" ? {} : { deliver }),
      ...(deliverOnly && deliver !== "" ? { deliver_only: true } : {}),
    };
    const answer = await rest<WebhookRoute & { secret: string }>(
      props.api,
      "POST",
      "/api/webhooks",
      { profile: props.profile, body },
    );
    setBusy(false);
    if (!answer.ok) {
      setError(answer.message);
      return;
    }
    setCreated({ name: answer.data.name, url: answer.data.url, secret: answer.data.secret });
    props.onCreated();
  };
  if (created !== null) {
    return (
      <SmallDialog
        size="wide"
        title={t("wh.createdTitle", { name: created.name })}
        description={t("manage.webhooks.secretOnce", { name: created.name })}
        closeLabel={t("common.close")}
        onClose={props.onClose}
        actions={
          <Button variant="primary" onClick={props.onClose}>
            {t("wh.done")}
          </Button>
        }
      >
        <CopyRow label={t("wh.url")} value={created.url} copyLabel={t("wh.copyUrl")} />
        <CopyRow
          label={t("wh.secret")}
          value={created.secret}
          copyLabel={t("wh.copySecret")}
          testId="webhook-secret"
        />
      </SmallDialog>
    );
  }
  return (
    <SmallDialog
      size="wide"
      title={t("wh.newTitle")}
      description={t("wh.newBody")}
      closeLabel={t("common.close")}
      onClose={props.onClose}
      actions={
        <>
          <Button variant="outline" onClick={props.onClose}>
            {t("manage.cancel")}
          </Button>
          <Button
            variant="primary"
            disabled={busy || name.trim() === ""}
            onClick={() => void create()}
          >
            {busy ? t("wh.creating") : t("wh.create")}
          </Button>
        </>
      }
    >
      <div className="kit-two-cols">
        <Field label={t("manage.webhooks.name")} htmlFor="wh-name">
          <input
            id="wh-name"
            value={name}
            placeholder={t("wh.namePlaceholder")}
            onChange={(e) => setName(e.target.value)}
          />
        </Field>
        <Field label={t("wh.description")} hint={t("cron.optional")} htmlFor="wh-desc">
          <input
            id="wh-desc"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </Field>
      </div>
      <Field label={t("manage.webhooks.prompt")} htmlFor="wh-prompt">
        <textarea
          id="wh-prompt"
          className="kit-mono"
          value={prompt}
          placeholder={t("wh.promptPlaceholder")}
          onChange={(e) => setPrompt(e.target.value)}
        />
      </Field>
      <div className="kit-two-cols">
        <Field label={t("manage.webhooks.events")} hint={t("wh.eventsHint")} htmlFor="wh-events">
          <input
            id="wh-events"
            value={events}
            placeholder="push, pull_request"
            onChange={(e) => setEvents(e.target.value)}
          />
        </Field>
        <Field label={t("manage.webhooks.auth")} htmlFor="wh-auth">
          <select
            id="wh-auth"
            value={auth}
            onChange={(e) => setAuth(e.target.value as (typeof WEBHOOK_AUTHS)[number])}
          >
            {WEBHOOK_AUTHS.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </Field>
      </div>
      <div className="kit-two-cols">
        <Field label={t("wh.deliver")} htmlFor="wh-deliver">
          <select id="wh-deliver" value={deliver} onChange={(e) => setDeliver(e.target.value)}>
            <option value="">{t("wh.deliverNone")}</option>
            {props.targets.map((target) => (
              <option key={target.id} value={target.id} disabled={!target.available}>
                {target.kind === "local" ? t("cron.thisComputer") : target.label}
                {target.available ? "" : ` ${t("cron.needsHome")}`}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t("wh.deliverOnly")} help={t("wh.deliverOnlyHelp")}>
          <Switch
            checked={deliverOnly}
            label={t("wh.deliverOnly")}
            disabled={deliver === ""}
            onChange={setDeliverOnly}
          />
        </Field>
      </div>
      {error !== "" && <InlineError>{error}</InlineError>}
    </SmallDialog>
  );
}

export function WebhooksOverlay(props: {
  api?: RestLike | null;
  profile: string | null;
  onClose?: () => void;
}) {
  const t = useT();
  const api = useApi(props.api);
  const routes = useRestValue<WebhooksAnswer>(
    () => rest(api, "GET", "/api/webhooks", { profile: props.profile }),
    [api, props.profile],
  );
  const targets = useRestValue<{ targets: DeliveryTarget[] }>(
    () => rest(api, "GET", "/api/cron/delivery-targets", { profile: props.profile }),
    [api, props.profile],
  );
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<WebhookRoute | null>(null);
  const [enabling, setEnabling] = useState(false);
  const [restartNeeded, setRestartNeeded] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [note, setNote] = useState("");

  const enableReceiver = async () => {
    setEnabling(true);
    const answer = await rest<{ restart_needed?: boolean }>(api, "PUT", "/api/webhooks/receiver", {
      profile: props.profile,
      body: { enabled: true },
    });
    setEnabling(false);
    if (!answer.ok) {
      setNote(answer.message);
      return;
    }
    setNote("");
    if (answer.data.restart_needed === true) setRestartNeeded(true);
    routes.reload();
  };
  const restart = async () => {
    setRestarting(true);
    const answer = await rest(api, "POST", "/api/daemon/restart", { profile: props.profile });
    setRestarting(false);
    if (answer.ok) setRestartNeeded(false);
    else setNote(answer.message);
  };
  const toggle = async (route: WebhookRoute) => {
    const answer = await rest(
      api,
      "PUT",
      `/api/webhooks/${encodeURIComponent(route.name)}/enabled`,
      { profile: props.profile, body: { enabled: !route.enabled } },
    );
    if (!answer.ok) setNote(answer.message);
    routes.reload();
  };
  const remove = async () => {
    if (deleting === null) return;
    const answer = await rest(api, "DELETE", `/api/webhooks/${encodeURIComponent(deleting.name)}`, {
      profile: props.profile,
    });
    setDeleting(null);
    if (!answer.ok) setNote(answer.message);
    else if (picked === deleting.name) setPicked(null);
    routes.reload();
  };

  let body: ReactNode;
  if (routes.state === "loading") body = <Loader />;
  else if (routes.state === "error") {
    body = (
      <div className="wh-pad">
        <InlineError>
          {routes.code === "not-connected" ? t("manage.notConnected") : routes.message}
        </InlineError>
        <Button variant="secondary" size="sm" onClick={routes.reload}>
          {t("manage.retry")}
        </Button>
      </div>
    );
  } else {
    const data = routes.data;
    const receiverOff = !data.receiver_enabled;
    const banner = (
      <>
        {receiverOff && (
          <Banner
            icon={<Globe aria-hidden="true" />}
            title={t("wh.offTitle")}
            action={
              <Button
                variant="primary"
                size="sm"
                icon={<Globe aria-hidden="true" size={14} />}
                disabled={enabling}
                onClick={() => void enableReceiver()}
              >
                {enabling ? t("wh.enabling") : t("wh.enable")}
              </Button>
            }
          >
            <p>{t("wh.offBody")}</p>
          </Banner>
        )}
        {restartNeeded && (
          <Banner
            icon={<AlertTriangle aria-hidden="true" />}
            title={t("wh.restartTitle")}
            action={
              <Button
                variant="secondary"
                size="sm"
                disabled={restarting}
                onClick={() => void restart()}
              >
                {restarting ? t("manage.restart.working") : t("cc.system.restart")}
              </Button>
            }
          >
            <p>{t("wh.restartBody")}</p>
          </Banner>
        )}
        {note !== "" && <InlineError>{note}</InlineError>}
      </>
    );
    const needle = query.trim().toLowerCase();
    const shown = data.routes.filter((r) => needle === "" || r.name.toLowerCase().includes(needle));
    const route = data.routes.find((r) => r.name === picked) ?? null;
    const newButton = (
      <Button
        variant="primary"
        size="sm"
        icon={<Plus aria-hidden="true" size={14} />}
        disabled={receiverOff}
        onClick={() => setCreating(true)}
      >
        {t("wh.new")}
      </Button>
    );
    body = (
      <ListDetail
        {...(data.routes.length > 0
          ? {
              title: t("wh.title", { count: data.routes.length }),
              subtitle: t("wh.subtitle"),
            }
          : {})}
        banner={banner}
        whole={data.routes.length === 0}
        search={{
          label: t("wh.search"),
          placeholder: t("wh.searchPlaceholder"),
          value: query,
          onChange: setQuery,
        }}
        add={{ label: t("wh.new"), onAdd: () => setCreating(true), disabled: receiverOff }}
        listLabel={t("nav.webhooks")}
        list={shown.map((r) => (
          <PanelRow
            key={r.name}
            title={r.name}
            dot={r.enabled ? "on" : "off"}
            selected={r.name === picked}
            onSelect={() => setPicked(r.name)}
            data={{ "data-route": r.name }}
            {...(r.source === "config"
              ? {}
              : {
                  menu: {
                    label: t("wh.rowMenu", { name: r.name }),
                    items: [
                      {
                        id: "toggle",
                        label: r.enabled ? t("wh.disable") : t("wh.enableRoute"),
                        run: () => void toggle(r),
                      },
                      {
                        id: "delete",
                        label: t("manage.delete"),
                        tone: "danger" as const,
                        run: () => setDeleting(r),
                      },
                    ],
                  },
                })}
          />
        ))}
        {...(route === null
          ? {}
          : {
              detail: (
                <div className="wh-detail">
                  <DetailHead
                    title={route.name}
                    pills={
                      <>
                        {route.deliver_only === true && (
                          <PanelPill tone="warn">{t("wh.deliverOnlyPill")}</PanelPill>
                        )}
                        {route.source === "config" && (
                          <PanelPill tone="muted">{t("wh.configPill")}</PanelPill>
                        )}
                        {!route.enabled && (
                          <PanelPill tone="muted">{t("wh.disabledPill")}</PanelPill>
                        )}
                      </>
                    }
                  />
                  <MetaGrid
                    rows={[
                      [
                        t("manage.webhooks.events"),
                        route.events.length === 0 ? (
                          <PanelPill key="all">{t("manage.webhooks.allEvents")}</PanelPill>
                        ) : (
                          route.events.map((e) => <PanelPill key={e}>{e}</PanelPill>)
                        ),
                      ],
                      [t("manage.webhooks.auth"), route.auth],
                      [t("wh.deliver"), route.deliver ?? t("wh.deliverNone")],
                      [t("manage.webhooks.created"), when(route.created_at)],
                    ]}
                  />
                  <CopyRow label={t("wh.url")} value={route.url} copyLabel={t("wh.copyUrl")} />
                  {route.description !== "" && (
                    <section className="wh-section">
                      <CapsLabel>{t("wh.description")}</CapsLabel>
                      <p className="wh-text">{route.description}</p>
                    </section>
                  )}
                  {route.prompt !== "" && (
                    <section className="wh-section">
                      <CapsLabel>{t("manage.webhooks.prompt")}</CapsLabel>
                      <PanelBlock>{route.prompt}</PanelBlock>
                    </section>
                  )}
                </div>
              ),
            })}
        empty={
          data.routes.length === 0
            ? { icon: <Globe aria-hidden="true" />, title: t("wh.none"), action: newButton }
            : { icon: <Globe aria-hidden="true" />, title: t("wh.pick") }
        }
      />
    );
  }

  return (
    <OverlayCard
      id="webhooks"
      title={t("nav.webhooks")}
      closeLabel={t("common.close")}
      onClose={props.onClose ?? (() => {})}
    >
      {body}
      {creating && (
        <NewSubscription
          api={api}
          profile={props.profile}
          targets={targets.state === "ok" ? targets.data.targets : []}
          onCreated={routes.reload}
          onClose={() => setCreating(false)}
        />
      )}
      {deleting !== null && (
        <SmallDialog
          role="alertdialog"
          title={t("wh.deleteTitle")}
          description={t("wh.deleteBody", { name: deleting.name })}
          onClose={() => setDeleting(null)}
          actions={
            <>
              <Button variant="outline" onClick={() => setDeleting(null)}>
                {t("manage.cancel")}
              </Button>
              <Button variant="danger" onClick={() => void remove()}>
                {t("manage.delete")}
              </Button>
            </>
          }
        />
      )}
    </OverlayCard>
  );
}
