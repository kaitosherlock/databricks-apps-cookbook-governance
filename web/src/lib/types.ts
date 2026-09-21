/**
 * The API contract, mirrored from governance-app/api/routers.
 *
 * These are hand-written rather than generated because the set is small and the
 * names carry meaning that a generator would flatten. The rule when changing a
 * router: change this file in the same commit, or the compiler stops telling
 * the truth.
 */

/** Stable error codes from ucg/errors.py. The UI branches on these, not on HTTP status. */
export type ErrorCode =
  | "INVALID_INPUT"
  | "INVALID_NAME"
  | "INVALID_PRIVILEGE"
  | "NOT_FOUND"
  | "ALREADY_SATISFIED"
  | "FORBIDDEN_ROLE"
  | "FORBIDDEN_SCOPE"
  | "WRITES_DISABLED"
  | "NO_IDENTITY"
  | "SELF_APPROVAL"
  | "PERMISSION_DENIED"
  | "NOT_CONFIGURED"
  | "CAPABILITY_UNAVAILABLE"
  | "WAREHOUSE_REQUIRED"
  | "STALE_PLAN"
  | "CONFLICT"
  | "TIMEOUT_UNKNOWN"
  | "UPSTREAM_ERROR"
  | "INTERNAL";

export interface GovernanceErrorBody {
  code: ErrorCode;
  message: string;
  next_step: string;
  correlation_id: string;
  detail: string;
  /** True when the change may or may not have been applied. Never retry blindly. */
  outcome_unknown: boolean;
}

/**
 * Whether a list is the whole answer.
 *
 * `partial_permission` is the one that matters: it means the executing identity
 * was not allowed to see everything, which is a different statement from "there
 * is nothing here" and must never be rendered the same way.
 */
export type Completeness = "complete" | "truncated" | "partial_permission";

export interface Listing<T> {
  items: T[];
  completeness: Completeness;
  observed_at: string;
  note: string;
}

// -- session ---------------------------------------------------------------
export type Role = "viewer" | "auditor" | "steward" | "access_admin" | "platform_admin";

export interface Session {
  actor: {
    email: string;
    label: string;
    /** True only when Databricks confirmed the identity with the user's own token. */
    verified: boolean;
    trust_note: string;
    role: Role;
    role_label: string;
    role_description: string;
  };
  execution: {
    identity: "app_service_principal" | "local_profile" | "end_user";
    label: string;
    running_as: string;
    /** True when what you see is what the service principal may see, not what you may. */
    explains_visibility: boolean;
  };
  workspace: { host: string };
  environment: {
    name: string;
    label: string;
    tone: "danger" | "info" | "unknown";
    high_risk: boolean;
    warning?: string;
  };
  mode: { writable: boolean; label: string; reason: string };
  scope: { catalogs: string[]; label: string; unbounded_writes: boolean };
  permissions: Record<string, { label: string; allowed: boolean; reason: string; code: string }>;
  pending_plans: { plan_id: string; target: string; summary: string }[];
  unresolved_operations: number;
}

// -- assets ----------------------------------------------------------------
export type ObjectKind = "table" | "volume" | "function" | "model";
export type LevelKind = "catalog" | "schema" | ObjectKind;

export interface AssetRef {
  kind: LevelKind;
  name: string;
  full_name: string;
  catalog: string;
  schema: string;
  owner: string;
  comment: string;
  sub_type: string;
  type_label: string;
  browse_only: boolean;
}

export interface AssetDetail {
  target: {
    kind: LevelKind;
    catalog: string;
    schema: string;
    name: string;
    full_name: string;
    key: string;
    label: string;
  };
  owner: string;
  comment: string;
  sub_type: string;
  type_label: string;
  created: string;
  updated: string;
  updated_by: string;
  storage_location: string;
  columns: Record<string, unknown>[];
  properties: Record<string, string>;
  constraints: string[];
  summary_fields: { label: string; value: string }[];
  protection: {
    row_filter: { function_name?: string } | null;
    column_masks: { column: string; function: string }[];
  };
  pipeline_managed: boolean;
  managed_by: string;
  raw: Record<string, unknown>;
}

// -- grants ----------------------------------------------------------------
export type GrantSource = "direct" | "inherited" | "ownership";
export type Risk = "low" | "medium" | "high";

