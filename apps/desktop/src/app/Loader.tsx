// The one busy indicator: an animated element with an accessible name, never "Loading…" text.

import { useT } from "../i18n/index.tsx";

export function Loader(props: { label?: string; size?: "sm" | "md" }) {
  const t = useT();
  return (
    <span
      role="status"
      aria-label={props.label ?? t("common.working")}
      className={`indus-loader indus-loader-${props.size ?? "md"}`}
    >
      <span aria-hidden="true" />
      <span aria-hidden="true" />
      <span aria-hidden="true" />
    </span>
  );
}
