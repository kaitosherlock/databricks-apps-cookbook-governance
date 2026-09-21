"use client";

/**
 * Who has access to this object, and where that access comes from.
 *
 * Effective and direct are separate tabs because building an access picture
 * from the direct endpoint alone is the classic Unity Catalog mistake. Radix
 * unmounts the hidden panel, so opening this screen makes one read, not two -
 * the Streamlit version fetched both every time because it rendered every tab.
 */
import Link from "next/link";
import { ArrowUpRight, PencilLine } from "lucide-react";
import { Suspense } from "react";

import { AssetPicker } from "@/components/governance/asset-picker";
import { GrantTable, InheritanceGuide } from "@/components/governance/grant-table";
import { ErrorState } from "@/components/governance/states";
import { Note } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { CopyField } from "@/components/ui/copy";
import { Explain } from "@/components/ui/disclosure";
import { DataTable, type Column } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Badge, Card, CardBody, CardHeader, LoadingRows, PageTitle } from "@/components/ui/surface";
import { useGrants, usePrivileges, useSession } from "@/lib/queries";
import { fullName, targetHref, useSelection } from "@/lib/selection";
import type { PrivilegeInfo, Risk } from "@/lib/types";
import type { TargetRef } from "@/lib/api";

export default function PermissionsPage() {
  return (
    <Suspense fallback={<LoadingRows rows={4} label="Đang tải" />}>
      <Permissions />
    </Suspense>
  );
}

function Permissions() {
  const { selection, target, update } = useSelection();
  const session = useSession();
  const canGrant = session.data?.permissions.grant?.allowed ?? false;

  return (
    <div className="space-y-6">
      <PageTitle
        title="Quyền truy cập"
        description="Xem ai có quyền gì trên một đối tượng, và quyền đó đến từ đâu."
        actions={
          target && canGrant ? (
            <Button asChild variant="outline" size="sm">
              <Link href={targetHref("/thay-doi-quyen", target)}>
                <PencilLine aria-hidden />
                Thay đổi quyền
              </Link>
            </Button>
          ) : null
        }
      />

      <Card>
        <CardHeader title="Đối tượng đang xem" />
        <CardBody className="space-y-3">
          <AssetPicker
            selection={selection}
            onChange={(changes) => update(changes, { replace: true })}
            allowLevels
          />
          {target ? (
            <CopyField value={fullName(target)} label="tên đầy đủ" />
          ) : null}
        </CardBody>
      </Card>

      {!target ? (
        <Note>Chọn một đối tượng để xem quyền truy cập.</Note>
      ) : (
        <>
          <Tabs defaultValue="effective">
            <TabsList>
              <TabsTrigger value="effective">Quyền hiệu lực (gồm kế thừa)</TabsTrigger>
              <TabsTrigger value="direct">Chỉ quyền cấp trực tiếp</TabsTrigger>
            </TabsList>

            <TabsContent value="effective">
              <GrantPanel target={target} effective />
            </TabsContent>
            <TabsContent value="direct">
              <GrantPanel target={target} effective={false} />
            </TabsContent>
          </Tabs>

          <PrivilegeGlossary target={target} />
        </>
      )}
    </div>
  );
}

function GrantPanel({ target, effective }: { target: TargetRef; effective: boolean }) {
  const grants = useGrants(target, effective);

  if (grants.isPending) {
    return <LoadingRows rows={5} label="Đang đọc quyền từ Databricks" />;
  }

  if (grants.isError) {
    return (
      <div className="space-y-2">
        <ErrorState error={grants.error} context="Không đọc được danh sách quyền." />
        <Note>
          Để đọc được toàn bộ quyền trên một đối tượng, danh tính thực thi cần là chủ sở hữu,
          hoặc có <code>MANAGE</code> trên đối tượng, hoặc là metastore admin. Nếu không,
          Databricks chỉ trả về quyền của chính danh tính đó.
        </Note>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <GrantTable
        view={grants.data}
        downloadName={`${fullName(target)}-${effective ? "quyen-hieu-luc" : "quyen-truc-tiep"}`}
      />
      {effective ? (
        <InheritanceGuide
          view={grants.data}
          renderLink={(parent) => {
            const parts = parent.name.split(".").filter(Boolean);
            if (parts.length > 2 || parts.length === 0) return null;
            const parentTarget: TargetRef =
              parts.length === 1
                ? { kind: "catalog", catalog: parts[0]! }
                : { kind: "schema", catalog: parts[0]!, schema: parts[1]! };
            return (
              <Button asChild variant="ghost" size="sm">
                <Link href={targetHref("/quyen", parentTarget)}>
                  Xem quyền tại cấp cha
                  <ArrowUpRight aria-hidden />
                </Link>
              </Button>
            );
          }}
        />
      ) : null}
    </div>
  );
}

const RISK_TONE: Record<Risk, "neutral" | "caution" | "danger"> = {
  low: "neutral",
  medium: "caution",
  high: "danger",
};

const RISK_WORD: Record<Risk, string> = {
  low: "Thấp",
  medium: "Trung bình",
  high: "Cao",
};

function PrivilegeGlossary({ target }: { target: TargetRef }) {
  // Mounted only when the disclosure is opened, so a closed glossary costs
  // nothing. The Streamlit expander ran its body on every rerun whether open or
  // not, which meant an extra metadata call per interaction.
  return (
    <Explain title="Các quyền này nghĩa là gì?">
      <GlossaryBody target={target} />
    </Explain>
  );
}

function GlossaryBody({ target }: { target: TargetRef }) {
  const catalogue = usePrivileges(target);

  if (catalogue.isPending) return <LoadingRows rows={3} label="Đang đọc danh mục quyền" />;
  if (catalogue.isError) return <ErrorState error={catalogue.error} />;
  if (catalogue.data.privileges.length === 0) {
    return <p>Loại đối tượng này không nhận quyền trực tiếp.</p>;
  }

  const columns: Column<PrivilegeInfo>[] = [
    {
      key: "label",
      header: "Quyền",
      cell: (row) => (
        <div>
          <div className="text-ink">{row.label}</div>
          <code className="text-2xs text-muted">{row.code}</code>
        </div>
      ),
      value: (row) => `${row.label} (${row.code})`,
    },
    { key: "explanation", header: "Ý nghĩa", value: (row) => row.explanation },
    {
      key: "risk",
      header: "Mức rủi ro",
      cell: (row) => <Badge tone={RISK_TONE[row.risk]}>{RISK_WORD[row.risk]}</Badge>,
      value: (row) => RISK_WORD[row.risk],
    },
  ];

  return (
    <div className="space-y-3">
      <DataTable
        rows={catalogue.data.privileges}
        columns={columns}
        rowKey={(row) => row.code}
        caption="Ý nghĩa của các quyền áp dụng được cho đối tượng này"
      />
      {catalogue.data.traversal_note ? <p>{catalogue.data.traversal_note}</p> : null}
      {catalogue.data.inheritance_note ? <p>{catalogue.data.inheritance_note}</p> : null}
    </div>
  );
}
