// Profiles (P24-27): create, rename, delete, export and import an archive, and edit the persona
// file. Rename and delete go through the window's profile switcher (P20-26) exactly like
// Settings › Profiles: main leaves that profile and tears down its connection before the backend
// change, then every profile-keyed store moves or goes (R08.7). Delete asks the person to type the
// profile's name. Export writes a zip without history, runtime state or any key (the server strips
// them; the page says so); import takes an archive path on the server's machine. A profile with no
// persona file is told so, and "Create persona" writes one.

import { useState } from "react";
import { useT } from "../../i18n/index.tsx";
import { useSwitcher } from "../../profiles/switcherReact.tsx";
import { type RestLike, rest } from "../../settings/settingsApi.ts";
import { LoadState, ManageOverlay, useApi, useRestValue } from "../manage/common.tsx";

interface ProfileRow {
  name: string;
  is_default: boolean;
  description?: string | null;
  model?: string | null;
  has_persona?: boolean;
}

/** What rename/delete need from the switcher (the real one, or a test fake). */
export interface ProfileChanges {
  renameProfile(
    from: string,
    to: string,
    run: () => Promise<void>,
  ): Promise<{ ok: true } | { ok: false; error: string }>;
  deleteProfile(
    name: string,
    run: () => Promise<void>,
  ): Promise<{ ok: true } | { ok: false; error: string }>;
}

function PersonaEditor(props: { api: RestLike | null; name: string }) {
  const t = useT();
  const persona = useRestValue<{ content: string; exists: boolean }>(
    () => rest(props.api, "GET", `/api/profiles/${encodeURIComponent(props.name)}/persona`),
    [props.api, props.name],
  );
  const [draft, setDraft] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const save = async (content: string) => {
    const answer = await rest(
      props.api,
      "PUT",
      `/api/profiles/${encodeURIComponent(props.name)}/persona`,
      {
        body: { content },
      },
    );
    setNote(answer.ok ? t("manage.saved") : answer.message);
    if (answer.ok) {
      setDraft(null);
      persona.reload();
    }
  };
  return (
    <LoadState value={persona}>
      {(data) =>
        !data.exists && draft === null ? (
          <div className="manage-inline">
            <p className="manage-muted">{t("manage.profiles.noPersona")}</p>
            <button type="button" className="button button-secondary" onClick={() => void save("")}>
              {t("manage.profiles.createPersona")}
            </button>
          </div>
        ) : (
          <div className="manage-field">
            <label className="manage-field">
              {t("manage.profiles.persona")}
              <textarea
                rows={6}
                value={draft ?? data.content}
                onChange={(e) => setDraft(e.target.value)}
              />
            </label>
            <div className="manage-inline">
              <button
                type="button"
                className="button button-primary"
                disabled={draft === null}
                onClick={() => void save(draft ?? data.content)}
              >
                {t("manage.save")}
              </button>
              {note !== "" && <span className="manage-muted">{note}</span>}
            </div>
          </div>
        )
      }
    </LoadState>
  );
}

