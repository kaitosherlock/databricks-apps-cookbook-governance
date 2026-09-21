"use client";

/**
 * Choosing who receives a grant.
 *
 * The distinction that has to survive here: "nothing matched" and "we were not
 * allowed to look" are different answers. The app service principal usually
 * sees only itself in workspace SCIM, and workspace SCIM never sees account
 * groups at all, so an empty directory result is normal and must not be
 * presented as proof that a principal does not exist. Typing the identifier
 * directly always works, and the UI says so instead of dead-ending.
 */
import { useState } from "react";

import { Alert, Note } from "@/components/ui/alert";
import { Field, Input } from "@/components/ui/field";
import { Select, type SelectOption } from "@/components/ui/select";
import { Badge, LoadingRows } from "@/components/ui/surface";
import { ErrorState } from "./states";
import { usePrincipalSearch } from "@/lib/queries";

type Mode = "search" | "manual";

export function PrincipalField({
  value,
  onChange,
  disabled,
}: {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  const [mode, setMode] = useState<Mode>("search");
  const [term, setTerm] = useState("");
  const results = usePrincipalSearch(term);

  return (
    <div className="space-y-2">
      <div
        role="radiogroup"
        aria-label="Cách chọn người nhận quyền"
        className="flex flex-wrap gap-1.5"
      >
        {(
          [
            ["search", "Tìm trong workspace"],
            ["manual", "Nhập trực tiếp"],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            role="radio"
            aria-checked={mode === key}
            onClick={() => setMode(key)}
            disabled={disabled}
            className={
              mode === key
                ? "rounded-md border border-primary bg-primary-soft px-2.5 py-1 text-[13px] font-medium text-primary"
                : "rounded-md border border-border-strong bg-raised px-2.5 py-1 text-[13px] text-muted hover:text-ink"
            }
          >
            {label}
          </button>
        ))}
      </div>

      {mode === "manual" ? (
        <Field
          label="Principal"
          hideLabel
          hint="Unity Catalog nhận: email người dùng, tên account group, hoặc application ID của service principal."
        >
          {({ id, describedBy }) => (
            <Input
              id={id}
              aria-describedby={describedBy}
              value={value}
              onChange={(event) => onChange(event.target.value)}
              placeholder="email@congty.com · tên account group · application ID"
              disabled={disabled}
              autoComplete="off"
              spellCheck={false}
            />
          )}
        </Field>
      ) : (
        <div className="space-y-2">
          <Field label="Tìm theo tên hoặc email" hideLabel>
            {({ id }) => (
              <Input
                id={id}
                type="search"
                value={term}
                onChange={(event) => setTerm(event.target.value)}
                placeholder="Nhập ít nhất 2 ký tự"
                disabled={disabled}
              />
            )}
          </Field>

          {results.isPending && term.trim().length >= 2 ? (
            <LoadingRows rows={1} label="Đang tìm principal" />
          ) : results.isError ? (
            <div className="space-y-1.5">
              <ErrorState error={results.error} context="Không tra cứu được principal." />
              <Note>Chuyển sang “Nhập trực tiếp” để nhập định danh thủ công.</Note>
            </div>
          ) : results.data && !results.data.too_short ? (
            <SearchResults data={results.data} value={value} onChange={onChange} term={term} />
          ) : (
            <Note>{results.data?.scope_note}</Note>
          )}
        </div>
      )}

      {value ? (
        <p className="text-[13px] text-muted">
          Sẽ cấp cho: <code className="text-ink">{value}</code>
        </p>
      ) : null}
    </div>
  );
}

function SearchResults({
  data,
  value,
  onChange,
  term,
}: {
  data: NonNullable<ReturnType<typeof usePrincipalSearch>["data"]>;
  value: string;
  onChange: (value: string) => void;
  term: string;
}) {
  if (data.items.length === 0) {
    // The two blank cases get different words, because only one of them is
    // evidence about whether the principal exists.
    if (data.completeness === "partial_permission") {
      return (
        <Alert level="caution" title="Không đọc được danh bạ người dùng/nhóm của workspace">
          <p>
            Chưa thể khẳng định có hay không principal khớp. Tài khoản dịch vụ của ứng dụng
            thường chỉ nhìn thấy chính nó trong SCIM cấp workspace, và SCIM cấp workspace vốn
            không thấy account group.
          </p>
          <p className="mt-1">
            Hãy dùng <strong>Nhập trực tiếp</strong> — Unity Catalog vẫn nhận đúng định danh bạn
            nhập.
          </p>
        </Alert>
      );
    }
    return (
      <div className="space-y-1.5">
        <Note>Không có principal nào khớp “{term.trim()}” trong danh bạ đọc được.</Note>
        <Note>{data.scope_note}</Note>
      </div>
    );
  }

  const options: SelectOption[] = data.items.map((item) => ({
    value: item.identifier,
    label: item.label,
    description: item.type_label,
  }));

  return (
    <div className="space-y-2">
      <Field label="Kết quả" hideLabel>
        {({ id }) => (
          <Select id={id} value={value} onValueChange={onChange} options={options} />
        )}
      </Field>

      {data.rejected.length > 0 ? (
        <div className="space-y-1">
          <Note>Unity Catalog không nhận những mục sau:</Note>
          <ul className="space-y-0.5">
            {data.rejected.map((item) => (
              <li key={item.identifier} className="flex flex-wrap items-center gap-1.5">
                <Badge tone="caution">Không dùng được</Badge>
                <code className="text-2xs text-muted">{item.identifier}</code>
                <span className="text-2xs text-muted">{item.note}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
