"use client";

/**
 * Preview, confirm, apply. One implementation.
 *
 * The Streamlit build had this flow eight times over - change_access,
 * federation, policies, quality, requests, sharing, storage and tags each
 * carried a byte-identical `_render_preview` plus its own `_apply_step`. Nothing
 * had drifted yet, which was luck: it is the app's core safety guarantee and a
 * fix would have had to land in eight files without being forgotten in any.
 *
 * Everything that made that flow safe is preserved:
 *
 *   - the preview is read-only and says so;
 *   - the change is described in the backend's own words, never re-derived here;
 *   - applying requires retyping the object's full name;
 *   - one plan can be submitted exactly once;
 *   - an unknown outcome is never presented as a failure.
 *
 * The change itself lives on the server. This component holds a `plan_id` and
 * a string the operator typed, and nothing else.
 */
import { Check, Eye, X } from "lucide-react";
import { useState } from "react";

import { Alert, Note } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/surface";
import { ConfirmTarget } from "@/components/ui/copy";
import { Field, Input } from "@/components/ui/field";
import { ErrorState } from "./states";
import { formatUtc, shortId } from "@/lib/utils";
import type { PlanPreview, PreviewLine } from "@/lib/types";

export interface PreviewApplyProps {
  /** The stored preview, or null when nothing has been previewed yet. */
  plan: PlanPreview | null;
  /** Whether the inputs are complete enough to ask for a preview. */
  ready: boolean;
  /** Why a preview cannot be requested yet, shown when `ready` is false. */
  blockedReason?: string;
  onPreview: () => void;
  onCancel: () => void;
  onApply: (confirmation: string) => void;
  previewing?: boolean;
  applying?: boolean;
  error?: unknown;
  /** Label for the apply button. Defaults to the plan's own summary. */
  applyLabel?: string;
}

export function PreviewApply({
  plan,
  ready,
  blockedReason,
  onPreview,
  onCancel,
  onApply,
  previewing,
  applying,
  error,
  applyLabel,
}: PreviewApplyProps) {
  const [confirmation, setConfirmation] = useState("");

  const matches = plan !== null && confirmation.trim() === plan.confirm_with;

  return (
    <div className="space-y-4">
      <section className="space-y-3" aria-labelledby="step-preview">
        <h3 id="step-preview" className="text-sm font-semibold text-ink">
          2. Xem trước phạm vi ảnh hưởng
        </h3>

        {plan === null ? (
          <div className="space-y-2">
            <Button variant="primary" onClick={onPreview} disabled={!ready} loading={previewing}>
              <Eye aria-hidden />
              Tạo bản xem trước
            </Button>
            <Note>
              {ready
                ? "Bản xem trước chỉ đọc dữ liệu, không thay đổi gì. Sửa bất kỳ trường nào ở trên sẽ huỷ bản xem trước cũ."
                : (blockedReason ?? "Nhập đủ thông tin để tạo bản xem trước.")}
            </Note>
            {error ? <ErrorState error={error} /> : null}
          </div>
        ) : (
          <PlanCard plan={plan} />
        )}
      </section>

      {plan !== null ? (
        <section className="space-y-3" aria-labelledby="step-apply">
          <h3 id="step-apply" className="text-sm font-semibold text-ink">
            3. Xác nhận và áp dụng
          </h3>

          <Card>
            <CardBody className="space-y-3">
              <Field
                label={
                  <>
                    Nhập lại tên đầy đủ của đối tượng để xác nhận:{" "}
                    <ConfirmTarget value={plan.confirm_with} />
                  </>
                }
                hint="Gõ lại bằng tay là một bước dừng có chủ đích, nên ô này không có nút sao chép."
              >
                {({ id, describedBy }) => (
                  <Input
                    id={id}
                    aria-describedby={describedBy}
                    value={confirmation}
                    onChange={(event) => setConfirmation(event.target.value)}
                    autoComplete="off"
                    spellCheck={false}
                    className="font-mono"
                  />
                )}
              </Field>

              <div className="flex flex-wrap items-center gap-2">
                <Button
                  variant="danger"
                  disabled={!matches}
                  loading={applying}
                  onClick={() => onApply(confirmation)}
                >
                  <Check aria-hidden />
                  {applyLabel ?? plan.summary}
                </Button>
                <Button variant="ghost" onClick={onCancel} disabled={applying}>
                  <X aria-hidden />
                  Huỷ bản xem trước
                </Button>
              </div>

              {!matches && confirmation.trim().length > 0 ? (
                <Note>Tên chưa khớp. Nút áp dụng chỉ bật khi gõ đúng tên đầy đủ.</Note>
              ) : null}

              {error ? <ErrorState error={error} /> : null}
            </CardBody>
          </Card>
        </section>
      ) : null}
    </div>
  );
}

function PlanCard({ plan }: { plan: PlanPreview }) {
  const changes = plan.preview.filter((line) => line.kind === "change");
  const warnings = plan.preview.filter((line) => line.kind === "warning");
  const unknowns = plan.preview.filter((line) => line.kind === "unknown");
  const notes = plan.preview.filter((line) => line.kind === "info");

  return (
    <Card>
      <CardHeader
        title="Những gì sẽ được gửi tới Databricks"
        description="Bản xem trước này chỉ đọc. Chưa có gì được thay đổi."
      />
      <CardBody className="space-y-3">
        {changes.length > 0 ? (
          <ul className="space-y-1.5">
            {changes.map((line, index) => (
              <li key={index} className="flex gap-2 text-sm text-ink">
                <span aria-hidden className="mt-2 size-1.5 shrink-0 rounded-full bg-primary" />
                <span>{line.text}</span>
              </li>
            ))}
          </ul>
        ) : null}

        {/* Warnings are grouped rather than interleaved, so a page cannot end up
            alternating one line of fact with one yellow box. */}
        {warnings.length > 0 ? (
          <Alert level="caution" title="Trước khi tiếp tục, cân nhắc">
            <ul className="list-disc space-y-1 pl-4">
              {warnings.map((line, index) => (
                <li key={index}>{line.text}</li>
              ))}
            </ul>
          </Alert>
        ) : null}

        {unknowns.length > 0 ? (
          <Alert level="info" title="Ứng dụng không xác định được">
            <ul className="list-disc space-y-1 pl-4">
              {unknowns.map((line, index) => (
                <li key={index}>{line.text}</li>
              ))}
            </ul>
          </Alert>
        ) : null}

        {notes.length > 0 ? (
          <div className="space-y-1">
            {notes.map((line: PreviewLine, index) => (
              <Note key={index}>{line.text}</Note>
            ))}
          </div>
        ) : null}

        <div className="border-t border-border pt-2.5 text-2xs text-muted">
          <p>Lý do: {plan.reason}</p>
          <p className="mt-0.5">
            Tạo lúc {formatUtc(plan.created_at)} · mã thao tác{" "}
            <code>{shortId(plan.operation_id)}</code>
          </p>
        </div>
      </CardBody>
    </Card>
  );
}
