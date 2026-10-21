// Profiles (P18-24): every Indus profile on this machine. Make one the sticky profile new CLI runs
// pick up, rename or delete one (never the default, nor the one this dashboard serves), edit its
// model, description and persona, export its settings or import an archive. New profiles are made
// in the builder (`/profiles/new`).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useEffect, useId, useState } from "react";
import { Link } from "react-router";
import { api } from "../lib/api.ts";
import type { ProfileSummary } from "../lib/restTypes.ts";
import { HeaderActions } from "../shell/PageHeader.tsx";
import { useProfileScope } from "../shell/ProfileScope.tsx";
import { errorText, useToast } from "../shell/Toasts.tsx";
import { ActionLog } from "../ui/ActionLog.tsx";
import { Badge, Button, Card, Field, inputClass, Notice, QueryState } from "../ui/controls.tsx";
import { ConfirmDialog, Modal } from "../ui/Dialog.tsx";
import { normaliseProfileName, profileNameProblem } from "./profiles/profileName.ts";

const LIST_KEY = ["profiles", "list"] as const;

function useProfilesChanged() {
  const client = useQueryClient();
  const scope = useProfileScope();
  return () => {
    void client.invalidateQueries({ queryKey: ["profiles"] });
    scope.refresh();
  };
}

function lockedReason(profile: ProfileSummary): string | null {
  if (profile.is_default) return "The default profile cannot be renamed or deleted.";
  if (profile.is_current) return "This dashboard serves this profile; change it from elsewhere.";
  return null;
}

function Editor({ profile, onClose }: { profile: ProfileSummary; onClose: () => void }) {
  const toast = useToast();
  const changed = useProfilesChanged();
  const ids = useId();
  const [provider, setProvider] = useState(profile.provider ?? "");
  const [model, setModel] = useState(profile.model ?? "");
  const [description, setDescription] = useState(profile.description ?? "");
  const [persona, setPersona] = useState("");
  const [problem, setProblem] = useState("");
  const loaded = useQuery({
    queryKey: ["profiles", "persona", profile.name],
    queryFn: () => api.profile.persona(profile.name),
    gcTime: 0,
  });
  useEffect(() => {
    if (loaded.data) setPersona(loaded.data.content);
  }, [loaded.data]);

  const done = (what: string) => {
    setProblem("");
    toast.show(`${what} of ${profile.name} saved.`, "success");
    changed();
  };
  const fail = (error: unknown) => setProblem(errorText(error));
  const saveModel = useMutation({
    mutationFn: () => api.profile.setModel(profile.name, provider.trim(), model.trim()),
    onSuccess: () => done("Model"),
    onError: fail,
  });
  const saveDescription = useMutation({
    mutationFn: () => api.profile.setDescription(profile.name, description),
    onSuccess: () => done("Description"),
    onError: fail,
  });
  const savePersona = useMutation({
    mutationFn: () => api.profile.setPersona(profile.name, persona),
    onSuccess: () => done("Persona"),
    onError: fail,
  });

  return (
    <Modal
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title={`Edit ${profile.name}`}
      description="Each part saves on its own; sessions already open keep their settings."
    >
      <div className="flex flex-col gap-4">
        <div className="flex flex-wrap items-end gap-2">
          <Field label="Provider" htmlFor={`${ids}-provider`}>
            <input
              id={`${ids}-provider`}
              className={inputClass}
              value={provider}
              onChange={(e) => setProvider(e.currentTarget.value)}
            />
          </Field>
          <Field label="Model" htmlFor={`${ids}-model`}>
            <input
              id={`${ids}-model`}
              className={inputClass}
              value={model}
              onChange={(e) => setModel(e.currentTarget.value)}
            />
          </Field>
          <Button
            onClick={() => saveModel.mutate()}
            disabled={!provider.trim() || !model.trim() || saveModel.isPending}
          >
            Save model
          </Button>
        </div>
        <div className="flex flex-col gap-2">
          <Field label="Description" htmlFor={`${ids}-description`}>
            <input
              id={`${ids}-description`}
              className={inputClass}
              value={description}
              onChange={(e) => setDescription(e.currentTarget.value)}
            />
          </Field>
          <div>
            <Button onClick={() => saveDescription.mutate()} disabled={saveDescription.isPending}>
              Save description
            </Button>
          </div>
        </div>
        <div className="flex flex-col gap-2">
          <Field
            label="Persona (PERSONA.md)"
            htmlFor={`${ids}-persona`}
            hint="How the agent behaves in this profile; added to every session's instructions."
          >
            <textarea
              id={`${ids}-persona`}
              className={`${inputClass} font-mono`}
              rows={8}
              value={persona}
              disabled={loaded.isPending}
              onChange={(e) => setPersona(e.currentTarget.value)}
            />
          </Field>
          <div>
            <Button
              onClick={() => savePersona.mutate()}
              disabled={loaded.isPending || savePersona.isPending}
            >
              Save persona
            </Button>
          </div>
        </div>
        {problem ? <Notice tone="danger">{problem}</Notice> : null}
        <div className="flex justify-end">
          <Button onClick={onClose}>Close</Button>
        </div>
      </div>
    </Modal>
  );
}

