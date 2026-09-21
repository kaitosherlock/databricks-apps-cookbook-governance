"use client";

/**
 * Grant and revoke, in three steps: choose, preview, apply.
 *
 * The rule the whole screen is built around is unchanged from the Streamlit
 * version: a preview may only be applied if it still describes what is on
 * screen and what is true at Databricks. What changed is where that rule is
 * kept. The plan now lives on the server and this page holds only an opaque id,
 * so editing a field cannot leave a stale preview applicable - it simply throws
 * the handle away and the server forgets the plan.
 */
import Link from "next/link";
import { KeyRound } from "lucide-react";
import { Suspense, useEffect, useState, type ReactNode } from "react";

import { AssetPicker } from "@/components/governance/asset-picker";
import { PrincipalField } from "@/components/governance/principal-field";
import { PreviewApply } from "@/components/governance/preview-apply";
import { ErrorState } from "@/components/governance/states";
import { ProductionBanner } from "@/components/shell/context-panel";
import { Alert, Note, UnknownOutcome } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { CopyField } from "@/components/ui/copy";
import { Field, Textarea } from "@/components/ui/field";
import {
  Badge,
  Card,
  CardBody,
  CardHeader,
  LoadingRows,
  PageTitle,
} from "@/components/ui/surface";
import { useApplyPlan, usePlanChange, usePrivileges, useSession } from "@/lib/queries";
import { fullName, targetHref, useSelection } from "@/lib/selection";
import { shortId } from "@/lib/utils";
import type { ApplyOutcome, PlanPreview, Risk } from "@/lib/types";
import type { TargetRef } from "@/lib/api";

const RISK_TONE: Record<Risk, "neutral" | "caution" | "danger"> = {
  low: "neutral",
  medium: "caution",
  high: "danger",
};

export default function ChangeAccessPage() {
  return (
    <Suspense fallback={<LoadingRows rows={4} label="Đang tải" />}>
      <ChangeAccess />
    </Suspense>
  );
}

