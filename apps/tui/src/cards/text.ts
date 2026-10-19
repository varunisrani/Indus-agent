// Plain-text helpers for cards: control-character cleanup and a column-aware hard wrap.

/** Columns a code point takes in a terminal: 2 for East Asian wide and emoji, else 1. */
export function columnsOf(codePoint: number): number {
  const wide =
    (codePoint >= 0x1100 && codePoint <= 0x115f) ||
    (codePoint >= 0x2e80 && codePoint <= 0xa4cf) ||
    (codePoint >= 0xac00 && codePoint <= 0xd7a3) ||
    (codePoint >= 0xf900 && codePoint <= 0xfaff) ||
    (codePoint >= 0xfe30 && codePoint <= 0xfe4f) ||
    (codePoint >= 0xff00 && codePoint <= 0xff60) ||
    (codePoint >= 0xffe0 && codePoint <= 0xffe6) ||
    (codePoint >= 0x1f300 && codePoint <= 0x1faff) ||
    (codePoint >= 0x20000 && codePoint <= 0x3fffd);
  return wide ? 2 : 1;
}

// Escape sequences (CSI, OSC) and the remaining C0 controls except tab and newline.
const CONTROL = new RegExp(
  [
    "\\u001b\\[[0-9;?]*[ -/]*[@-~]",
    "\\u001b\\][^\\u0007]*\\u0007",
    "[\\u0000-\\u0008\\u000b-\\u001f\\u007f]",
  ].join("|"),
  "g",
);

/** Drop escape sequences and control bytes (tabs become two spaces, newlines stay). */
export const cleanText = (text: string): string =>
  text.replace(/\t/g, "  ").replace(/\r\n?/g, "\n").replace(CONTROL, "");

/** Cut one line into pieces no wider than `width` columns, never dropping a character. */
export function hardWrap(line: string, width: number): string[] {
  const limit = Math.max(1, width);
  const pieces: string[] = [];
  let current = "";
  let used = 0;
  for (const char of line) {
    const cols = columnsOf(char.codePointAt(0) ?? 32);
    if (used + cols > limit && current !== "") {
      pieces.push(current);
      current = "";
      used = 0;
    }
    current += char;
    used += cols;
  }
  pieces.push(current);
  return pieces;
}

/** Wrap every line of `text`; an empty line stays one empty row. */
export const wrapText = (text: string, width: number): string[] =>
  cleanText(text)
    .split("\n")
    .flatMap((line) => hardWrap(line, width));
