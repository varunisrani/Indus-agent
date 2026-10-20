// Small display helpers shared by the pages.

/** "3 min ago" style text for a Unix time in seconds (or an ISO string). */
export function relativeTime(value: number | string | null, now: number = Date.now()): string {
  if (value === null) return "never";
  const at = typeof value === "number" ? value * 1000 : Date.parse(value);
  if (Number.isNaN(at)) return "unknown";
  const seconds = Math.round((now - at) / 1000);
  const future = seconds < 0;
  const span = Math.abs(seconds);
  const [amount, unit] =
    span < 60
      ? [span, "s"]
      : span < 3600
        ? [Math.floor(span / 60), "min"]
        : span < 86_400
          ? [Math.floor(span / 3600), "h"]
          : [Math.floor(span / 86_400), "d"];
  if (span < 5) return "just now";
  return future ? `in ${amount} ${unit}` : `${amount} ${unit} ago`;
}

export function formatCount(value: number): string {
  return new Intl.NumberFormat("en").format(value);
}

export function formatCost(value: number): string {
  return `$${value.toFixed(value < 1 ? 4 : 2)}`;
}

export function formatBytes(value: number | null): string {
  if (value === null) return "";
  if (value < 1024) return `${value} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let size = value / 1024;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(1)} ${units[unit]}`;
}

/** Offer `data` as a file download (object URL; a no-op where the browser lacks one). */
export function saveJson(data: unknown, filename: string): void {
  if (typeof URL.createObjectURL !== "function") return;
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}
