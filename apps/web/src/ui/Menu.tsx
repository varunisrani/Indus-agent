// A dropdown action menu (W52-e-1, added to the foundation): a square ghost trigger, then a
// square panel under it, right-aligned to the trigger, with the canvas fill, a 1 px strong rule and
// a soft shadow. Items are uppercase rows with a leading icon. Escape, a press outside, or running
// an item closes it; opening another menu closes this one (the press lands outside).

import { EllipsisVertical } from "lucide-react";
import {
  type ButtonHTMLAttributes,
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useRef,
  useState,
} from "react";
import { cx } from "./cx.ts";
import { useDismiss } from "./useDismiss.ts";

const CloseContext = createContext<() => void>(() => {});

export function Menu({
  label = "Actions",
  icon = <EllipsisVertical />,
  children,
  className = "",
}: {
  /** The trigger's accessible name and tooltip. */
  label?: string;
  icon?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, root, close);
  return (
    <div ref={root} className={cx("relative shrink-0", className)}>
      <button
        type="button"
        aria-label={label}
        title={label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="inline-flex size-7 items-center justify-center text-ink transition-colors hover:bg-hover [&_svg]:size-4"
      >
        {icon}
      </button>
      {open ? (
        <div
          role="menu"
          aria-label={label}
          className="absolute top-full right-0 z-30 mt-1 flex min-w-[12.5rem] flex-col border border-line-strong bg-canvas py-1 shadow-xl [animation:indus-fade-in_120ms_ease-out]"
        >
          <CloseContext.Provider value={close}>{children}</CloseContext.Provider>
        </div>
      ) : null}
    </div>
  );
}

export function MenuItem({
  icon,
  danger = false,
  onSelect,
  children,
  className = "",
  ...rest
}: Omit<ButtonHTMLAttributes<HTMLButtonElement>, "onClick" | "onSelect"> & {
  icon?: ReactNode;
  danger?: boolean;
  /** Runs the item; the menu closes first. */
  onSelect: () => void;
}) {
  const close = useContext(CloseContext);
  return (
    <button
      type="button"
      role="menuitem"
      onClick={() => {
        close();
        onSelect();
      }}
      className={cx(
        "flex min-h-[1.875rem] w-full items-center gap-2.5 px-3 py-1 text-left text-[0.75rem] tracking-[0.08em] uppercase transition-colors disabled:cursor-not-allowed disabled:opacity-40 [&_svg]:size-4 [&_svg]:shrink-0",
        danger ? "text-danger hover:bg-danger-wash" : "text-ink hover:bg-hover",
        className,
      )}
      {...rest}
    >
      {icon}
      <span className="min-w-0 flex-1">{children}</span>
    </button>
  );
}

export function MenuDivider() {
  return <hr className="my-1 border-0 border-t border-line" />;
}
