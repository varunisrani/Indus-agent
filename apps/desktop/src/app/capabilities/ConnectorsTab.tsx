// The Connectors tab (P24-26, W52-k): the profile's MCP servers as a two-column card grid with a
// search and an "Add your own" dialog; a card opens the server's dialog (test, sign in, remove).
// We have no hosted catalog; under the configured servers a Catalog group lists the server presets
// our backend knows (`GET /api/mcp/catalog`, W55-F2-3) that this profile has not added yet, each
// with an Add button that goes through the normal add route. A catalog that fails to load is left
// out quietly (the servers stay usable).

import { AlertCircle, Plug } from "lucide-react";
import { type FormEvent, useMemo, useState } from "react";
import { type MessageKey, useT } from "../../i18n/index.tsx";
import { type RestAnswer, type RestLike, rest } from "../../settings/settingsApi.ts";
import { Button, EmptyState, SmallDialog } from "../../ui/components.tsx";
import { MetaGrid, QuietSearch } from "../kit.tsx";
import { RowSwitch } from "../masterDetail.tsx";
import { notify } from "../Toasts.tsx";
import { field, type McpCatalogRow, type McpServerRow, useList } from "./shared.ts";

function isRemote(server: McpServerRow): boolean {
  return typeof server.url === "string" && server.url !== "";
}

export function ConnectorsTab(props: {
  api: RestLike | null;
  profile: string;
  /** Open the sign-in page in the browser (`external.open`, a real click in this window). */
  openUrl?: (url: string) => void;
}) {
  const t = useT();
  const { api, profile } = props;
  const list = useList<McpServerRow>(api, "/api/mcp/servers", profile, field("servers"));
  const catalog = useList<McpCatalogRow>(api, "/api/mcp/catalog", profile, field("entries"));
  const [query, setQuery] = useState("");
  const [adding, setAdding] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const rows = list.rows ?? [];
  const offered = useMemo(
    () => (catalog.rows ?? []).filter((entry) => !entry.installed),
    [catalog.rows],
  );
  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (list.rows ?? []).filter(
      (row) =>
        needle === "" ||
        `${row.name} ${row.command ?? ""} ${row.url ?? ""}`.toLowerCase().includes(needle),
    );
  }, [list.rows, query]);
  const shownOffers = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return offered.filter(
      (entry) =>
        needle === "" || `${entry.name} ${entry.description}`.toLowerCase().includes(needle),
    );
  }, [offered, query]);
  const total = rows.length + offered.length;

  const call = async (
    method: "POST" | "PUT" | "DELETE",
    path: string,
    body?: unknown,
  ): Promise<RestAnswer<unknown>> => {
    const answer = await rest<unknown>(api, method, path, {
      ...(body === undefined ? {} : { body }),
      profile,
      timeoutMs: 60_000,
    });
    if (!answer.ok) notify({ tone: "error", title: answer.message });
    list.reload();
    catalog.reload();
    return answer;
  };

  const install = (entry: McpCatalogRow) =>
    call(
      "POST",
      "/api/mcp/servers",
      entry.url
        ? { name: entry.name, url: entry.url }
        : { name: entry.name, command: entry.command ?? "", args: entry.args },
    );

  const toggle = async (server: McpServerRow, on: boolean) => {
    list.setRows((all) =>
      all.map((row) => (row.name === server.name ? { ...row, enabled: on } : row)),
    );
    await call("PUT", `/api/mcp/servers/${encodeURIComponent(server.name)}/enabled`, {
      enabled: on,
    });
  };

  const addButton = (
    <Button variant="outline" size="sm" onClick={() => setAdding(true)}>
      {t("connectors.addOwn")}
    </Button>
  );
  const openRow = rows.find((row) => row.name === open) ?? null;

  return (
    <div className="cap-connectors" data-testid="tab-connectors">
      <div className="cap-connectors-title">
        <h2>{t("connectors.title")}</h2>
        {addButton}
      </div>
      {list.error !== null ? (
        <EmptyState
          look="panel"
          fill
          role="alert"
          icon={<AlertCircle aria-hidden="true" />}
          title={t("capabilities.loadFailed")}
          description={list.error}
          action={
            <Button variant="outline" size="sm" onClick={list.reload}>
              {t("capabilities.refresh")}
            </Button>
          }
        />
      ) : list.rows !== null && total === 0 ? (
        <EmptyState
          look="panel"
          fill
          icon={<Plug aria-hidden="true" size={16} />}
          title={t("connectors.empty")}
          action={addButton}
        />
      ) : (
        <>
          <div className="cap-connectors-search">
            <QuietSearch
              fill
              label={t("connectors.searchApps", { count: total })}
              placeholder={t("connectors.searchApps", { count: total })}
              value={query}
              onChange={setQuery}
            />
          </div>
          {rows.length > 0 && (
            <h3 className="cap-group">
              {t("connectors.configured")}
              <span className="cap-group-count">{shown.length}</span>
            </h3>
          )}
          <ul className="cap-card-grid">
            {shown.map((server) => {
              const on = server.enabled !== false;
              const remote = isRemote(server);
              return (
                <li
                  key={server.name}
                  className="cap-card"
                  data-selected={open === server.name ? "true" : undefined}
                >
                  <button
                    type="button"
                    className="cap-card-main"
                    aria-label={`${t("connectors.details")}: ${server.name}`}
                    onClick={() => setOpen(server.name)}
                  />
                  <span className="cap-tile" aria-hidden="true">
                    {server.name.charAt(0).toUpperCase()}
                  </span>
                  <span className="cap-card-text">
                    <span className="cap-card-line">
                      <span className="cap-card-name">{server.name}</span>
                      <span className="cap-card-kind">
                        {t(remote ? "connectors.kind.remote" : "connectors.kind.local")}
                      </span>
                    </span>
                    <span className="cap-card-desc">{remote ? server.url : server.command}</span>
                  </span>
                  <span className="cap-card-lane">
                    <span className="cap-card-state" data-on={on ? "true" : undefined}>
                      {on && <span className="cap-dot" aria-hidden="true" />}
                      {t(on ? "connectors.on" : "connectors.off")}
                    </span>
                    <RowSwitch
                      label={t("connectors.enabled", { name: server.name })}
                      checked={on}
                      onChange={(next) => void toggle(server, next)}
                    />
                  </span>
                </li>
              );
            })}
          </ul>
          {offered.length > 0 && (
            <>
              <h3 className="cap-group">
                {t("connectors.catalog")}
                <span className="cap-group-count">{shownOffers.length}</span>
              </h3>
              <ul className="cap-card-grid" aria-label={t("connectors.catalog")}>
                {shownOffers.map((entry) => (
                  <CatalogCard key={entry.name} entry={entry} onAdd={() => install(entry)} />
                ))}
              </ul>
            </>
          )}
        </>
      )}
      {adding && (
        <AddConnectorDialog
          onClose={() => setAdding(false)}
          onAdd={async (body) => {
            const answer = await call("POST", "/api/mcp/servers", body);
            if (answer.ok) setAdding(false);
          }}
        />
      )}
      {openRow !== null && (
        <ConnectorDialog
          server={openRow}
          onClose={() => setOpen(null)}
          call={call}
          {...(props.openUrl === undefined ? {} : { openUrl: props.openUrl })}
        />
      )}
    </div>
  );
}

