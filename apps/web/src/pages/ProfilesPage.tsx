// Profiles (P18-24, re-laid out in W52-e): every Indus profile on this machine as a card grid.
// Each card's menu sets the sticky profile new CLI runs pick up, edits the model, description or
// persona, opens its skills, copies its CLI command, exports it, and renames or deletes it (never
// the default, nor the one this dashboard serves). IMPORT and CREATE open modals; BUILD opens the
// step-by-step builder (`/profiles/new`).

import { useMutation, useQuery } from "@tanstack/react-query";
import { Check, Users } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router";
import { api } from "../lib/api.ts";
import { HeaderActions } from "../shell/PageHeader.tsx";
import { useProfileScope } from "../shell/ProfileScope.tsx";
import { errorText, useToast } from "../shell/Toasts.tsx";
import { Button, ErrorState, Field, inputClass, Loading } from "../ui/controls.tsx";
import { ConfirmDialog } from "../ui/Dialog.tsx";
import { CreateProfileDialog } from "./profiles/CreateProfileDialog.tsx";
import { ImportProfileDialog } from "./profiles/ImportProfileDialog.tsx";
import { ProfileCard } from "./profiles/ProfileCard.tsx";
import { type EditorKind, ProfileEditorDialog } from "./profiles/ProfileEditorDialog.tsx";
import { PROFILES_KEY, useProfilesChanged } from "./profiles/useProfilesChanged.ts";

type Dialog = "create" | "import" | null;

export function ProfilesPage() {
  const toast = useToast();
  const changed = useProfilesChanged();
  const navigate = useNavigate();
  const { search } = useProfileScope();
  const [deleting, setDeleting] = useState<string | null>(null);
  const [typed, setTyped] = useState("");
  const [editing, setEditing] = useState<{ name: string; kind: EditorKind } | null>(null);
  const [dialog, setDialog] = useState<Dialog>(null);
  const profiles = useQuery({ queryKey: PROFILES_KEY, queryFn: () => api.profiles() });
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
  const active = profiles.data?.active ?? "";
  const current = profiles.data?.current;
  const editProfile = editing ? list.find((p) => p.name === editing.name) : undefined;
  const defaultName = list.find((p) => p.is_default)?.name ?? "";

  return (
    <div className="flex flex-col px-4 py-[1.375rem] lg:px-[1.4375rem]">
      <HeaderActions>
        <Button size="sm" caps onClick={() => setDialog("import")}>
          Import
        </Button>
        <Button size="sm" caps onClick={() => void navigate({ pathname: "/profiles/new", search })}>
          Build
        </Button>
        <Button size="sm" tone="primary" caps onClick={() => setDialog("create")}>
          Create
        </Button>
      </HeaderActions>

      {profiles.isPending ? (
        <div className="flex justify-center py-24">
          <Loading />
        </div>
      ) : profiles.error ? (
        <ErrorState message={errorText(profiles.error)} onRetry={() => void profiles.refetch()} />
      ) : (
        <>
          <div
            role="status"
            className="flex h-10 items-center gap-2.5 border border-line bg-canvas px-4 text-[0.75rem]"
          >
            <Check aria-hidden="true" className="size-3.5 shrink-0 text-success" />
            <span className="text-ink-muted">
              Active profile: <strong className="font-medium text-ink">{active}</strong>
              {current && current !== active ? (
                <span className="type-data ml-1 text-ink-soft">(serving {current})</span>
              ) : null}
            </span>
          </div>

          <h2 className="mt-[26px] mb-[15px] flex items-center gap-2 font-sans text-[0.9375rem] font-bold tracking-[0.15em] text-ink-muted">
            <Users aria-hidden="true" className="size-4" />
            Profiles ({list.length})
          </h2>

          {list.length === 0 ? (
            <div className="border border-line px-4 py-10 text-center text-[0.8125rem] text-ink-soft">
              No profiles yet
            </div>
          ) : (
            <ul
              aria-label="Profiles"
              className="m-0 grid list-none grid-cols-1 gap-3 p-0 md:grid-cols-2 xl:grid-cols-3"
            >
              {list.map((profile) => (
                <ProfileCard
                  key={profile.name}
                  profile={profile}
                  others={names.filter((n) => n !== profile.name)}
                  onEdit={(kind) =>
                    setEditing((open) =>
                      open?.name === profile.name && open.kind === kind
                        ? null
                        : { name: profile.name, kind },
                    )
                  }
                  onDelete={() => {
                    setTyped("");
                    setDeleting(profile.name);
                  }}
                />
              ))}
            </ul>
          )}
        </>
      )}

      {editing && editProfile ? (
        <ProfileEditorDialog
          key={`${editing.name}-${editing.kind}`}
          profile={editProfile}
          kind={editing.kind}
          onClose={() => setEditing(null)}
        />
      ) : null}
      {dialog === "create" ? (
        <CreateProfileDialog
          existing={names}
          defaultSource={defaultName}
          onClose={() => setDialog(null)}
        />
      ) : null}
      {dialog === "import" ? (
        <ImportProfileDialog existing={names} onClose={() => setDialog(null)} />
      ) : null}
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
