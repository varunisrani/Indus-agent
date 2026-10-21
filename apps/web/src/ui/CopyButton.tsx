// Copy a value to the clipboard; says "Copied" for a moment, or that the browser refused.

import { useState } from "react";
import { Button } from "./controls.tsx";

export function CopyButton({ value, label }: { value: string; label: string }) {
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
  return (
    <Button onClick={() => void copy()} aria-label={label}>
      {said || "Copy"}
    </Button>
  );
}
