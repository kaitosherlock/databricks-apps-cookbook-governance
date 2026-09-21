import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** Absolute UTC timestamps only: "2 minutes ago" is useless in an audit trail. */
export function formatUtc(iso: string): string {
  if (!iso) return "";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return `${parsed.toISOString().slice(0, 19).replace("T", " ")} UTC`;
}

export function shortId(value: string, length = 12): string {
  return (value || "").slice(0, length);
}
