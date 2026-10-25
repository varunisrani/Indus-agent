// The `?` help card (W52-h): shown while the composer holds exactly `?`. Common commands from the
// catalogue and the keys the composer really has (rebindable ones read from the key bindings).

import { type MessageKey, useT } from "../../i18n/index.tsx";
import { effectiveBindings } from "../../settings/keybindings.ts";
import { keyCaps } from "../../ui/components.tsx";
import { shortDescription } from "../slash.ts";

export const HELP_COMMANDS = ["help", "new", "resume", "model", "reasoning", "stop"] as const;

function Caps(props: { keys: readonly string[] }) {
  return (
    <span className="composer-help-keys">
      {props.keys.map((key, index) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: caps can repeat (↑ ↓ are distinct anyway)
        <kbd key={`${key}-${index}`} className="desk-kbd">
          {key}
        </kbd>
      ))}
    </span>
  );
}

export function ComposerHelp(props: {
  mac: boolean;
  overrides?: Readonly<Record<string, string>>;
}) {
  const t = useT();
  const focus = effectiveBindings(props.overrides ?? {})["composer.focus"] ?? "";
  const hotkeys: ReadonlyArray<{ keys: string[]; label: MessageKey }> = [
    { keys: ["@"], label: "help.mention" },
    { keys: ["/"], label: "help.slash" },
    { keys: ["?"], label: "help.self" },
    { keys: ["Enter", props.mac ? "⇧" : "Shift", "Enter"], label: "help.send" },
    { keys: ["↑", "↓"], label: "help.history" },
    { keys: ["Esc"], label: "help.escape" },
    ...(focus === "" ? [] : [{ keys: keyCaps(focus, props.mac), label: "help.focus" as const }]),
  ];
  return (
    <section className="desk-popover composer-help" aria-label={t("help.label")}>
      <div className="desk-popover-head composer-help-head">{t("help.commands")}</div>
      <ul className="composer-help-list">
        {HELP_COMMANDS.map((name) => (
          <li key={name} className="composer-help-row">
            <code className="composer-help-cmd">/{name}</code>
            <span className="composer-help-text">{shortDescription(name)}</span>
          </li>
        ))}
      </ul>
      <div className="desk-popover-head composer-help-head">{t("help.hotkeys")}</div>
      <ul className="composer-help-list">
        {hotkeys.map((row) => (
          <li key={row.label} className="composer-help-row">
            <Caps keys={row.keys} />
            <span className="composer-help-text">{t(row.label)}</span>
          </li>
        ))}
      </ul>
      <p className="composer-help-foot">
        <code className="composer-help-cmd">/help</code> {t("help.footer")}
      </p>
    </section>
  );
}
