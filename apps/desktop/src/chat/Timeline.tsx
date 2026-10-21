// The timeline rail: one marker per prompt; a click jumps to that turn.

import { useT } from "../i18n/index.tsx";
import type { UserItem } from "./messageStream.ts";

const LABEL_MAX = 60;

function label(text: string): string {
  const line = text.replace(/\s+/g, " ").trim();
  return line.length > LABEL_MAX ? `${line.slice(0, LABEL_MAX - 1)}…` : line;
}

export function Timeline(props: { prompts: readonly UserItem[]; onJump: (key: string) => void }) {
  const t = useT();
  if (props.prompts.length < 2) return null;
  return (
    <nav className="timeline" aria-label={t("transcript.timeline")}>
      <ol>
        {props.prompts.map((prompt) => (
          <li key={prompt.key}>
            <button
              type="button"
              className="timeline-marker"
              aria-label={t("transcript.jumpTo", { text: label(prompt.text) })}
              onClick={() => props.onJump(prompt.key)}
            />
          </li>
        ))}
      </ol>
    </nav>
  );
}
