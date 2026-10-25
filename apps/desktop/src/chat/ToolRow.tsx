// One tool call (W52-g look): a single faint line — the tool's kind icon, then "verb target" as
// plain words — that opens into a thin shell. A terminal call shows its command in a `$` box with
// an exit badge, then its output; other tools show labelled argument and output sections. Red only
// for an explicit failure or a refused call (see toolOutcome). A settled row can be hidden for this
// view (client state only).

import {
  ChevronDown,
  CircleAlert,
  Copy,
  FilePen,
  FileText,
  Globe,
  LoaderCircle,
  Search,
  SquareTerminal,
  Wrench,
  X,
} from "lucide-react";
import { type ReactNode, useState } from "react";
import { copyText } from "../app/clipboard.ts";
import { type MessageKey, useT } from "../i18n/index.tsx";
import type { ToolPart } from "./messageStream.ts";
import { type ToolVerb, terminalResult, toolImages, toolSummary } from "./toolSummary.ts";

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

function KindIcon(props: { verb: ToolVerb }) {
  const common = { "aria-hidden": true, size: 14, className: "tool-icon" } as const;
  switch (props.verb) {
    case "run":
      return <SquareTerminal {...common} />;
    case "read":
      return <FileText {...common} />;
    case "write":
    case "edit":
      return <FilePen {...common} />;
    case "search":
      return <Search {...common} />;
    case "fetch":
      return <Globe {...common} />;
    case "use":
      return <Wrench {...common} />;
  }
}

function Glyph(props: { state: ToolPart["state"]; verb: ToolVerb }) {
  if (props.state === "running") {
    return <LoaderCircle aria-hidden="true" size={14} className="tool-icon tool-spin" />;
  }
  if (props.state === "failed") {
    return <CircleAlert aria-hidden="true" size={14} className="tool-icon" />;
  }
  return <KindIcon verb={props.verb} />;
}

function Section(props: { label: string; children: ReactNode }) {
  return (
    <section className="tool-section">
      <h4 className="tool-section-label">{props.label}</h4>
      {props.children}
    </section>
  );
}

function OutputBlock(props: { text: string; failed: boolean }) {
  const t = useT();
  const [all, setAll] = useState(false);
  const { text } = props;
  if (text === "") return <p className="tool-empty">{t("tool.noOutput")}</p>;
  const shown = all || text.length <= OUTPUT_PREVIEW ? text : text.slice(0, OUTPUT_PREVIEW);
  return (
    <>
      <pre className="tool-pre" data-failed={props.failed || undefined}>
        {shown}
      </pre>
      {shown.length < text.length && (
        <button type="button" className="text-button" onClick={() => setAll(true)}>
          {t("tool.showAll")}
        </button>
      )}
    </>
  );
}

function ToolBody(props: { part: ToolPart; command: string | null }) {
  const { part } = props;
  const t = useT();
  const result = part.result ?? "";
  const terminal = props.command !== null ? terminalResult(part.result) : null;
  const images = toolImages(part.result);
  const failed = part.state === "failed";
  return (
    <div className="tool-body">
      {props.command !== null ? (
        <div className="tool-command">
          <span className="tool-prompt" aria-hidden="true">
            $
          </span>
          <code className="tool-command-text">{props.command}</code>
          {terminal?.exitCode !== null && terminal?.exitCode !== undefined && (
            <span className="tool-exit" data-zero={terminal.exitCode === 0 || undefined}>
              {t("tool.exit", { code: terminal.exitCode })}
            </span>
          )}
        </div>
      ) : (
        part.args !== null &&
        Object.keys(part.args).length > 0 && (
          <Section label={t("tool.arguments")}>
            <pre className="tool-pre">{JSON.stringify(part.args, null, 2)}</pre>
          </Section>
        )
      )}
      {terminal !== null ? (
        (terminal.output !== "" || terminal.error !== "") && (
          <Section label={t("tool.output")}>
            <OutputBlock
              text={[terminal.output, terminal.error].filter((x) => x !== "").join("\n")}
              failed={failed}
            />
          </Section>
        )
      ) : part.state !== "running" || result !== "" ? (
        <Section label={t("tool.output")}>
          <OutputBlock text={result} failed={failed} />
        </Section>
      ) : null}
      {images.map((src) => (
        <img
          key={src}
          src={src}
          alt={t("tool.image", { name: part.name })}
          className="tool-image"
        />
      ))}
    </div>
  );
}

export function ToolRow(props: { part: ToolPart; settled?: boolean }) {
  const { part } = props;
  const t = useT();
  const [open, setOpen] = useState(false);
  const [hidden, setHidden] = useState(false);
  if (hidden) return null;
  const summary = toolSummary(part.name, part.args);
  const command = summary.verb === "run" && summary.target !== "" ? fullCommand(part) : null;
  const settled = props.settled ?? part.state !== "running";
  return (
    <div
      className="tool-row"
      data-ui="transcript-tool-row"
      data-state={part.state}
      data-open={open || undefined}
    >
      <div className="tool-head">
        <button
          type="button"
          className="tool-summary"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
        >
          <Glyph state={part.state} verb={summary.verb} />
          <span className="tool-label">
            <span className="tool-verb">{t(VERB_KEYS[summary.verb], { name: part.name })}</span>
            {summary.target !== "" && (
              <>
                {" "}
                <span className="tool-target">{summary.target}</span>
              </>
            )}
          </span>
          <span className="sr-only">{t(STATE_KEYS[part.state])}</span>
          <ChevronDown aria-hidden="true" size={12} className="tool-caret" />
          {part.state === "running" && part.progress !== null && (
            <span className="tool-progress">{part.progress}</span>
          )}
          {part.durationMs !== null && part.state !== "running" && summary.verb !== "run" && (
            <span className="tool-duration">
              {t("tool.duration", { seconds: (part.durationMs / 1000).toFixed(1) })}
            </span>
          )}
        </button>
        {open && copyOf(part) !== "" && (
          <button
            type="button"
            className="desk-icon-btn tool-copy"
            aria-label={t("tool.copy")}
            onClick={() => void copyText(copyOf(part))}
          >
            <Copy aria-hidden="true" size={13} />
          </button>
        )}
        {settled && (
          <button
            type="button"
            className="tool-dismiss"
            aria-label={t("tool.dismiss")}
            onClick={() => setHidden(true)}
          >
            <X aria-hidden="true" size={12} />
          </button>
        )}
      </div>
      {open && <ToolBody part={part} command={command} />}
    </div>
  );
}

/** What "Copy output" puts on the clipboard: the terminal output, else the raw result. */
function copyOf(part: ToolPart): string {
  const terminal = terminalResult(part.result);
  if (terminal !== null) return terminal.output || terminal.error;
  return part.result ?? "";
}

/** The whole command (the summary clips it to one short line). */
function fullCommand(part: ToolPart): string {
  const args = part.args ?? {};
  for (const key of ["command", "cmd"]) {
    const value = args[key];
    if (typeof value === "string" && value.trim() !== "") return value;
  }
  return toolSummary(part.name, part.args).target;
}