export function ProfilesOverlay(props: {
  api?: RestLike | null;
  onClose: () => void;
  /** Defaults to the window's switcher. */
  changes?: ProfileChanges | null;
}) {
  const t = useT();
  const api = useApi(props.api);
  const switcher = useSwitcher();
  const changes: ProfileChanges | null = props.changes === undefined ? switcher : props.changes;
  const list = useRestValue<{ profiles: ProfileRow[] }>(
    () => rest(api, "GET", "/api/profiles"),
    [api],
  );
  const [newName, setNewName] = useState("");
  const [cloneFrom, setCloneFrom] = useState("");
  const [renaming, setRenaming] = useState<Record<string, string>>({});
  const [deleting, setDeleting] = useState<{ name: string; typed: string } | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [archivePath, setArchivePath] = useState("");
  const [note, setNote] = useState("");

  const say = (ok: boolean, message: string) =>
    setNote(ok ? message : message || t("manage.failed"));
  const runChange = async (call: () => ReturnType<typeof rest>) => {
    const answer = await call();
    if (!answer.ok) throw new Error(answer.message);
  };

  const create = async () => {
    const body = { name: newName.trim(), ...(cloneFrom === "" ? {} : { clone_from: cloneFrom }) };
    const answer = await rest(api, "POST", "/api/profiles", { body });
    say(answer.ok, answer.ok ? t("manage.profiles.created", { name: body.name }) : answer.message);
    if (answer.ok) {
      setNewName("");
      list.reload();
    }
  };

  const rename = async (from: string) => {
    const to = (renaming[from] ?? "").trim();
    if (to === "" || to === from || changes === null) return;
    const result = await changes.renameProfile(from, to, () =>
      runChange(() =>
        rest(api, "PATCH", `/api/profiles/${encodeURIComponent(from)}`, { body: { new_name: to } }),
      ),
    );
    say(result.ok, result.ok ? t("manage.profiles.renamed", { name: to }) : result.error);
    if (result.ok) list.reload();
  };

  const remove = async () => {
    if (deleting === null || deleting.typed !== deleting.name || changes === null) return;
    const name = deleting.name;
    const result = await changes.deleteProfile(name, () =>
      runChange(() => rest(api, "DELETE", `/api/profiles/${encodeURIComponent(name)}`)),
    );
    setDeleting(null);
    say(result.ok, result.ok ? t("manage.profiles.deleted", { name }) : result.error);
    if (result.ok) list.reload();
  };

  const exportProfile = async (name: string) => {
    const answer = await rest<{ archive: string }>(
      api,
      "POST",
      `/api/profiles/${encodeURIComponent(name)}/export`,
    );
    say(
      answer.ok,
      answer.ok ? t("manage.profiles.exported", { path: answer.data.archive }) : answer.message,
    );
  };

  const importArchive = async () => {
    const answer = await rest<{ name?: string }>(api, "POST", "/api/profiles/import", {
      body: { archive: archivePath.trim() },
    });
    say(answer.ok, answer.ok ? t("manage.profiles.imported") : answer.message);
    if (answer.ok) {
      setArchivePath("");
      list.reload();
    }
  };

  return (
    <ManageOverlay title={t("nav.profiles")} onClose={props.onClose}>
      {note !== "" && (
        <p className="manage-muted" role="status">
          {note}
        </p>
      )}
      <LoadState value={list}>
        {(data) => (
          <>
            {data.profiles.map((row) => (
              <div className="manage-card" key={row.name} data-profile={row.name}>
                <div className="manage-inline">
                  <h3>{row.name}</h3>
                  {row.is_default && (
                    <span className="manage-badge">{t("manage.profiles.default")}</span>
                  )}
                  {row.model && <span className="manage-muted">{row.model}</span>}
                </div>
                <div className="manage-inline">
                  {!row.is_default && (
                    <>
                      <input
                        aria-label={t("manage.profiles.renameTo", { name: row.name })}
                        value={renaming[row.name] ?? ""}
                        placeholder={t("manage.profiles.newName")}
                        onChange={(e) => setRenaming({ ...renaming, [row.name]: e.target.value })}
                      />
                      <button
                        type="button"
                        className="button button-secondary"
                        onClick={() => void rename(row.name)}
                      >
                        {t("manage.profiles.rename")}
                      </button>
                      <button
                        type="button"
                        className="button button-secondary"
                        onClick={() => setDeleting({ name: row.name, typed: "" })}
                      >
                        {t("manage.delete")}
                      </button>
                    </>
                  )}
                  <button
                    type="button"
                    className="button button-secondary"
                    onClick={() => void exportProfile(row.name)}
                  >
                    {t("manage.profiles.export")}
                  </button>
                  <button
                    type="button"
                    className="button button-secondary"
                    onClick={() => setOpen(open === row.name ? null : row.name)}
                  >
                    {t("manage.profiles.persona")}
                  </button>
                </div>
                {deleting?.name === row.name && (
                  <div
                    className="manage-banner"
                    role="alertdialog"
                    aria-label={t("manage.profiles.confirmDelete", { name: row.name })}
                  >
                    <label className="manage-field">
                      {t("manage.profiles.typeToDelete", { name: row.name })}
                      <input
                        value={deleting.typed}
                        onChange={(e) => setDeleting({ name: row.name, typed: e.target.value })}
                      />
                    </label>
                    <button
                      type="button"
                      className="button button-primary"
                      disabled={deleting.typed !== row.name}
                      onClick={() => void remove()}
                    >
                      {t("manage.profiles.deleteForGood")}
                    </button>
                    <button
                      type="button"
                      className="button button-secondary"
                      onClick={() => setDeleting(null)}
                    >
                      {t("manage.cancel")}
                    </button>
                  </div>
                )}
                {open === row.name && <PersonaEditor api={api} name={row.name} />}
              </div>
            ))}
            <form
              className="manage-card"
              onSubmit={(e) => {
                e.preventDefault();
                void create();
              }}
            >
              <h3>{t("manage.profiles.new")}</h3>
              <label className="manage-field">
                {t("manage.profiles.name")}
                <input value={newName} onChange={(e) => setNewName(e.target.value)} />
              </label>
              <label className="manage-field">
                {t("manage.profiles.cloneFrom")}
                <select value={cloneFrom} onChange={(e) => setCloneFrom(e.target.value)}>
                  <option value="">{t("manage.profiles.fresh")}</option>
                  {data.profiles.map((p) => (
                    <option key={p.name} value={p.name}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="submit"
                className="button button-primary"
                disabled={newName.trim() === ""}
              >
                {t("manage.profiles.create")}
              </button>
            </form>
          </>
        )}
      </LoadState>
      <div className="manage-card">
        <h3>{t("manage.profiles.import")}</h3>
        <p className="manage-muted">{t("manage.profiles.exportNote")}</p>
        <label className="manage-field">
          {t("manage.profiles.archive")}
          <input value={archivePath} onChange={(e) => setArchivePath(e.target.value)} />
        </label>
        <button
          type="button"
          className="button button-secondary"
          disabled={archivePath.trim() === ""}
          onClick={() => void importArchive()}
        >
          {t("manage.profiles.importButton")}
        </button>
      </div>
    </ManageOverlay>
  );
}
