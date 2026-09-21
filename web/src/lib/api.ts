/**
 * The only place this app talks to the server.
 *
 * Every failure arrives as an {@link ApiError} carrying the backend's stable
 * code, its Vietnamese message and a correlation id. Screens branch on `code`
 * rather than on HTTP status, which keeps the UI in step with ucg/errors.py
 * instead of with a transport detail.
 */
import type {
  ActivityFeed,
  ApplyOutcome,
  AssetDetail,
  AssetRef,
  Diagnostics,
  GovernanceErrorBody,
  GrantView,
  Listing,
  LevelKind,
  ObjectKind,
  PlanPreview,
  PrincipalSearch,
  PrivilegeCatalogue,
  ProbeResult,
  RevocableView,
  Session,
} from "./types";

export class ApiError extends Error {
  readonly body: GovernanceErrorBody;
  readonly status: number;

  constructor(body: GovernanceErrorBody, status: number) {
    super(body.message);
    this.name = "ApiError";
    this.body = body;
    this.status = status;
  }

  get code() {
    return this.body.code;
  }

  /** True when a change may already have taken effect. The UI must say so. */
  get outcomeUnknown() {
    return this.body.outcome_unknown;
  }
}

const UNREACHABLE: GovernanceErrorBody = {
  code: "UPSTREAM_ERROR",
  message: "Không liên lạc được với máy chủ ứng dụng.",
  next_step: "Kiểm tra kết nối mạng rồi tải lại trang.",
  correlation_id: "",
  detail: "network",
  outcome_unknown: false,
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...init,
      headers: {
        Accept: "application/json",
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...init?.headers,
      },
      // The Databricks Apps proxy supplies identity headers; there is no
      // cookie or token for this client to hold or send.
      credentials: "same-origin",
    });
  } catch {
    // A mutation that never reached the server is genuinely unknown in outcome
    // only if it might have been sent. A connection that failed to open was not.
    throw new ApiError(UNREACHABLE, 0);
  }

  if (!response.ok) {
    let body: GovernanceErrorBody | undefined;
    try {
      const parsed = (await response.json()) as { error?: GovernanceErrorBody };
      body = parsed?.error;
    } catch {
      body = undefined;
    }
    throw new ApiError(
      body ?? {
        ...UNREACHABLE,
        code: "INTERNAL",
        message: `Máy chủ trả về lỗi ${response.status}.`,
        detail: String(response.status),
      },
      response.status,
    );
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function query(params: Record<string, string | number | boolean | string[] | undefined>) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === "") continue;
    if (Array.isArray(value)) {
      for (const entry of value) search.append(key, entry);
    } else {
      search.set(key, String(value));
    }
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

/** Identity of an object, as every endpoint expects it. */
export interface TargetRef {
  kind: LevelKind;
  catalog: string;
  schema?: string;
  name?: string;
}

const targetQuery = (target: TargetRef) => ({
  kind: target.kind,
  catalog: target.catalog,
  schema: target.schema ?? "",
  name: target.name ?? "",
});

export const api = {
  session: () => request<Session>("/session"),
  refreshSession: () => request<{ ok: boolean }>("/session/refresh", { method: "POST" }),

  catalogs: () => request<Listing<AssetRef>>("/assets/catalogs"),
  schemas: (catalog: string) =>
    request<Listing<AssetRef>>(`/assets/schemas${query({ catalog })}`),
  objects: (catalog: string, schema: string, kind: ObjectKind) =>
    request<Listing<AssetRef>>(`/assets/objects${query({ catalog, schema, kind })}`),
  search: (catalog: string, schema: string, term: string, kinds: ObjectKind[]) =>
    request<Listing<AssetRef>>(`/assets/search${query({ catalog, schema, term, kinds })}`),
  detail: (target: TargetRef) =>
    request<AssetDetail>(`/assets/detail${query(targetQuery(target))}`),

  grants: (target: TargetRef, effective: boolean) =>
    request<GrantView>(`/grants${query({ ...targetQuery(target), effective })}`),
  privileges: (target: TargetRef) =>
    request<PrivilegeCatalogue>(`/grants/privileges${query(targetQuery(target))}`),
  revocable: (target: TargetRef) =>
    request<RevocableView>(`/grants/revocable${query(targetQuery(target))}`),

  planChange: (input: {
    target: TargetRef;
    action: "grant" | "revoke";
    principal: string;
    privileges: string[];
    reason: string;
  }) =>
    request<PlanPreview>("/grants/plan", {
      method: "POST",
      body: JSON.stringify({
        ...targetQuery(input.target),
        action: input.action,
        principal: input.principal,
        privileges: input.privileges,
        reason: input.reason,
      }),
    }),

  /**
   * Confirm a stored preview.
   *
   * Only the handle and the typed confirmation are sent. The change itself is
   * read back from the server's copy of the plan, so this call cannot be made
   * to apply anything other than what was previewed.
   */
  applyPlan: (planId: string, confirmation: string) =>
    request<ApplyOutcome>("/grants/apply", {
      method: "POST",
      body: JSON.stringify({ plan_id: planId, confirmation }),
    }),

  cancelPlan: (planId: string) =>
    request<{ ok: boolean }>(`/grants/plan/${encodeURIComponent(planId)}`, {
      method: "DELETE",
    }),

  searchPrincipals: (term: string) =>
    request<PrincipalSearch>(`/principals/search${query({ term })}`),

  activity: () => request<ActivityFeed>("/activity"),

  diagnostics: () => request<Diagnostics>("/diagnostics"),
  probe: (includeSql: boolean) =>
    request<ProbeResult>(`/diagnostics/probe${query({ include_sql: includeSql })}`, {
      method: "POST",
    }),
};