function CatalogCard(props: { entry: McpCatalogRow; onAdd: () => Promise<unknown> }) {
  const t = useT();
  const { entry } = props;
  const [busy, setBusy] = useState(false);
  const remote = entry.transport === "http";
  return (
    <li className="cap-card" data-catalog={entry.name}>
      <span className="cap-tile" aria-hidden="true">
        {entry.name.charAt(0).toUpperCase()}
      </span>
      <span className="cap-card-text">
        <span className="cap-card-line">
          <span className="cap-card-name">{entry.name}</span>
          <span className="cap-card-kind">
            {t(remote ? "connectors.kind.remote" : "connectors.kind.local")}
          </span>
        </span>
        <span
          className="cap-card-desc"
          title={entry.url ?? [entry.command, ...entry.args].join(" ")}
        >
          {entry.description}
        </span>
      </span>
      <span className="cap-card-lane">
        <Button
          variant="outline"
          size="sm"
          disabled={busy}
          aria-label={t("connectors.addPreset", { name: entry.name })}
          onClick={() => {
            setBusy(true);
            void props.onAdd().finally(() => setBusy(false));
          }}
        >
          {t("connectors.save")}
        </Button>
      </span>
    </li>
  );
}

function AddConnectorDialog(props: {
  onClose: () => void;
  onAdd: (body: Record<string, unknown>) => Promise<void>;
}) {
  const t = useT();
  const [draft, setDraft] = useState({ name: "", command: "", url: "", oauth: false });
  const add = async (event: FormEvent) => {
    event.preventDefault();
    const command = draft.command.trim().split(/\s+/).filter(Boolean);
    const body =
      draft.url.trim() !== ""
        ? { name: draft.name.trim(), url: draft.url.trim(), auth: draft.oauth ? "oauth" : "none" }
        : { name: draft.name.trim(), command: command[0] ?? "", args: command.slice(1) };
    await props.onAdd(body);
  };
  const ready =
    draft.name.trim() !== "" && (draft.command.trim() !== "" || draft.url.trim() !== "");
  return (
    <SmallDialog title={t("connectors.add")} onClose={props.onClose}>
      <form className="cap-form" onSubmit={add}>
        {(
          [
            ["name", "connectors.name"],
            ["command", "connectors.command"],
            ["url", "connectors.url"],
          ] as const
        ).map(([key, label]) => (
          <label key={key} className="cap-form-field">
            <span>{t(label as MessageKey)}</span>
            <input
              className="desk-input"
              aria-label={t(label as MessageKey)}
              value={draft[key]}
              onChange={(event) => setDraft({ ...draft, [key]: event.target.value })}
            />
          </label>
        ))}
        <label className="cap-form-check">
          <input
            type="checkbox"
            checked={draft.oauth}
            onChange={(event) => setDraft({ ...draft, oauth: event.target.checked })}
          />
          {t("connectors.oauth")}
        </label>
        <div className="desk-dialog-actions">
          <Button variant="ghost" size="sm" onClick={props.onClose}>
            {t("capabilities.cancel")}
          </Button>
          <Button type="submit" variant="primary" size="sm" disabled={!ready}>
            {t("connectors.save")}
          </Button>
        </div>
      </form>
    </SmallDialog>
  );
}

