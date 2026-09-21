"use client";

/**
 * One asset: what it is, what it contains, and how it is protected.
 *
 * Working information first, raw API payloads behind a disclosure. A steward
 * should be able to answer "what is this and who owns it" without opening JSON.
 */
import Link from "next/link";
import { KeyRound, PencilLine } from "lucide-react";
import { Suspense } from "react";

import { AssetPicker } from "@/components/governance/asset-picker";
import { EmptyState, ErrorState } from "@/components/governance/states";
import { Alert, Note } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { CopyField } from "@/components/ui/copy";
import { Explain, TechnicalDetails } from "@/components/ui/disclosure";
import { DataTable, type Column } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Badge,
  Card,
  CardBody,
  CardHeader,
  LoadingRows,
  PageTitle,
} from "@/components/ui/surface";
import { useDetail, useSession } from "@/lib/queries";
import { targetHref, useSelection } from "@/lib/selection";
import type { AssetDetail } from "@/lib/types";

export default function AssetPage() {
  return (
    <Suspense fallback={<LoadingRows rows={4} label="Đang tải" />}>
      <Asset />
    </Suspense>
  );
}

function Asset() {
  const { selection, target, update } = useSelection();
  const detail = useDetail(target);
  const session = useSession();

  const canGrant = session.data?.permissions.grant?.allowed ?? false;
  const grantReason = session.data?.permissions.grant?.reason ?? "";

  return (
    <div className="space-y-6">
      <PageTitle title="Thông tin tài sản" />

      <Card>
        <CardHeader title="Đối tượng đang xem" />
        <CardBody>
          <AssetPicker
            selection={selection}
            onChange={(changes) => update(changes, { replace: true })}
          />
        </CardBody>
      </Card>

      {!target ? (
        <Note>Chọn đủ catalog, schema và đối tượng để xem thông tin.</Note>
      ) : detail.isPending ? (
        <LoadingRows rows={5} label="Đang đọc metadata" />
      ) : detail.isError ? (
        <ErrorState error={detail.error} context="Không đọc được metadata của đối tượng này." />
      ) : (
        <>
          <AssetHeader detail={detail.data} />

          <div className="flex flex-wrap gap-2">
            <Button asChild variant="outline" size="sm">
              <Link href={targetHref("/quyen", target)}>
                <KeyRound aria-hidden />
                Xem quyền
              </Link>
            </Button>
            {canGrant ? (
              <Button asChild variant="outline" size="sm">
                <Link href={targetHref("/thay-doi-quyen", target)}>
                  <PencilLine aria-hidden />
                  Thay đổi quyền
                </Link>
              </Button>
            ) : (
              <Button variant="outline" size="sm" disabled title={grantReason}>
                <PencilLine aria-hidden />
                Thay đổi quyền
              </Button>
            )}
          </div>
          {!canGrant && grantReason ? <Note>{grantReason}</Note> : null}

          <Tabs defaultValue="overview">
            <TabsList>
              <TabsTrigger value="overview">Tổng quan</TabsTrigger>
              <TabsTrigger value="structure">Cấu trúc</TabsTrigger>
              <TabsTrigger value="protection">Bảo vệ dữ liệu</TabsTrigger>
              <TabsTrigger value="technical">Kỹ thuật</TabsTrigger>
            </TabsList>

            <TabsContent value="overview">
              <Overview detail={detail.data} />
            </TabsContent>
            <TabsContent value="structure">
              <Structure detail={detail.data} />
            </TabsContent>
            <TabsContent value="protection">
              <Protection detail={detail.data} />
            </TabsContent>
            <TabsContent value="technical">
              <div className="space-y-3">
                <TechnicalDetails payload={detail.data.raw} label="Metadata JSON" />
                <Note>
                  Dữ liệu thô do API Unity Catalog trả về, dùng để đối chiếu khi cần. Ứng dụng
                  không đọc nội dung bảng hay tệp để hiển thị trang này.
                </Note>
              </div>
            </TabsContent>
          </Tabs>
        </>
      )}
    </div>
  );
}

function AssetHeader({ detail }: { detail: AssetDetail }) {
  const { target } = detail;

  return (
    <Card>
      <CardBody className="space-y-3">
        <nav aria-label="Đường dẫn đối tượng" className="text-2xs text-muted">
          <ol className="flex flex-wrap items-center gap-1">
            <li>Catalog: {target.catalog}</li>
            {target.schema ? <li>› Schema: {target.schema}</li> : null}
            {target.name ? <li>› {detail.type_label}</li> : null}
          </ol>
        </nav>

        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-lg font-semibold text-ink">
            {target.name || target.schema || target.catalog}
          </h2>
          <Badge tone="neutral">{detail.type_label}</Badge>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <p className="text-2xs font-semibold uppercase tracking-wide text-muted">
              Chủ sở hữu
            </p>
            <p className="text-[13px] text-ink">{detail.owner || "Chưa xác định"}</p>
          </div>
          <div>
            <p className="text-2xs font-semibold uppercase tracking-wide text-muted">
              Tên đầy đủ
            </p>
            <CopyField value={target.full_name} label="tên đầy đủ" className="mt-0.5" />
          </div>
        </div>

        {detail.comment ? (
          <p className="text-sm text-muted">{detail.comment}</p>
        ) : (
          <Note>Chưa có mô tả.</Note>
        )}

        {detail.pipeline_managed ? (
          <Alert level="info" title={`Đối tượng này được quản lý bởi ${detail.managed_by}`}>
            <p>Thay đổi cấu trúc và dữ liệu phải thực hiện ở pipeline, không phải ở đây.</p>
          </Alert>
        ) : null}
      </CardBody>
    </Card>
  );
}