function ChangeAccess() {
  const { selection, target, update } = useSelection();
  const session = useSession();

  const [action, setAction] = useState<"grant" | "revoke">("grant");
  const [principal, setPrincipal] = useState("");
  const [privileges, setPrivileges] = useState<string[]>([]);
  const [reason, setReason] = useState("");
  const [plan, setPlan] = useState<PlanPreview | null>(null);
  const [outcome, setOutcome] = useState<ApplyOutcome | null>(null);

  const catalogue = usePrivileges(target);
  const planMutation = usePlanChange();
  const applyMutation = useApplyPlan(target ?? null);

  /**
   * Any edit to a material field invalidates the preview.
   *
   * On the server the plan is already bound to the actor, the object and a
   * fingerprint of the state it was built from, so a stale plan could not be
   * applied even if this effect were missing. Dropping the handle here is what
   * keeps the screen honest about it.
   */
  useEffect(() => {
    setPlan(null);
  }, [action, principal, privileges, reason, target?.catalog, target?.schema, target?.name, target?.kind]);

  const decision = session.data?.permissions[action === "grant" ? "grant" : "revoke"];
  const allowed = decision?.allowed ?? false;
  const ready = Boolean(target && principal.trim() && privileges.length > 0 && reason.trim());

  function togglePrivilege(code: string) {
    setPrivileges((current) =>
      current.includes(code) ? current.filter((c) => c !== code) : [...current, code],
    );
  }

  function preview() {
    if (!target) return;
    setOutcome(null);
    planMutation.mutate(
      { target, action, principal: principal.trim(), privileges, reason: reason.trim() },
      { onSuccess: setPlan },
    );
  }

  function apply(confirmation: string) {
    if (!plan) return;
    applyMutation.mutate(
      { planId: plan.plan_id, confirmation },
      {
        onSuccess: (result) => {
          setOutcome(result);
          setPlan(null);
          if (result.succeeded) {
            setPrivileges([]);
            setReason("");
          }
        },
      },
    );
  }

  return (
    <div className="space-y-6">
      <PageTitle title="Thay đổi quyền truy cập" />

      {session.data ? <ProductionBanner session={session.data} /> : null}

      {outcome ? <Outcome outcome={outcome} onDismiss={() => setOutcome(null)} /> : null}

      <Card>
        <CardHeader title="Đối tượng đang thao tác" />
        <CardBody className="space-y-3">
          <AssetPicker
            selection={selection}
            onChange={(changes) => update(changes, { replace: true })}
            allowLevels
          />
          {target ? <CopyField value={fullName(target)} label="tên đầy đủ" /> : null}
        </CardBody>
      </Card>

      {!target ? (
        <Note>Chọn một đối tượng để bắt đầu.</Note>
      ) : !allowed ? (
        <ReadOnly reason={decision?.reason ?? ""} code={decision?.code ?? ""} target={target} />
      ) : (
        <>
          <Card>
            <CardHeader title="1. Chọn người nhận và quyền" />
            <CardBody className="space-y-4">
              <fieldset>
                <legend className="mb-1.5 text-[13px] font-medium text-ink">Thao tác</legend>
                <div role="radiogroup" aria-label="Thao tác" className="flex flex-wrap gap-1.5">
                  {(
                    [
                      ["grant", "Cấp quyền"],
                      ["revoke", "Thu hồi quyền"],
                    ] as const
                  ).map(([key, label]) => (
                    <button
                      key={key}
                      type="button"
                      role="radio"
                      aria-checked={action === key}
                      onClick={() => setAction(key)}
                      className={
                        action === key
                          ? "rounded-md border border-primary bg-primary-soft px-3 py-1.5 text-[13px] font-medium text-primary"
                          : "rounded-md border border-border-strong bg-raised px-3 py-1.5 text-[13px] text-muted hover:text-ink"
                      }
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </fieldset>

              {/*
                A fieldset, not a Field: this group holds a mode switch plus a
                control, so there is no single input for a <label> to name.
              */}
              <fieldset>
                <legend className="mb-1.5 text-[13px] font-medium text-ink">
                  Người nhận quyền
                </legend>
                <PrincipalField value={principal} onChange={setPrincipal} />
              </fieldset>

              <fieldset>
                <legend className="mb-1.5 text-[13px] font-medium text-ink">Quyền</legend>
                {catalogue.isPending ? (
                  <LoadingRows rows={2} label="Đang đọc danh mục quyền" />
                ) : catalogue.isError ? (
                  <ErrorState error={catalogue.error} />
                ) : catalogue.data.privileges.length === 0 ? (
                  <Alert level="info" title="Loại đối tượng này không nhận quyền qua API Unity Catalog." />
                ) : (
                  <div className="space-y-2">
                    <div className="flex flex-wrap gap-1.5">
                      {catalogue.data.privileges.map((item) => {
                        const active = privileges.includes(item.code);
                        return (
                          <button
                            key={item.code}
                            type="button"
                            aria-pressed={active}
                            onClick={() => togglePrivilege(item.code)}
                            title={item.explanation}
                            className={
                              active
                                ? "flex items-center gap-1.5 rounded-md border border-primary bg-primary-soft px-2.5 py-1.5 text-[13px] font-medium text-primary"
                                : "flex items-center gap-1.5 rounded-md border border-border-strong bg-raised px-2.5 py-1.5 text-[13px] text-muted hover:text-ink"
                            }
                          >
                            {item.label}
                            {item.risk !== "low" ? (
                              <Badge tone={RISK_TONE[item.risk]}>
                                {item.risk === "high" ? "Rủi ro cao" : "Cân nhắc"}
                              </Badge>
                            ) : null}
                          </button>
                        );
                      })}
                    </div>

                    {privileges.length > 0 ? (
                      <ul className="space-y-1 rounded-md border border-border bg-surface px-3 py-2">
                        {catalogue.data.privileges
                          .filter((item) => privileges.includes(item.code))
                          .map((item) => (
                            <li key={item.code} className="text-[13px] text-muted">
                              <code className="text-ink">{item.code}</code> — {item.explanation}
                            </li>
                          ))}
                      </ul>
                    ) : null}

                    {catalogue.data.traversal_note ? (
                      <Note>{catalogue.data.traversal_note}</Note>
                    ) : null}
                  </div>
                )}
              </fieldset>

              <Field
                label="Lý do thay đổi"
                hint="Lý do được ghi vào nhật ký thao tác của ứng dụng. Tối đa 500 ký tự."
              >
                {({ id, describedBy }) => (
                  <Textarea
                    id={id}
                    aria-describedby={describedBy}
                    maxLength={500}
                    value={reason}
                    onChange={(event) => setReason(event.target.value)}
                    placeholder="Ví dụ: cấp quyền đọc cho nhóm phân tích theo yêu cầu TICKET-123."
                  />
                )}
              </Field>
            </CardBody>
          </Card>

          <PreviewApply
            plan={plan}
            ready={ready}
            blockedReason="Nhập đủ principal, quyền và lý do để tạo bản xem trước."
            onPreview={preview}
            onCancel={() => setPlan(null)}
            onApply={apply}
            previewing={planMutation.isPending}
            applying={applyMutation.isPending}
            error={plan ? applyMutation.error : planMutation.error}
          />
        </>
      )}
    </div>
  );
}

function ReadOnly({
  reason,
  code,
  target,
}: {
  reason: string;
  code: string;
  target: TargetRef;
}) {
  const guidance: Record<string, ReactNode> = {
    WRITES_DISABLED: (
      <p>
        Quản trị viên ứng dụng cần bật <code>GOVERNANCE_ENABLE_WRITES</code> trong cấu hình rồi{" "}
        <strong>deploy lại</strong> ứng dụng. Thay đổi biến môi trường chỉ có hiệu lực sau khi
        deploy.
      </p>
    ),
    FORBIDDEN_ROLE: (
      <p>
        Liên hệ quản trị viên ứng dụng để được gán vai trò “Quản trị quyền truy cập” nếu bạn cần
        cấp hoặc thu hồi quyền.
      </p>
    ),
    FORBIDDEN_SCOPE: <p>Đối tượng này nằm ngoài phạm vi catalog mà ứng dụng được phép quản lý.</p>,
    NO_IDENTITY: <p>Mở ứng dụng bằng đúng URL Databricks Apps để proxy gắn danh tính của bạn.</p>,
  };

  return (
    <div className="space-y-3">
      <Alert level="info" title={`Chỉ đọc — ${reason}`}>
        {guidance[code] ?? null}
      </Alert>
      <Button asChild variant="outline" size="sm">
        <Link href={targetHref("/quyen", target)}>
          <KeyRound aria-hidden />
          Xem quyền hiện tại
        </Link>
      </Button>
    </div>
  );
}

/**
 * What actually happened.
 *
 * Three distinct endings, never collapsed: applied and verified, applied but
 * unverified, and genuinely unknown. The last one is the dangerous one, because
 * the safe next step is to go and look rather than to try again.
 */
function Outcome({ outcome, onDismiss }: { outcome: ApplyOutcome; onDismiss: () => void }) {
  if (outcome.unknown) {
    return (
      <UnknownOutcome>
        <p>Yêu cầu đã được gửi nhưng ứng dụng không nhận được xác nhận.</p>
        <p className="mt-1">
          <strong>Việc cần làm:</strong> làm mới và kiểm tra quyền hiện tại của{" "}
          <code>{outcome.principal}</code> trên <code>{outcome.target}</code> TRƯỚC KHI thử lại,
          để tránh thực hiện hai lần.
        </p>
        <p className="mt-1 text-2xs">
          Event ID: <code>{shortId(outcome.event_id)}</code>
        </p>
      </UnknownOutcome>
    );
  }

  if (!outcome.succeeded) {
    return (
      <Alert level="danger" title={outcome.message || "Thay đổi không thành công."}>
        {outcome.error?.next_step ? <p>{outcome.error.next_step}</p> : null}
        <p className="mt-1 text-2xs">
          Event ID: <code>{shortId(outcome.event_id)}</code>
        </p>
      </Alert>
    );
  }

  const verified = outcome.verified;
  const revoking = outcome.operation === "revoke";

  return (
    <Alert level="good" title={outcome.summary || "Hoàn tất"}>
      <ul className="space-y-0.5">
        <li>
          Principal: <code>{outcome.principal}</code>
        </li>
        <li>Quyền: {outcome.privileges.join(", ")}</li>
        <li>
          Đối tượng: <code>{outcome.target}</code>
        </li>
      </ul>

      {verified ? (
        <>
          <p className="mt-1.5">
            Đã đọc lại từ Databricks. Quyền trực tiếp hiện tại của principal:{" "}
            {verified.privileges_now.length > 0
              ? verified.privileges_now.join(", ")
              : "(không còn quyền trực tiếp nào)"}
          </p>
          {!verified.applied ? (
            <p className="mt-1 font-medium">
              Databricks nhận yêu cầu nhưng trạng thái đọc lại chưa khớp mong đợi. Hãy kiểm tra
              lại trong Catalog Explorer.
            </p>
          ) : null}
        </>
      ) : (
        <p className="mt-1.5">Đã gửi thành công nhưng chưa đọc lại được để xác minh.</p>
      )}

      <p className="mt-1.5">
        {revoking
          ? "Thu hồi một quyền trực tiếp không đảm bảo principal mất toàn bộ quyền truy cập — họ vẫn có thể còn quyền qua nhóm hoặc cấp cha."
          : "Quyền có hiệu lực theo mô hình Unity Catalog; principal còn cần USE_CATALOG / USE_SCHEMA để thực sự dùng được."}
      </p>

      <div className="mt-2 flex items-center gap-3">
        <span className="text-2xs text-muted">
          Event ID: <code>{shortId(outcome.event_id)}</code>
        </span>
        <Button variant="ghost" size="sm" onClick={onDismiss}>
          Đóng
        </Button>
      </div>
    </Alert>
  );
}