export interface GrantRow {
  principal: string;
  principal_type: string;
  privilege: string;
  privilege_label: string;
  risk: Risk;
  source: GrantSource;
  source_label: string;
  inherited_from: string;
  inherited_from_type: string;
  granted_at: string;
  /** False for inherited and ownership rows. The UI must not offer a revoke. */
  revocable_here: boolean;
  /** Databricks named a privilege the pinned SDK cannot. The grant is still real. */
  unreadable: boolean;
}

export interface GrantView {
  rows: GrantRow[];
  owner: string;
  completeness: Completeness;
  observed_at: string;
  has_unreadable: boolean;
  caveat: string;
  unreadable_note: string;
}

export interface PrivilegeInfo {
  code: string;
  label: string;
  display: string;
  explanation: string;
  risk: Risk;
}

export interface PrivilegeCatalogue {
  privileges: PrivilegeInfo[];
  traversal_note: string;
  inheritance_note: string;
}

export interface RevocableView {
  can_revoke: boolean;
  reason: string;
  principals: { principal: string; principal_type: string; privileges: string[] }[];
  observed_at: string;
}

// -- the plan pipeline -----------------------------------------------------
export type PreviewKind = "info" | "change" | "warning" | "unknown";

export interface PreviewLine {
  text: string;
  kind: PreviewKind;
}

/**
 * What the browser is allowed to know about a pending change.
 *
 * Note what is absent: the principal, the privilege list and the state
 * fingerprint. Those stay on the server, so /apply cannot be pointed at a
 * different change than the one that was previewed.
 */
export interface PlanPreview {
  plan_id: string;
  operation_id: string;
  summary: string;
  reason: string;
  target_name: string;
  target_type: string;
  action: string;
  created_at: string;
  age_seconds: number;
  preview: PreviewLine[];
  confirm_with: string;
}

export type OutcomeStatus =
  | "planned"
  | "applying"
  | "applied"
  | "failed"
  | "unknown"
  | "verified"
  | "verify_failed";

export interface ApplyOutcome {
  status: OutcomeStatus;
  headline: string;
  event_id: string;
  operation_id: string;
  message: string;
  succeeded: boolean;
  /** True when the request was sent but the result could not be confirmed. */
  unknown: boolean;
  verified: { principal: string; privileges_now: string[]; applied: boolean } | null;
  summary: string;
  principal: string;
  privileges: string[];
  target: string;
  operation: string;
  error: GovernanceErrorBody | null;
}

// -- principals ------------------------------------------------------------
export interface Principal {
  identifier: string;
  kind: string;
  display_name: string;
  label: string;
  type_label: string;
  valid_for_uc: boolean;
  note: string;
  active: boolean;
}

export interface PrincipalSearch {
  items: Principal[];
  /** Matches Unity Catalog will refuse, surfaced so the reason is visible. */
  rejected: Principal[];
  completeness: Completeness;
  observed_at: string;
  note: string;
  too_short: boolean;
  min_length: number;
  scope_note: string;
}

// -- activity --------------------------------------------------------------
export interface ActivityEvent {
  event_id: string;
  operation_id: string;
  time_utc: string;
  actor: string;
  execution_identity: string;
  action: string;
  summary: string;
  target: string;
  target_type: string;
  principal: string;
  reason: string;
  status: OutcomeStatus;
  status_label: string;
  needs_reconcile: boolean;
  error_code: string;
  correlation_id: string;
  details: Record<string, unknown>;
}

export interface ActivityFeed {
  items: ActivityEvent[];
  unresolved: ActivityEvent[];
  disclaimer: string;
  statuses: Record<string, string>;
}

// -- diagnostics -----------------------------------------------------------
export type CapabilityState =
  | "available"
  | "read_only"
  | "not_configured"
  | "no_permission"
  | "unsupported"
  | "not_implemented"
  | "unknown";

export interface Capability {
  key: string;
  name: string;
  group: string;
  api: string;
  state: CapabilityState;
  state_label: string;
  detail: string;
  usable: boolean;
  maturity: string;
  needs_warehouse: boolean;
  needs_account: boolean;
}

export interface Diagnostics {
  capabilities: Capability[];
  groups: { group: string; items: Capability[] }[];
  summary: Record<string, number>;
  state_labels: Record<string, string>;
  config: { key: string; value: string }[];
  runtime: {
    started_at: string;
    execution_identity: string;
    host: string;
    warehouse_id: string;
  };
}

export interface ProbeResult {
  ran: number;
  usable: number;
  skipped: string[];
  sql_probed: boolean;
  summary: Record<string, number>;
}
