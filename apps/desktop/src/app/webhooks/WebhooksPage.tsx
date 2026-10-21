// The Webhooks page (P24-27): the routes the daemon's webhook receiver serves (P15-32) — list,
// create, enable/disable, delete. Routes defined in config.yaml are shown read-only. The signing
// secret exists in exactly one answer (the create) and lives only in this component's state until
// the person closes the notice; it is never written to storage or a log.

import { useState } from "react";
import { useT } from "../../i18n/index.tsx";
import { type RestLike, rest } from "../../settings/settingsApi.ts";
import { LoadState, PageFrame, useApi, useRestValue, when } from "../manage/common.tsx";

export const WEBHOOK_AUTHS = ["auto", "github", "gitlab", "hmac", "hmac_ts"] as const;

export interface WebhookRoute {
  name: string;
  source: "subscribed" | "config" | string;
  url: string;
  auth: string;
  events: string[];
  prompt: string;
  deliver: string | null;
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

export function WebhooksPage(props: { api?: RestLike | null; profile: string | null }) {
  const t = useT();
  const api = useApi(props.api);
  const routes = useRestValue<WebhooksAnswer>(
    () => rest(api, "GET", "/api/webhooks", { profile: props.profile }),
    [api, props.profile],
  );
  const [name, setName] = useState("");
  const [events, setEvents] = useState("");
  const [prompt, setPrompt] = useState("");
  const [deliver, setDeliver] = useState("");
  const [auth, setAuth] = useState<(typeof WEBHOOK_AUTHS)[number]>("auto");
  const [secret, setSecret] = useState<{ name: string; value: string; url: string } | null>(null);
  const [note, setNote] = useState("");

  const create = async () => {
    const body = {
      name: name.trim(),
      events: events
        .split(",")
        .map((e) => e.trim())
        .filter((e) => e !== ""),
      prompt,
      auth,
      ...(deliver.trim() === "" ? {} : { deliver: deliver.trim() }),
    };
    const answer = await rest<WebhookRoute & { secret: string }>(api, "POST", "/api/webhooks", {
      profile: props.profile,
      body,
    });
    if (!answer.ok) {
      setNote(answer.message);
      return;
    }
    setSecret({ name: answer.data.name, value: answer.data.secret, url: answer.data.url });
    setName("");
    setEvents("");
    setPrompt("");
    setDeliver("");
    setNote("");
    routes.reload();
  };

  const toggle = async (route: WebhookRoute) => {
    const answer = await rest(
      api,
      "PUT",
      `/api/webhooks/${encodeURIComponent(route.name)}/enabled`,
      {
        profile: props.profile,
        body: { enabled: !route.enabled },
      },
    );
    if (!answer.ok) setNote(answer.message);
    routes.reload();
  };

  const remove = async (route: WebhookRoute) => {
    const answer = await rest(api, "DELETE", `/api/webhooks/${encodeURIComponent(route.name)}`, {
      profile: props.profile,
    });
    if (!answer.ok) setNote(answer.message);
    routes.reload();
  };

  return (
    <PageFrame title={t("nav.webhooks")} id="webhooks">
      {secret !== null && (
        <div className="manage-banner" role="alert">
          <div>
            <p>{t("manage.webhooks.secretOnce", { name: secret.name })}</p>
            <p className="manage-code" data-testid="webhook-secret">
              {secret.value}
            </p>
            <p className="manage-muted">{secret.url}</p>
          </div>
          <button type="button" className="button button-secondary" onClick={() => setSecret(null)}>
            {t("manage.webhooks.secretDone")}
          </button>
        </div>
      )}
      {note !== "" && (
        <p className="manage-error" role="alert">
          {note}
        </p>
      )}
      <LoadState value={routes}>
        {(data) => (
          <>
            {!data.receiver_enabled && (
              <p className="manage-muted">{t("manage.webhooks.receiverOff")}</p>
            )}
            <table className="manage-table">
              <thead>
                <tr>
                  <th>{t("manage.webhooks.name")}</th>
                  <th>{t("manage.webhooks.events")}</th>
                  <th>{t("manage.webhooks.auth")}</th>
                  <th>{t("manage.webhooks.created")}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.routes.map((route) => (
                  <tr key={route.name}>
                    <td>
                      <strong>{route.name}</strong>
                      <div className="manage-muted">{route.url}</div>
                    </td>
                    <td>
                      {route.events.length === 0
                        ? t("manage.webhooks.allEvents")
                        : route.events.join(", ")}
                    </td>
                    <td>{route.auth}</td>
                    <td>{when(route.created_at)}</td>
                    <td>
                      {route.source === "config" ? (
                        <span className="manage-muted">{t("manage.webhooks.fromConfig")}</span>
                      ) : (
                        <span className="manage-inline">
                          <label className="manage-inline">
                            <input
                              type="checkbox"
                              checked={route.enabled}
                              aria-label={t("manage.webhooks.enable", { name: route.name })}
                              onChange={() => void toggle(route)}
                            />
                          </label>
                          <button
                            type="button"
                            className="button button-secondary"
                            onClick={() => void remove(route)}
                          >
                            {t("manage.delete")}
                          </button>
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {data.routes.length === 0 && (
              <p className="manage-muted">{t("manage.webhooks.none")}</p>
            )}
          </>
        )}
      </LoadState>
      <form
        className="manage-card"
        onSubmit={(e) => {
          e.preventDefault();
          void create();
        }}
      >
        <h2>{t("manage.webhooks.new")}</h2>
        <label className="manage-field">
          {t("manage.webhooks.name")}
          <input value={name} onChange={(e) => setName(e.target.value)} required />
        </label>
        <label className="manage-field">
          {t("manage.webhooks.eventsHelp")}
          <input value={events} onChange={(e) => setEvents(e.target.value)} />
        </label>
        <label className="manage-field">
          {t("manage.webhooks.prompt")}
          <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={3} />
        </label>
        <label className="manage-field">
          {t("manage.webhooks.deliver")}
          <input value={deliver} onChange={(e) => setDeliver(e.target.value)} />
        </label>
        <label className="manage-field">
          {t("manage.webhooks.auth")}
          <select
            value={auth}
            onChange={(e) => setAuth(e.target.value as (typeof WEBHOOK_AUTHS)[number])}
          >
            {WEBHOOK_AUTHS.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </label>
        <button type="submit" className="button button-primary" disabled={name.trim() === ""}>
          {t("manage.webhooks.create")}
        </button>
      </form>
    </PageFrame>
  );
}
