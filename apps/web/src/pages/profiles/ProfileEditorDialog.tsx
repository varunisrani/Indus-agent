// One editor dialog per profile action (W52-pc-2): the model, the description or the persona.

import { useMutation, useQuery } from "@tanstack/react-query";
import { type FormEvent, useEffect, useId, useState } from "react";
import { api } from "../../lib/api.ts";
import type { ProfileSummary } from "../../lib/restTypes.ts";
import { errorText, useToast } from "../../shell/Toasts.tsx";
import { Button, inputClass, Notice, Select } from "../../ui/controls.tsx";
import { cx } from "../../ui/cx.ts";
import { Modal } from "../../ui/Dialog.tsx";
import { useProfilesChanged } from "./useProfilesChanged.ts";

export type EditorKind = "model" | "description" | "persona";

const TITLES: Record<EditorKind, string> = {
  model: "Change model",
  description: "Edit description",
  persona: "Edit persona",
};

const DESCRIPTIONS: Record<EditorKind, string> = {
  model: "New sessions in this profile start on this model.",
  description: "A line about what this profile is for, shown on its card.",
  persona: "How the agent behaves in this profile; added to every session's instructions.",
};

export const labelClass = "type-label text-[0.6875rem] tracking-[0.1em] text-ink";

/** A provider select and a model name input on one row (no per-provider model list exists). */
export function ModelFields({
  provider,
  model,
  onProvider,
  onModel,
}: {
  provider: string;
  model: string;
  onProvider: (next: string) => void;
  onModel: (next: string) => void;
}) {
  const ids = useId();
  const options = useQuery({
    queryKey: ["profiles", "model-options"],
    queryFn: () => api.model.options(),
  });
  const providers = options.data?.providers ?? [];
  const known = providers.some((p) => p.name === provider);
  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
      <div className="flex flex-col gap-1.5">
        <label htmlFor={`${ids}-provider`} className={labelClass}>
          Provider
        </label>
        <Select
          id={`${ids}-provider`}
          value={provider}
          onChange={(e) => onProvider(e.currentTarget.value)}
          className="w-full"
        >
          <option value="">Pick a provider</option>
          {provider && !known ? <option value={provider}>{provider}</option> : null}
          {providers.map((p) => (
            <option key={p.name} value={p.name}>
              {p.label}
              {p.configured ? "" : " (no key yet)"}
            </option>
          ))}
        </Select>
      </div>
      <div className="flex flex-col gap-1.5">
        <label htmlFor={`${ids}-model`} className={labelClass}>
          Model
        </label>
        <input
          id={`${ids}-model`}
          className={cx(inputClass, "w-full")}
          placeholder="model name"
          value={model}
          onChange={(e) => onModel(e.currentTarget.value)}
        />
      </div>
    </div>
  );
}

export function ProfileEditorDialog({
  profile,
  kind,
  onClose,
}: {
  profile: ProfileSummary;
  kind: EditorKind;
  onClose: () => void;
}) {
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
    enabled: kind === "persona",
    gcTime: 0,
  });
  useEffect(() => {
    if (loaded.data) setPersona(loaded.data.content);
  }, [loaded.data]);

  const save = useMutation({
    mutationFn: () => {
      if (kind === "model")
        return api.profile.setModel(profile.name, provider.trim(), model.trim());
      if (kind === "description") return api.profile.setDescription(profile.name, description);
      return api.profile.setPersona(profile.name, persona);
    },
    onSuccess: () => {
      const what = kind === "model" ? "Model" : kind === "description" ? "Description" : "Persona";
      toast.show(`${what} of ${profile.name} saved.`, "success");
      changed();
      onClose();
    },
    onError: (error) => setProblem(errorText(error)),
  });

  const blocked =
    save.isPending ||
    (kind === "model" && (!provider.trim() || !model.trim())) ||
    (kind === "persona" && loaded.isPending);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!blocked) save.mutate();
  };

  return (
    <Modal
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title={`${TITLES[kind]} · ${profile.name}`}
      description={DESCRIPTIONS[kind]}
    >
      <form className="flex flex-col gap-4 py-1" onSubmit={submit} noValidate>
        {kind === "model" ? (
          <ModelFields
            provider={provider}
            model={model}
            onProvider={setProvider}
            onModel={setModel}
          />
        ) : null}
        {kind === "description" ? (
          <div className="flex flex-col gap-1.5">
            <label htmlFor={`${ids}-description`} className={labelClass}>
              Description
            </label>
            <textarea
              id={`${ids}-description`}
              className={cx(inputClass, "min-h-24 w-full py-2")}
              value={description}
              onChange={(e) => setDescription(e.currentTarget.value)}
            />
          </div>
        ) : null}
        {kind === "persona" ? (
          <div className="flex flex-col gap-1.5">
            <label htmlFor={`${ids}-persona`} className={labelClass}>
              Persona (PERSONA.md)
            </label>
            <textarea
              id={`${ids}-persona`}
              className={cx(inputClass, "min-h-[280px] w-full py-2 font-mono text-[0.8125rem]")}
              value={persona}
              disabled={loaded.isPending}
              onChange={(e) => setPersona(e.currentTarget.value)}
            />
          </div>
        ) : null}
        {problem ? <Notice tone="danger">{problem}</Notice> : null}
        <div className="flex justify-end">
          <Button type="submit" tone="primary" size="sm" disabled={blocked}>
            Save
          </Button>
        </div>
      </form>
    </Modal>
  );
}
