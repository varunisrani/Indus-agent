// One profile as a square card: name and state badges with the actions menu, the daemon line, the
// description, and a meta block (model, enabled skills). Rename happens inline in the card.

import { useMutation, useQuery } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { useNavigate } from "react-router";
import { api } from "../../lib/api.ts";
import type { ProfileSummary } from "../../lib/restTypes.ts";
import { errorText, useToast } from "../../shell/Toasts.tsx";
import { ActionLog } from "../../ui/ActionLog.tsx";
import { Badge, Button, inputClass } from "../../ui/controls.tsx";
import { cx } from "../../ui/cx.ts";
import { NAME_RULE } from "./CreateProfileDialog.tsx";
import { ProfileActionsMenu } from "./ProfileActionsMenu.tsx";
import type { EditorKind } from "./ProfileEditorDialog.tsx";
import { normaliseProfileName, profileNameProblem } from "./profileName.ts";
import { useProfilesChanged } from "./useProfilesChanged.ts";

/** "Daemon running" in green, else "Daemon stopped"; hidden when the status cannot be read. */
function DaemonLine({ name }: { name: string }) {
  const status = useQuery({
    queryKey: ["profiles", "status", name],
    queryFn: () => api.statusOf(name),
    staleTime: 15_000,
  });
  if (!status.data) return null;
  const running = status.data.daemon.state === "running";
  return (
    <p
      className={cx(
        "m-0 flex items-center gap-2 text-[0.75rem]",
        running ? "text-success" : "text-ink-muted",
      )}
    >
      <span
        aria-hidden="true"
        className={cx("size-1.5 rounded-full", running ? "bg-success" : "bg-ink-faint")}
      />
      {running ? "Daemon running" : "Daemon stopped"}
    </p>
  );
}

/** "Skills: N" from the row's `skill_count` (W56-G2-2); hidden when the backend could not count. */
function SkillsLine({ count }: { count: number | null | undefined }) {
  if (typeof count !== "number") return null;
  return <span>Skills: {count}</span>;
}

export function ProfileCard({
  profile,
  others,
  onEdit,
  onDelete,
}: {
  profile: ProfileSummary;
  others: string[];
  onEdit: (kind: EditorKind) => void;
  onDelete: () => void;
}) {
  const toast = useToast();
  const changed = useProfilesChanged();
  const navigate = useNavigate();
  const [renaming, setRenaming] = useState(false);
  const [newName, setNewName] = useState(profile.name);
  const [exportRun, setExportRun] = useState(0);
  const [archive, setArchive] = useState("");
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

  const copyCommand = async () => {
    const command = `indus -p ${profile.name}`;
    try {
      await navigator.clipboard.writeText(command);
      toast.show(`Command copied: ${command}`, "success");
    } catch {
      toast.show("Could not copy the command; the browser refused the clipboard.", "error");
    }
  };

  const submitRename = (event: FormEvent) => {
    event.preventDefault();
    if (renameProblem === null && !rename.isPending) rename.mutate();
  };

  const modelLine = profile.model
    ? `${profile.model}${profile.provider ? ` (${profile.provider})` : ""}`
    : "inherited";

  return (
    <li data-testid={`profile-${profile.name}`} className="flex">
      <article className="flex min-h-[165px] min-w-0 flex-1 flex-col gap-2 border border-line bg-canvas p-4">
        <div className="flex min-w-0 items-center gap-1.5">
          <h3 className="m-0 mr-1 truncate text-[0.8125rem] font-medium text-ink">
            {profile.name}
          </h3>
          {profile.is_sticky ? <Badge tone="ok">active</Badge> : null}
          {profile.is_default ? <Badge tone="secondary">default</Badge> : null}
          {profile.is_current ? <Badge className="text-ink-muted">this dashboard</Badge> : null}
          <ProfileActionsMenu
            profile={profile}
            stickyPending={sticky.isPending}
            onSticky={() => sticky.mutate()}
            onEdit={onEdit}
            onSkills={() => void navigate(`/skills?profile=${encodeURIComponent(profile.name)}`)}
            onCopyCommand={() => void copyCommand()}
            onExport={() => exporting.mutate()}
            onRename={() => {
              setNewName(profile.name);
              setRenaming(true);
            }}
            onDelete={onDelete}
          />
        </div>

        {renaming ? (
          <form className="flex flex-col gap-2" onSubmit={submitRename} noValidate>
            <input
              aria-label="New name"
              // biome-ignore lint/a11y/noAutofocus: rename starts from the menu; typing follows
              autoFocus
              className={cx(inputClass, "w-full")}
              aria-invalid={renameProblem !== null}
              value={newName}
              onChange={(e) => setNewName(e.currentTarget.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") setRenaming(false);
              }}
            />
            <p
              className={cx("m-0 text-[0.75rem]", renameProblem ? "text-danger" : "text-ink-soft")}
            >
              {renameProblem ?? NAME_RULE}
            </p>
            <div className="flex gap-2">
              <Button
                type="submit"
                tone="primary"
                size="sm"
                disabled={renameProblem !== null || rename.isPending}
              >
                Save
              </Button>
              <Button tone="ghost" size="sm" onClick={() => setRenaming(false)}>
                Cancel
              </Button>
            </div>
          </form>
        ) : (
          <>
            <DaemonLine name={profile.name} />
            {profile.description ? (
              <p className="m-0 line-clamp-2 text-[0.75rem] text-ink-muted">
                {profile.description}
              </p>
            ) : (
              <p className="m-0 text-[0.75rem] text-ink-faint italic">No description</p>
            )}
            <div className="mt-auto flex flex-col pt-2 text-[0.75rem] leading-[17px] text-ink-muted">
              <span>Model: {modelLine}</span>
              <SkillsLine count={profile.skill_count} />
            </div>
          </>
        )}

        {exportRun > 0 ? (
          <div className="flex flex-col gap-2 border-t border-line pt-2">
            <ActionLog
              name="profile-export"
              run={exportRun}
              profile={profile.name}
              title={`Export of ${profile.name}`}
            />
            <p className="m-0 text-[0.75rem] text-ink-muted">
              Archive: <code className="type-data break-all">{archive}</code> (settings only: no
              history, secrets or channel state).
            </p>
          </div>
        ) : null}
      </article>
    </li>
  );
}
