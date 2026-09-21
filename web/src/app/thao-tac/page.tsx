"use client";

/**
 * What this app did, and what it is not sure it did.
 *
 * Unresolved operations come first and separately. After a timeout the only
 * question that matters is "did my change go through", and burying that row in
 * a chronological list is how a grant gets applied twice.
 */
import { Alert, Note } from "@/components/ui/alert";
import { Badge, Card, CardBody, CardHeader, LoadingRows, PageTitle } from "@/components/ui/surface";
import { DataTable, type Column } from "@/components/ui/table";
import { Explain } from "@/components/ui/disclosure";
import { EmptyState, ErrorState } from "@/components/governance/states";
import { useActivity } from "@/lib/queries";
import { formatUtc, shortId } from "@/lib/utils";
import type { ActivityEvent, OutcomeStatus } from "@/lib/types";

const STATUS_TONE: Record<OutcomeStatus, "good" | "danger" | "caution" | "neutral"> = {
  planned: "neutral",
  applying: "neutral",
  applied: "good",
  verified: "good",
  failed: "danger",
  unknown: "caution",
  verify_failed: "caution",
};

export default function ActivityPage() {
  const activity = useActivity();

  if (activity.isPending) return <LoadingRows rows={5} label="Đang đọc nhật ký thao tác" />;
  if (activity.isError) return <ErrorState error={activity.error} />;

  const { items, unresolved, disclaimer } = activity.data;

  const columns: Column<ActivityEvent>[] = [
    {
      key: "time_utc",
      header: "Thời điểm (UTC)",
      cell: (row) => <span className="whitespace-nowrap">{formatUtc(row.time_utc)}</span>,
      value: (row) => formatUtc(row.time_utc),
    },
    { key: "actor", header: "Người thao tác", value: (row) => row.actor || "—" },
    {
      key: "summary",
      header: "Hành động",
      cell: (row) => (
        <div className="min-w-0">
          <div className="text-ink">{row.summary}</div>
          {row.principal ? (
            <div className="text-2xs text-muted">Principal: {row.principal}</div>
          ) : null}
        </div>
      ),
      value: (row) => row.summary,
    },
    {
      key: "target",
      header: "Đối tượng",
      cell: (row) => <code className="text-2xs">{row.target}</code>,
      value: (row) => row.target,
    },
    {
      key: "status",
      header: "Kết quả",
      cell: (row) => <Badge tone={STATUS_TONE[row.status]}>{row.status_label}</Badge>,
      value: (row) => row.status_label,
    },
    {
      key: "reason",
      header: "Lý do",
      cell: (row) => <span className="text-muted">{row.reason || "—"}</span>,
      value: (row) => row.reason,
    },
    {
      key: "event_id",
      header: "Event ID",
      cell: (row) => <code className="text-2xs">{shortId(row.event_id)}</code>,
      value: (row) => row.event_id,
    },
  ];

  return (
    <div className="space-y-6">
      <PageTitle
        title="Thao tác gần đây"
        description="Nhật ký của chính ứng dụng này, tách biệt với nhật ký kiểm toán của Databricks."
      />

      {unresolved.length > 0 ? (
        <Alert
          level="caution"
          title={`${unresolved.length} thao tác chưa xác định kết quả`}
        >
          <p>
            Những thao tác này đã được gửi nhưng ứng dụng không nhận được xác nhận.{" "}
            <strong>Hãy đối chiếu trạng thái thật trước khi thử lại</strong>, để tránh thực hiện
            hai lần.
          </p>
          <ul className="mt-2 space-y-1">
            {unresolved.map((event) => (
              <li key={event.event_id}>
                <code>{event.target}</code> — {event.summary}{" "}
                <span className="text-2xs">({shortId(event.event_id)})</span>
              </li>
            ))}
          </ul>
        </Alert>
      ) : null}

      <Card>
        <CardHeader title={`Lịch sử (${items.length})`} />
        <CardBody>
          {items.length === 0 ? (
            <EmptyState
              what="thao tác"
              detail="Nhật ký này chỉ ghi các thao tác thực hiện qua ứng dụng, trong phiên máy chủ hiện tại."
            />
          ) : (
            <DataTable
              rows={items}
              columns={columns}
              rowKey={(row) => row.event_id}
              caption="Nhật ký thao tác của ứng dụng"
              downloadName="nhat-ky-thao-tac"
            />
          )}
        </CardBody>
      </Card>

      <Explain title="Nhật ký này ghi và không ghi những gì">
        <p>{disclaimer}</p>
      </Explain>
      <Note>
        Databricks không giữ log sau khi compute của app dừng. Muốn lưu lâu dài, bật App telemetry
        hoặc dựa vào <code>system.access.audit</code>.
      </Note>
    </div>
  );
}
