// Settings › Gateways › Connections (P24-29): the connections registry — "This computer", remote
// servers (token, native sign-in or user name + password) and SSH hosts. Add or edit an entry
// (tested on both legs before it is saved), make one the primary (the app reconnects), choose what
// a launch opens (the primary, or the last one that worked), sign in or out, remove. Tokens,
// passwords and access-proxy header values go to main and the keychain; this page never shows or
// keeps them after the call, and it sees only names and flags back.

import { useCallback, useEffect, useState } from "react";
import { desktopBridge } from "../app/bridge.ts";
import type { IndusDesktopConnections } from "../global.d.ts";
import { useT } from "../i18n/index.tsx";
import "../app/manage/manage.css";

type View = Awaited<ReturnType<IndusDesktopConnections["list"]>>;
type Entry = View["connections"][number];
type SaveInput = Parameters<IndusDesktopConnections["save"]>[0];

function parseHeaders(text: string): Record<string, string> | undefined {
  if (text.trim() === "") return undefined;
  const out: Record<string, string> = {};
  for (const line of text.split(/\r?\n/)) {
    const cut = line.indexOf(":");
    if (cut > 0) out[line.slice(0, cut).trim()] = line.slice(cut + 1).trim();
  }
  return out;
}

function EntryForm(props: {
  bridge: IndusDesktopConnections;
  entry: Entry | null;
  onDone: () => void;
  onCancel: () => void;
}) {
  const t = useT();
  const editing = props.entry;
  const [kind, setKind] = useState<"remote" | "ssh">(editing?.kind === "ssh" ? "ssh" : "remote");
  const [label, setLabel] = useState(editing?.label ?? "");
  const [url, setUrl] = useState(editing?.url ?? "");
  const [authMode, setAuthMode] = useState<"token" | "oauth" | "basic">(
    editing?.authMode ?? "token",
  );
  const [token, setToken] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [headers, setHeaders] = useState("");
  const [host, setHost] = useState(editing?.ssh?.host ?? "");
  const [user, setUser] = useState(editing?.ssh?.user ?? "");
  const [port, setPort] = useState(
    editing?.ssh?.port === undefined ? "" : String(editing.ssh.port),
  );
  const [keyPath, setKeyPath] = useState(editing?.ssh?.keyPath ?? "");
  const [remoteProfile, setRemoteProfile] = useState(editing?.ssh?.remoteProfile ?? "");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const input = (): SaveInput => {
    const base: SaveInput = {
      kind,
      ...(editing === null ? {} : { id: editing.id }),
      ...(label.trim() === "" ? {} : { label: label.trim() }),
    };
    if (kind === "ssh") {
      return {
        ...base,
        ssh: {
          host: host.trim(),
          ...(user.trim() === "" ? {} : { user: user.trim() }),
          ...(port.trim() === "" ? {} : { port: Number(port) }),
          ...(keyPath.trim() === "" ? {} : { keyPath: keyPath.trim() }),
          ...(remoteProfile.trim() === "" ? {} : { remoteProfile: remoteProfile.trim() }),
        },
      };
    }
    const extra = parseHeaders(headers);
    return {
      ...base,
      url,
      authMode,
      ...(authMode === "token" && token !== "" ? { token } : {}),
      ...(authMode === "basic" ? { username, ...(password === "" ? {} : { password }) } : {}),
      ...(extra === undefined ? {} : { headers: extra }),
    };
  };

  const finish = (message: string, ok: boolean) => {
    setToken("");
    setPassword("");
    setNote(message);
    if (ok) props.onDone();
  };

  const test = async () => {
    setBusy(true);
    try {
      const result = await props.bridge.test(input());
      setNote(result.ok ? t("manage.conn.testOk") : (result.message ?? t("manage.failed")));
    } catch (error) {
      setNote(error instanceof Error ? error.message : t("manage.failed"));
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    setBusy(true);
    try {
      const result = await props.bridge.save(input());
      const dropped =
        result.droppedHeaders.length > 0
          ? ` ${t("manage.conn.dropped", { names: result.droppedHeaders.join(", ") })}`
          : "";
      finish(
        result.saved ? `${t("manage.saved")}${dropped}` : (result.message ?? t("manage.failed")),
        result.saved,
      );
    } catch (error) {
      finish(error instanceof Error ? error.message : t("manage.failed"), false);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="manage-card"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <div className="manage-inline" role="radiogroup" aria-label={t("manage.conn.kind")}>
        {(["remote", "ssh"] as const).map((k) => (
          <label key={k} className="manage-inline">
            <input
              type="radio"
              checked={kind === k}
              onChange={() => setKind(k)}
              disabled={editing !== null}
            />
            {t(k === "remote" ? "manage.conn.remote" : "manage.conn.ssh")}
          </label>
        ))}
      </div>
      <label className="manage-field">
        {t("manage.conn.label")}
        <input value={label} onChange={(e) => setLabel(e.target.value)} />
      </label>
      {kind === "remote" ? (
        <>
          <label className="manage-field">
            {t("manage.conn.url")}
            <input
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://indus.example.com"
            />
          </label>
          <label className="manage-field">
            {t("manage.conn.auth")}
            <select
              value={authMode}
              onChange={(e) => setAuthMode(e.target.value as typeof authMode)}
            >
              <option value="token">{t("manage.conn.auth.token")}</option>
              <option value="oauth">{t("manage.conn.auth.oauth")}</option>
              <option value="basic">{t("manage.conn.auth.basic")}</option>
            </select>
          </label>
          {authMode === "token" && (
            <label className="manage-field">
              {t("manage.conn.token")}
              <input
                type="password"
                autoComplete="off"
                value={token}
                placeholder={editing?.hasCredential ? t("manage.conn.keep") : ""}
                onChange={(e) => setToken(e.target.value)}
              />
            </label>
          )}
          {authMode === "basic" && (
            <>
              <label className="manage-field">
                {t("manage.conn.username")}
                <input
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  autoComplete="off"
                />
              </label>
              <label className="manage-field">
                {t("manage.conn.password")}
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete="off"
                />
              </label>
            </>
          )}
          <label className="manage-field">
            {t("manage.conn.headers")}
            <textarea
              rows={2}
              value={headers}
              placeholder={
                editing?.headerNames.length
                  ? editing.headerNames.join(", ")
                  : "CF-Access-Client-Id: …"
              }
              onChange={(e) => setHeaders(e.target.value)}
            />
          </label>
        </>
      ) : (
        <>
          <label className="manage-field">
            {t("manage.conn.host")}
            <input value={host} onChange={(e) => setHost(e.target.value)} />
          </label>
          <label className="manage-field">
            {t("manage.conn.user")}
            <input value={user} onChange={(e) => setUser(e.target.value)} />
          </label>
          <label className="manage-field">
            {t("manage.conn.port")}
            <input
              value={port}
              inputMode="numeric"
              onChange={(e) => setPort(e.target.value)}
              placeholder="22"
            />
          </label>
          <label className="manage-field">
            {t("manage.conn.key")}
            <input
              value={keyPath}
              onChange={(e) => setKeyPath(e.target.value)}
              placeholder="~/.ssh/id_ed25519"
            />
          </label>
          <label className="manage-field">
            {t("manage.conn.remoteProfile")}
            <input
              value={remoteProfile}
              onChange={(e) => setRemoteProfile(e.target.value)}
              placeholder="default"
            />
          </label>
          <p className="manage-muted">{t("manage.conn.sshHelp")}</p>
        </>
      )}
      {note !== "" && (
        <p className="manage-muted" role="status">
          {note}
        </p>
      )}
      <div className="manage-inline">
        <button
          type="button"
          className="button button-secondary"
          disabled={busy}
          onClick={() => void test()}
        >
          {t("manage.conn.test")}
        </button>
        <button type="submit" className="button button-primary" disabled={busy}>
          {t("manage.save")}
        </button>
        <button type="button" className="button button-secondary" onClick={props.onCancel}>
          {t("manage.cancel")}
        </button>
      </div>
    </form>
  );
}

export function ConnectionsPage(props: { bridge?: IndusDesktopConnections | null }) {
  const t = useT();
  const bridge = props.bridge === undefined ? (desktopBridge()?.connections ?? null) : props.bridge;
  const [view, setView] = useState<View | null>(null);
  const [editing, setEditing] = useState<Entry | "new" | null>(null);
  const [note, setNote] = useState("");
  const load = useCallback(() => {
    if (bridge === null) return;
    void bridge.list().then(setView, (error: Error) => setNote(error.message));
  }, [bridge]);
  useEffect(load, [load]);
  if (bridge === null) return <p className="settings-row-note">{t("manage.conn.unavailable")}</p>;
  const act = async (run: () => Promise<unknown>) => {
    try {
      const answer = await run();
      if (answer !== null && typeof answer === "object" && "connections" in answer)
        setView(answer as View);
      else load();
      setNote("");
    } catch (error) {
      setNote(error instanceof Error ? error.message : t("manage.failed"));
    }
  };
  return (
    <div className="settings-rows" data-row="connections">
      <p className="settings-row-note">{t("manage.conn.body")}</p>
      {view !== null && (
        <>
          <label className="manage-inline">
            <input
              type="checkbox"
              checked={view.launchMode === "last-used"}
              onChange={(e) =>
                void act(() =>
                  bridge.setLaunchMode({ mode: e.target.checked ? "last-used" : "primary" }),
                )
              }
            />
            {t("manage.conn.lastUsed")}
          </label>
          {view.quarantined > 0 && (
            <p className="manage-muted">
              {t("manage.conn.quarantined", { count: view.quarantined })}
            </p>
          )}
          {view.connections.map((entry) => (
            <div className="manage-card" key={entry.id} data-connection={entry.id}>
              <div className="manage-inline">
                <strong>{entry.label}</strong>
                {entry.primary && (
                  <span className="manage-badge" data-tone="ok">
                    {t("manage.conn.primary")}
                  </span>
                )}
                <span className="manage-muted">
                  {entry.kind === "local"
                    ? t("manage.conn.local")
                    : entry.kind === "ssh"
                      ? `ssh ${entry.ssh?.user ? `${entry.ssh.user}@` : ""}${entry.ssh?.host ?? ""}`
                      : entry.url}
                </span>
              </div>
              <div className="manage-inline">
                {!entry.primary && (
                  <button
                    type="button"
                    className="button button-secondary"
                    onClick={() => void act(() => bridge.setPrimary({ id: entry.id }))}
                  >
                    {t("manage.conn.makePrimary")}
                  </button>
                )}
                {entry.kind !== "local" && (
                  <>
                    <button
                      type="button"
                      className="button button-secondary"
                      onClick={() => setEditing(entry)}
                    >
                      {t("manage.cron.edit")}
                    </button>
                    {entry.authMode === "oauth" &&
                      (entry.signedIn ? (
                        <button
                          type="button"
                          className="button button-secondary"
                          onClick={() => void act(() => bridge.signOut({ id: entry.id }))}
                        >
                          {t("manage.conn.signOut")}
                        </button>
                      ) : (
                        <button
                          type="button"
                          className="button button-primary"
                          onClick={() =>
                            void act(async () => {
                              const answer = await bridge.signIn({ id: entry.id });
                              if (!answer.ok) throw new Error(answer.message ?? t("manage.failed"));
                            })
                          }
                        >
                          {t("manage.conn.signIn")}
                        </button>
                      ))}
                    <button
                      type="button"
                      className="button button-secondary"
                      onClick={() => void act(() => bridge.remove({ id: entry.id }))}
                    >
                      {t("manage.delete")}
                    </button>
                  </>
                )}
              </div>
            </div>
          ))}
        </>
      )}
      {note !== "" && (
        <p className="manage-error" role="alert">
          {note}
        </p>
      )}
      {editing === null ? (
        <button type="button" className="button button-primary" onClick={() => setEditing("new")}>
          {t("manage.conn.add")}
        </button>
      ) : (
        <EntryForm
          bridge={bridge}
          entry={editing === "new" ? null : editing}
          onDone={() => {
            setEditing(null);
            load();
          }}
          onCancel={() => setEditing(null)}
        />
      )}
    </div>
  );
}
