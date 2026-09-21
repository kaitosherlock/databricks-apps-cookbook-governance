"use client";

/**
 * What this deployment can actually do, and why anything is switched off.
 *
 * A capability is never just "unavailable". "Not configured", "no permission",
 * "workspace does not support it" and "not checked" lead to four different
 * actions, and only one of them is fixed by granting something.
 */
import { PlayCircle } from "lucide-react";
import { useState } from "react";

import { Alert, Note } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Badge, Card, CardBody, CardHeader, LoadingRows, PageTitle } from "@/components/ui/surface";
import { DataTable, type Column } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ErrorState } from "@/components/governance/states";
import { useDiagnostics, useProbe, useSession } from "@/lib/queries";
import type { Capability, CapabilityState } from "@/lib/types";

const TONE: Record<CapabilityState, "good" | "danger" | "caution" | "neutral" | "info"> = {
  available: "good",
  read_only: "info",
  not_configured: "neutral",
  no_permission: "caution",
  unsupported: "danger",
  not_implemented: "neutral",
  unknown: "caution",
};

export default function DiagnosticsPage() {
  const diagnostics = useDiagnostics();
  const session = useSession();
  const probe = useProbe();
  const [includeSql, setIncludeSql] = useState(false);

  if (diagnostics.isPending) return <LoadingRows rows={6} label="Đang đọc khả năng hệ thống" />;
  if (diagnostics.isError) return <ErrorState error={diagnostics.error} />;

  const data = diagnostics.data;
  const unchecked = data.capabilities.filter((c) => c.state === "unknown").length;
  const hasWarehouse = Boolean(data.runtime.warehouse_id);

  const columns: Column<Capability>[] = [
    {
      key: "name",
      header: "Chức năng",
      cell: (row) => (
        <div className="min-w-0">
          <div className="text-ink">{row.name}</div>
          <code className="text-2xs text-muted">{row.key}</code>
        </div>
      ),
      value: (row) => row.name,
    },
    {
      key: "state",
      header: "Trạng thái",
      cell: (row) => <Badge tone={TONE[row.state]}>{row.state_label}</Badge>,
      value: (row) => row.state_label,
    },
    {
      key: "detail",
      header: "Giải thích",
      cell: (row) => <span className="text-muted">{row.detail || "—"}</span>,
      value: (row) => row.detail,
    },
    { key: "api", header: "API", value: (row) => row.api },
    { key: "maturity", header: "Mức độ", value: (row) => row.maturity },
  ];

  return (
    <div className="space-y-6">
      <PageTitle
        title="Khả năng và cấu hình"
        description="Ứng dụng kiểm tra bằng cách gọi thật vào Databricks, chỉ với các lệnh đọc."
      />

      {unchecked > 0 ? (
        <Alert level="info" title={`${unchecked} chức năng chưa được kiểm tra`}>
          <p>
            Chạy dò khả năng để biết trạng thái thật, thay vì để trống. Các lệnh dò đều là lệnh
            đọc và không thay đổi gì.
          </p>
        </Alert>
      ) : null}

      <Card>
        <CardHeader
          title="Dò khả năng"
          description="Gọi thật vào Databricks bằng danh tính thực thi hiện tại."
          actions={
            <Button
              variant="primary"
              size="sm"
              loading={probe.isPending}
              onClick={() => probe.mutate(includeSql)}
            >
              <PlayCircle aria-hidden />
              Chạy dò
            </Button>
          }
        />
        <CardBody className="space-y-2">
          <label className="flex items-center gap-2 text-[13px] text-ink">
            <input
              type="checkbox"
              checked={includeSql}
              disabled={!hasWarehouse}
              onChange={(event) => setIncludeSql(event.target.checked)}
              className="size-4 rounded-sm border-border-strong"
            />
            Bao gồm các phép dò cần SQL Warehouse
          </label>
          <Note>
            {hasWarehouse
              ? "Các phép dò SQL tiêu tốn thời gian chạy của warehouse, nên mặc định tắt."
              : "Chưa đặt GOVERNANCE_WAREHOUSE_ID, nên không có phép dò SQL nào để chạy."}
          </Note>

          {probe.isSuccess ? (
            <Alert level="good" title="Đã chạy xong">
              <p>
                Đã dò {probe.data.ran} chức năng, {probe.data.usable} dùng được
                {probe.data.skipped.length > 0
                  ? `, bỏ qua ${probe.data.skipped.length} (đã biết là chưa cấu hình hoặc không hỗ trợ)`
                  : ""}
                .
              </p>
            </Alert>
          ) : null}
          {probe.isError ? <ErrorState error={probe.error} /> : null}
        </CardBody>
      </Card>

      <Tabs defaultValue="capabilities">
        <TabsList>
          <TabsTrigger value="capabilities">Khả năng</TabsTrigger>
          <TabsTrigger value="config">Cấu hình</TabsTrigger>
        </TabsList>

        <TabsContent value="capabilities">
          <div className="space-y-5">
            {data.groups.map((group) => (
              <Card key={group.group}>
                <CardHeader title={group.group} />
                <CardBody>
                  <DataTable
                    rows={group.items}
                    columns={columns}
                    rowKey={(row) => row.key}
                    caption={`Khả năng nhóm ${group.group}`}
                  />
                </CardBody>
              </Card>
            ))}
          </div>
        </TabsContent>

        <TabsContent value="config">
          <Card>
            <CardHeader
              title="Biến môi trường"
              description="Chỉ hiển thị giá trị không nhạy cảm. Thay đổi chỉ có hiệu lực sau khi deploy lại."
            />
            <CardBody className="space-y-4">
              <dl className="grid gap-3 sm:grid-cols-2">
                {data.config.map((entry) => (
                  <div key={entry.key} className="min-w-0">
                    <dt className="text-2xs font-semibold uppercase tracking-wide text-muted">
                      {entry.key}
                    </dt>
                    <dd className="truncate text-[13px] text-ink" title={entry.value}>
                      {entry.value}
                    </dd>
                  </div>
                ))}
              </dl>

              <div className="border-t border-border pt-3">
                <dl className="grid gap-3 sm:grid-cols-2">
                  <div>
                    <dt className="text-2xs font-semibold uppercase tracking-wide text-muted">
                      Máy chủ khởi động lúc
                    </dt>
                    <dd className="text-[13px] text-ink">{data.runtime.started_at}</dd>
                  </div>
                  <div>
                    <dt className="text-2xs font-semibold uppercase tracking-wide text-muted">
                      Danh tính thực thi
                    </dt>
                    <dd className="text-[13px] text-ink">
                      {session.data?.execution.label ?? data.runtime.execution_identity}
                    </dd>
                  </div>
                </dl>
              </div>
            </CardBody>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}
