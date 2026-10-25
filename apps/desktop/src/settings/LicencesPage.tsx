// Settings → About → Licences (W46, F-2): the third-party notices the packager puts beside the app
// (`THIRD_PARTY_NOTICES.md`, the same file the repository and the Docker image carry). Main reads
// the file (`about.notices`); the page shows it as plain text, never as HTML.

import { useEffect, useState } from "react";
import { useT } from "../i18n/index.tsx";
import { attempt, useSettings } from "./context.tsx";
import { PageBody } from "./primitives.tsx";

type Notices = { readonly state: "loading" } | { readonly state: "done"; text: string | null };

export function LicencesPage() {
  const t = useT();
  const { bridge, focusRow } = useSettings();
  const source = bridge?.about;
  const [notices, setNotices] = useState<Notices>({ state: "loading" });
  useEffect(() => {
    let live = true;
    setNotices({ state: "loading" });
    void attempt(source ? () => source.notices() : null).then((answer) => {
      if (!live) return;
      const text = answer.ok && answer.value.text !== "" ? answer.value.text : null;
      setNotices({ state: "done", text });
    });
    return () => {
      live = false;
    };
  }, [source]);
  // restyled on the page primitives (W52-j): intro line, then the notices block
  return (
    <PageBody intro={t("settings.licences.body")}>
      <div
        className="sb-about"
        data-row="licences"
        data-highlighted={focusRow === "licences" ? "true" : undefined}
      >
        {notices.state === "loading" ? (
          <p className="settings-row-note" role="status">
            {t("settings.licences.loading")}
          </p>
        ) : notices.text === null ? (
          <p className="settings-row-note" role="status">
            {t("settings.licences.missing")}
          </p>
        ) : (
          <pre
            className="settings-notices"
            data-ui="settings-licences-text"
            // biome-ignore lint/a11y/noNoninteractiveTabindex: a long scrolling region keyboard users must reach
            tabIndex={0}
          >
            {notices.text}
          </pre>
        )}
      </div>
    </PageBody>
  );
}
