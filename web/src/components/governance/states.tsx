"use client";

/**
 * The three reasons a list can be blank, kept apart.
 *
 * This is the single most important rule the old build enforced and the easiest
 * one to lose in a rewrite. "There is nothing here", "you were not allowed to
 * look" and "the read failed" look identical if you render them all as an empty
 * table, and only one of them means the thing does not exist. A steward who
 * reads "no grants" and concludes "nobody has access" has been misled.
 */
import { Inbox, Lock, Settings, TriangleAlert } from "lucide-react";
import type { ReactNode } from "react";

import { ApiError } from "@/lib/api";
import type { Completeness } from "@/lib/types";
import { Alert, Note } from "@/components/ui/alert";

const PERMISSION_CODES = new Set(["PERMISSION_DENIED", "FORBIDDEN_ROLE", "FORBIDDEN_SCOPE"]);

export function EmptyState({
  what,
  detail,
  action,
}: {
  what: string;
  detail?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="rounded-md border border-dashed border-border-strong bg-surface px-4 py-6 text-center">
      <Inbox aria-hidden className="mx-auto size-5 text-muted" />
      <p className="mt-2 text-sm font-medium text-ink">Không có {what} nào trong phạm vi này.</p>
      <p className="mx-auto mt-1 max-w-md text-[13px] leading-relaxed text-muted">
        Đây là kết quả rỗng do Databricks trả về cho danh tính thực thi — không phải bằng chứng
        rằng không tồn tại đối tượng nào.
      </p>
      {detail ? <div className="mt-2 text-[13px] text-muted">{detail}</div> : null}
      {action ? <div className="mt-3">{action}</div> : null}
    </div>
  );
}

export function ForbiddenState({ what, detail }: { what: string; detail?: ReactNode }) {
  return (
    <Alert level="caution" title={`Không đủ quyền để xem ${what} ở đây.`}>
      <p>
        Danh tính thực thi cần thêm quyền Unity Catalog.{" "}
        <strong>Điều này không có nghĩa là danh sách trống.</strong>
      </p>
      {detail ? <p className="mt-1">{detail}</p> : null}
    </Alert>
  );
}

export function UnconfiguredState({ detail }: { detail?: ReactNode }) {
  return (
    <div className="flex gap-3 rounded-md border border-border bg-surface px-3.5 py-3 text-sm">
      <Settings aria-hidden className="mt-0.5 size-4 shrink-0 text-muted" />
      <div>
        <p className="font-medium text-ink">Chức năng này chưa được cấu hình.</p>
        {detail ? <p className="mt-1 text-[13px] text-muted">{detail}</p> : null}
      </div>
    </div>
  );
}

/**
 * Any failure, rendered from the backend's own words.
 *
 * The message, the next step and the lookup code all come from
 * ucg/errors.py. Nothing here invents an explanation, and a stack trace or an
 * upstream response body never reaches this component.
 */
export function ErrorState({ error, context }: { error: unknown; context?: string }) {
  if (!(error instanceof ApiError)) {
    return (
      <Alert level="danger" title="Ứng dụng gặp lỗi không mong đợi.">
        <p>Tải lại trang. Nếu vẫn lỗi, báo cho quản trị viên ứng dụng.</p>
      </Alert>
    );
  }

  const { body } = error;
  const forbidden = PERMISSION_CODES.has(body.code);

  if (body.outcome_unknown) {
    return (
      <Alert level="caution" title="Chưa xác định được kết quả.">
        <p>{body.message}</p>
        {body.next_step ? <p className="mt-1">{body.next_step}</p> : null}
        <LookupCode body={body} />
      </Alert>
    );
  }

  return (
    <Alert
      level={forbidden ? "caution" : "danger"}
      title={context ? `${context} ${body.message}` : body.message}
    >
      {body.next_step ? <p>{body.next_step}</p> : null}
      <LookupCode body={body} />
    </Alert>
  );
}

function LookupCode({ body }: { body: ApiError["body"] }) {
  if (!body.correlation_id) return null;
  return (
    <p className="mt-1.5 text-2xs text-muted">
      Mã tra cứu: <code>{body.correlation_id}</code>
      {body.detail ? (
        <>
          {" · "}Loại: <code>{body.detail}</code>
        </>
      ) : null}
    </p>
  );
}

/** Icon helper for a permission-shaped failure, used in compact places. */
export function ForbiddenInline({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-[13px] text-muted">
      <Lock aria-hidden className="size-3.5" />
      {children}
    </span>
  );
}

/**
 * Whether what you are looking at is the whole answer.
 *
 * Travels with every listing. Rendered quietly, because in the normal case it
 * says "complete" and should not draw the eye.
 */
export function CompletenessNote({
  completeness,
  observedAt,
  note,
}: {
  completeness: Completeness;
  observedAt?: string;
  note?: string;
}) {
  const parts: string[] = [];
  if (completeness === "truncated") {
    parts.push("Danh sách đã bị cắt bớt vì quá dài — hãy thu hẹp bằng ô tìm kiếm.");
  }
  if (note) parts.push(note);
  if (observedAt) parts.push(`Dữ liệu đọc lúc ${observedAt}.`);

  if (completeness === "partial_permission") {
    return (
      <div className="flex items-start gap-2 text-[13px] text-caution-ink">
        <TriangleAlert aria-hidden className="mt-0.5 size-3.5 shrink-0 text-caution" />
        <p>
          Một phần dữ liệu không đọc được do thiếu quyền, nên danh sách này chưa chắc đầy đủ.
          {observedAt ? ` Dữ liệu đọc lúc ${observedAt}.` : ""}
        </p>
      </div>
    );
  }

  if (parts.length === 0) return null;
  return <Note>{parts.join(" ")}</Note>;
}