function ConnectorDialog(props: {
  server: McpServerRow;
  onClose: () => void;
  call: (
    method: "POST" | "PUT" | "DELETE",
    path: string,
    body?: unknown,
  ) => Promise<RestAnswer<unknown>>;
  openUrl?: (url: string) => void;
}) {
  const t = useT();
  const { server } = props;
  const remote = isRemote(server);
  const path = `/api/mcp/servers/${encodeURIComponent(server.name)}`;
  const [result, setResult] = useState<string | null>(null);
  const [removing, setRemoving] = useState(false);

  const test = async () => {
    const answer = await props.call("POST", `${path}/test`);
    if (answer.ok) {
      const count = ((answer.data as { tools?: unknown[] }).tools ?? []).length;
      setResult(t(count === 1 ? "connectors.testOkOne" : "connectors.testOk", { count }));
    }
  };
  const signIn = async () => {
    const answer = await props.call("POST", `${path}/auth`);
    const url = answer.ok
      ? (answer.data as { authorization_url?: unknown }).authorization_url
      : undefined;
    if (typeof url === "string") props.openUrl?.(url);
  };

  if (removing) {
    return (
      <SmallDialog
        role="alertdialog"
        title={t("connectors.remove.title")}
        description={t("connectors.remove.body", { name: server.name })}
        onClose={() => setRemoving(false)}
        actions={
          <>
            <Button variant="ghost" size="sm" onClick={() => setRemoving(false)}>
              {t("capabilities.cancel")}
            </Button>
            <Button
              variant="danger"
              size="sm"
              onClick={() => void props.call("DELETE", path).then(props.onClose)}
            >
              {t("connectors.remove")}
            </Button>
          </>
        }
      />
    );
  }
  return (
    <SmallDialog
      title={server.name}
      onClose={props.onClose}
      actions={
        <>
          <Button variant="danger" size="sm" onClick={() => setRemoving(true)}>
            {t("connectors.remove")}
          </Button>
          <span className="cap-dialog-gap" />
          {remote && server.auth === "oauth" && (
            <Button variant="outline" size="sm" onClick={() => void signIn()}>
              {t("connectors.signIn")}
            </Button>
          )}
          <Button variant="outline" size="sm" onClick={() => void test()}>
            {t("connectors.test")}
          </Button>
        </>
      }
    >
      <MetaGrid
        framed
        rows={[
          [
            t("connectors.transport"),
            t(remote ? "connectors.kind.remote" : "connectors.kind.local"),
          ],
          [t("connectors.target"), (remote ? server.url : server.command) ?? "—"],
          ...(remote ? ([[t("connectors.auth"), server.auth ?? "none"]] as const) : []),
        ]}
      />
      {result !== null && (
        <p className="md-status" role="status">
          {result}
        </p>
      )}
    </SmallDialog>
  );
}
