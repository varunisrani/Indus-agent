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

/** Compact token figure: raw below 1,000, then one decimal with K or M ("3.7K", "1.2M"). */
export function formatTokens(value: number): string {
  if (!Number.isFinite(value)) return "0";
  const size = Math.abs(value);
  if (size >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (size >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return String(Math.round(value));
}

const SHORT_MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
] as const;

/** "Oct 24" for "2026-10-24"; anything that is not such a date is returned unchanged. */
export function formatShortDay(day: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day);
  const month = match ? SHORT_MONTHS[Number(match[2]) - 1] : undefined;
  return match && month ? `${month} ${Number(match[3])}` : day;
}

/** "2.6 GB" style: one decimal and a short unit (1,024 steps), as the System and Files pages show. */
export function formatSize(value: number | null): string {
  if (value === null) return "-";
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = value / 1024;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(1)} ${units[unit]}`;
}

const DATE_TIME = new Intl.DateTimeFormat("en-US", { dateStyle: "medium", timeStyle: "short" });

/** "Oct 24, 2026, 1:26 PM" for a Unix time in seconds; "-" when unknown. */
export function formatDateTime(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "-";
  return DATE_TIME.format(new Date(seconds * 1000));
}
