"use client";

/**
 * The front door: find a data asset.
 *
 * Answers the question a steward actually arrives with - "where is this table?"
 * - without a detour through a dashboard of decorative numbers.
 */
import Link from "next/link";
import { ArrowRight, RefreshCw, SearchX } from "lucide-react";
import { Suspense, useState } from "react";

import { AssetPicker } from "@/components/governance/asset-picker";
import { CompletenessNote, EmptyState, ErrorState } from "@/components/governance/states";
import { Note } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/field";
import { Badge, Card, CardBody, LoadingRows, PageTitle } from "@/components/ui/surface";
import { useRefreshAll, useSearch } from "@/lib/queries";
import { KIND_LABELS, OBJECT_KINDS, targetHref, useSelection } from "@/lib/selection";
import type { AssetRef, Listing, ObjectKind } from "@/lib/types";

/** How many results one screen shows before asking for a narrower search. */
const SHOWN = 100;

export default function FindAssetsPage() {
  return (
    <Suspense fallback={<LoadingRows rows={4} label="Đang tải" />}>
      <FindAssets />
    </Suspense>
  );
}

function FindAssets() {
  const { selection, update } = useSelection();
  const refresh = useRefreshAll();
  const [term, setTerm] = useState("");
  const [kinds, setKinds] = useState<ObjectKind[]>(["table"]);

  const results = useSearch(selection.catalog, selection.schema, term, kinds);

  function toggleKind(kind: ObjectKind) {
    setKinds((current) =>
      current.includes(kind) ? current.filter((k) => k !== kind) : [...current, kind],
    );
  }

  return (
    <div className="space-y-6">
      <PageTitle
        title="Tìm tài sản dữ liệu"
        description="Chọn catalog và schema, rồi tìm theo tên. Ứng dụng chỉ tải dữ liệu của phạm vi bạn đang chọn, không quét toàn bộ workspace."
        actions={
          <Button variant="outline" size="sm" loading={refresh.isPending} onClick={() => refresh.mutate()}>
            <RefreshCw aria-hidden />
            Làm mới
          </Button>
        }
      />

      <Card>
        <CardBody className="space-y-4">
          <AssetPicker
            selection={{ ...selection, kind: "schema" }}
            onChange={(changes) => update(changes, { replace: true })}
          />

          <div className="grid gap-3 sm:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
            <Field
              label="Tìm theo tên"
              hint="Tìm trong tên và mô tả của các đối tượng thuộc schema đã chọn."
            >
              {({ id, describedBy }) => (
                <Input
                  id={id}
                  aria-describedby={describedBy}
                  type="search"
                  value={term}
                  onChange={(event) => setTerm(event.target.value)}
                  placeholder="Ví dụ: routes"
                  disabled={!selection.schema}
                />
              )}
            </Field>

            <fieldset>
              <legend className="mb-1.5 text-[13px] font-medium text-ink">Loại đối tượng</legend>
              <div className="flex flex-wrap gap-1.5">
                {OBJECT_KINDS.map((kind) => {
                  const active = kinds.includes(kind);
                  return (
                    <button
                      key={kind}
                      type="button"
                      onClick={() => toggleKind(kind)}
                      aria-pressed={active}
                      className={
                        active
                          ? "rounded-md border border-primary bg-primary-soft px-2.5 py-1.5 text-[13px] font-medium text-primary"
                          : "rounded-md border border-border-strong bg-raised px-2.5 py-1.5 text-[13px] text-muted hover:text-ink"
                      }
                    >
                      {KIND_LABELS[kind]}
                    </button>
                  );
                })}
              </div>
            </fieldset>
          </div>
        </CardBody>
      </Card>

      {!selection.catalog || !selection.schema ? (
        <Note>Chọn catalog và schema để xem danh sách tài sản.</Note>
      ) : kinds.length === 0 ? (
        <Note>Chọn ít nhất một loại đối tượng để xem kết quả.</Note>
      ) : results.isPending ? (
        <LoadingRows rows={5} label="Đang đọc danh sách tài sản" />
      ) : results.isError ? (
        <ErrorState error={results.error} context="Không tải được danh sách tài sản." />
      ) : results.data.items.length === 0 ? (
        term ? (
          <div className="rounded-md border border-dashed border-border-strong bg-surface px-4 py-6 text-center">
            <SearchX aria-hidden className="mx-auto size-5 text-muted" />
            <p className="mt-2 text-sm font-medium text-ink">
              Không có đối tượng nào khớp “{term}” trong{" "}
              <code>
                {selection.catalog}.{selection.schema}
              </code>
              .
            </p>
            <Note className="mt-1">Thử bỏ bớt từ khoá, hoặc chọn loại đối tượng khác.</Note>
          </div>
        ) : (
          <EmptyState what="đối tượng" />
        )
      ) : (
        <Results listing={results.data} />
      )}
    </div>
  );
}

function Results({ listing }: { listing: Listing<AssetRef> }) {
  const shown = listing.items.slice(0, SHOWN);

  return (
    <div className="space-y-3">
      <p className="text-[13px] text-muted">
        {listing.items.length} đối tượng
        {listing.items.length > SHOWN ? ` · đang hiển thị ${SHOWN}` : ""}.
      </p>

      <ul className="space-y-2">
        {shown.map((item) => (
          <li key={item.full_name}>
            <Card className="transition-colors hover:border-border-strong">
              <CardBody className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0 flex-1 space-y-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium text-ink">{item.name}</span>
                    <Badge tone="neutral">{item.type_label}</Badge>
                  </div>
                  {/* The full path is always shown: two schemas can hold the
                      same name, and an operator must not act on the wrong one. */}
                  <code className="block truncate text-2xs text-muted" title={item.full_name}>
                    {item.full_name}
                  </code>
                  {item.comment ? (
                    <p className="line-clamp-2 text-[13px] text-muted">{item.comment}</p>
                  ) : null}
                </div>

                <div className="flex shrink-0 flex-col items-end gap-2">
                  <span className="text-2xs text-muted">
                    Chủ sở hữu
                    <br />
                    <span className="text-ink">{item.owner || "—"}</span>
                  </span>
                  <Button asChild size="sm" variant="outline">
                    <Link
                      href={targetHref("/tai-san", {
                        kind: item.kind,
                        catalog: item.catalog,
                        schema: item.schema,
                        name: item.name,
                      })}
                    >
                      Mở
                      <ArrowRight aria-hidden />
                    </Link>
                  </Button>
                </div>
              </CardBody>
            </Card>
          </li>
        ))}
      </ul>

      {listing.items.length > SHOWN ? (
        <Note>
          Đang hiển thị {SHOWN}/{listing.items.length} kết quả. Dùng ô tìm kiếm để thu hẹp.
        </Note>
      ) : null}

      <CompletenessNote
        completeness={listing.completeness}
        observedAt={listing.observed_at}
        note={listing.note}
      />
    </div>
  );
}
