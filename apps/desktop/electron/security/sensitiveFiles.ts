// Files whose contents never go into a renderer (P20-14, shared with the fs IPC of P20-29): env
// files, key stores, credential files and the Indus home's own secret records. The REST server
// refuses them too (web/routes/files.py `is_sensitive`); main refuses them first, so a renderer
// bug or a hostile page cannot even ask.

const SECRET_NAMES = new Set([
  ".envrc",
  ".git-credentials",
  ".netrc",
  ".pgpass",
  ".npmrc",
  ".pypirc",
  "auth.json",
  "auth.json.lock",
  "credentials",
  "credentials.json",
  "secrets.json",
  "secrets.index.json",
  "dashboard.json",
  "serve.json",
  "gateway.sock.path",
  "id_rsa",
  "id_dsa",
  "id_ecdsa",
  "id_ed25519",
  "id_ecdsa_sk",
  "id_ed25519_sk",
  "known_hosts",
  "authorized_keys",
]);

const SECRET_SUFFIXES = [
  ".pem",
  ".key",
  ".p12",
  ".pfx",
  ".jks",
  ".keystore",
  ".kdbx",
  ".keychain",
  ".keychain-db",
  ".gpg",
];

const SECRET_DIRS = new Set([".ssh", ".gnupg", ".aws", "pairing", "mcp-tokens", "mcp_auth"]);

/** True for a path whose file (or a folder on the way) holds secrets; case-insensitive. */
export function isSensitivePath(raw: string): boolean {
  const normal = raw.replace(/\\/g, "/");
  const parts = normal.split("/").filter((part) => part !== "");
  const name = (parts.at(-1) ?? "").toLowerCase();
  if (name === ".env" || name.startsWith(".env.") || name.endsWith(".env")) return true;
  if (SECRET_NAMES.has(name)) return true;
  if (SECRET_SUFFIXES.some((suffix) => name.endsWith(suffix))) return true;
  return parts.some((part) => SECRET_DIRS.has(part.toLowerCase()));
}