function ProfileCard({
  profile,
  others,
  onDelete,
  onEdit,
}: {
  profile: ProfileSummary;
  others: string[];
  onDelete: () => void;
  onEdit: () => void;
}) {
  const toast = useToast();
  const changed = useProfilesChanged();
  const ids = useId();
  const [renaming, setRenaming] = useState(false);
  const [newName, setNewName] = useState(profile.name);
  const [exportRun, setExportRun] = useState(0);
  const [archive, setArchive] = useState("");
  const locked = lockedReason(profile);
  const renameProblem = renaming ? profileNameProblem(newName, others) : null;

  const sticky = useMutation({
    mutationFn: () => api.profile.setSticky(profile.name),
    onSuccess: () => {
      toast.show(`New CLI runs now use ${profile.name}.`, "success");
      changed();
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });
  const rename = useMutation({
    mutationFn: () => api.profile.rename(profile.name, normaliseProfileName(newName)),
    onSuccess: (reply) => {
      setRenaming(false);
      toast.show(`${profile.name} is now ${reply.name}.`, "success");
      changed();
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });
  const exporting = useMutation({
    mutationFn: () => api.profile.exportArchive(profile.name),
    onSuccess: (reply) => {
      setArchive(reply.archive);
      setExportRun((n) => n + 1);
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });

  const submitRename = (event: FormEvent) => {
    event.preventDefault();
    if (renameProblem === null) rename.mutate();
  };

  return (
    <li data-testid={`profile-${profile.name}`}>
      <Card className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="flex-1 text-sm font-semibold">{profile.name}</h2>
          {profile.is_default ? <Badge>default</Badge> : null}
          {profile.is_sticky ? <Badge tone="accent">sticky</Badge> : null}
          {profile.is_current ? <Badge tone="ok">this dashboard</Badge> : null}
        </div>
        <p className="text-xs text-muted">
          Model:{" "}
          {profile.model
            ? `${profile.model}${profile.provider ? ` (${profile.provider})` : ""}`
            : "inherited"}
          {" · "}
          {profile.has_persona ? "has a persona" : "no persona"}
        </p>
        {profile.description ? <p className="text-sm">{profile.description}</p> : null}
        {renaming ? (
          <form className="flex flex-wrap items-end gap-2" onSubmit={submitRename} noValidate>
            <Field label="New name" htmlFor={`${ids}-rename`}>
              <input
                id={`${ids}-rename`}
                className={inputClass}
                value={newName}
                onChange={(e) => setNewName(e.currentTarget.value)}
              />
            </Field>
            <Button
              type="submit"
              tone="primary"
              disabled={renameProblem !== null || rename.isPending}
            >
              Save name
            </Button>
            <Button onClick={() => setRenaming(false)}>Cancel</Button>
            {renameProblem ? <p className="w-full text-xs text-danger">{renameProblem}</p> : null}
          </form>
        ) : null}
        <div className="flex flex-wrap gap-1">
          {profile.is_sticky ? null : (
            <Button onClick={() => sticky.mutate()} disabled={sticky.isPending}>
              Make sticky
            </Button>
          )}
          <Button onClick={onEdit}>Edit</Button>
          <Button
            onClick={() => {
              setNewName(profile.name);
              setRenaming(true);
            }}
            disabled={locked !== null}
            title={locked ?? undefined}
          >
            Rename
          </Button>
          <Button onClick={() => exporting.mutate()} disabled={exporting.isPending}>
            Export
          </Button>
          <Button
            tone="danger"
            onClick={onDelete}
            disabled={locked !== null}
            title={locked ?? undefined}
          >
            Delete
          </Button>
        </div>
        {locked ? <p className="text-xs text-muted">{locked}</p> : null}
        {exportRun > 0 ? (
          <>
            <ActionLog
              name="profile-export"
              run={exportRun}
              profile={profile.name}
              title={`Export of ${profile.name}`}
            />
            <p className="text-xs text-muted">
              Archive: <code className="font-mono">{archive}</code> (settings only: no history,
              secrets or channel state).
            </p>
          </>
        ) : null}
      </Card>
    </li>
  );
}

function ImportProfile({ existing }: { existing: string[] }) {
  const toast = useToast();
  const changed = useProfilesChanged();
  const ids = useId();
  const [archive, setArchive] = useState("");
  const [name, setName] = useState("");
  const [problem, setProblem] = useState("");
  const run = useMutation({
    mutationFn: () => api.profile.importArchive(archive.trim(), normaliseProfileName(name)),
    onSuccess: (reply) => {
      setArchive("");
      setName("");
      setProblem("");
      toast.show(`Profile ${reply.name} imported.`, "success");
      changed();
    },
    onError: (error) => setProblem(errorText(error)),
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!archive.trim().startsWith("/")) {
      setProblem("Give the archive's full path on this machine.");
      return;
    }
    const nameProblem = name.trim() ? profileNameProblem(name, existing) : null;
    if (nameProblem) {
      setProblem(nameProblem);
      return;
    }
    run.mutate();
  };
  return (
    <Card>
      <h2 className="mb-3 text-sm font-semibold">Import a profile</h2>
      <form className="flex flex-wrap items-end gap-3" onSubmit={submit} noValidate>
        <Field label="Archive path" htmlFor={`${ids}-archive`}>
          <input
            id={`${ids}-archive`}
            className={`${inputClass} font-mono`}
            value={archive}
            onChange={(e) => setArchive(e.currentTarget.value)}
          />
        </Field>
        <Field label="Name (optional)" htmlFor={`${ids}-name`}>
          <input
            id={`${ids}-name`}
            className={inputClass}
            value={name}
            onChange={(e) => setName(e.currentTarget.value)}
          />
        </Field>
        <Button type="submit" disabled={run.isPending}>
          Import
        </Button>
      </form>
      {problem ? (
        <div className="mt-2">
          <Notice tone="danger">{problem}</Notice>
        </div>
      ) : null}
    </Card>
  );
}

export function ProfilesPage() {
  const toast = useToast();
  const changed = useProfilesChanged();
  const { search } = useProfileScope();
  const [deleting, setDeleting] = useState<string | null>(null);
  const [typed, setTyped] = useState("");
  const [editing, setEditing] = useState<ProfileSummary | null>(null);
  const profiles = useQuery({ queryKey: LIST_KEY, queryFn: () => api.profiles() });
  const remove = useMutation({
    mutationFn: (name: string) => api.profile.remove(name),
    onSuccess: (_reply, name) => {
      setDeleting(null);
      setTyped("");
      toast.show(`${name} deleted.`, "success");
      changed();
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });
  const list = profiles.data?.profiles ?? [];
  const names = list.map((p) => p.name);
  return (
    <div className="flex flex-col gap-4 p-4">
      <HeaderActions>
        <Link
          to={{ pathname: "/profiles/new", search }}
          className="rounded-md border border-accent bg-accent px-3 py-1.5 text-sm text-accent-fg"
        >
          New profile
        </Link>
      </HeaderActions>
      <QueryState loading={profiles.isPending} error={profiles.error}>
        <ul className="grid gap-3 lg:grid-cols-2" aria-label="Profiles">
          {list.map((profile) => (
            <ProfileCard
              key={profile.name}
              profile={profile}
              others={names.filter((n) => n !== profile.name)}
              onDelete={() => {
                setTyped("");
                setDeleting(profile.name);
              }}
              onEdit={() => setEditing(profile)}
            />
          ))}
        </ul>
      </QueryState>
      <ImportProfile existing={names} />
      {editing ? <Editor profile={editing} onClose={() => setEditing(null)} /> : null}
      <ConfirmDialog
        open={deleting !== null}
        title={`Delete ${deleting ?? ""}?`}
        description="Its settings, history and secrets go. A deleted name stays retired until it is made again on purpose."
        confirmLabel="Delete"
        danger
        busy={remove.isPending || typed !== deleting}
        onConfirm={() => {
          if (deleting && typed === deleting) remove.mutate(deleting);
        }}
        onCancel={() => setDeleting(null)}
      >
        <Field label={`Type ${deleting ?? ""} to confirm`} htmlFor="profile-delete-confirm">
          <input
            id="profile-delete-confirm"
            className={inputClass}
            value={typed}
            onChange={(e) => setTyped(e.currentTarget.value)}
          />
        </Field>
      </ConfirmDialog>
    </div>
  );
}
