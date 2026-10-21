// Masks secret-shaped text before it reaches desktop.log, the logs viewer or a diagnostics bundle.
// The same shapes the Python side masks (src/indus/foundation/redact.py): vendor key prefixes,
// authorization headers, bearer tokens, JWTs, user-info in URLs, secret-named query parameters and
// assignments, our WebSocket ticket subprotocol and the serve session-token header. A false
// positive costs a log line its detail; a false negative leaks a credential, so the patterns lean
// wide.

const MASK = "[redacted]";

const EDGE = "(?<![A-Za-z0-9_-])";

const VENDOR_KEYS = new RegExp(
  `${EDGE}(?:${[
    "sk-ant-[A-Za-z0-9_-]{16,400}",
    "sk-or-[A-Za-z0-9_-]{16,400}",
    "sk-[A-Za-z0-9_-]{16,400}",
    "gh[opsur]_[A-Za-z0-9]{20,255}",
    "github_pat_[A-Za-z0-9_]{20,255}",
    "xox[abpr]-[A-Za-z0-9-]{10,255}",
    "AKIA[0-9A-Z]{16}",
    "AIza[0-9A-Za-z_-]{30,60}",
    "hf_[A-Za-z0-9]{20,255}",
    "glpat-[A-Za-z0-9_-]{20,255}",
  ].join("|")})`,
  "g",
);

const SECRET_WORDS = "token|secret|password|passwd|api[_-]?key|apikey|auth|credential|ticket";

// Order matters: header and assignment forms keep their names and lose only their values.
const RULES: ReadonlyArray<readonly [RegExp, (match: string, ...groups: string[]) => string]> = [
  // `Authorization: Bearer x`, `Proxy-Authorization: Basic x`
  [
    /\b((?:proxy-)?authorization\s*[:=]\s*["']?(?:bearer|basic|token|bot)\s+)[^\s"',;]{4,4096}/gi,
    (_m, head) => `${head}${MASK}`,
  ],
  // `X-Indus-Session-Token: x` (and any x-*-token header)
  [/\b(x-[a-z0-9-]*token\s*[:=]\s*["']?)[^\s"',;]{4,4096}/gi, (_m, head) => `${head}${MASK}`],
  [/\b(bearer\s+)[A-Za-z0-9._~+/=-]{12,4096}/gi, (_m, head) => `${head}${MASK}`],
  [
    /(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{6,4096}\.[A-Za-z0-9_-]{6,8192}\.[A-Za-z0-9_-]{6,4096}/g,
    () => MASK,
  ],
  // the single-use WebSocket ticket, wherever it is quoted
  [/\b(indus-gateway-ticket\.)[A-Za-z0-9_-]{1,512}/gi, (_m, head) => `${head}${MASK}`],
  // user-info in a URL: keep the user name, drop the password
  [
    /\b([A-Za-z][A-Za-z0-9+.-]{0,31}:\/\/[^\s/:@?#]{1,256}:)[^\s/@?#]{1,256}@/g,
    (_m, head) => `${head}${MASK}@`,
  ],
  // ?token=x&ticket=y
  [
    new RegExp(
      `([?&](?:[A-Za-z0-9_.-]*(?:${SECRET_WORDS})[A-Za-z0-9_.-]*)=)[^&#\\s"'<>]{1,4096}`,
      "gi",
    ),
    (_m, head) => `${head}${MASK}`,
  ],
  // "api_key": "x"
  [
    new RegExp(
      `(["'][A-Za-z0-9_.-]*(?:${SECRET_WORDS})[A-Za-z0-9_.-]*["']\\s{0,4}:\\s{0,4}["'])[^"'\\n]{1,4096}(["'])`,
      "gi",
    ),
    (_m, head, tail) => `${head}${MASK}${tail}`,
  ],
  // OPENAI_API_KEY=x, password = "x"
  [
    new RegExp(
      `((?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]*(?:${SECRET_WORDS})[A-Za-z0-9_.-]*\\s{0,4}=\\s{0,4})("[^"\\n]{1,4096}"|'[^'\\n]{1,4096}'|[^\\s"'&,;]{1,4096})`,
      "gi",
    ),
    (_m, head) => `${head}${MASK}`,
  ],
  [VENDOR_KEYS, () => MASK],
];

/** Mask every secret-shaped part of `text`; everything else is kept as written. */
export function redactSecrets(text: string): string {
  let out = text;
  for (const [pattern, replace] of RULES) {
    out = out.replace(pattern, replace as (substring: string, ...args: string[]) => string);
  }
  return out;
}

/** Literal values (a token main minted) masked wherever they appear, then the patterns. */
export function redactKnown(text: string, values: Iterable<string>): string {
  let out = text;
  for (const value of values) {
    if (value.length >= 8) out = out.split(value).join(MASK);
  }
  return redactSecrets(out);
}