function Overview({ detail }: { detail: AssetDetail }) {
  const properties = Object.entries(detail.properties ?? {});

  return (
    <div className="space-y-4">
      <dl className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {detail.summary_fields.map((field) => (
          <div key={field.label}>
            <dt className="text-2xs font-semibold uppercase tracking-wide text-muted">
              {field.label}
            </dt>
            <dd className="text-[13px] text-ink">{field.value}</dd>
          </div>
        ))}
        {detail.created ? (
          <div>
            <dt className="text-2xs font-semibold uppercase tracking-wide text-muted">Tạo lúc</dt>
            <dd className="text-[13px] text-ink">{detail.created}</dd>
          </div>
        ) : null}
        {detail.updated_by ? (
          <div>
            <dt className="text-2xs font-semibold uppercase tracking-wide text-muted">
              Cập nhật bởi
            </dt>
            <dd className="text-[13px] text-ink">{detail.updated_by}</dd>
          </div>
        ) : null}
        {detail.storage_location ? (
          <div className="sm:col-span-2 lg:col-span-3">
            <dt className="text-2xs font-semibold uppercase tracking-wide text-muted">
              Vị trí lưu trữ
            </dt>
            <dd>
              <code className="text-2xs text-muted">{detail.storage_location}</code>
            </dd>
          </div>
        ) : null}
      </dl>

      {properties.length > 0 ? (
        <Explain title={`Thuộc tính (${properties.length})`}>
          <dl className="grid gap-1.5 sm:grid-cols-2">
            {properties.map(([key, value]) => (
              <div key={key} className="min-w-0">
                <dt className="truncate font-medium text-ink">{key}</dt>
                <dd className="truncate">{String(value)}</dd>
              </div>
            ))}
          </dl>
        </Explain>
      ) : null}
    </div>
  );
}

function Structure({ detail }: { detail: AssetDetail }) {
  if (detail.target.kind !== "table" && detail.target.kind !== "function") {
    return <Note>Loại đối tượng này không có cấu trúc cột.</Note>;
  }

  if (detail.columns.length === 0) {
    return (
      <EmptyState
        what="cột"
        detail="API không trả về thông tin cột cho đối tượng này."
      />
    );
  }

  const keys = Object.keys(detail.columns[0] ?? {});
  const columns: Column<Record<string, unknown>>[] = keys.map((key) => ({
    key,
    header: key,
    value: (row) => String(row[key] ?? ""),
  }));

  return (
    <DataTable
      rows={detail.columns}
      columns={columns}
      rowKey={(_, index) => String(index)}
      caption={`Cấu trúc của ${detail.target.full_name}`}
      downloadName={`${detail.target.full_name}-columns`}
    />
  );
}

function Protection({ detail }: { detail: AssetDetail }) {
  if (detail.target.kind !== "table") {
    return <Note>Chỉ áp dụng cho bảng và view.</Note>;
  }

  const { row_filter: rowFilter, column_masks: masks } = detail.protection;
  const hasAny = Boolean(rowFilter) || masks.length > 0;

  return (
    <div className="space-y-4">
      {rowFilter ? (
        <Alert level="danger" title="Bảng này có row filter gắn trực tiếp">
          <p>
            Dùng hàm <code>{rowFilter.function_name ?? "—"}</code>. Người truy vấn chỉ thấy những
            dòng hàm này cho phép. <strong>Gỡ filter sẽ để lộ toàn bộ dòng dữ liệu.</strong>
          </p>
        </Alert>
      ) : null}

      {masks.length > 0 ? (
        <Alert level="danger" title={`${masks.length} cột đang được che bằng column mask`}>
          <p>
            <strong>Gỡ mask sẽ để lộ giá trị gốc của cột</strong> cho người có quyền đọc.
          </p>
          <ul className="mt-1.5 list-disc space-y-0.5 pl-4">
            {masks.map((mask) => (
              <li key={mask.column}>
                <code>{mask.column}</code> → <code>{mask.function || "—"}</code>
              </li>
            ))}
          </ul>
        </Alert>
      ) : null}

      {!hasAny ? (
        <Note>Không có row filter hoặc column mask nào gắn trực tiếp trên bảng này.</Note>
      ) : null}

      {/*
        The scope limit is the important part and it is easy to misread this
        screen as the whole picture, so it is stated once, plainly, rather than
        repeated beside every item.
      */}
      <Explain title="Vì sao đây chưa phải bức tranh đầy đủ">
        <p>
          Trang này <strong>chỉ</strong> hiển thị cơ chế gắn trực tiếp trên bảng. Chính sách ABAC
          tập trung (row filter / column mask theo tag) là cơ chế khác và không hiện ở đây.
        </p>
        <p>
          Unity Catalog cũng không cho tạo hoặc gỡ filter/mask gắn trực tiếp qua API; việc đó chỉ
          làm được bằng câu lệnh SQL <code>ALTER TABLE</code> trong Databricks.
        </p>
      </Explain>

      {detail.constraints.length > 0 ? (
        <Explain title={`Ràng buộc (${detail.constraints.length})`}>
          <ul className="list-disc space-y-1 pl-4">
            {detail.constraints.map((constraint, index) => (
              <li key={index}>
                <code className="text-2xs">{constraint}</code>
              </li>
            ))}
          </ul>
        </Explain>
      ) : null}
    </div>
  );
}
