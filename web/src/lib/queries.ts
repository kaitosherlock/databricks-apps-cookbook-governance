"use client";

/**
 * Data fetching, with caching that the Streamlit build had to hand-roll.
 *
 * Three problems this fixes outright:
 *
 * 1. In Streamlit every widget interaction re-ran the script, so typing in the
 *    reason box re-issued the SCIM principal search and the metadata read. Here
 *    a query is keyed by its inputs and only refetches when they change.
 * 2. A collapsed expander still executed its body, so a closed glossary cost an
 *    API call on every rerun. React renders what is mounted, and the tabs below
 *    mount lazily.
 * 3. After a write, the old code cleared every cache by hand. Here the mutation
 *    names what it invalidated.
 */
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryOptions,
} from "@tanstack/react-query";

import { ApiError, api, type TargetRef } from "./api";
import type { ObjectKind } from "./types";

/** One place that builds cache keys, so an invalidation cannot miss a reader. */
export const keys = {
  session: ["session"] as const,
  catalogs: ["catalogs"] as const,
  schemas: (catalog: string) => ["schemas", catalog] as const,
  objects: (catalog: string, schema: string, kind: ObjectKind) =>
    ["objects", catalog, schema, kind] as const,
  search: (catalog: string, schema: string, term: string, kinds: ObjectKind[]) =>
    ["search", catalog, schema, term, [...kinds].sort().join(",")] as const,
  detail: (target: TargetRef | null) =>
    ["detail", target?.kind, target?.catalog, target?.schema, target?.name] as const,
  grants: (target: TargetRef | null, effective: boolean) =>
    ["grants", effective, target?.kind, target?.catalog, target?.schema, target?.name] as const,
  privileges: (target: TargetRef | null) =>
    ["privileges", target?.kind, target?.catalog, target?.schema, target?.name] as const,
  revocable: (target: TargetRef | null) =>
    ["revocable", target?.kind, target?.catalog, target?.schema, target?.name] as const,
  principals: (term: string) => ["principals", term] as const,
  activity: ["activity"] as const,
  diagnostics: ["diagnostics"] as const,
};

/**
 * Refusals are answers, not outages.
 *
 * Retrying a PERMISSION_DENIED just spends the operator's time and hammers a
 * workspace that has already given its verdict.
 */
const NEVER_RETRY = new Set([
  "PERMISSION_DENIED",
  "FORBIDDEN_ROLE",
  "FORBIDDEN_SCOPE",
  "WRITES_DISABLED",
  "NO_IDENTITY",
  "NOT_FOUND",
  "INVALID_INPUT",
  "INVALID_NAME",
  "INVALID_PRIVILEGE",
  "CAPABILITY_UNAVAILABLE",
  "NOT_CONFIGURED",
  "STALE_PLAN",
  "ALREADY_SATISFIED",
]);

export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && NEVER_RETRY.has(error.code)) return false;
  return failureCount < 2;
}

type Options<T> = Omit<UseQueryOptions<T, ApiError>, "queryKey" | "queryFn">;

export function useSession(options?: Options<Awaited<ReturnType<typeof api.session>>>) {
  return useQuery({
    queryKey: keys.session,
    queryFn: api.session,
    retry: shouldRetry,
    staleTime: 60_000,
    ...options,
  });
}

export function useCatalogs() {
  return useQuery({ queryKey: keys.catalogs, queryFn: api.catalogs, retry: shouldRetry });
}

export function useSchemas(catalog: string) {
  return useQuery({
    queryKey: keys.schemas(catalog),
    queryFn: () => api.schemas(catalog),
    enabled: Boolean(catalog),
    retry: shouldRetry,
  });
}

export function useObjects(catalog: string, schema: string, kind: ObjectKind) {
  return useQuery({
    queryKey: keys.objects(catalog, schema, kind),
    queryFn: () => api.objects(catalog, schema, kind),
    enabled: Boolean(catalog && schema),
    retry: shouldRetry,
  });
}

export function useSearch(
  catalog: string,
  schema: string,
  term: string,
  kinds: ObjectKind[],
) {
  return useQuery({
    queryKey: keys.search(catalog, schema, term, kinds),
    queryFn: () => api.search(catalog, schema, term, kinds),
    enabled: Boolean(catalog && schema && kinds.length),
    retry: shouldRetry,
  });
}

export function useDetail(target: TargetRef | null) {
  return useQuery({
    queryKey: keys.detail(target),
    queryFn: () => api.detail(target!),
    enabled: Boolean(target),
    retry: shouldRetry,
  });
}

export function useGrants(target: TargetRef | null, effective: boolean) {
  return useQuery({
    queryKey: keys.grants(target, effective),
    queryFn: () => api.grants(target!, effective),
    enabled: Boolean(target),
    retry: shouldRetry,
  });
}

export function usePrivileges(target: TargetRef | null) {
  return useQuery({
    queryKey: keys.privileges(target),
    queryFn: () => api.privileges(target!),
    enabled: Boolean(target),
    retry: shouldRetry,
    // The catalogue for one object type does not move during a session.
    staleTime: 10 * 60_000,
  });
}

export function useRevocable(target: TargetRef | null, enabled = true) {
  return useQuery({
    queryKey: keys.revocable(target),
    queryFn: () => api.revocable(target!),
    enabled: Boolean(target) && enabled,
    retry: shouldRetry,
  });
}

export function usePrincipalSearch(term: string) {
  const cleaned = term.trim();
  return useQuery({
    queryKey: keys.principals(cleaned),
    queryFn: () => api.searchPrincipals(cleaned),
    // Two characters is the backend's own floor; below it the call is pointless.
    enabled: cleaned.length >= 2,
    retry: shouldRetry,
    staleTime: 30_000,
  });
}

export function useActivity() {
  return useQuery({ queryKey: keys.activity, queryFn: api.activity, retry: shouldRetry });
}

export function useDiagnostics() {
  return useQuery({
    queryKey: keys.diagnostics,
    queryFn: api.diagnostics,
    retry: shouldRetry,
  });
}

// -- mutations -------------------------------------------------------------
export function usePlanChange() {
  return useMutation({ mutationFn: api.planChange, retry: false });
}

export function useApplyPlan(target: TargetRef | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ planId, confirmation }: { planId: string; confirmation: string }) =>
      api.applyPlan(planId, confirmation),
    // A write is never retried automatically. If the outcome is unknown the
    // operator must re-read state first; retrying could apply it twice.
    retry: false,
    onSettled: () => {
      // Settled, not success: a failed or unknown write also makes every cached
      // view of this object untrustworthy.
      void client.invalidateQueries({ queryKey: keys.grants(target, true) });
      void client.invalidateQueries({ queryKey: keys.grants(target, false) });
      void client.invalidateQueries({ queryKey: keys.revocable(target) });
      void client.invalidateQueries({ queryKey: keys.activity });
      void client.invalidateQueries({ queryKey: keys.session });
    },
  });
}

export function useRefreshAll() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: api.refreshSession,
    onSettled: () => client.invalidateQueries(),
  });
}

export function useProbe() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (includeSql: boolean) => api.probe(includeSql),
    onSettled: () => client.invalidateQueries({ queryKey: keys.diagnostics }),
  });
}
