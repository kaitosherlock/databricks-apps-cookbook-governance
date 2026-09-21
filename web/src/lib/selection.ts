"use client";

/**
 * The selected object lives in the URL, not in memory.
 *
 * The Streamlit build kept it in session state so it survived navigation. Put
 * it in the query string instead and it survives a reload, a bookmark and a
 * link pasted into a ticket - which is what an operator actually does when they
 * want a colleague to look at the same grant.
 */
import { useCallback, useMemo } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

import type { LevelKind, ObjectKind } from "./types";
import type { TargetRef } from "./api";

export const OBJECT_KINDS: ObjectKind[] = ["table", "volume", "function", "model"];
export const LEVEL_KINDS: LevelKind[] = ["catalog", "schema", ...OBJECT_KINDS];

export const KIND_LABELS: Record<LevelKind, string> = {
  catalog: "Catalog",
  schema: "Schema",
  table: "Bảng / View",
  volume: "Volume",
  function: "Hàm",
  model: "Mô hình",
};

export interface Selection {
  kind: LevelKind;
  catalog: string;
  schema: string;
  name: string;
}

const isLevelKind = (value: string | null): value is LevelKind =>
  !!value && (LEVEL_KINDS as string[]).includes(value);

export function useSelection() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();

  const selection = useMemo<Selection>(() => {
    const kind = params.get("kind");
    return {
      kind: isLevelKind(kind) ? kind : "table",
      catalog: params.get("catalog") ?? "",
      schema: params.get("schema") ?? "",
      name: params.get("name") ?? "",
    };
  }, [params]);

  const update = useCallback(
    (changes: Partial<Selection>, options?: { replace?: boolean }) => {
      const next = new URLSearchParams(params.toString());
      for (const [key, value] of Object.entries(changes)) {
        if (value) next.set(key, value);
        else next.delete(key);
      }
      const url = `${pathname}?${next.toString()}`;
      if (options?.replace) router.replace(url);
      else router.push(url);
    },
    [params, pathname, router],
  );

  /** A complete, addressable object, or null when the choice is unfinished. */
  const target = useMemo<TargetRef | null>(() => {
    const { kind, catalog, schema, name } = selection;
    if (!catalog) return null;
    if (kind === "catalog") return { kind, catalog };
    if (!schema) return null;
    if (kind === "schema") return { kind, catalog, schema };
    if (!name) return null;
    return { kind, catalog, schema, name };
  }, [selection]);

  return { selection, target, update };
}

export function targetHref(path: string, target: TargetRef): string {
  const params = new URLSearchParams({
    kind: target.kind,
    catalog: target.catalog,
  });
  if (target.schema) params.set("schema", target.schema);
  if (target.name) params.set("name", target.name);
  // The export is built with trailingSlash, so links carry one too and the
  // server resolves the route directly instead of falling back to the shell.
  const base = path.endsWith("/") ? path : `${path}/`;
  return `${base}?${params.toString()}`;
}

export function fullName(target: TargetRef): string {
  return [target.catalog, target.schema, target.name].filter(Boolean).join(".");
}
