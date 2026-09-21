"use client";

/**
 * Who has access, and where that access came from.
 *
 * `source` is the column the whole screen turns on. A grant inherited from a
 * catalog cannot be revoked at a table, so it is labelled with its origin and
 * never offered a revoke control - the old build was careful about this and it
 * would be easy to lose by rendering one flat list.
 */
import { useMemo, useState, type ReactNode } from "react";

import { Badge } from "@/components/ui/surface";
import { DataTable, type Column } from "@/components/ui/table";
import { Field, Input } from "@/components/ui/field";
import { Select } from "@/components/ui/select";
import { Note } from "@/components/ui/alert";
import { CompletenessNote, EmptyState } from "./states";
import type { GrantRow, GrantSource, GrantView } from "@/lib/types";

const SOURCE_FILTERS: { value: string; label: string }[] = [
  { value: "all", label: "Tất cả" },
  { value: "direct", label: "Cấp trực tiếp" },
  { value: "inherited", label: "Kế thừa" },
  { value: "ownership", label: "Từ quyền sở hữu" },
];

const SOURCE_TONE: Record<GrantSource, "primary" | "info" | "neutral"> = {
  direct: "primary",
  inherited: "info",
  ownership: "neutral",
};

export function GrantTable({ view, downloadName }: { view: GrantView; downloadName: string }) {
  const [principalTerm, setPrincipalTerm] = useState("");
  const [source, setSource] = useState("all");

  const filtered = useMemo(() => {
    const needle = principalTerm.trim().toLowerCase();
    return view.rows.filter((row) => {
      if (needle && !row.principal.toLowerCase().includes(needle)) return false;
      if (source !== "all" && row.source !== source) return false;
      return true;
    });
  }, [view.rows, principalTerm, source]);

  const columns: Column<GrantRow>[] = [
    {
      key: "principal",
      header: "Principal",
      cell: (row) => (
        <div className="min-w-0">
          <div className="truncate font-medium text-ink">{row.principal}</div>
          <div className="text-2xs text-muted">{row.principal_type}</div>
        </div>
      ),
      value: (row) => row.principal,
    },
    {
      key: "privilege",
      header: "Quyền",
      cell: (row) =>
        row.unreadable ? (
          <span className="text-muted">Không đọc được mã quyền</span>
        ) : (
          <div className="min-w-0">
            <div className="text-ink">{row.privilege_label}</div>
            <code className="text-2xs text-muted">{row.privilege}</code>
          </div>
        ),
      value: (row) => row.privilege || "(không đọc được)",
    },
    {
      key: "source",
      header: "Nguồn quyền",
      cell: (row) => (
        <div className="space-y-1">
          <Badge tone={SOURCE_TONE[row.source]}>{row.source_label}</Badge>
          {!row.revocable_here && row.source === "inherited" ? (
            <div className="text-2xs text-muted">Phải sửa ở cấp cha</div>
          ) : null}
        </div>
      ),
      value: (row) => row.source_label,
    },
    {
      key: "granted_at",
      header: "Cấp tại",
      cell: (row) =>
        row.granted_at ? (
          <code className="text-2xs text-muted">{row.granted_at}</code>
        ) : (
          <span className="text-muted">—</span>
        ),
      value: (row) => row.granted_at,
    },
  ];

  if (view.rows.length === 0) {
    return (
      <div className="space-y-3">
        <EmptyState
          what="quyền"
          detail={
            <>
              Đây <strong>không</strong> phải kết luận “không ai có quyền”: nếu danh tính thực
              thi không đủ quyền đọc ACL, Databricks chỉ trả về phần nó được phép thấy.
            </>
          }
        />
        <Note>{view.caveat}</Note>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Field label="Tìm principal">
          {({ id }) => (
            <Input
              id={id}
              value={principalTerm}
              onChange={(event) => setPrincipalTerm(event.target.value)}
              placeholder="email, tên nhóm hoặc application ID"
              type="search"
            />
          )}
        </Field>
        <Field label="Nguồn quyền">
          {({ id }) => (
            <Select id={id} value={source} onValueChange={setSource} options={SOURCE_FILTERS} />
          )}
        </Field>
      </div>

      <p className="text-[13px] text-muted">
        {filtered.length === view.rows.length
          ? `${view.rows.length} dòng.`
          : `${filtered.length}/${view.rows.length} dòng khớp bộ lọc.`}
      </p>

      <DataTable
        rows={filtered}
        columns={columns}
        rowKey={(row, index) => `${row.principal}-${row.privilege}-${row.source}-${index}`}
        caption="Danh sách quyền truy cập trên đối tượng đang chọn"
        downloadName={downloadName}
        empty={<EmptyState what="dòng nào khớp bộ lọc" />}
      />

      <CompletenessNote completeness={view.completeness} observedAt={view.observed_at} />
      <Note>{view.caveat}</Note>
      {view.unreadable_note ? <Note>{view.unreadable_note}</Note> : null}
    </div>
  );
}

/**
 * Where to go to change an inherited grant.
 *
 * Offering a revoke button that would fail is worse than offering none, so the
 * screen points at the object that actually holds the grant.
 */
export function InheritanceGuide({
  view,
  renderLink,
}: {
  view: GrantView;
  renderLink: (parent: { name: string; kind: string }) => ReactNode;
}) {
  const parents = useMemo(() => {
    const seen = new Map<string, { name: string; kind: string }>();
    for (const row of view.rows) {
      if (row.source === "inherited" && row.inherited_from) {
        seen.set(row.inherited_from, {
          name: row.inherited_from,
          kind: row.inherited_from_type,
        });
      }
    }
    return [...seen.values()];
  }, [view.rows]);

  if (parents.length === 0) return null;

  return (
    <div className="space-y-2 rounded-md border border-border bg-surface px-3.5 py-3">
      <p className="text-sm font-medium text-ink">Quyền kế thừa — sửa ở cấp cha</p>
      <Note>
        Quyền kế thừa không thu hồi được tại đối tượng này. Hãy mở đúng đối tượng cha đã cấp
        quyền đó.
      </Note>
      <ul className="space-y-1.5">
        {parents.map((parent) => (
          <li key={parent.name} className="flex flex-wrap items-center justify-between gap-2">
            <code className="text-[13px] text-ink">
              {parent.name}{" "}
              <span className="text-muted">({parent.kind.toLowerCase() || "cấp cha"})</span>
            </code>
            {renderLink(parent)}
          </li>
        ))}
      </ul>
    </div>
  );
}
