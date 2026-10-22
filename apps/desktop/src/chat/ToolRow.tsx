// One tool call: a summary line (verb + target, state, duration) that expands to its arguments,
// output and images. Red only for an explicit failure (see toolOutcome).

import { CheckCircle2, ChevronRight, CircleDashed, CircleSlash, XCircle } from "lucide-react";
import { useState } from "react";
import { type MessageKey, useT } from "../i18n/index.tsx";
import type { ToolPart } from "./messageStream.ts";
import { type ToolVerb, toolImages, toolSummary } from "./toolSummary.ts";

const OUTPUT_PREVIEW = 4_000;

const VERB_KEYS: Readonly<Record<ToolVerb, MessageKey>> = {
  run: "tool.verb.run",
  read: "tool.verb.read",
  write: "tool.verb.write",
  edit: "tool.verb.edit",
  search: "tool.verb.search",
  fetch: "tool.verb.fetch",
  use: "tool.verb.use",
};

const STATE_KEYS: Readonly<Record<ToolPart["state"], MessageKey>> = {
  running: "tool.state.running",
  ok: "tool.state.ok",
  failed: "tool.state.failed",
  neutral: "tool.state.neutral",
};

function StateIcon(props: { state: ToolPart["state"] }) {
  switch (props.state) {
    case "running":
      return <CircleDashed aria-hidden="true" className="tool-icon tool-spin" size={14} />;
    case "ok":
      return <CheckCircle2 aria-hidden="true" className="tool-icon" size={14} />;
    case "failed":
      return <XCircle aria-hidden="true" className="tool-icon" size={14} />;
    case "neutral":
      return <CircleSlash aria-hidden="true" className="tool-icon" size={14} />;
  }
}

export function ToolRow(props: { part: ToolPart }) {
  const { part } = props;
  const t = useT();
  const [open, setOpen] = useState(false);
  const [all, setAll] = useState(false);
  const summary = toolSummary(part.name, part.args);
  const result = part.result ?? "";
  const images = open ? toolImages(part.result) : [];
  const shown = all || result.length <= OUTPUT_PREVIEW ? result : result.slice(0, OUTPUT_PREVIEW);
  return (
    <div className="tool-row" data-ui="transcript-tool-row" data-state={part.state}>
      <button
        type="button"
        className="tool-summary"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <ChevronRight aria-hidden="true" size={14} className="tool-chevron" />
        <StateIcon state={part.state} />
        <span className="tool-verb">{t(VERB_KEYS[summary.verb], { name: part.name })}</span>
        {summary.target !== "" && <code className="tool-target">{summary.target}</code>}
        <span className="sr-only">{t(STATE_KEYS[part.state])}</span>
        {part.state === "running" && part.progress !== null && (
          <span className="tool-progress">{part.progress}</span>
        )}
        {part.durationMs !== null && part.state !== "running" && (
          <span className="tool-duration">
            {t("tool.duration", { seconds: (part.durationMs / 1000).toFixed(1) })}
          </span>
        )}
      </button>
      {open && (
        <div className="tool-detail">
          {part.args !== null && Object.keys(part.args).length > 0 && (
            <section>
              <h4>{t("tool.arguments")}</h4>
              <pre>{JSON.stringify(part.args, null, 2)}</pre>
            </section>
          )}
          <section>
            <h4>{t("tool.output")}</h4>
            {result === "" ? (
              <p className="tool-empty">{t("tool.noOutput")}</p>
            ) : (
              <pre>{shown}</pre>
            )}
            {shown.length < result.length && (
              <button type="button" className="text-button" onClick={() => setAll(true)}>
                {t("tool.showAll")}
              </button>
            )}
          </section>
          {images.map((src) => (
            <img
              key={src}
              src={src}
              alt={t("tool.image", { name: part.name })}
              className="tool-image"
            />
          ))}
        </div>
      )}
    </div>
  );
}
