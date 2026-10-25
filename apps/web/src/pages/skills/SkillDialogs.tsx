// The New skill editor and the Learn a skill dialog (W52-c). Our backend has no route that writes a
// skill or starts a chat with a prompt yet, so the editor composes SKILL.md and copies it for the
// person to save, and the learn dialog explains where to ask instead (decisions W52-c-7).

import { useId, useState } from "react";
import { useToast } from "../../shell/Toasts.tsx";
import { Button, Field, Input, Notice, Textarea } from "../../ui/controls.tsx";
import { DialogActions, Modal } from "../../ui/Dialog.tsx";

/** Starter text for a new SKILL.md (front matter, a heading and one line of guidance). */
export function skillTemplate(name: string): string {
  const slug = name.trim() || "my-skill";
  return [
    "---",
    `name: ${slug}`,
    "description: One line on what this skill does and when to reach for it.",
    "---",
    "",
    `# ${slug}`,
    "",
    "Write the steps the agent should follow, in the order it should take them.",
    "",
  ].join("\n");
}

/** Where a skill's file lives under the Indus home. */
export function skillPath(name: string, category: string): string {
  const parts = ["~/.indus/skills", category.trim(), name.trim() || "<name>", "SKILL.md"];
  return parts.filter(Boolean).join("/");
}

const NAME = /^[a-z0-9][a-z0-9._-]{0,63}$/;

export function SkillEditorDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const toast = useToast();
  const ids = useId();
  const [name, setName] = useState("");
  const [category, setCategory] = useState("");
  const [body, setBody] = useState(() => skillTemplate(""));
  const [problem, setProblem] = useState("");

  const create = async () => {
    if (!NAME.test(name.trim()))
      return setProblem(
        "Name the skill in lower case: letters, digits, dots, dashes or underscores.",
      );
    if (!body.trim()) return setProblem("Write the skill's SKILL.md.");
    setProblem("");
    try {
      await navigator.clipboard.writeText(body);
      toast.show(`Copied. Save it as ${skillPath(name, category)}.`, "success");
      onClose();
    } catch {
      setProblem("The browser refused the clipboard; copy the text by hand.");
    }
  };

  return (
    <Modal
      open={open}
      size="lg"
      onOpenChange={(next) => (next ? undefined : onClose())}
      title="New skill"
      description="Front matter plus markdown. Saved in your skills folder, it is ready for the agent and for cron jobs."
    >
      <div className="flex flex-col gap-4 py-1">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label="Name" htmlFor={`${ids}-name`}>
            <Input
              id={`${ids}-name`}
              autoFocus
              placeholder="my-skill"
              value={name}
              onChange={(e) => setName(e.currentTarget.value)}
            />
          </Field>
          <Field label="Category (optional)" htmlFor={`${ids}-category`}>
            <Input
              id={`${ids}-category`}
              placeholder="devops"
              value={category}
              onChange={(e) => setCategory(e.currentTarget.value)}
            />
          </Field>
        </div>
        <Field label="SKILL.md" htmlFor={`${ids}-body`}>
          <Textarea
            id={`${ids}-body`}
            spellCheck={false}
            className="min-h-80 max-h-[55vh] resize-y text-[0.75rem] leading-relaxed"
            value={body}
            onChange={(e) => setBody(e.currentTarget.value)}
          />
        </Field>
        <p className="type-serif m-0 text-[0.75rem] text-ink-soft">
          Saving from the dashboard is not available yet: Create skill copies the file so you can
          save it as <code className="type-data text-[0.75rem]">{skillPath(name, category)}</code>.
        </p>
        {problem ? <Notice tone="danger">{problem}</Notice> : null}
        <DialogActions>
          <Button tone="ghost" size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button tone="primary" size="sm" className="uppercase" onClick={() => void create()}>
            Create skill
          </Button>
        </DialogActions>
      </div>
    </Modal>
  );
}

export function LearnSkillDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const ids = useId();
  const [task, setTask] = useState("");
  const [name, setName] = useState("");
  const [source, setSource] = useState("");
  return (
    <Modal
      open={open}
      size="lg"
      onOpenChange={(next) => (next ? undefined : onClose())}
      title="Learn a skill"
      description="Describe a task; the agent works out the steps and writes them up as a skill."
    >
      <div className="flex flex-col gap-4 py-1">
        <Field label="What should it learn?" htmlFor={`${ids}-task`}>
          <Textarea
            id={`${ids}-task`}
            autoFocus
            className="min-h-24"
            placeholder="e.g. how we cut a release of the docs site"
            value={task}
            onChange={(e) => setTask(e.currentTarget.value)}
          />
        </Field>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label="Name (optional)" htmlFor={`${ids}-name`}>
            <Input
              id={`${ids}-name`}
              placeholder="release-docs"
              value={name}
              onChange={(e) => setName(e.currentTarget.value)}
            />
          </Field>
          <Field label="Source (optional)" htmlFor={`${ids}-source`}>
            <Input
              id={`${ids}-source`}
              placeholder="a URL or a folder"
              value={source}
              onChange={(e) => setSource(e.currentTarget.value)}
            />
          </Field>
        </div>
        <p className="type-serif m-0 text-[0.75rem] text-ink-soft">
          Starting this from the dashboard is not available yet. Ask the agent in a chat to learn it
          as a skill instead.
        </p>
        <DialogActions>
          <Button tone="ghost" size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button tone="primary" size="sm" className="uppercase" disabled>
            Start learning
          </Button>
        </DialogActions>
      </div>
    </Modal>
  );
}
