// A thin notice strip on the composer's top edge (W52-g): a small icon, a lead phrase, a muted hint
// and text actions on the right. Used for our own notices (the restored draft); we have no usage
// statistics to ask about (D-16.49), so no consent prompt uses it.

import type { ReactNode } from "react";

export interface NoticeAction {
  readonly label: string;
  readonly run: () => void;
  /** A quiet action (drawn muted, e.g. "Dismiss"). */
  readonly quiet?: boolean;
}

export function NoticeStrip(props: {
  icon?: ReactNode;
  lead: string;
  hint?: string;
  actions?: readonly NoticeAction[];
}) {
  return (
    <div className="notice-strip" role="status">
      {props.icon !== undefined && <span className="notice-strip-icon">{props.icon}</span>}
      <span className="notice-strip-text">
        <span className="notice-strip-lead">{props.lead}</span>
        {props.hint !== undefined && <span className="notice-strip-hint">{props.hint}</span>}
      </span>
      {(props.actions ?? []).map((action) => (
        <button
          key={action.label}
          type="button"
          className="notice-strip-action"
          data-quiet={action.quiet || undefined}
          onClick={action.run}
        >
          {action.label}
        </button>
      ))}
    </div>
  );
}
