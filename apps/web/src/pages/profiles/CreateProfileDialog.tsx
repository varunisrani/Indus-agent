// CREATE: a quick new-profile modal (name, clone source, description, model, two advanced options).
// The step-by-step builder (`/profiles/new`) stays the way to add skills and MCP servers too.

import { useMutation } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";
import { api } from "../../lib/api.ts";
import type { ProfileCreate } from "../../lib/restTypes.ts";
import { errorText, useToast } from "../../shell/Toasts.tsx";
import { Button, Checkbox, inputClass, Notice, Select } from "../../ui/controls.tsx";
import { cx } from "../../ui/cx.ts";
import { Modal } from "../../ui/Dialog.tsx";
import { labelClass, ModelFields } from "./ProfileEditorDialog.tsx";
import { normaliseProfileName, profileNameProblem } from "./profileName.ts";
import { useProfilesChanged } from "./useProfilesChanged.ts";

export const NAME_RULE = "Lower-case letters, digits, '-' or '_'; at most 64 characters.";

export function CreateProfileDialog({
  existing,
  defaultSource,
  onClose,
}: {
  existing: readonly string[];
  /** The clone source picked at first (the default profile). */
  defaultSource: string;
  onClose: () => void;
}) {
  const toast = useToast();
  const changed = useProfilesChanged();
  const ids = useId();
  const [name, setName] = useState("");
  const [cloneFrom, setCloneFrom] = useState(defaultSource);
  const [description, setDescription] = useState("");
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [cloneAll, setCloneAll] = useState(false);
  const [noSkills, setNoSkills] = useState(false);
  const [problem, setProblem] = useState("");
  const nameProblem = profileNameProblem(name, existing);
  const cloning = cloneFrom !== "";

  const create = useMutation({
    mutationFn: async () => {
      const id = normaliseProfileName(name);
      const body: ProfileCreate = { name: id };
      if (cloning) body.clone_from = cloneFrom;
      if (cloning && cloneAll) body.clone_all = true;
      if (!cloning && noSkills) body.no_skills = true;
      if (description.trim()) body.description = description.trim();
      const reply = await api.profile.create(body);
      if (provider && model.trim()) {
        try {
          await api.profile.setModel(reply.name, provider, model.trim());
        } catch (error) {
          toast.show(`Profile made, but its model was not set: ${errorText(error)}`, "error");
        }
      }
      return reply;
    },
    onSuccess: (reply) => {
      toast.show(`Profile ${reply.name} created.`, "success");
      changed();
      onClose();
    },
    onError: (error) => setProblem(errorText(error)),
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (nameProblem === null && !create.isPending) create.mutate();
  };

  return (
    <Modal
      open
      size="md"
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title="New profile"
      description="A separate home with its own settings, keys, skills and history."
    >
      <form className="flex flex-col gap-4 py-1" onSubmit={submit} noValidate>
        <div className="flex flex-col gap-1.5">
          <label htmlFor={`${ids}-name`} className={labelClass}>
            Name
          </label>
          <input
            id={`${ids}-name`}
            // biome-ignore lint/a11y/noAutofocus: the dialog exists to type this name
            autoFocus
            className={cx(inputClass, "w-full")}
            placeholder="coder"
            aria-invalid={name !== "" && nameProblem !== null}
            value={name}
            onChange={(e) => setName(e.currentTarget.value)}
          />
          <p
            className={cx(
              "m-0 text-[0.75rem]",
              name && nameProblem ? "text-danger" : "text-ink-soft",
            )}
          >
            {name && nameProblem ? nameProblem : NAME_RULE}
          </p>
        </div>
        <div className="flex flex-col gap-1.5">
          <label htmlFor={`${ids}-clone`} className={labelClass}>
            Clone from
          </label>
          <Select
            id={`${ids}-clone`}
            className="w-full"
            value={cloneFrom}
            onChange={(e) => setCloneFrom(e.currentTarget.value)}
          >
            <option value="">Fresh profile</option>
            {existing.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </Select>
        </div>
        <div className="flex flex-col gap-1.5">
          <label htmlFor={`${ids}-description`} className={labelClass}>
            Description (optional)
          </label>
          <textarea
            id={`${ids}-description`}
            className={cx(inputClass, "min-h-16 w-full py-2")}
            value={description}
            onChange={(e) => setDescription(e.currentTarget.value)}
          />
        </div>
        <fieldset className="m-0 flex flex-col gap-1.5 border-0 p-0">
          <legend className={cx(labelClass, "mb-1.5 p-0")}>Model (optional)</legend>
          <ModelFields
            provider={provider}
            model={model}
            onProvider={setProvider}
            onModel={setModel}
          />
        </fieldset>
        <fieldset className="m-0 flex flex-col gap-2 border-0 border-t border-line p-0 pt-4">
          <legend className="type-label float-left mb-2 w-full p-0 text-[0.6875rem] text-ink-soft">
            Advanced options
          </legend>
          <label
            htmlFor={`${ids}-clone-all`}
            className={cx(
              "flex items-center gap-2 text-[0.8125rem]",
              !cloning && "cursor-not-allowed opacity-50",
            )}
          >
            <Checkbox
              id={`${ids}-clone-all`}
              checked={cloneAll}
              disabled={!cloning}
              onChange={(e) => setCloneAll(e.currentTarget.checked)}
            />
            Clone everything (the whole profile folder)
          </label>
          <label
            htmlFor={`${ids}-no-skills`}
            className={cx(
              "flex items-center gap-2 text-[0.8125rem]",
              cloning && "cursor-not-allowed opacity-50",
            )}
          >
            <Checkbox
              id={`${ids}-no-skills`}
              checked={noSkills}
              disabled={cloning}
              onChange={(e) => setNoSkills(e.currentTarget.checked)}
            />
            No bundled skills
          </label>
        </fieldset>
        {problem ? <Notice tone="danger">{problem}</Notice> : null}
        <div className="flex justify-end">
          <Button
            type="submit"
            tone="primary"
            size="sm"
            disabled={nameProblem !== null || create.isPending}
          >
            {create.isPending ? "Creating…" : "Create"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
