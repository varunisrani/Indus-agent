// IMPORT (ours): make a profile from an exported archive on this machine.

import { useMutation } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";
import { api } from "../../lib/api.ts";
import { errorText, useToast } from "../../shell/Toasts.tsx";
import { Button, inputClass, Notice } from "../../ui/controls.tsx";
import { cx } from "../../ui/cx.ts";
import { Modal } from "../../ui/Dialog.tsx";
import { labelClass } from "./ProfileEditorDialog.tsx";
import { normaliseProfileName, profileNameProblem } from "./profileName.ts";
import { useProfilesChanged } from "./useProfilesChanged.ts";

export function ImportProfileDialog({
  existing,
  onClose,
}: {
  existing: readonly string[];
  onClose: () => void;
}) {
  const toast = useToast();
  const changed = useProfilesChanged();
  const ids = useId();
  const [archive, setArchive] = useState("");
  const [name, setName] = useState("");
  const [problem, setProblem] = useState("");
  const run = useMutation({
    mutationFn: () => api.profile.importArchive(archive.trim(), normaliseProfileName(name)),
    onSuccess: (reply) => {
      toast.show(`Profile ${reply.name} imported.`, "success");
      changed();
      onClose();
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
    <Modal
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title="Import a profile"
      description="Settings only: an export carries no history, secrets or channel state."
    >
      <form className="flex flex-col gap-4 py-1" onSubmit={submit} noValidate>
        <div className="flex flex-col gap-1.5">
          <label htmlFor={`${ids}-archive`} className={labelClass}>
            Archive path
          </label>
          <input
            id={`${ids}-archive`}
            className={cx(inputClass, "w-full font-mono")}
            placeholder="/path/to/profile.zip"
            value={archive}
            onChange={(e) => setArchive(e.currentTarget.value)}
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <label htmlFor={`${ids}-name`} className={labelClass}>
            Name (optional)
          </label>
          <input
            id={`${ids}-name`}
            className={cx(inputClass, "w-full")}
            value={name}
            onChange={(e) => setName(e.currentTarget.value)}
          />
        </div>
        {problem ? <Notice tone="danger">{problem}</Notice> : null}
        <div className="flex justify-end">
          <Button type="submit" tone="primary" size="sm" disabled={run.isPending}>
            Import
          </Button>
        </div>
      </form>
    </Modal>
  );
}
