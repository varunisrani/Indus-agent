// Bot avatars (P24-31): our own geometric face drawn from a seed (deterministic: the same seed
// always gives the same face), an uploaded image or portrait (a data URL the daemon checked), or
// the profile's pet (P24-19 draws it; here a placeholder mark). No artwork from anywhere else.

import type { ReactNode } from "react";

export type Avatar =
  | { kind: "face"; seed: string }
  | { kind: "upload" | "portrait"; data: string }
  | { kind: "pet" };

export const MAX_AVATAR_BYTES = 256 * 1024;
export const AVATAR_TYPES = ["image/png", "image/jpeg", "image/webp"] as const;

/** 32 pseudo-random bytes from a seed (FNV-1a, re-mixed per byte). */
export function seedBytes(seed: string): number[] {
  const out: number[] = [];
  let h = 2166136261;
  for (let i = 0; i < seed.length; i += 1) {
    h ^= seed.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  for (let i = 0; i < 32; i += 1) {
    h ^= i + 0x9e;
    h = Math.imul(h, 16777619) >>> 0;
    out.push(h & 0xff);
  }
  return out;
}

export interface FaceParts {
  hue: number;
  shape: "circle" | "square" | "hexagon";
  eyes: "dots" | "lines" | "rings";
  mouth: "smile" | "flat" | "open";
  eyeGap: number;
}

export function faceParts(seed: string): FaceParts {
  const b = seedBytes(seed);
  return {
    hue: Math.round(((b[0] as number) / 255) * 359),
    shape: (["circle", "square", "hexagon"] as const)[(b[1] as number) % 3] as FaceParts["shape"],
    eyes: (["dots", "lines", "rings"] as const)[(b[2] as number) % 3] as FaceParts["eyes"],
    mouth: (["smile", "flat", "open"] as const)[(b[3] as number) % 3] as FaceParts["mouth"],
    eyeGap: 8 + ((b[4] as number) % 5),
  };
}

export function BotFace(props: { seed: string; size?: number; label?: string }) {
  const parts = faceParts(props.seed);
  const size = props.size ?? 40;
  const fill = `hsl(${parts.hue} 55% 55%)`;
  const ink = `hsl(${parts.hue} 40% 18%)`;
  let body: ReactNode;
  if (parts.shape === "circle") body = <circle cx="24" cy="24" r="22" fill={fill} />;
  else if (parts.shape === "square")
    body = <rect x="3" y="3" width="42" height="42" rx="10" fill={fill} />;
  else body = <polygon points="24,2 44,13 44,35 24,46 4,35 4,13" fill={fill} />;
  const left = 24 - parts.eyeGap;
  const right = 24 + parts.eyeGap;
  const eye = (x: number) =>
    parts.eyes === "dots" ? (
      <circle key={x} cx={x} cy="20" r="2.6" fill={ink} />
    ) : parts.eyes === "lines" ? (
      <line
        key={x}
        x1={x - 3}
        y1="20"
        x2={x + 3}
        y2="20"
        stroke={ink}
        strokeWidth="2.4"
        strokeLinecap="round"
      />
    ) : (
      <circle key={x} cx={x} cy="20" r="3" fill="none" stroke={ink} strokeWidth="1.8" />
    );
  const mouth =
    parts.mouth === "smile" ? (
      <path
        d="M16 30 Q24 37 32 30"
        fill="none"
        stroke={ink}
        strokeWidth="2.4"
        strokeLinecap="round"
      />
    ) : parts.mouth === "flat" ? (
      <line x1="17" y1="32" x2="31" y2="32" stroke={ink} strokeWidth="2.4" strokeLinecap="round" />
    ) : (
      <ellipse cx="24" cy="32" rx="5" ry="3.4" fill={ink} />
    );
  return (
    <svg
      className="bots-avatar"
      viewBox="0 0 48 48"
      width={size}
      height={size}
      role="img"
      aria-label={props.label ?? "bot"}
      data-face={`${parts.shape}-${parts.eyes}-${parts.mouth}`}
    >
      {body}
      {eye(left)}
      {eye(right)}
      {mouth}
    </svg>
  );
}

export function AvatarView(props: { avatar: Avatar; fallbackSeed: string; label: string }) {
  const { avatar } = props;
  if (avatar.kind === "upload" || avatar.kind === "portrait") {
    return <img className="bots-avatar" src={avatar.data} alt={props.label} />;
  }
  if (avatar.kind === "pet") {
    return (
      <span className="bots-avatar" role="img" aria-label={props.label} data-avatar="pet">
        <BotFace seed={`pet:${props.fallbackSeed}`} label={props.label} />
      </span>
    );
  }
  const seed = avatar.kind === "face" ? avatar.seed : "";
  return <BotFace seed={seed || props.fallbackSeed} label={props.label} />;
}

/** Read an image file into a data URL the daemon will accept, or say why not. */
export function readAvatarFile(file: Blob & { type: string }): Promise<string> {
  if (!(AVATAR_TYPES as readonly string[]).includes(file.type)) {
    return Promise.reject(new Error("use a PNG, JPEG or WebP image"));
  }
  if (file.size > MAX_AVATAR_BYTES)
    return Promise.reject(new Error("the image is larger than 256 KiB"));
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("the image could not be read"));
    reader.onload = () => resolve(String(reader.result));
    reader.readAsDataURL(file);
  });
}
