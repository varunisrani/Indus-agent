// Copy a value to the clipboard; says "Copied" for a moment, or that the browser refused.
// `look`: "button" (outlined typewriter, default), "icon" (square icon-only), "text" (a small
// quiet word after a command chip).

import { Check, Copy } from "lucide-react";
import { useState } from "react";
import { Button, IconButton } from "./controls.tsx";

export function CopyButton({
  value,
  label,
  look = "button",
}: {
  value: string;
  label: string;
  look?: "button" | "icon" | "text";
}) {
  const [said, setSaid] = useState<"" | "Copied" | "Copy failed">("");
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setSaid("Copied");
    } catch {
      setSaid("Copy failed");
    }
    setTimeout(() => setSaid(""), 2_000);
  };
  if (look === "icon") {
    return (
      <IconButton label={label} title={said || label} onClick={() => void copy()}>
        {said === "Copied" ? <Check /> : <Copy />}
      </IconButton>
    );
  }
  if (look === "text") {
    return (
      <button
        type="button"
        aria-label={label}
        onClick={() => void copy()}
        className="type-button px-1 text-[0.6875rem] text-ink-muted hover:text-ink"
      >
        {said || "Copy"}
      </button>
    );
  }
  return (
    <Button onClick={() => void copy()} aria-label={label}>
      {said || "Copy"}
    </Button>
  );
}
