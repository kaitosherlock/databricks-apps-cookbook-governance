"use client";

/**
 * Who you are, where you are, and what you may do.
 *
 * This lives in the shell, so it is on screen on every route. That is the fix
 * for a real defect in the Streamlit build: `chrome.render_banner` was
 * documented as appearing "on every screen" and was in fact called from exactly
 * one page, so someone who opened the grant/revoke screen directly - which the
 * URLs made easy - got no environment marker and no read/write marker at all.
 *
 * Nothing here depends on colour. The PROD marker is a word, an icon and a
 * border, so it survives a greyscale screenshot and a colour-blind reader.
 */
import { Database, Eye, Pencil, ShieldAlert, UserCheck, UserCog } from "lucide-react";

import { Alert, Note } from "@/components/ui/alert";
import { Badge } from "@/components/ui/surface";
import type { Session } from "@/lib/types";

export function EnvironmentMarker({ session }: { session: Session }) {
  const { environment, mode } = session;

  return (
    <div className="space-y-2">
      <div
        className={
          environment.high_risk
            ? "rounded-md border-2 border-danger bg-danger-soft px-2.5 py-2"
            : "rounded-md border border-border bg-surface px-2.5 py-2"
        }
      >
        <div className="flex items-center gap-2">
          {environment.high_risk ? (
            <ShieldAlert aria-hidden className="size-4 shrink-0 text-danger" />
          ) : (
            <Database aria-hidden className="size-4 shrink-0 text-muted" />
          )}
          <span
            className={
              environment.high_risk
                ? "text-[13px] font-bold uppercase tracking-wide text-danger-ink"
                : "text-[13px] font-medium text-ink"
            }
          >
            {environment.label}
          </span>
        </div>
        <p className="mt-1 truncate text-2xs text-muted" title={session.workspace.host}>
          {session.workspace.host}
        </p>
      </div>

      <div className="flex items-center gap-2 px-0.5">
        {mode.writable ? (
          <Pencil aria-hidden className="size-3.5 shrink-0 text-primary" />
        ) : (
          <Eye aria-hidden className="size-3.5 shrink-0 text-muted" />
        )}
        <span className="text-[13px] font-medium text-ink">{mode.label}</span>
      </div>
      <Note className="px-0.5 text-2xs">{mode.reason}</Note>
    </div>
  );
}

/**
 * The two identities, never blurred.
 *
 * The person clicking and the identity the API call authenticates as are
 * different things, and what you can see is decided by the second one. Stating
 * that once on entry is not enough, so it is here on every screen.
 */
export function IdentityPanel({ session }: { session: Session }) {
  const { actor, execution } = session;

  return (
    <div className="space-y-3">
      <div>
        <div className="flex items-center gap-1.5">
          <UserCheck aria-hidden className="size-3.5 shrink-0 text-muted" />
          <span className="text-2xs font-semibold uppercase tracking-wide text-muted">
            Bạn đang đăng nhập
          </span>
        </div>
        <p className="mt-1 truncate text-[13px] font-medium text-ink" title={actor.label}>
          {actor.label || "Chưa xác định"}
        </p>
        <div className="mt-1 flex flex-wrap items-center gap-1">
          <Badge tone="neutral">{actor.role_label}</Badge>
          <Badge tone={actor.verified ? "good" : "caution"}>
            {actor.verified ? "Đã xác minh" : "Chưa xác minh"}
          </Badge>
        </div>
        <Note className="mt-1 text-2xs">{actor.trust_note}</Note>
      </div>

      <div>
        <div className="flex items-center gap-1.5">
          <UserCog aria-hidden className="size-3.5 shrink-0 text-muted" />
          <span className="text-2xs font-semibold uppercase tracking-wide text-muted">
            Yêu cầu được thực hiện bằng
          </span>
        </div>
        <p className="mt-1 text-[13px] font-medium text-ink">{execution.label}</p>
        {execution.running_as ? (
          <p className="truncate text-2xs text-muted" title={execution.running_as}>
            {execution.running_as}
          </p>
        ) : null}
        {execution.explains_visibility ? (
          <Note className="mt-1 text-2xs">
            Những gì bạn nhìn thấy là những gì tài khoản dịch vụ được phép nhìn thấy, không phải
            quyền cá nhân của bạn.
          </Note>
        ) : null}
      </div>
    </div>
  );
}

export function ScopePanel({ session }: { session: Session }) {
  const { scope } = session;

  return (
    <div className="space-y-1.5">
      <span className="text-2xs font-semibold uppercase tracking-wide text-muted">
        Phạm vi quản lý
      </span>
      {scope.catalogs.length > 0 ? (
        <ul className="space-y-0.5">
          {scope.catalogs.map((catalog) => (
            <li key={catalog}>
              <code className="text-[13px] text-ink">{catalog}</code>
            </li>
          ))}
        </ul>
      ) : (
        <Note className="text-2xs">Tất cả catalog mà tài khoản dịch vụ nhìn thấy.</Note>
      )}

      {scope.unbounded_writes ? (
        <Alert level="caution" title="Chưa giới hạn catalog" className="mt-2">
          <p className="text-2xs">
            Thao tác ghi áp dụng được cho <strong>mọi</strong> catalog mà tài khoản dịch vụ quản
            lý được.
          </p>
        </Alert>
      ) : null}
    </div>
  );
}

/**
 * The banner that sits above a write screen in a high-risk environment.
 *
 * The sidebar marker is always there; this is the one that interrupts, and it
 * appears only where a change can actually be made.
 */
export function ProductionBanner({ session }: { session: Session }) {
  if (!session.environment.high_risk) return null;
  return (
    <Alert level="danger" title={session.environment.label}>
      <p>{session.environment.warning ?? "Mọi thay đổi ở đây ảnh hưởng tới hệ thống thật."}</p>
    </Alert>
  );
}
